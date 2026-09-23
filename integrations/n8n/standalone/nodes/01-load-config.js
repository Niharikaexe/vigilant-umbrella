// Load config -- the only node you edit to make this yours.
//
// CONFIG is replaced at build time with the contents of data/config.json, so the
// exported
// workflow stays a single importable file with no external dependencies.
// Downstream nodes read it back with $('Load config').first().json.config
// rather than passing it along, so the payload stays small.

const CONFIG = /*__CONFIG_START__*/ {} /*__CONFIG_END__*/;

const raw = $input.first().json;
// A webhook delivers { body, headers, query }; a manual execution or a Chat
// trigger delivers the fields directly. Accept both so the same workflow can be
// driven from curl, from the n8n UI, or from a Teams/Power Automate POST.
const body = raw.body ?? raw;

const text = String(body.text ?? body.chatInput ?? '').trim();
const readings = body.readings && typeof body.readings === 'object' ? body.readings : {};

if (!text) {
  throw new Error('No signal text supplied. POST { "text": "what was reported" }.');
}

return [{
  json: {
    config: CONFIG,
    signal: {
      runId: 'RUN-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8),
      receivedAt: new Date().toISOString(),
      text,
      originalText: text,
      kind: body.kind ?? 'operator_request',
      channel: body.channel ?? 'n8n',
      reportedBy: body.reported_by ?? body.reportedBy ?? null,
      assetHint: body.asset_hint ?? body.assetHint ?? null,
      readings,
    },
  },
}];
