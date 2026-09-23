// Risk scoring, policy engine and action planning.
//
// This is the part that decides, and it is deliberately deterministic. The model
// (when configured) diagnoses and explains; what priority the work gets, whether
// a human must sign it off, and which actions are permitted are all decided
// here from the rules in data/config.json. You can unit-test this. You cannot
// unit-test a prompt's judgement.

const cfg = $('Load config').first().json.config;
const input = $input.first().json;
const { signal, guard, asset, assetConfidence, assetCandidates, citations, diagnosis } = input;

// ------------------------------------------------------------ risk scoring

// Published, boring, and the same answer every time -- which is exactly what a
// maintenance planner wants from an automated system.
const MATRIX = {
  critical: { 5: 'P1', 4: 'P1', 3: 'P2', 2: 'P2', 1: 'P2' },
  high:     { 5: 'P1', 4: 'P2', 3: 'P2', 2: 'P3', 1: 'P3' },
  medium:   { 5: 'P2', 4: 'P3', 3: 'P3', 2: 'P3', 1: 'P4' },
  low:      { 5: 'P3', 4: 'P4', 3: 'P4', 2: 'P4', 1: 'P4' },
};
const ORDER = { P1: 1, P2: 2, P3: 3, P4: 4 };

const criticality = asset?.criticality ?? 3;
let priority = MATRIX[diagnosis.severity]?.[criticality] ?? 'P3';
const reasons = [`severity=${diagnosis.severity} x criticality=${criticality} -> ${priority}`];

if (diagnosis.safetyRisk && ORDER[priority] > 1) { priority = 'P1'; reasons.push('safety risk to personnel escalates to P1'); }
if (diagnosis.injury) { priority = 'P1'; reasons.push('injury reported escalates to P1'); }
if (diagnosis.environmentalRisk && ORDER[priority] > 2) { priority = 'P2'; reasons.push('environmental release risk escalates to at least P2'); }

// ------------------------------------------------------------ policy engine

const spare = diagnosis.recommendedSpare ? cfg.spares.find((s) => s.sku === diagnosis.recommendedSpare) : null;

const ctx = {
  resolved: Boolean(asset),
  criticality,
  severity: diagnosis.severity,
  safetyRisk: diagnosis.safetyRisk,
  environmentalRisk: diagnosis.environmentalRisk,
  injury: diagnosis.injury,
  nearMiss: diagnosis.nearMiss,
  confidence: diagnosis.confidence,
  citationCount: citations.length,
  isLifting: asset ? ['crane', 'winch'].includes(asset.class) : false,
  spareSku: spare?.sku ?? null,
  spareInStock: spare ? spare.stock > 0 : false,
  spareLeadTimeDays: spare?.leadTimeDays ?? 0,
};

