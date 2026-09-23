# The whole thing, as one n8n workflow

Same automation as the Python service, running **entirely inside n8n**. No
backend, no container, no Python. Import one file and it works.

```
Signal received ─→ Load config ─→ Guard & redact ─→ Input safe? ─┬─ no ──→ Blocked ─────────────┐
                                                                 │                              │
                                                                 └─ yes ─→ Resolve asset &      │
                                                                           retrieve manuals     │
                                                                                │               │
                                                              Azure OpenAI configured?          │
                                                                    ├─ yes → Azure OpenAI       │
                                                                    │        diagnosis →        │
                                                                    │        Parse diagnosis ┐  │
                                                                    └─ no ─→ Diagnose         │  │
                                                                             (built-in) ──────┤  │
                                                                                              ▼  │
                                                          Score risk, apply policy & plan        │
                                                                          │                      │
                                                            Needs human approval?                │
                                                              ├─ yes → Ask a supervisor in Teams ─┤
                                                              └─ no ─→ Auto-dispatch ─────────────┤
                                                                                                 ▼
                                                                                             Respond
```

15 nodes, 6 of them Code.

## Run it

```bash
docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n
```

Open <http://localhost:5678> → **Workflows → Import from File** →
`integrations/n8n/standalone/workflow.json` → **Save** → **Active**.

```bash
curl -X POST http://localhost:5678/webhook/maintenance \
  -H 'Content-Type: application/json' \
  -d '{"text":"Crane 2 is grinding when it brakes and the load drifts down 50mm after it stops."}'
```

```
status        awaiting_approval
priority      P1
asset         CRN-002 Gantry Crane 2 (criticality 5, confidence 0.93)
severity      critical · safety risk · confidence 0.92
spare         BRK-PAD-40T
findings      P1 SAF-002  Safety-critical fault on a criticality-5 asset
                          → HSE Procedure s9.1 Lock-Out/Tag-Out
              P1 SAF-003  Defect on lifting equipment: statutory re-examination
                          → Lifting Equipment Standard s4.4
              P1 OPS-001  Critical fault on a production-stopping asset
                          → Maintenance Policy s3.2 Priority Matrix
actions       freeze_asset · notify_duty_engineer · require_permit_to_work
              schedule_statutory_inspection · create_work_order
held because  P1 ≥ the P2 threshold; a blocker rule fired; irreversible actions
```

Nothing needs configuring for that. Every Azure service is optional:

| Variable | Effect when unset |
|---|---|
| `AZURE_OPENAI_API_KEY` | The `Azure OpenAI configured?` switch takes the **built-in** branch |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` | — |
| `TEAMS_WEBHOOK_URL` | The Teams node errors and continues; the run still completes |

The `Azure OpenAI configured?` switch reads `$env`, so leave n8n's
`N8N_BLOCK_ENV_ACCESS_IN_NODE` at its default (`false`).

## Making it yours

Edit **[`data/config.json`](data/config.json)** — your equipment, your policy
rules, your manual extracts — then rebuild:

```bash
node build.mjs && node test.mjs
```

`build.mjs` folds `data/config.json` and `nodes/*.js` into the single
`workflow.json` that n8n imports. The Code-node bodies live as real `.js` files
so they can be syntax-checked, linted and tested, rather than edited as escaped
strings inside a JSON blob.

## Tests

```bash
node test.mjs      # 11 checks, no network
```

It runs the **exact `jsCode` strings that ship inside `workflow.json`**, wired
in the order the workflow connects them, against a mock n8n runtime — plus
structural checks that every connection target exists, every `$('Node Name')`
reference resolves, and every node is reachable from the trigger. A renamed
node breaks the suite instead of breaking at 3am on the one branch nobody
exercised.

It earns its keep. Building this, it caught the expression guard rejecting
`confidence < 0.55` (the decimal point tripped the no-property-access rule), an
asset-confidence floor applied one layer too broadly, and a priority split that
made an 8.2 mm/s vibration trend and a 12.9 mm/s stop-now land on the same
priority.

## How this differs from the Python service

Same domain logic, same rules, same citations, same fail-closed behaviour. The
honest differences:

| | Python service | This workflow |
|---|---|---|
| Rule sandbox | `ast` walk against a node allow-list | Regex allow-list + `new Function` |
| Retrieval | BM25 with IDF | TF over section length |
| Audit trail | JSONL per run | n8n execution history |
| Human approval | Resumable — run parks, `/approve` resumes it | Notifies and ends; approval is a second call |
| Tests | 41 (pytest) | 11 (node) |

**The approval difference is the one that matters.** The Python service parks a
run as `awaiting_approval` and the `dispatch` node is *unreachable* until a
named person approves it. n8n has no durable mid-execution pause without
[Wait nodes](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/)
and a callback, so here the workflow notifies a supervisor and ends. Approving
means a second execution. For a prototype that is fine; for production, either
add a Wait node with a resume webhook or put the Python service behind it.

**The rule sandbox difference is worth knowing about.** The Python engine parses
each expression and walks the AST against an allow-list, so property-chain
escapes are impossible by construction. Here the expressions are checked against
a character allow-list and then evaluated with `new Function`. That is sound
while the rules live in `data/config.json` — the workflow author already
controls the Code nodes, so they gain nothing by injecting into a rule. It
stops being sound the moment you load rules from somewhere a third party can
write, like a shared spreadsheet or an inbound API. Do that and you need the
AST approach.

## Which one should you use?

- **This workflow** to demo the idea, hand it to someone who lives in n8n, or
  run it somewhere you cannot deploy a service.
- **The Python service** ([`../../../`](../../)) when the approval gate has to
  be durable, when the rules must be edited by people who should not be able to
  run arbitrary code, or when you want CI to tell you the safety logic still
  works before you ship.
- **[`../workflow.json`](../workflow.json)** — the thin version — when you want
  n8n's triggers and connectors but the service doing the reasoning. That is
  the combination I would actually run in production.
