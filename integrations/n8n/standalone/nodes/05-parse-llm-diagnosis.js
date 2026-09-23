// Parse the Azure OpenAI reply into the same shape the built-in branch emits.
//
// Two jobs, both about not trusting the model's output shape:
//  1. Extract and validate JSON. A model occasionally wraps it in prose or a
//     fenced block, and a free-text answer cannot drive a work order.
//  2. Map the section ids it cited back to sections we actually retrieved.
//     A model can only legitimately cite what it was given; anything else is
//     dropped rather than displayed. That is the difference between a citation
//     and a plausible-looking string.
//
// If the reply cannot be salvaged we fall back to the retrieved evidence rather
// than failing the run -- but we say so in `source`, so nobody mistakes a
// degraded run for a good one.

const upstream = $('Resolve asset & retrieve manuals').first().json;
const { signal, guard, asset, assetConfidence, assetCandidates, citations } = upstream;

const raw = $input.first().json;
let content = raw?.choices?.[0]?.message?.content ?? '';

let data = null;
try {
  const cleaned = String(content).trim().replace(/^```(?:json)?\s*|\s*```$/g, '');
  data = JSON.parse(cleaned);
} catch (e) {
  const match = String(content).match(/\{[\s\S]*\}/);
  if (match) { try { data = JSON.parse(match[0]); } catch (_) { data = null; } }
}

const RANK = { low: 0, medium: 1, high: 2, critical: 3 };
const valid = data && typeof data.summary === 'string' && data.summary.trim() && RANK[String(data.severity).toLowerCase()] !== undefined;

if (!valid) {
  return [{
    json: {
      signal, guard, asset, assetConfidence, assetCandidates, citations,
      diagnosis: {
        summary: citations.length
          ? `Model reply could not be parsed. Closest manual section: ${citations[0].heading}.`
          : 'Model reply could not be parsed and no manual section matched.',
        severity: 'medium',
        safetyRisk: false, environmentalRisk: false, injury: false, nearMiss: false,
        // Deliberately below the confidence floor so rule AIQ-001 forces a human.
        confidence: 0.3,
        recommendedSpare: null,
        failureModes: [],
        source: 'azure openai (unparseable reply, degraded)',
        evidence: {},
      },
    },
  }];
}

const bySection = Object.fromEntries(citations.map((c) => [c.section.toLowerCase(), c]));
const failureModes = (Array.isArray(data.failure_modes) ? data.failure_modes : []).map((m) => {
  const cited = (Array.isArray(m.sections) ? m.sections : [])
    .map((s) => bySection[String(s).trim().toLowerCase().split(' ')[0]])
    .filter(Boolean);
  return {
    name: String(m.name ?? 'unnamed'),
    likelihood: Number(m.likelihood ?? 0),
    rationale: String(m.rationale ?? ''),
    citation: cited.length ? `${cited[0].docId} ${cited[0].section}` : null,
  };
}).filter((m) => m.citation);  // an uncited failure mode is not shown

return [{
  json: {
    signal, guard, asset, assetConfidence, assetCandidates, citations,
    diagnosis: {
      summary: String(data.summary).trim(),
      severity: String(data.severity).toLowerCase(),
      safetyRisk: Boolean(data.safety_risk),
      environmentalRisk: Boolean(data.environmental_risk),
      injury: Boolean(data.injury_reported),
      nearMiss: Boolean(data.near_miss),
      confidence: Number(Number(data.confidence ?? 0).toFixed(2)),
      recommendedSpare: data.recommended_spare || null,
      failureModes,
      source: 'azure openai',
      evidence: {},
    },
  },
}];
