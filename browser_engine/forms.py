"""Form helpers that need no model at all (0 tokens).

`choose` answers a multiple-choice question by its visible text. It works on native radios and
checkboxes, hidden ("sr-only") inputs behind styled labels, ARIA radios/options, and plain
clickable divs/spans that many job-application forms (ClanX, Lever, Greenhouse...) use.

`check_form` lists required questions that are still unanswered, so a plan can stop before Send
instead of submitting a form the site will reject.
"""

COMMON_JS = r"""
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const low = s => clean(s).toLowerCase().replace(/[*:?]+$/g, '').trim();
const ownText = el => clean(Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));
const visible = el => {
  if (!el || !el.getBoundingClientRect) return false;
  const r = el.getBoundingClientRect(), st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && st.visibility !== 'hidden' && st.display !== 'none';
};
const OPTION_SEL = 'input[type=radio],input[type=checkbox],[role=radio],[role=option],[role=checkbox],' +
  '[role=menuitemradio],label,button,li,[tabindex],div,span';
const textOf = el => {
  if (el.tagName === 'INPUT') {
    const l = (el.labels && el.labels[0]) || el.closest('label');
    return l ? clean(l.innerText) : clean(el.getAttribute('aria-label') || el.value);
  }
  return clean(el.getAttribute('aria-label') || el.innerText || el.textContent);
};
const isPointer = el => getComputedStyle(el).cursor === 'pointer';
const isOptionLike = el => {
  const t = el.tagName;
  if (t === 'INPUT') return true;
  const role = el.getAttribute('role');
  if (role && /radio|option|checkbox/.test(role)) return true;
  if (t === 'LABEL' || t === 'BUTTON') return true;
  if (el.querySelector('input[type=radio],input[type=checkbox]')) return true;
  return isPointer(el) || el.hasAttribute('tabindex') || el.hasAttribute('onclick');
};
const stateOf = el => {
  const inp = el.tagName === 'INPUT' ? el : el.querySelector('input[type=radio],input[type=checkbox]') ||
              ((el.tagName === 'LABEL' && el.control) || null);
  if (inp) return inp.checked;
  for (const a of ['aria-checked', 'aria-selected', 'aria-pressed']) {
    const v = el.getAttribute(a); if (v !== null) return v === 'true';
  }
  const c = (el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '';
  return /\b(selected|active|checked|is-selected|is-active)\b/i.test(String(c)) ? true : null;
};
const STOP = new Set(['a','an','the','of','to','in','on','for','with','as','is','are','you','your','do','have','what','how','many','much','and','or','at','we','us','our','any','if','be','this','that','please']);
const words = s => low(s).split(/[^a-z0-9+#.]+/).filter(w => w && !STOP.has(w));
const findQuestion = q => {
  const want = low(q), ww = words(q);
  let best = null, bestScore = 0, bestLen = 1e9;
  for (const el of document.querySelectorAll('label,legend,h1,h2,h3,h4,h5,h6,p,span,div,dt,th,strong,b')) {
    if (!visible(el)) continue;
    const t = low(el.innerText);
    if (!t || t.length > 400) continue;
    let score;
    if (t.includes(want)) score = 2;
    else {
      const tw = new Set(words(t));
      const hit = ww.filter(w => tw.has(w)).length;
      score = ww.length ? hit / ww.length : 0;
      if (score < 0.75) continue;
    }
    if (score > bestScore || (score === bestScore && t.length < bestLen)) { best = el; bestScore = score; bestLen = t.length; }
  }
  return best;
};
"""