// Rule expressions live in config, so they get a character allow-list before
// evaluation: identifiers, numbers, quoted strings and comparison/boolean
// operators only. No property access, brackets, calls or arrows -- which rules
// out property-chain escapes (`x.constructor.constructor`) by construction
// rather than by blacklist.
//
// Number literals are blanked before the check, because a decimal point is a
// dot and `confidence < 0.55` is a perfectly ordinary rule.
const SAFE_EXPR = /^[A-Za-z0-9_\s'"=!<>&|()+\-*/.]*$/;
const BANNED = /(\.|=>|\[|\]|`|;|\bthis\b|\bfunction\b|\bnew\b|\bimport\b|\brequire\b|\bglobalThis\b|\bprocess\b|\bconstructor\b)/;
const withoutNumbers = (expr) => expr.replace(/\d+(?:\.\d+)?/g, '0');

const keys = Object.keys(ctx);
const values = keys.map((k) => ctx[k]);
const findings = [];
const badRules = [];

for (const rule of cfg.rules) {
  const expr = String(rule.when);
  if (!SAFE_EXPR.test(expr) || BANNED.test(withoutNumbers(expr))) { badRules.push(rule.id); continue; }
  let fired = false;
  try {
    fired = Boolean(new Function(...keys, `"use strict"; return (${expr});`)(...values));
  } catch (e) {
    badRules.push(rule.id);
    continue;
  }
  if (!fired) continue;
  findings.push({
    ruleId: rule.id,
    severity: rule.severity,
    priority: rule.priority,
    message: String(rule.message).replace(/\{(\w+)\}/g, (_, k) => (ctx[k] ?? 'n/a')),
    actions: rule.actions ?? [],
    citation: rule.citation,
  });
}

// A rule we could not evaluate might have been the one that would have stopped
// us. Fail closed rather than pretend a clean pass.
if (badRules.length) {
  throw new Error(`Policy rules could not be evaluated: ${badRules.join(', ')}. Refusing to continue.`);
}

findings.sort((a, b) => ORDER[a.priority] - ORDER[b.priority]);
if (findings.length && ORDER[findings[0].priority] < ORDER[priority]) {
  priority = findings[0].priority;
  reasons.push(`policy rule ${findings[0].ruleId} raises it to ${priority}`);
}

// ------------------------------------------------------------ action plan

const allowed = Object.fromEntries(cfg.allowedActions.map((a) => [a.id, a]));
const actions = [];
for (const finding of findings) {
  for (const id of finding.actions) {
    // The allow-list is a ceiling, not a suggestion.
    if (!allowed[id] || actions.some((a) => a.id === id)) continue;
    actions.push({
      id,
      label: allowed[id].label,
      reversible: allowed[id].reversible,
      rationale: `${finding.message} (${finding.citation})`,
      sourceRules: findings.filter((f) => f.actions.includes(id)).map((f) => f.ruleId),
    });
  }
}
if (!actions.length) {
  // A silent no-op is the worst outcome: nobody knows nothing happened.
  actions.push({
    id: 'require_human_review',
    label: allowed.require_human_review.label,
    reversible: true,
    rationale: 'No policy rule covered this case; routing to a human so the gap is visible.',
    sourceRules: [],
  });
}

// ------------------------------------------------------- the approval gate

const approvalReasons = [];
if (ORDER[priority] <= ORDER[cfg.policy.humanApprovalAtOrAbove]) approvalReasons.push(`priority ${priority} is at or above the ${cfg.policy.humanApprovalAtOrAbove} approval threshold`);
if (diagnosis.confidence < cfg.policy.confidenceFloor) approvalReasons.push(`confidence ${diagnosis.confidence} is below the ${cfg.policy.confidenceFloor} floor`);
if (!asset) approvalReasons.push('the asset could not be identified');
if (findings.some((f) => f.severity === 'blocker')) approvalReasons.push('a blocker-severity policy rule fired');
const irreversible = actions.filter((a) => !a.reversible).map((a) => a.id);
if (irreversible.length) approvalReasons.push(`the plan contains irreversible action(s): ${irreversible.join(', ')}`);

const slaHours = cfg.policy.slaHours[priority] ?? 168;

return [{
  json: {
    runId: signal.runId,
    receivedAt: signal.receivedAt,
    status: approvalReasons.length ? 'awaiting_approval' : 'auto_dispatch',
    requiresApproval: approvalReasons.length > 0,
    approvalReason: approvalReasons.join('; '),
    priority,
    priorityReasons: reasons,
    slaDueAt: new Date(Date.now() + slaHours * 3600 * 1000).toISOString(),
    asset: asset ? { id: asset.id, name: asset.name, site: asset.site, criticality: asset.criticality, class: asset.class } : null,
    assetConfidence,
    assetCandidates,
    diagnosis,
    findings,
    actions,
    citations: citations.map((c) => ({ docId: c.docId, section: c.section, heading: c.heading, score: c.score })),
    redactions: signal.redactions,
    reportedText: signal.text,
  },
}];
