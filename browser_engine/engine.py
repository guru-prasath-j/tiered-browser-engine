"""The tiered engine: cache -> local LLM -> Claude text -> Claude vision."""
import base64
import hashlib
import json
import time
import uuid
from collections import Counter

from .cache import norm_intent
from .executor import NEEDS_ELEMENT, VALUE_ACTIONS, extract_text, locate_cached, perform, verify
from .llm import Usage, parse_json
from .observer import observe, page_key
from .safety import describe

CLAUDE_TIERS = {"planner", "claude_text", "claude_vision"}


class StepFailed(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class StepLog:
    def __init__(self, path, run_id=None):
        self.path = path
        self.run_id = run_id or uuid.uuid4().hex[:10]

    def write(self, kind, **fields):
        rec = {"ts": round(time.time(), 3), "run_id": self.run_id, "kind": kind, **fields}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


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
    def __init__(self, page, cfg, cache, log, local=None, claude=None, guard=None):
        self.page, self.cfg, self.cache, self.log = page, cfg, cache, log
        self.local, self.claude, self.guard = local, claude, guard
        self.intent = ""
        self.extracted = []  # [{"intent", "text"}] from extract steps, returned to the caller
        self.local_down = False
        self.claude_tokens = 0
        self.tiers_used = Counter()

    # ---- public ----------------------------------------------------------
    def run(self, goal, start_url=None, plan=None):
        t0 = time.time()
        if start_url:
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
        t0 = time.time()
        before = observe(self.page, self.cfg.max_elements)
        domain, pkey = page_key(before.url)
        feedback = None

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

        # Tiers 1-3: ask models, cheapest first.
        for tier, tries in self._tiers():
            for _ in range(tries):
                if tier in CLAUDE_TIERS:
                    self._check_budget()
                obs = observe(self.page, self.cfg.max_elements)
                try:
                    raw, usage = self._decide(tier, intent, value, obs, feedback)
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

        self.log.write("step", intent=key, tier=None, ok=False, ms=int((time.time() - t0) * 1000))
        raise StepFailed(f"could not complete step '{intent}': {feedback}")

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
        text = value if (value is not None and action in VALUE_ACTIONS) else decision.get("text")
        loc = None
        if action in NEEDS_ELEMENT or (action == "press" and ref):
            if not ref or ref not in obs.by_ref:
                return False, f"ref {ref!r} is not in the element list", None
            loc = self.page.locator(f'[data-be-ref="{ref}"]')
        if self.guard:
            self.guard.check(self.intent, action, text, describe(loc))  # raises Blocked
        if action == "extract":
            ok, why = self._extract(loc)
        else:
            try:
                perform(self.page, action, loc, text, decision, self.cfg.settle_ms)
            except Exception as e:
                return False, f"{action} raised: {str(e).splitlines()[0][:200]}", None
            ok, why = verify(self.page, action, obs, loc, text, decision.get("expect"), self.cfg.max_elements)
        if not ok:
            return False, why, None

        entry = None
        if action != "click_xy":  # coordinates are too fragile to cache
            selectors = obs.by_ref[ref].selectors if ref in obs.by_ref else []
            if selectors or action not in NEEDS_ELEMENT:
                entry = {"action": action, "selectors": selectors, "expect": decision.get("expect") or {}}
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

    def _done(self, key, tier, t0):
        self.tiers_used[tier] += 1
        self.log.write("step", intent=key, tier=tier, ok=True, ms=int((time.time() - t0) * 1000))
        return tier
