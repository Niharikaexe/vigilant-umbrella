# Running the pipeline from n8n

n8n is the fastest way to see this orchestrated visually without a Microsoft
tenant. The workflow is deliberately thin: n8n handles the **trigger, the human
notification and the response**, and AgentFlow does the reasoning.

## Run it

```bash
docker compose --profile n8n up        # starts AgentFlow and n8n together
```

Then open <http://localhost:5678>, go to **Workflows -> Import from file**, and
choose `integrations/n8n/workflow.json`.

Set two environment variables in n8n (**Settings -> Variables**, or the
`docker-compose.yml` environment block):

| Variable | Value |
|---|---|
| `AGENTFLOW_URL` | `http://agentflow:8000` when both run in compose |
| `TEAMS_WEBHOOK_URL` | Your Teams incoming webhook (optional) |

Fire it with:

```bash
curl -X POST http://localhost:5678/webhook/maintenance-signal \
  -H 'Content-Type: application/json' \
  -d '{"text":"Crane 2 is grinding when it brakes and the load drifts down after it stops."}'
```

## What the workflow does

```
Webhook  ->  AgentFlow triage  ->  Approval needed?  ->  Teams adaptive card
                                                     \->  Auto-dispatched
                                                            \-> Respond
```

The switch node branches on `requires_approval`, which AgentFlow sets from the
policy engine. That is the point worth noticing: **n8n does not decide whether
something is safe to automate** -- it asks the service, which decides from
versioned rules and can explain itself. Swap n8n for Power Automate and the
branch logic is identical, because the decision does not live in either tool.
