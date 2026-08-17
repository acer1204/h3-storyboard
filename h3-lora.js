/* h3-lora.js — LoRA trigger-word engine for the H3 Prompt WebUI.
 *
 * Browser port of lora_h3.py. Same contract:
 *   - the MODEL only marks WHERE a behaviour happens, by writing <<SUB:key>>;
 *   - THIS code decides WHAT text goes there (exact trigger, byte-for-byte);
 *   - MAIN is never seen by the model; it is prepended by us at the very
 *     start of integrated_multimodal_description (before [Shot 1]).
 *   - gloss matching is the safety net for keys the model described in
 *     prose but forgot to mark.
 *
 * Preset shape (stored server-side via /api/loras, and in localStorage):
 *   { id, name, main: "2dan1m", subs: [{key:"dr1nk", gloss:"drinks from a bottle or cup"}, ...] }
 */
(function (root) {
  "use strict";

  const PLACEHOLDER = /<{1,2}\s*SUB\s*:\s*([^<>]+?)\s*>{1,2}/gi;
  const MAIN_PH     = /<{1,2}\s*MAIN\s*>{1,2}/gi;
  // bounded on purpose - an unbounded [^>]* would eat the rest of the text when ">" is missing
  const BROKEN_PH   = /<{1,2}\s*(?:SUB\s*:?\s*[A-Za-z0-9_\-]{0,32}|MAIN)\s*>{0,2}/gi;
  const KEY_RE = k => new RegExp("(?<![A-Za-z0-9])" + esc(k) + "(?![A-Za-z0-9])");
  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  /* ---------------- parsing "Trigger Main: / Trigger Sub:" text ---------------- */
  function splitList(s) { return String(s || "").split(/[,，]/).map(x => x.trim()).filter(Boolean); }

  function parseText(text) {
    let main = [], subs = [];
    for (let ln of String(text || "").split(/\r?\n/)) {
      ln = ln.trim();
      if (!ln || ln.startsWith("#")) continue;
      let m = ln.match(/^trigger\s*main\s*[:：]\s*(.*)$/i);
      if (m) { main = main.concat(splitList(m[1])); continue; }
      m = ln.match(/^trigger\s*sub\s*[:：]\s*(.*)$/i);
      if (m) { subs = subs.concat(splitList(m[1])); continue; }
    }
    return build(main, subs);
  }

  function build(mainItems, subItems) {
    const seen = new Set(), subs = [];
    for (const it of subItems || []) {
      let key, gloss, alias = "";
      if (typeof it === "object" && it) { key = it.key; gloss = it.gloss || ""; alias = it.alias || ""; }
      else {
        // text form: "dr1nk=drinks from a bottle" or "dr1nk=drinks from a bottle|喝東西"
        const p = String(it).split("="); key = p[0];
        const rest = p.slice(1).join("=") || "";
        const bar = rest.indexOf("|");
        if (bar >= 0) { gloss = rest.slice(0, bar); alias = rest.slice(bar + 1); } else gloss = rest;
      }
      key = (key || "").trim(); gloss = (gloss || "").trim(); alias = (alias || "").trim();
      if (!key || seen.has(key.toLowerCase())) continue;
      seen.add(key.toLowerCase());
      subs.push({ key, gloss, alias });
    }
    const main = (mainItems || []).map(x => String(x).trim()).filter(Boolean);
    return { main, subs, mainText: main.join(", ") };
  }

  function isActive(cfg) { return !!(cfg && ((cfg.main && cfg.main.length) || (cfg.subs && cfg.subs.length))); }

  /* Which SUB keys does this note name via their Chinese alias?  Returns [{key, alias}].
   * Substring match on purpose: Chinese has no word boundaries, and the user writes free prose
   * ("她把瓶子舉起來喝東西" should hit alias "喝東西"). Longer aliases are checked first so an
   * alias that contains another ("坐下來" vs "坐下") reports the more specific one. */
  function noteTriggers(note, cfg) {
    if (!note || !isActive(cfg)) return [];
    const txt = String(note).replace(/\s+/g, "");
    const out = [];
    const subs = (cfg.subs || []).filter(s => s.alias).slice().sort((a, b) => b.alias.length - a.alias.length);
    for (const s of subs) {
      const al = s.alias.replace(/\s+/g, "");
      if (al && txt.includes(al)) out.push({ key: s.key, alias: s.alias });
    }
    return out;
  }

  /* ---------------- system-prompt block ---------------- */
  function promptBlock(cfg, forced) {
    if (!isActive(cfg)) return "";
    forced = forced || [];
    const L = [];
    L.push("");
    L.push("==================== LORA TRIGGER PLACEMENT (active for this request) ====================");
    L.push("This request uses LoRA trigger words. YOU DO NOT WRITE THE TRIGGER TEXT. You only mark WHERE it goes,");
    L.push("using placeholders. A script replaces each placeholder with the exact trigger string afterwards.");
    L.push("Never write the raw trigger word, never paraphrase it, never expand it. Placeholders only.");
    if (cfg.main.length) {
      L.push("");
      L.push("MAIN trigger: handled entirely by the script. Do NOT write anything for it. Do NOT write <<MAIN>>.");
      L.push("Begin the field with [Shot 1] and continue straight into the prose exactly as you normally would;");
      L.push("the script prepends the main trigger in front of [Shot 1] afterwards.");
    }
    const glossed = cfg.subs.filter(s => s.gloss), unglossed = cfg.subs.filter(s => !s.gloss);
    if (cfg.subs.length) {
      L.push("");
      L.push("SUB triggers (behaviour keys). Placeholder form is exactly:  <<SUB:KEY>>");
      L.push("Each placeholder is a token inside the sentence that describes that behaviour, placed right where");
      L.push("the behaviour happens - e.g. \"...she lifts the bottle and <<SUB:DDD>> drinks from it...\".");
      L.push("Put it INSIDE the shot where the action occurs, never in the CONTROL line, never in the audio fields,");
      L.push("never in the Preserve or negative clauses.");
      if (glossed.length) {
        L.push("");
        L.push("Available SUB keys and what each one means:");
        for (const s of glossed) L.push("  <<SUB:" + s.key + ">>   =  " + s.gloss);
        L.push("");
        L.push("AUTO rule - this is MANDATORY, not optional: whenever a sentence you write describes one of these");
        L.push("behaviours, that sentence MUST contain the matching placeholder. If you write \"stands with her hands");
        L.push("on her hips\" and a key means \"hands on hips\", the placeholder goes in that sentence. If you write");
        L.push("\"sits down onto the bench\" and a key means \"sits down\", the placeholder goes there. Omitting it is");
        L.push("an error. Several different keys may fire in one clip if several behaviours happen. Do NOT invent a");
        L.push("behaviour just to use a key - but if the behaviour is in your prose, the key is not optional.");
        L.push("Place each key once per occurrence of its behaviour.");
      }
      if (unglossed.length) {
        L.push("");
        L.push("These keys have NO description, so you cannot judge them - use them ONLY if listed as REQUIRED below:");
        L.push("  " + unglossed.map(s => "<<SUB:" + s.key + ">>").join(", "));
      }
    }
    if (forced.length) {
      L.push("");
      L.push("REQUIRED this run - each of these placeholders MUST appear exactly once inside a shot body:");
      for (const k of forced) {
        const sub = cfg.subs.find(s => s.key.toLowerCase() === k.toLowerCase()) || {};
        const g = sub.gloss || "";
        const why = sub.alias && cfg._noteHits && cfg._noteHits.includes(k) ? "   (the user's note explicitly asks for this: \"" + sub.alias + "\")" : "";
        L.push("  <<SUB:" + k + ">>" + (g ? "   =  " + g : "") + why);
      }
      L.push("Build the story so that each required behaviour actually happens on screen, then mark it.");
      L.push("If a required behaviour is named in the user's note, it is a story beat the user WANTS - give it");
      L.push("real screen time (a full phase if the note centres on it), not a token mention.");
    }
    L.push("");
    L.push("SELF-CHECK for LoRA: (a) no raw trigger words anywhere, only <<SUB:...>> placeholders;");
    L.push("(b) every REQUIRED placeholder present exactly once; (c) placeholders only inside shot bodies.");
    return L.join("\n");
  }

  /* ---------------- gloss matching (safety net) ---------------- */
  const STOP = new Set(["a","an","the","her","his","their","its","from","into","onto","toward","towards","at",
                        "on","in","of","to","with","and","or","up","down","out","off","over","then"]);
  const SYN = {
    walk:  "(?:walk|stride|step|stroll|march|pace|approach)\\w*",
    run:   "(?:run|sprint|dash|jog|race|bolt)\\w*",
    sit:   "(?:sit|seat|settle|lower(?:s|ing)? (?:her|him)self|plop|perch)\\w*",
    stand: "(?:stand|rise|get(?:s|ting)? up|straighten)\\w*",
    drink: "(?:drink|sip|gulp|swig|swallow|takes? a(?: [a-z]+,?){0,3} (?:drink|sip|swig))\\w*",
    turn:  "(?:turn|swivel|pivot|rotate|swing)\\w*",
    head:  "(?:head|face|gaze|chin)",
    hip:   "hips?", hand: "hands?", camera: "(?:camera|lens|viewer)",
    wave:  "(?:wave|waving|waves)", smile: "(?:smile|grin|smirk|beam)\\w*",
    jump:  "(?:jump|leap|hop|bounce)\\w*", kneel: "(?:kneel|crouch|squat)\\w*",
    lean:  "(?:lean|tilt|bend)\\w*", raise: "(?:raise|lift|hoist)\\w*",
    point: "(?:point|gesture)\\w*", look: "(?:look|glance|gaze|stare|peer)\\w*",
  };
  function wordRx(w) {
    const base = (w.endsWith("s") && w.length > 3) ? w.slice(0, -1) : w;
    return new RegExp("(?<![A-Za-z])" + (SYN[base] || SYN[w] || (esc(base) + "\\w*")), "i");
  }
  // gloss -> {anchor, support, need}: anchor required; if >=2 content words, at least one support word too
  function glossRx(gloss) {
    const words = (String(gloss || "").toLowerCase().match(/[a-z]+/g) || []).filter(w => !STOP.has(w));
    if (!words.length) return null;
    return { anchor: wordRx(words[0]), support: words.slice(1).map(wordRx), need: words.length > 1 };
  }
  function glossHit(rx, sent) {
    const m = rx.anchor.exec(sent);
    if (!m) return null;
    if (rx.need && !rx.support.some(sp => sp.test(sent))) return null;
    return m;
  }
  function sentenceSpans(text) {
    const spans = []; let start = 0; const re = /\.(?=\s|$)/g; let m;
    while ((m = re.exec(text))) { spans.push([start, m.index + 1]); start = m.index + 1; }
    if (start < text.length) spans.push([start, text.length]);
    return spans;
  }
  function stripFragments(s) { return s.replace(BROKEN_PH, ""); }

  function glossInject(imd, cfg, placedLower, report) {
    let tailAt = imd.length;
    const mt = /\s*Preserve(?![A-Za-z])/.exec(imd);
    if (mt) tailAt = mt.index;
    const injected = [];
    for (const sub of cfg.subs) {
      if (!sub.gloss || placedLower.has(sub.key.toLowerCase())) continue;
      if (KEY_RE(sub.key).test(stripFragments(imd))) continue;
      const rx = glossRx(sub.gloss);
      if (!rx) continue;
      for (const [a, b] of sentenceSpans(imd.slice(0, tailAt))) {
        const sent = imd.slice(a, b);
        if (/^\s*The camera/.test(sent)) continue;
        const m = glossHit(rx, sent);
        if (!m) continue;
        const at = a + m.index;
        imd = imd.slice(0, at) + sub.key + " " + imd.slice(at);
        tailAt += sub.key.length + 1;
        injected.push(sub.key); placedLower.add(sub.key.toLowerCase());
        break;
      }
    }
    if (injected.length) report.subs_gloss_injected = injected;
    return imd;
  }

  /* ---------------- the main pass ---------------- */
  /**
   * apply(imd, cfg, forced) -> { imd, report, fixes, issues }
   * imd = the integrated_multimodal_description body (with or without a leading "[Shot 1]").
   */
  function apply(imd, cfg, forced) {
    const fixes = [], issues = [];
    const report = { main_inserted: false, subs_placed: [], subs_forced_appended: [], subs_unknown_dropped: [], subs_auto: [], subs_gloss_injected: [] };
    if (!isActive(cfg)) return { imd, report, fixes, issues };
    forced = (forced || []).filter(Boolean);
    const keymap = {}; for (const s of cfg.subs) keymap[s.key.toLowerCase()] = s.key;

    if (MAIN_PH.test(imd)) { imd = imd.replace(MAIN_PH, ""); fixes.push("removed stray <<MAIN>> placeholder written by model"); }
    MAIN_PH.lastIndex = 0;

    // 1. resolve placeholders -> exact key text
    imd = imd.replace(PLACEHOLDER, (m, raw) => {
      const k = keymap[raw.trim().toLowerCase()];
      if (!k) { report.subs_unknown_dropped.push(raw.trim()); return ""; }
      report.subs_placed.push(k); return k;
    });
    imd = imd.replace(/\s{2,}/g, " ").replace(/ ,/g, ",").replace(/ \./g, ".");
    if (report.subs_unknown_dropped.length)
      issues.push("model invented unknown SUB key(s), dropped: " + [...new Set(report.subs_unknown_dropped)].join(", "));

    // 1b. gloss safety net
    const placedLower = new Set(report.subs_placed.map(k => k.toLowerCase()));
    imd = glossInject(imd, cfg, placedLower, report);
    if (report.subs_gloss_injected.length)
      fixes.push("gloss-matched SUB key(s) the model missed, injected: " + report.subs_gloss_injected.join(", "));

    // 2. forced keys still missing -> append before Preserve
    const present = k => KEY_RE(k).test(stripFragments(imd));
    const missing = forced.filter(k => !placedLower.has(k.toLowerCase()) && !present(keymap[k.toLowerCase()] || k));
    if (missing.length) {
      const canon = missing.map(k => keymap[k.toLowerCase()] || k);
      const mt = /\s*(Preserve(?![A-Za-z]).*)$/s.exec(imd);
      const ins = " " + canon.join(" ");
      imd = mt ? imd.slice(0, mt.index).trimEnd() + ins + " " + imd.slice(mt.index).trimStart() : imd.trimEnd() + ins;
      report.subs_forced_appended = canon;
      fixes.push("forced SUB key(s) missing from model output, appended to last shot: " + canon.join(", "));
    }
    const forcedLower = new Set(forced.map(x => x.toLowerCase()));
    report.subs_auto = report.subs_placed.concat(report.subs_gloss_injected).filter(k => !forcedLower.has(k.toLowerCase()));

    // 3. MAIN at the very start, before [Shot 1]
    if (cfg.mainText) {
      const lead = cfg.mainText + ",";
      let body = imd.replace(/^\s+/, "");
      if (body.startsWith(lead)) report.main_inserted = true;
      else {
        body = body.replace(new RegExp("\\[Shot\\s*1\\]\\s*" + esc(lead) + "\\s*"), "[Shot 1] ");
        if (!/\[Shot\s*1\]/.test(body)) issues.push("no [Shot 1] marker - MAIN placed at field start anyway");
        imd = lead + " " + body; report.main_inserted = true;
        fixes.push("MAIN trigger prepended at field start (before [Shot 1]): " + cfg.mainText);
      }
    }

    // 4. scrub any leftover / broken fragment
    if (PLACEHOLDER.test(imd) || MAIN_PH.test(imd) || BROKEN_PH.test(imd)) {
      imd = imd.replace(PLACEHOLDER, "").replace(MAIN_PH, "").replace(BROKEN_PH, "").replace(/\s{2,}/g, " ");
      fixes.push("stripped stray placeholder fragment");
    }
    PLACEHOLDER.lastIndex = MAIN_PH.lastIndex = BROKEN_PH.lastIndex = 0;
    return { imd: imd.replace(/\s{2,}/g, " ").trim(), report, fixes, issues };
  }

  /* apply to a FULL H3 prompt string that contains the three labels (what the frontend holds) */
  function applyToContent(content, cfg, forced) {
    if (!isActive(cfg)) return { content, report: null, fixes: [], issues: [] };   // byte-identical passthrough
    const A = "integrated_multimodal_description:", B = "overall_soundscape:";
    const i = content.indexOf(A); if (i < 0) return { content, report: null, fixes: [], issues: ["no integrated_multimodal_description label"] };
    const j = content.indexOf(B, i + A.length);
    const head = content.slice(0, i + A.length);
    const body = content.slice(i + A.length, j > 0 ? j : undefined);
    const rest = j > 0 ? content.slice(j) : "";
    // preserve the original separator (newline vs space) after the label
    const sep = /^\s*\n/.test(body) ? "\n" : " ";
    const r = apply(body.trim(), cfg, forced);
    // scrub placeholders that leaked into audio fields too
    let rest2 = rest.replace(PLACEHOLDER, "").replace(MAIN_PH, "").replace(BROKEN_PH, "");
    if (rest2 !== rest) rest2 = rest2.replace(/[ 	]{2,}/g, " ").replace(/ ,/g, ",").replace(/ \./g, ".");
    if (rest2 !== rest) r.fixes.push("stripped stray placeholder from audio field");
    return { content: head + sep + r.imd + (rest2 ? "\n\n" + rest2.trimStart() : ""), report: r.report, fixes: r.fixes, issues: r.issues };
  }

  function verify(imd, cfg, forced) {
    const probs = [];
    if (!isActive(cfg)) return probs;
    if (cfg.mainText) {
      const lead = cfg.mainText + ",";
      if (!imd.replace(/^\s+/, "").startsWith(lead)) probs.push("MAIN trigger not at the very start");
      const n = imd.split(cfg.mainText).length - 1;
      if (n !== 1) probs.push("MAIN trigger appears " + n + " times (must be exactly 1)");
    }
    for (const k of forced || []) if (!KEY_RE(k).test(imd)) probs.push("forced SUB key '" + k + "' not present");
    if (PLACEHOLDER.test(imd) || MAIN_PH.test(imd) || BROKEN_PH.test(imd)) probs.push("unresolved or broken placeholder remains");
    PLACEHOLDER.lastIndex = MAIN_PH.lastIndex = BROKEN_PH.lastIndex = 0;
    return probs;
  }

  root.H3Lora = { parseText, build, isActive, promptBlock, apply, applyToContent, verify, splitList, noteTriggers };
})(typeof window !== "undefined" ? window : globalThis);