CHOOSE_JS = "([question, option]) => {" + COMMON_JS + r"""
  const qEl = findQuestion(question);
  if (!qEl) return {ok: false, error: 'question not found on the page: ' + question};
  const want = low(option);
  let node = qEl;
  for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
    // a <select> under the question: pick by option text
    const sel = node.querySelector('select');
    if (sel && depth > 0) {
      const o = Array.from(sel.options).find(o => low(o.text) === want) ||
                Array.from(sel.options).find(o => low(o.text).includes(want));
      if (o) {
        sel.value = o.value;
        sel.dispatchEvent(new Event('input', {bubbles: true}));
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        return {ok: true, kind: 'select', chosen: clean(o.text), checked: true};
      }
    }
    const cands = Array.from(node.querySelectorAll(OPTION_SEL)).filter(el => !el.contains(qEl));
    const exact = [], loose = [];
    for (const el of cands) {
      if (!isOptionLike(el)) continue;
      const t = low(textOf(el));
      if (!t || t.length > 120) continue;
      if (t === want) exact.push(el); else if (t.startsWith(want) || t.includes(want)) loose.push(el);
    }
    const pool = exact.length ? exact : loose;
    if (!pool.length) continue;
    // the innermost match (a label/option, not the wrapper div that also contains other options)
    pool.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    const target = pool.find(el => visible(el) || el.tagName === 'INPUT') || pool[0];
    const inp = target.tagName === 'INPUT' ? target :
                target.querySelector('input[type=radio],input[type=checkbox]') ||
                (target.tagName === 'LABEL' ? target.control : null);
    target.scrollIntoView({block: 'center'});
    if (inp && !inp.checked) inp.click(); else if (!inp) target.click();
    return {ok: true, kind: inp ? inp.type : (target.getAttribute('role') || target.tagName.toLowerCase()),
            chosen: textOf(target), question: clean(qEl.innerText).slice(0, 120),
            checked: inp ? inp.checked : stateOf(target)};
  }
  return {ok: false, error: 'no option matching ' + JSON.stringify(option) + ' near the question',
          question: clean(qEl.innerText).slice(0, 120)};
}"""

CHECK_JS = "() => {" + COMMON_JS + r"""
  const answered = box => {
    for (const el of box.querySelectorAll('input,textarea,select')) {
      const ty = (el.type || '').toLowerCase();
      if (ty === 'hidden' || ty === 'submit' || ty === 'button') continue;
      if (ty === 'radio' || ty === 'checkbox') { if (el.checked) return true; continue; }
      if (ty === 'file') { if (el.files && el.files.length) return true; continue; }
      if (clean(el.value)) return true;
    }
    for (const el of box.querySelectorAll('[aria-checked=true],[aria-selected=true],[contenteditable=true]')) {
      if (el.getAttribute('contenteditable') !== 'true' || clean(el.innerText)) return true;
    }
    return false;
  };
  const hasField = box => box.querySelector('input:not([type=hidden]),textarea,select,[role=radio],[role=option],[role=checkbox],[contenteditable=true]');
  const missing = [], seen = new Set();
  // 1) questions whose label text ends with '*'
  for (const el of document.querySelectorAll('label,legend,p,span,div,h3,h4,h5,h6')) {
    if (!visible(el)) continue;
    const own = ownText(el) || (el.children.length === 0 ? clean(el.innerText) : '');
    if (!own || own.length > 300 || !/\*\s*$/.test(own)) continue;
    let box = el;
    for (let i = 0; i < 5 && box && !hasField(box); i++) box = box.parentElement;
    if (!box || seen.has(box)) continue;
    seen.add(box);
    if (!answered(box)) missing.push(own.replace(/\s*\*\s*$/, '').slice(0, 140));
  }
  // 2) native required fields
  for (const el of document.querySelectorAll('[required],[aria-required=true]')) {
    const ty = (el.type || '').toLowerCase();
    let ok;
    if (ty === 'radio') ok = !!document.querySelector('input[type=radio][name="' + CSS.escape(el.name) + '"]:checked');
    else if (ty === 'checkbox') ok = el.checked;
    else if (ty === 'file') ok = el.files && el.files.length > 0;
    else ok = !!clean(el.value);
    if (ok) continue;
    const l = (el.labels && el.labels[0] && clean(el.labels[0].innerText)) || el.getAttribute('aria-label') ||
              el.getAttribute('placeholder') || el.name || el.id || el.tagName.toLowerCase();
    const name = clean(l).replace(/\s*\*\s*$/, '').slice(0, 140);
    if (!missing.includes(name)) missing.push(name);
  }
  return {missing, complete: missing.length === 0};
}"""


def choose(page, question, option, settle_ms=400):
    """Click the answer `option` under the question whose text contains `question`."""
    res = page.evaluate(CHOOSE_JS, [question, option])
    if res.get("ok"):
        page.wait_for_timeout(settle_ms)
    return res


def check_form(page):
    """Required questions still unanswered on the current page."""
    return page.evaluate(CHECK_JS)
