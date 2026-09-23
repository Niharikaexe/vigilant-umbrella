#!/usr/bin/env node
/**
 * Assemble workflow.json from nodes/*.js and data/config.json.
 *
 * The Code-node bodies live as real .js files so they can be syntax-checked,
 * linted and unit-tested (see test.mjs) instead of being edited as escaped
 * strings inside a JSON blob. This script folds them -- and the config -- into
 * the single importable workflow file n8n expects.
 *
 *   node build.mjs
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const src = (f) => readFileSync(join(here, 'nodes', f), 'utf8');

const config = JSON.parse(readFileSync(join(here, 'data', 'config.json'), 'utf8'));
delete config._comment;

const loadConfig = src('01-load-config.js').replace(
  /\/\*__CONFIG_START__\*\/[\s\S]*?\/\*__CONFIG_END__\*\//,
  JSON.stringify(config, null, 2),
);

const code = (name, jsCode, position, notes) => ({
  parameters: { jsCode, options: {} },
  id: name.toLowerCase().replace(/[^a-z0-9]+/g, '-'),
  name,
  type: 'n8n-nodes-base.code',
  typeVersion: 2,
  position,
  notes,
});

const workflow = {
  name: 'AgentFlow - Maintenance Triage (standalone)',
  nodes: [
    {
      parameters: {
        httpMethod: 'POST',
        path: 'maintenance',
        responseMode: 'responseNode',
        options: {},
      },
      id: 'signal-received',
      name: 'Signal received',
      type: 'n8n-nodes-base.webhook',
      typeVersion: 2,
      position: [-660, 340],
      webhookId: 'agentflow-standalone-maintenance',
      notes: 'POST { "text": "...", "readings": { "vibration_mm_s": 12.9 } }',
    },

    code('Load config', loadConfig, [-440, 340], 'Edit data/config.json and re-run build.mjs to change assets, rules or manuals.'),
    code('Guard & redact', src('02-guard-redact.js'), [-220, 340], 'Prompt-injection screening and PII redaction, before any model sees the text.'),

    {
      parameters: {
        conditions: {
          options: { caseSensitive: true, leftValue: '', version: 2 },
          conditions: [{
            id: 'safe',
            leftValue: '={{ $json.guard.safe }}',
            rightValue: '',
            operator: { type: 'boolean', operation: 'true', singleValue: true },
          }],
          combinator: 'and',
        },
        options: {},
      },
      id: 'input-safe',
      name: 'Input safe?',
      type: 'n8n-nodes-base.if',
      typeVersion: 2,
      position: [0, 340],
      notes: 'Fails closed: unsafe input stops here and never reaches a model.',
    },

    code('Resolve asset & retrieve manuals', src('03-resolve-retrieve.js'), [220, 240],
      'Registry match, then manual sections filtered by asset class. Chunked by section because the section id is the citation.'),

    {
      parameters: {
        conditions: {
          options: { caseSensitive: true, leftValue: '', version: 2 },
          conditions: [{
            id: 'has-key',
            leftValue: '={{ $env.AZURE_OPENAI_API_KEY }}',
            rightValue: '',
            operator: { type: 'string', operation: 'notEmpty', singleValue: true },
          }],
          combinator: 'and',
        },
        options: {},
      },
      id: 'azure-configured',
      name: 'Azure OpenAI configured?',
      type: 'n8n-nodes-base.if',
      typeVersion: 2,
      position: [440, 240],
      notes: 'With no key set the workflow still runs end to end on the built-in rules.',
    },

    {
      parameters: {
        method: 'POST',
        url: '={{ $env.AZURE_OPENAI_ENDPOINT }}/openai/deployments/{{ $env.AZURE_OPENAI_DEPLOYMENT || "gpt-4o-mini" }}/chat/completions?api-version={{ $env.AZURE_OPENAI_API_VERSION || "2024-06-01" }}',
        sendHeaders: true,
        headerParameters: { parameters: [{ name: 'api-key', value: '={{ $env.AZURE_OPENAI_API_KEY }}' }] },
        sendBody: true,
        specifyBody: 'json',
        jsonBody: '={{ JSON.stringify({ messages: [ { role: "system", content: $json.llmPrompt.system }, { role: "user", content: $json.llmPrompt.user } ], temperature: 0.1, max_tokens: 900, response_format: { type: "json_object" } }) }}',
        options: { timeout: 45000 },
      },
      id: 'azure-openai',
      name: 'Azure OpenAI diagnosis',
      type: 'n8n-nodes-base.httpRequest',
      typeVersion: 4.2,
      position: [660, 140],
      onError: 'continueRegularOutput',
      notes: 'On error the run continues; the parser downgrades confidence so a human is pulled in.',
    },

    code('Parse diagnosis', src('05-parse-llm-diagnosis.js'), [880, 140],
      'Validates the JSON and drops any citation the model did not actually receive.'),
    code('Diagnose (built-in)', src('04-diagnose-builtin.js'), [660, 340],
      'Rule-based diagnosis over the same retrieved sections. Real logic, not a stub.'),
    code('Score risk, apply policy & plan', src('06-policy-plan.js'), [1120, 240],
      'Deterministic: priority matrix, policy rules with citations, allow-listed action plan.'),

    {
      parameters: {
        conditions: {
          options: { caseSensitive: true, leftValue: '', version: 2 },
          conditions: [{
            id: 'needs-human',
            leftValue: '={{ $json.requiresApproval }}',
            rightValue: '',
            operator: { type: 'boolean', operation: 'true', singleValue: true },
          }],
          combinator: 'and',
        },
        options: {},
      },
      id: 'needs-approval',
      name: 'Needs human approval?',
      type: 'n8n-nodes-base.if',
      typeVersion: 2,
      position: [1340, 240],
      notes: 'The policy engine decides this, not the workflow author and not the model.',
    },

    {
      parameters: {
        method: 'POST',
        url: '={{ $env.TEAMS_WEBHOOK_URL }}',
        sendBody: true,
        specifyBody: 'json',
        jsonBody: '={{ JSON.stringify({ type: "message", attachments: [{ contentType: "application/vnd.microsoft.card.adaptive", content: { $schema: "http://adaptivecards.io/schemas/adaptive-card.json", type: "AdaptiveCard", version: "1.4", body: [ { type: "TextBlock", size: "Large", weight: "Bolder", text: $json.priority + " - " + ($json.asset ? $json.asset.name : "Unidentified asset") }, { type: "TextBlock", wrap: true, text: $json.diagnosis.summary }, { type: "TextBlock", wrap: true, isSubtle: true, text: "Held because: " + $json.approvalReason }, { type: "FactSet", facts: $json.findings.map(f => ({ title: f.ruleId, value: f.citation })) }, { type: "TextBlock", wrap: true, weight: "Bolder", text: "Proposed: " + $json.actions.map(a => a.label).join(", ") } ] } }] }) }}',
        options: {},
      },
      id: 'teams-card',
      name: 'Ask a supervisor in Teams',
      type: 'n8n-nodes-base.httpRequest',
      typeVersion: 4.2,
      position: [1560, 140],
      onError: 'continueRegularOutput',
      notes: 'Optional. Unset TEAMS_WEBHOOK_URL and the run still completes.',
    },

    {
      parameters: {
        assignments: {
          assignments: [
            { id: 'o1', name: 'outcome', value: 'auto-dispatched within policy', type: 'string' },
            { id: 'o2', name: 'dispatched', value: '={{ $json.actions.map(a => a.id).join(", ") }}', type: 'string' },
          ],
        },
        includeOtherFields: true,
        options: {},
      },
      id: 'auto-dispatch',
      name: 'Auto-dispatch',
      type: 'n8n-nodes-base.set',
      typeVersion: 3.4,
      position: [1560, 340],
      notes: 'Low-priority, high-confidence, reversible work only. Wire real connectors here.',
    },

    {
      parameters: {
        assignments: {
          assignments: [
            { id: 'b1', name: 'status', value: 'rejected_at_guardrail', type: 'string' },
            { id: 'b2', name: 'reason', value: '={{ $json.guard.reason }}', type: 'string' },
            { id: 'b3', name: 'message', value: 'Input rejected before it reached a model. Nothing was diagnosed or dispatched.', type: 'string' },
          ],
        },
        options: {},
      },
      id: 'blocked',
      name: 'Blocked',
      type: 'n8n-nodes-base.set',
      typeVersion: 3.4,
      position: [220, 460],
    },

    {
      parameters: { options: {} },
      id: 'respond',
      name: 'Respond',
      type: 'n8n-nodes-base.respondToWebhook',
      typeVersion: 1,
      position: [1800, 260],
    },
  ],

  connections: {
    'Signal received': { main: [[{ node: 'Load config', type: 'main', index: 0 }]] },
    'Load config': { main: [[{ node: 'Guard & redact', type: 'main', index: 0 }]] },
    'Guard & redact': { main: [[{ node: 'Input safe?', type: 'main', index: 0 }]] },
    'Input safe?': {
      main: [
        [{ node: 'Resolve asset & retrieve manuals', type: 'main', index: 0 }],
        [{ node: 'Blocked', type: 'main', index: 0 }],
      ],
    },
    'Blocked': { main: [[{ node: 'Respond', type: 'main', index: 0 }]] },
    'Resolve asset & retrieve manuals': { main: [[{ node: 'Azure OpenAI configured?', type: 'main', index: 0 }]] },
    'Azure OpenAI configured?': {
      main: [
        [{ node: 'Azure OpenAI diagnosis', type: 'main', index: 0 }],
        [{ node: 'Diagnose (built-in)', type: 'main', index: 0 }],
      ],
    },
    'Azure OpenAI diagnosis': { main: [[{ node: 'Parse diagnosis', type: 'main', index: 0 }]] },
    'Parse diagnosis': { main: [[{ node: 'Score risk, apply policy & plan', type: 'main', index: 0 }]] },
    'Diagnose (built-in)': { main: [[{ node: 'Score risk, apply policy & plan', type: 'main', index: 0 }]] },
    'Score risk, apply policy & plan': { main: [[{ node: 'Needs human approval?', type: 'main', index: 0 }]] },
    'Needs human approval?': {
      main: [
        [{ node: 'Ask a supervisor in Teams', type: 'main', index: 0 }],
        [{ node: 'Auto-dispatch', type: 'main', index: 0 }],
      ],
    },
    'Ask a supervisor in Teams': { main: [[{ node: 'Respond', type: 'main', index: 0 }]] },
    'Auto-dispatch': { main: [[{ node: 'Respond', type: 'main', index: 0 }]] },
  },

  settings: { executionOrder: 'v1' },
  pinData: {},
  meta: { instanceId: 'agentflow-standalone' },
  tags: [{ name: 'agentflow' }],
};

writeFileSync(join(here, 'workflow.json'), JSON.stringify(workflow, null, 2) + '\n');
const codeNodes = workflow.nodes.filter((n) => n.type === 'n8n-nodes-base.code');
console.log(`built workflow.json - ${workflow.nodes.length} nodes (${codeNodes.length} code), ` +
            `${config.assets.length} assets, ${config.rules.length} rules, ${config.manuals.length} manual sections`);
