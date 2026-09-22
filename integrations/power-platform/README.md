# Copilot Studio, Power Automate and the custom connector

Three pieces, in the order you set them up.

## 1. Publish the service

Copilot Studio and Power Automate need a public HTTPS endpoint. Azure Container
Apps is the shortest path:

```bash
az containerapp up \
  --name agentflow --resource-group rg-agentflow \
  --source . --ingress external --target-port 8000 \
  --env-vars AGENTFLOW_LLM_PROVIDER=azure \
             AZURE_OPENAI_ENDPOINT=https://<your>.openai.azure.com \
             AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
```

Put the secrets in Key Vault and reference them, rather than passing
`--env-vars` for anything sensitive:

```bash
az containerapp secret set --name agentflow --resource-group rg-agentflow \
  --secrets openai-key=keyvaultref:https://<vault>.vault.azure.net/secrets/openai-key,identityref:system
```

Better still, give the container app a managed identity, grant it
`Cognitive Services OpenAI User`, and drop the key entirely. The adapters read
`AZURE_OPENAI_API_KEY` today; swapping in `DefaultAzureCredential` is a
contained change in `agentflow/llm.py` and is the right production answer.

## 2. Import the custom connector

Power Platform custom connectors take **Swagger 2.0**, not the OpenAPI 3.1 that
FastAPI serves at `/openapi.json`. That is why there is a hand-maintained
`apiDefinition.swagger.json` here rather than a generated file.

**Portal:** make.powerautomate.com -> Custom connectors -> New -> *Import an
OpenAPI file* -> `apiDefinition.swagger.json`. Set **Host** to your deployment's
hostname and the security type to **API Key**, header `X-API-Key`.

**CLI:**

```bash
pac connector create --api-definition-file apiDefinition.swagger.json \
                     --api-properties-file apiProperties.json \
                     --environment <environment-id>
```

The connector exposes three operations:

| Operation | Use it for |
|---|---|
| `TriageSignal` | The main call. Text in, diagnosis + priority + actions out. |
| `GetRun` | Poll a run started asynchronously. |
| `DecideRun` | Apply a human approval or rejection. |

## 3. Wire it up

### Copilot Studio agent

Create a topic, e.g. **Report a fault**, and add a **Tool -> Connector ->
AgentFlow -> Triage a maintenance signal**.

- Input `text`: the user's utterance (`Activity.Text` or a question variable).
- Input `channel`: `teams`.

Then branch the response on `requires_approval`:

- **false** -> tell the user what was raised: *"I've logged {priority} on
  {asset.name}. {diagnosis.summary}"*
- **true** -> tell them it needs a supervisor and why: *"This needs a
  supervisor's approval first: {approval_reason}."*

Suggested instructions for a generative-orchestration agent:

> You help maintenance staff report equipment faults. When someone describes a
> problem with equipment, call the AgentFlow triage tool with their exact words.
> Report back the priority, the asset, and the one-line summary. Always quote
> the policy citations from `findings` when you explain why something was
> escalated. Never tell a user an action has been taken when
> `requires_approval` is true -- say it is waiting for a supervisor. Never
> invent an asset name, a priority or a policy reference that is not in the
> tool's response.

That last pair of sentences is the important part. The service already fails
closed; the agent's instructions must not undo it by narrating an optimistic
version of what happened.

### Power Automate flow

The natural shape, using the connector:

```
When a new email arrives (V3)          [or: Teams keyword, Forms response, IoT alert]
  -> AgentFlow: Triage a maintenance signal     text: @{triggerOutputs()?['body/body']}
  -> Condition: requires_approval is true
       Yes -> Start and wait for an approval    (Approvals connector)
              -> AgentFlow: Approve or reject   approved: @{outcome == 'Approve'}
                                                approver: @{approver email}
       No  -> Post an adaptive card to Teams    (already dispatched)
```

`sample-flow.json` is a working skeleton of that flow. Import it via **My flows
-> Import -> Import package**, then reconnect the trigger and the connector
references (the connection ids are environment-specific, so they always need
rebinding after an import).

## Why the logic is not in Copilot Studio

A fair question in an interview, so here is the answer this repo is built on.

Copilot Studio and Power Automate are genuinely good at the things they own:
triggers, Microsoft 365 connectors, approvals, identity, DLP policy, and putting
a conversational surface in Teams in an afternoon. They are poor at
version-controlled multi-step reasoning: you cannot meaningfully diff a topic,
unit-test a branch, or roll back a prompt change on its own.

So the split is:

| Concern | Where it lives | Why |
|---|---|---|
| Triggers, connectors, approvals, identity | Power Platform | It is already there and governed |
| Diagnosis, retrieval, policy, planning | This service | Needs tests, diffs, and a rollback story |
| The rules themselves | `config/rules.yaml` | Reviewable by the people who own the policy |

The cost is one more thing to deploy. The benefit is that `make test` tells you
whether the safety logic still works, which no amount of clicking through a
topic designer will.
