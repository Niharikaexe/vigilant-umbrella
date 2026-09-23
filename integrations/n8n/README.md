# Running this on n8n

Two versions, depending on what you want to show.

## 1. Standalone — the whole automation inside n8n

**[`standalone/`](standalone/)** — no backend, no Python, no container. Import
one file and it runs: guardrails, PII redaction, asset resolution, manual
retrieval with citations, a deterministic policy engine, and an approval gate.
15 nodes, 6 of them Code, with Azure OpenAI as an optional branch that the
workflow takes automatically when a key is present.

```bash
docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n
# Import standalone/workflow.json, activate, then:
curl -X POST http://localhost:5678/webhook/maintenance \
  -H 'Content-Type: application/json' \
  -d '{"text":"Crane 2 is grinding when it brakes and the load drifts down after it stops."}'
```

Full setup, the design trade-offs against the Python service, and the tests are
in **[`standalone/README.md`](standalone/README.md)**.

## 2. Thin — n8n drives, the service reasons

**[`workflow.json`](workflow.json)** — n8n owns the trigger, the Teams
notification and the response; AgentFlow does the reasoning over HTTP.

```bash
docker compose --profile n8n up        # starts AgentFlow and n8n together
```

Import `workflow.json`, then set two variables in n8n
(**Settings → Variables**, or the compose `environment` block):

| Variable | Value |
|---|---|
| `AGENTFLOW_URL` | `http://agentflow:8000` when both run in compose |
| `TEAMS_WEBHOOK_URL` | Your Teams incoming webhook (optional) |

```bash
curl -X POST http://localhost:5678/webhook/maintenance-signal \
  -H 'Content-Type: application/json' \
  -d '{"text":"Crane 2 is grinding when it brakes and the load drifts down after it stops."}'
```

## Which to use

The thin version is the one worth running for real: the switch node branches on
`requires_approval`, which the **service** sets from versioned, unit-tested
rules. n8n never decides whether something is safe to automate — it asks
something that can explain itself and be tested in CI. Swap n8n for Power
Automate and the branch logic is identical, which is the proof the split is in
the right place.

The standalone version exists because "import this one file" is a much better
demo than "first deploy my Python service", and because it shows the same
design surviving a move to a completely different runtime.
