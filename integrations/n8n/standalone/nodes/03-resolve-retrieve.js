// Asset resolution + manual retrieval.
//
// Resolution is layered cheapest-first: explicit id, then a configured alias,
// then "only one machine of that class exists", then fuzzy token overlap. Most
// real signals name the asset plainly and paying a model to read "PMP-003" back
// to you is how a demo becomes expensive in production.
//
// Retrieval is a small TF-IDF-ish score over manual sections, filtered to the
// asset's class first -- returning the crane manual for a compressor fault is
// worse than returning nothing. Chunks are whole manual sections, because the
// section id IS the citation an engineer needs.

const cfg = $('Load config').first().json.config;
const { signal, guard } = $input.first().json;

const STOP = new Set(['the','a','an','is','are','was','on','in','at','of','and','to','it','its','there','has','have','with','from','for','be','been']);
const tokens = (s) => String(s).toLowerCase().match(/[a-z0-9]+/g)?.filter((t) => !STOP.has(t)) ?? [];

const NUMBER_WORDS = { one:'1', two:'2', three:'3', four:'4', five:'5', six:'6', seven:'7', eight:'8', nine:'9', ten:'10', fourteen:'14' };

// Operator vocabulary vs manual vocabulary. A fitter says "leaking oil"; the
// manual says "shaft seal". Bridging that gap is most of what makes retrieval
// work on real maintenance text.
const SYNONYMS = {
  leak:['leakage','weeping','drip','seal'], leaking:['leakage','seal','weeping','drip'],
  noise:['noisy','knocking','rattling','grinding'], grinding:['noise','brake','wear'],
  hot:['temperature','overheating'], overheating:['temperature','hot','cooler'],
  vibration:['vibrating','juddering','bearing','rms'], smoke:['exhaust','combustion','black'],
  drift:['drifts','brake','holding'], drifts:['drift','brake','holding'],
  rope:['wire','discard','broken'], pressure:['loss','hydraulic'],
  belt:['tracking','conveyor','torn'], start:['starting','crank'],
};
const expand = (ts) => ts.flatMap((t) => [t, ...(SYNONYMS[t] ?? [])]);

// ---------------------------------------------------------------- resolution

const haystack = `${signal.assetHint ?? ''} ${signal.text}`;
// Telemetry is deliberately NOT part of the matching text: "8.2" tokenises to
// {8, 2} and the "2" then matches "Gantry Crane 2", so a conveyor alert
// resolves to a crane.
const signalTokens = new Set(tokens(haystack));
for (const [word, digit] of Object.entries(NUMBER_WORDS)) {
  if (new RegExp(`\\b${word}\\b`, 'i').test(haystack)) signalTokens.add(digit);
}

const scoreAsset = (asset) => {
  const upper = haystack.toUpperCase();
  if (upper.includes(asset.id) || upper.replace(/[-\s]/g, '').includes(asset.id.replace('-', ''))) return 1.0;
  for (const alias of asset.aliases ?? []) {
    if (haystack.toLowerCase().includes(alias.toLowerCase())) return 0.93;
  }
  const assetTokens = new Set([...tokens(`${asset.name} ${asset.class} ${asset.site}`), asset.id.split('-')[1].replace(/^0+/, '')]);
  const overlap = [...signalTokens].filter((t) => assetTokens.has(t));
  if (!overlap.length) return 0;
  let score = overlap.length / assetTokens.size;
  const unit = asset.id.split('-')[1].replace(/^0+/, '');
  if (unit && signalTokens.has(unit)) score += 0.35;
  return Math.min(Number(score.toFixed(3)), 0.9);
};

let ranked = cfg.assets.map((a) => ({ asset: a, score: scoreAsset(a) }))
  .sort((x, y) => y.score - x.score);

let asset = null;
let confidence = ranked[0]?.score ?? 0;

