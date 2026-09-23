// Input guardrail + PII redaction.
//
// Runs BEFORE anything reaches a model. The order matters: screen first so a
// prompt-injection payload never reaches the LLM at all, then redact so personal
// data never leaves the trust boundary. A maintenance report can arrive from an
// email or a Teams message, so the text is genuinely untrusted input.

const signal = $input.first().json.signal;

const INJECTION = [
  /ignore (?:all |any )?(?:previous|prior|above) instructions/i,
  /disregard (?:the )?(?:system|previous) (?:prompt|instructions)/i,
  /you are now (?:a|an|in) /i,
  /reveal (?:your )?(?:system )?prompt/i,
  /\bauto[- ]?approve\b.*\beverything\b/i,
  /<\/?(?:system|instructions?)>/i,
];

const PII = [
  ['Email', /\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b/g],
  ['PhoneNumber', /(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?)\d{3}[\s-]?\d{3,4}\b/g],
  ['EmployeeId', /\b(?:emp|employee|badge|pers)[\s#:-]*\d{3,8}\b/gi],
];

const matched = INJECTION.filter((re) => re.test(signal.text));

let redacted = signal.text;
let redactions = 0;
const categories = [];
for (const [label, re] of PII) {
  const before = redacted;
  redacted = redacted.replace(re, `[${label}]`);
  if (redacted !== before) {
    // Count how many placeholders this pattern introduced.
    const n = (redacted.match(new RegExp(`\\[${label}\\]`, 'g')) || []).length;
    redactions += n;
    categories.push(label);
  }
}

return [{
  json: {
    signal: { ...signal, text: redacted, redactions, piiCategories: categories },
    guard: {
      safe: matched.length === 0,
      reason: matched.length ? 'prompt_injection' : null,
      redactions,
      categories,
    },
  },
}];
