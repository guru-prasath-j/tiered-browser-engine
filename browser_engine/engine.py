"""The tiered engine.

Layer 1 (reads the page's elements): rule -> cache -> label match -> local LLM -> Claude text/vision.
Layer 2 (vision.py, screenshots only): local vision models ONLY (Ollama), never a paid API. Runs when layer 1
gives up on a step, or straight away when the page has no readable elements.
"""
import base64
import re
import hashlib
import json
import time
import uuid
from collections import Counter

from .cache import norm_intent
from . import human
from . import popups as popup_mod
from . import forms
from .executor import NEEDS_ELEMENT, VALUE_ACTIONS, extract_text, locate_cached, perform, verify, verify_ex, wrong_target
from . import matcher
from .llm import Usage, parse_json
from .observer import observe, page_key
from .safety import describe

CLAUDE_TIERS = {"planner", "claude_text", "claude_vision"}


class StepFailed(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class HumanNeeded(Exception):
    """A bot check is on screen and the user didn't clear it within human_wait_s."""

    def __init__(self, reason, resume_url=None):
        super().__init__(f"{reason}: waiting for the user to solve it in Chrome")
        self.reason, self.resume_url = reason, resume_url


class StepLog:
    def __init__(self, path, run_id=None):
        self.path = path
        self.run_id = run_id or uuid.uuid4().hex[:10]

    def write(self, kind, **fields):
        from .logviz import append_jsonl

        rec = {"ts": round(time.time(), 3), "run_id": self.run_id, "kind": kind, **fields}
        try:
            append_jsonl(self.path, rec)
        except OSError:
            pass  # logging must never break a run


def plan_key(goal, start_url):
    return hashlib.sha1(f"{norm_intent(goal)}|{start_url or ''}".encode()).hexdigest()[:16]


def normalize_steps(steps):
    out = []
    for s in steps:
        if isinstance(s, str):
            s = {"intent": s, "value": None}
        intent = str(s.get("intent", "")).strip()
        if not intent:
            continue
        value = s.get("value")
        out.append({"intent": intent, "value": None if value is None else str(value)})
    if not out:
        raise StepFailed("plan has no steps")
    return out


class Engine:
    guest_popups = False  # GuestEngine also clicks "continue as guest"

    def __init__(self, page, cfg, cache, log, local=None, claude=None, guard=None, popups=None,
                 vision=None):
        self.page, self.cfg, self.cache, self.log = page, cfg, cache, log
        self.vision = vision         # vision.VisionLayer (layer 2), or None
        self.local, self.claude, self.guard = local, claude, guard
        self.popups = popups         # PopupMemory, or None to never touch popups
        self.popups_closed = []      # labels clicked this run, returned to the caller
        self._popup_url = None
        self.intent = ""
        self.extracted = []  # [{"intent", "text"}] from extract steps, returned to the caller
        self.local_down = False
        self.claude_tokens = 0
        self.tiers_used = Counter()
        self.target_url = None   # where the plan meant to be; revisited after a bot check redirects away
        self.human_waits = 0

    # ---- public ----------------------------------------------------------
    def run(self, goal, start_url=None, plan=None):
        t0 = time.time()
        if start_url:
            self.target_url = start_url
            self.page.goto(start_url, wait_until="domcontentloaded")
        key = plan_key(goal, start_url)
        steps = normalize_steps(plan) if plan else self._get_plan(goal, key)
        try:
            for step in steps:
                self.do_step(step["intent"], step["value"])
        except StepFailed:
            if not plan:
                self.cache.drop_plan(key)  # a bad plan shouldn't be replayed forever
            raise
        summary = {
            "run_id": self.log.run_id, "ok": True, "steps": len(steps),
            "tiers": dict(self.tiers_used), "claude_tokens": self.claude_tokens,
            "seconds": round(time.time() - t0, 1), "extracted": self.extracted,
        }
        self.log.write("run", **summary)
        return summary

    def do_step(self, intent, value=None):
        key = norm_intent(intent)
        self.intent = intent
        self.human_gate()
        self.before_step()
        t0 = time.time()
        try:
            before = observe(self.page, self.cfg.max_elements)
            blind = not before.elements
        except Exception as e:
            before, blind = None, True
            self.log.write("attempt", intent=key, tier="dom", ok=False,
                           note=f"page elements unreadable: {str(e).splitlines()[0][:150]}")
        if blind and self.vision and not key.startswith("answer"):
            # Nothing to read in the HTML (canvas, blocked scripts, shadow DOM...): go to layer 2.
            tier = self._vision(key, intent, value, "the page shows no readable elements")
            if tier:
                return self._done(key, tier, t0)
            self.log.write("step", intent=key, tier=None, ok=False, ms=int((time.time() - t0) * 1000),
                           url=self._url(), note="no readable elements and the screenshot layer failed")
            raise StepFailed(f"could not complete step '{intent}': page has no readable elements and "
                             "the screenshot layer could not do it")
        if before is None:
            before = observe(self.page, self.cfg.max_elements)  # raise the real error
        domain, pkey = page_key(before.url)
        feedback = None

        # Rule tier: "answer: <question>" + value = pick that option by visible text, no model.
        if key.startswith("answer") and value is not None:
            question = intent.split(":", 1)[1].strip() if ":" in intent else intent[len("answer"):].strip()
            if self.guard:
                self.guard.check(self.intent, "click", value, {"label": str(value)})
            res = forms.choose(self.page, question, value, self.cfg.settle_ms)
            self.log.write("attempt", intent=key, tier="rule", ok=bool(res.get("ok")), note=str(res)[:200],
                           tokens_in=0, tokens_out=0)
            if res.get("ok") and res.get("checked") is not False:
                return self._done(key, "rule", t0)
            raise StepFailed(f"could not complete step '{intent}': {res.get('error') or res}")

        # Tier 0: replay a cached action, no model at all.
        hit = self.cache.get(domain, pkey, key)
        if hit:
            hit_page, entry = hit
            ok, why = self._replay(entry, value, before)
            self.log.write("attempt", intent=key, tier="cache", ok=ok, note=why, tokens_in=0, tokens_out=0)
            if ok:
                self.cache.hit(domain, hit_page, key)
                return self._done(key, "cache", t0)
            self.cache.fail(domain, hit_page, key)
            feedback = f"a previously working action failed here: {why}"
            if self.clear_popups(force=True):  # something may have been covering the target
                before = observe(self.page, self.cfg.max_elements)
                ok, why = self._replay(entry, value, before)
                self.log.write("attempt", intent=key, tier="cache", ok=ok, note=f"after closing popup: {why}",
                               tokens_in=0, tokens_out=0)
                if ok:
                    self.cache.hit(domain, hit_page, key)
                    return self._done(key, "cache", t0)

        # Tier "match": the intent names a label that exactly one element carries. No model.
        if self.cfg.label_match:
            obs = observe(self.page, self.cfg.max_elements)
            decision = matcher.match(intent, value, obs)
            if decision:
                action = decision["action"]
                ok, why, entry = self.execute_decision(decision, action, value, obs)
                self.log.write("attempt", intent=key, tier="match", ok=ok, action=action, note=why,
                               tokens_in=0, tokens_out=0)
                if ok:
                    if entry:
                        d, p = page_key(obs.url)
                        self.cache.put(d, p, key, entry)
                    return self._done(key, "match", t0)
                feedback = f"clicking the element labelled like the intent did not work: {why}"

        # Tiers 1-3: ask models, cheapest first.
        for tier, tries in self._tiers():
            for attempt in range(tries):
                if tier in CLAUDE_TIERS:
                    self._check_budget()
                obs = observe(self.page, self.cfg.max_elements)
                # Models see only the elements related to the step; a retry sees more.
                limit = self.cfg.prompt_elements if attempt == 0 else self.cfg.prompt_elements_retry
                prompt_obs = obs.subset(f"{intent} {value or ''}", limit)
                try:
                    raw, usage = self._decide(tier, intent, value, prompt_obs, feedback)
                except Exception as e:
                    if tier == "local":
                        self.local_down = True
                        self.log.write("attempt", intent=key, tier=tier, ok=False,
                                       note=f"local LLM unavailable: {str(e)[:150]}")
                        break
                    raise
                self._account(tier, usage)
                tok = {"tokens_in": usage.tokens_in, "tokens_out": usage.tokens_out}

                try:
                    decision = parse_json(raw)
                except ValueError as e:
                    self.log.write("attempt", intent=key, tier=tier, ok=False, note=str(e), **tok)
                    feedback = "your previous reply was not valid JSON; reply with only the JSON object"
                    continue

                if not isinstance(decision, dict):
                    feedback = "reply with one JSON object"
                    continue
                if tier == "local":
                    decision["expect"] = {}  # small models guess these wrong; effects are checked instead
                action = str(decision.get("action", "")).lower().strip()
                reason = str(decision.get("reason", ""))[:200]

                if action == "done":
                    # Small models sometimes claim "done" too early; sanity-check when we can.
                    if tier == "local" and value is not None and value.lower() not in obs.text.lower():
                        self.log.write("attempt", intent=key, tier=tier, ok=False, action=action,
                                       note="claimed done but value not on page", **tok)
                        feedback = "the intent is NOT done yet: the value is not present on the page"
                        continue
                    self.log.write("attempt", intent=key, tier=tier, ok=True, action=action, note=reason, **tok)
                    return self._done(key, tier, t0)

                if action == "fail":
                    self.log.write("attempt", intent=key, tier=tier, ok=False, action=action, note=reason, **tok)
                    feedback = f"a previous model could not do it: {reason}"
                    break

                ok, why, entry = self.execute_decision(decision, action, value, obs)
                self.log.write("attempt", intent=key, tier=tier, ok=ok, action=action, note=why, **tok)
                if ok:
                    if entry:  # learner: next time this step costs nothing
                        d, p = page_key(obs.url)
                        self.cache.put(d, p, key, entry)
                    return self._done(key, tier, t0)
                feedback = f"last action ({action} {decision.get('ref') or ''}) failed: {why}"

        if self.vision:
            tier = self._vision(key, intent, value, feedback)
            if tier:
                return self._done(key, tier, t0)
        self.log.write("step", intent=key, tier=None, ok=False, ms=int((time.time() - t0) * 1000),
                       url=self._url(), note=(feedback or "")[:300] or None)
        raise StepFailed(f"could not complete step '{intent}': {feedback}")

    def before_step(self):
        """Runs before every step. Closes optional popups once per newly loaded page."""
        self.clear_popups()

    def clear_popups(self, force=False):
        """Close optional popups (0 tokens). Only on a new URL unless forced. True if any closed."""
        if self.popups is None:
            return False
        url = self.page.url
        if not force and url == self._popup_url:
            return False
        if not force and self.cfg.popup_wait_ms:
            self.page.wait_for_timeout(self.cfg.popup_wait_ms)  # most popups appear right after load
        self._popup_url = url
        r = popup_mod.dismiss(self.page, self.popups, guest=self.guest_popups)
        closed = r["clicked"] + (["google one tap"] if r["removed_google_one_tap"] else [])
        if closed:
            self.popups_closed += closed
            self.log.write("popup", intent=norm_intent(self.intent), closed=closed, url=url)
            self._popup_url = self.page.url
        return bool(closed)

    def human_gate(self):
        """If a bot check is showing, hand over to the user and resume when it's gone."""
        for _ in range(2):  # solve -> redirected to home -> reopen target -> maybe one more check
            reason = human.detect(self.page)
            if not reason:
                return
            self.human_waits += 1
            self.log.write("human", intent=norm_intent(self.intent), note=reason, url=self.page.url)
            human.notify(self.page, reason)
            if not human.wait_until_clear(self.page, self.cfg.human_wait_s):
                raise HumanNeeded(reason, self.target_url)
            self.log.write("human", intent=norm_intent(self.intent), note="cleared by user", url=self.page.url)
            # PerimeterX & co. often bounce to "/" after solving: go back to where the plan was.
            if self.target_url and not human.same_target(self.page.url, self.target_url):
                self.page.goto(self.target_url, wait_until="domcontentloaded")
        if human.detect(self.page):
            raise HumanNeeded("bot check keeps coming back", self.target_url)

    # ---- internals -------------------------------------------------------
    def _get_plan(self, goal, key):
        cached = self.cache.get_plan(key)
        if cached:
            self.log.write("plan", tier="plan_cache", ok=True, steps=len(cached), tokens_in=0, tokens_out=0)
            return cached
        if not self.claude:
            raise StepFailed("no cached plan and Claude is disabled: pass --plan plan.json")
        self._check_budget()
        obs = observe(self.page, self.cfg.max_elements)
        raw, usage = self.claude.plan(goal, obs.text)
        self._account("planner", usage)
        self.log.write("plan", tier="planner", ok=True, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out)
        data = parse_json(raw)
        steps = normalize_steps(data.get("steps", []) if isinstance(data, dict) else data)
        self.cache.put_plan(key, goal, steps)
        return steps

    def _tiers(self):
        tiers = []
        if self.local and not self.local_down:
            tiers.append(("local", self.cfg.local_tries))
        if self.claude:
            tiers.append(("claude_text", self.cfg.claude_text_tries))
            tiers.append(("claude_vision", self.cfg.vision_tries))
        return tiers

    def _decide(self, tier, intent, value, obs, feedback):
        if tier == "local":
            return self.local.decide(intent, value, obs, feedback)
        if tier == "claude_text":
            return self.claude.decide_text(intent, value, obs, feedback)
        shot = self.page.screenshot(type="jpeg", quality=60, scale="css")
        return self.claude.decide_vision(intent, value, obs, feedback, base64.b64encode(shot).decode())

    def execute_decision(self, decision, action, value, obs):
        ref = decision.get("ref")
        if isinstance(ref, str) and ("," in ref or " " in ref.strip()):
            # models sometimes list several refs ("e20,e21,e22"): use the first one on the page
            ref = next((r for r in re.split(r"[,\s]+", ref) if r in obs.by_ref), ref)
        elif isinstance(ref, list):
            ref = next((r for r in ref if r in obs.by_ref), None)
        text = value if (value is not None and action in VALUE_ACTIONS) else decision.get("text")
        loc = None
        if action in NEEDS_ELEMENT or (action == "press" and ref):
            if not ref or ref not in obs.by_ref:
                return False, f"ref {ref!r} is not in the element list", None
            loc = self.page.locator(f'[data-be-ref="{ref}"]')
        if action == "click":
            miss = wrong_target(self.intent, obs.by_ref.get(ref), obs.elements)
            if miss:
                picked, better = miss
                return False, (f'picked {picked.ref} "{picked.name[:40]}", but {better.ref} '
                               f'"{better.name[:40]}" matches the intent'), None
        if self.guard:
            self.guard.check(self.intent, action, text, describe(loc))  # raises Blocked
        if action == "extract":
            ok, why = self._extract(loc)
        else:
            try:
                perform(self.page, action, loc, text, decision, self.cfg.settle_ms)
            except Exception as e:
                return False, f"{action} raised: {str(e).splitlines()[0][:200]}", None
            ok, why, met = verify_ex(self.page, action, obs, loc, text, decision.get("expect"),
                                     self.cfg.max_elements)
        if not ok:
            return False, why, None
        if action == "extract":
            met = False

        entry = None
        if action != "click_xy":  # coordinates are too fragile to cache
            selectors = obs.by_ref[ref].selectors if ref in obs.by_ref else []
            if selectors or action not in NEEDS_ELEMENT:
                # Only cache an expectation that really held; a wrong guess would fail every replay.
                entry = {"action": action, "selectors": selectors,
                         "expect": (decision.get("expect") or {}) if met else {}}
                if value is None or action not in VALUE_ACTIONS:
                    entry["text"] = text  # values come from the plan; keys/URLs/scroll amounts are cached
        return True, why, entry

    def _replay(self, entry, value, before):
        action = entry["action"]
        text = value if (value is not None and action in VALUE_ACTIONS) else entry.get("text")
        loc = None
        if action in NEEDS_ELEMENT or (action == "press" and entry.get("selectors")):
            loc = locate_cached(self.page, entry.get("selectors"))
            if loc is None:
                return False, "no cached selector matched exactly one element"
        if self.guard:
            self.guard.check(self.intent, action, text, describe(loc))  # raises Blocked
        if action == "extract":
            return self._extract(loc)
        try:
            perform(self.page, action, loc, text, entry, self.cfg.settle_ms)
        except Exception as e:
            return False, f"{action} raised: {str(e).splitlines()[0][:200]}"
        return verify(self.page, action, before, loc, text, entry.get("expect"), self.cfg.max_elements)

    def _extract(self, loc):
        try:
            text = extract_text(loc)
        except Exception as e:
            return False, f"extract raised: {str(e).splitlines()[0][:200]}"
        if not text or len(text.strip()) < 3:
            return False, "nothing readable around that element"
        self.extracted.append({"intent": self.intent, "text": text})
        return True, "ok"

    def _account(self, tier, usage: Usage):
        if tier in CLAUDE_TIERS:
            self.claude_tokens += usage.total

    def _check_budget(self):
        if self.claude_tokens >= self.cfg.max_claude_tokens:
            raise BudgetExceeded(f"Claude token budget reached ({self.claude_tokens}/{self.cfg.max_claude_tokens})")

    def _vision(self, key, intent, value, feedback):
        """Layer 2: the same step from screenshots only. Returns the tier name or None."""
        def on_usage(tier, usage):
            if tier in CLAUDE_TIERS:
                self._check_budget()
            self._account(tier, usage)

        self.log.write("layer", intent=key, note=f"layer 2 (screenshots): {feedback or 'layer 1 failed'}"[:300])
        return self.vision.run(self, key, intent, value, feedback, on_usage=on_usage)

    def _done(self, key, tier, t0):
        self.tiers_used[tier] += 1
        self.log.write("step", intent=key, tier=tier, ok=True, ms=int((time.time() - t0) * 1000),
                       url=self._url())
        return tier

    def _url(self):
        try:
            return self.page.url[:300]
        except Exception:
            return None