if (confidence >= 0.25 && (confidence - (ranked[1]?.score ?? 0) >= 0.08 || confidence >= 0.9)) {
  asset = ranked[0].asset;
} else {
  // "The generator will not start" is unambiguous on a site with one generator
  // and genuinely ambiguous on a site with six. Let the registry decide which
  // situation we are in rather than hard-coding either answer.
  const byClass = cfg.assets.filter((a) => a.class.split('_').some((w) => signalTokens.has(w)));
  if (byClass.length === 1) {
    asset = byClass[0];
    confidence = 0.78;
  } else {
    // Nothing matched clearly and the class is ambiguous. Below the floor it is
    // a guess, and guessing is worse than saying we do not know: rule AIQ-002
    // routes the unknown case to a person.
    confidence = Math.min(confidence, cfg.policy.assetConfidenceFloor - 0.01);
    asset = null;
  }
}

// ---------------------------------------------------------------- retrieval

const queryTerms = expand(tokens(signal.text));
const pool = cfg.manuals.filter((m) => !asset || m.classes.includes(asset.class));

const scored = pool.map((m) => {
  const docTokens = tokens(`${m.heading} ${m.text}`);
  const counts = docTokens.reduce((acc, t) => (acc[t] = (acc[t] ?? 0) + 1, acc), {});
  let score = 0;
  for (const term of queryTerms) {
    if (counts[term]) score += counts[term] / Math.sqrt(docTokens.length);
  }
  return { m, score };
}).filter((x) => x.score > 0).sort((x, y) => y.score - x.score).slice(0, 4);

const best = scored[0]?.score || 1;
const citations = scored.map(({ m, score }) => ({
  docId: m.docId,
  section: m.section,
  heading: m.heading,
  text: m.text,
  score: Number(Math.min(score / best, 1).toFixed(3)),
}));

return [{
  json: {
    signal,
    guard,
    asset,
    assetConfidence: Number(confidence.toFixed(3)),
    assetCandidates: ranked.slice(0, 3).filter((r) => r.score > 0.05).map((r) => ({ id: r.asset.id, name: r.asset.name, score: r.score })),
    citations,
    // Built here rather than inside the HTTP node's expression so the prompt is
    // readable, diffable and testable. House style: a narrow role, the grounding
    // material quoted verbatim with its section ids, an explicit instruction to
    // cite, a closed output schema, and a stated escape hatch -- most
    // hallucination is a prompt refusing to accept "I don't know" as an answer.
    llmPrompt: {
      system: [
        'You are a maintenance engineer\'s diagnostic assistant for industrial assets.',
        'You reason ONLY from the manual extracts provided. Every failure mode you',
        'propose must cite the section it came from, using the exact section id shown.',
        'If the extracts do not support a diagnosis, return an empty failure_modes',
        'list and a confidence below 0.4 rather than speculating.',
        '',
        'Severity definitions (use exactly these):',
        '  critical - asset must stop now; safety or total loss of function',
        '  high     - significant loss of function or a protective device impaired',
        '  medium   - degraded but serviceable; plan into the next window',
        '  low      - cosmetic or early-stage; monitor and trend',
        '',
        'Set safety_risk true only if the extracts indicate danger to people.',
        'Set environmental_risk true only for a release of oil, fuel or chemicals.',
      ].join('\n'),
      user: [
        `ASSET: ${asset ? `${asset.name} (${asset.class}, criticality ${asset.criticality} of 5)` : 'Unidentified'}`,
        '',
        'MANUAL EXTRACTS:',
        citations.map((c) => `[${c.docId} ${c.section} ${c.heading}]\n${c.text}`).join('\n\n') || '(none retrieved)',
        '',
        `REPORTED SIGNAL (${signal.kind}):`,
        `"${signal.text}"`,
        Object.keys(signal.readings ?? {}).length ? `TELEMETRY: ${JSON.stringify(signal.readings)}` : '',
        '',
        'Reply with JSON only:',
        '{"summary":"<two sentences for a supervisor>",',
        ' "failure_modes":[{"name":"<short name>","likelihood":<0.0-1.0>,"rationale":"<why>","sections":["<section id e.g. s3.1>"]}],',
        ' "severity":"critical|high|medium|low","safety_risk":true|false,"environmental_risk":true|false,',
        ' "injury_reported":true|false,"near_miss":true|false,"confidence":<0.0-1.0>,"recommended_spare":"<sku or null>"}',
      ].join('\n'),
    },
  },
}];
