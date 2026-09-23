// Built-in diagnosis -- the no-cloud path.
//
// This is not a stub. It is a real rule-based diagnosis over the same retrieved
// manual sections the LLM branch would use, so the workflow produces sensible,
// citable output with no Azure account attached. That is what makes this
// importable-and-working rather than importable-and-needs-six-keys.

const cfg = $('Load config').first().json.config;
const { signal, guard, asset, assetConfidence, assetCandidates, citations } = $input.first().json;

const text = signal.text.toLowerCase();
const has = (s, markers) => markers.filter((m) => new RegExp(`\\b${m.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i').test(s));

// Phrases that, in a MANUAL, mean the equipment is dangerous right now.
const MANUAL_SAFETY = ['safety critical','unsafe to operate','must not be operated','taken out of service immediately','must be taken out of service','lock out','snap-back','lethal','statutory','drop under gravity','no lifting operation may continue','severe safety hazard','must not run'];
const MANUAL_ENV = ['oil release','release to water','spill','contaminates','fire risk'];

// What an operator writes, graded. First hit wins, most severe first.
const SIGNAL_SEVERITY = [
  ['critical', ['will not start','wont start',"won't start",'failed to start','seized','snapped','parted','fire','cracked','crack','total failure','load drifts','drifts down','drifting down','does not stop',"doesn't stop",'not stopping','unresponsive','collapsed','no longer works']],
  ['high',     ['leaking','leak','leaks','overheating','overheat','alarm','tripped','grinding','pressure loss','loss of pressure','torn','burning smell','sparking','out of tolerance','quality escape','smoke','black smoke']],
  ['medium',   ['noise','noisy','knocking','rattling','vibration','vibrating','juddering','weeping','seeping','intermittent','sluggish','warm','hot','deviation','occasionally']],
  ['low',      ['cosmetic','scratch','paint','label','minor','slight','monitoring','trending','routine','for information']],
];
const INJURY = ['injured','injury','hurt','burned','burnt','crushed','struck by','hit by','taken to hospital','first aid','laceration','fracture'];
const NEAR_MISS = ['near miss','near-miss','nearly hit','almost hit','close call'];
const ENV_SIGNAL = ['spill','spilled','into the water','overboard','on the ground','on the floor','reached the deck','contaminated'];

const RANK = { low: 0, medium: 1, high: 2, critical: 3 };
const worst = (a, b) => (RANK[a] >= RANK[b] ? a : b);

// Telemetry thresholds. In production these come from the asset's condition
// monitoring policy; here they are the manual's published limits.
const THRESHOLDS = [['vibration', 7.1, 11.0], ['temperature', 90, 98], ['temp', 90, 98]];

let severity = 'medium';
let evidence = [];
for (const [level, markers] of SIGNAL_SEVERITY) {
  const hits = has(text, markers);
  if (hits.length) { severity = level; evidence = hits; break; }
}

const readingHits = [];
for (const [key, value] of Object.entries(signal.readings ?? {})) {
  for (const [marker, high, critical] of THRESHOLDS) {
    if (!key.toLowerCase().includes(marker)) continue;
    if (value >= critical) { severity = worst(severity, 'critical'); readingHits.push(`${key}=${value} at or above the critical limit ${critical}`); }
    else if (value >= high) { severity = worst(severity, 'high'); readingHits.push(`${key}=${value} at or above the alert limit ${high}`); }
  }
}

// Hazard flags come from the section that describes THIS fault -- the top hit --
// plus anything effectively tied with it. Within a class-filtered corpus every
// section shares the class word, so a section about an unrelated fault on the
// same machine ("Conveyor Emergency Stop Integrity") scores high on overlap
// alone and would otherwise flag a routine bearing trend as safety-critical.
const evidenceCitations = citations.filter((c) => c.score >= 0.97);
const corpus = evidenceCitations.map((c) => `${c.heading} ${c.text}`).join(' ').toLowerCase();

const safetyRisk = has(corpus, MANUAL_SAFETY).length > 0;
if (safetyRisk) severity = worst(severity, 'high');

const injury = has(text, INJURY).length > 0;
const nearMiss = has(text, NEAR_MISS).length > 0;
if (injury) severity = 'critical';

const environmentalRisk =
  (has(corpus, MANUAL_ENV).length > 0 && has(text, ['leak','leaking','leakage','spill','oil','fuel','coolant','hydraulic','release']).length > 0)
  || has(text, ENV_SIGNAL).length > 0;

// Only sections that plausibly describe this fault become failure modes. A
// weakly matched section is a useful source but listing it as a "22% likely
// failure mode" is noise that trains people to skim.
const failureModes = citations.filter((c) => c.score >= 0.3).slice(0, 3).map((c) => ({
  name: c.heading,
  likelihood: c.score,
  rationale: c.text.split('. ')[0] + '.',
  citation: `${c.docId} ${c.section}`,
}));

let confidence = 0.35 + 0.35 * (citations[0]?.score ?? 0)
  + (evidence.length || readingHits.length ? 0.12 : 0)
  + (citations.length >= 2 ? 0.10 : 0);
if (!citations.length) confidence = 0.25;
confidence = Number(Math.min(confidence, 0.93).toFixed(2));

// Prefer a SKU the manual itself names; fall back to the class catalogue.
let spare = null;
for (const c of evidenceCitations.length ? evidenceCitations : citations.slice(0, 1)) {
  const m = c.text.match(/\b[A-Z]{3,4}-[A-Z0-9-]{2,}\b/);
  if (m && cfg.spares.some((s) => s.sku === m[0])) { spare = m[0]; break; }
}
if (!spare && asset) {
  const forClass = cfg.spares.filter((s) => s.classes.includes(asset.class));
  if (forClass.length === 1) spare = forClass[0].sku;
}

const lead = failureModes[0]?.name?.toLowerCase() ?? 'an unclassified fault';
const summary = `Most likely ${lead}, assessed as ${severity} severity`
  + (safetyRisk ? ' with a safety risk to personnel' : '')
  + (environmentalRisk ? ' and a potential environmental release' : '')
  + `. Grounded in ${citations.length} manual section(s); `
  + (readingHits.length ? 'telemetry exceeds published limits' : 'based on the reported symptoms') + '.';

return [{
  json: {
    signal, guard, asset, assetConfidence, assetCandidates, citations,
    diagnosis: {
      summary, severity, safetyRisk, environmentalRisk, injury, nearMiss,
      confidence, recommendedSpare: spare, failureModes,
      source: 'built-in rules',
      evidence: { signalMarkers: evidence, readingMarkers: readingHits },
    },
  },
}];
