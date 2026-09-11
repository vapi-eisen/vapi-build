# Writing `plan/plan.json`

The plan says what the agent does, with which knowledge and tools, and how it behaves. It references the approved ontology by ID and the OpenAPI operations by `operationId`. Schema: `vapi_build/schemas/plan.schema.json`.

## Shape
```json
{
  "agent": {"name": "Standard Charter Assistant", "purpose": "…", "audience": "customers", "language": "en"},
  "runtime": {"serverUrl": "https://standardcharter.co",
              "model": {"provider": "openai", "model": "gpt-4.1", "temperature": 0.2},
              "voice": {"provider": "vapi", "voiceId": "Elliot"},
              "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"}},
  "jobs": [{"id": "job:compare-products", "label": "Compare savings products", "goals": ["goal:compare-products"], "handling": "ANSWER",
            "knowledge": ["claim:mma-minimum", "rule:disclose-fees"], "tools": ["listOfferings"],
            "slots": [{"name": "product", "description": "Which product the caller means", "required": false}],
            "steps": ["Ask which products…", "Look up…"], "safeguards": ["Never quote a rate not returned by the tool or knowledge base."],
            "escalation": "Offer a callback from a banker when the caller wants advice.",
            "examples": [{"caller": "What's the difference between…", "agent": "…"}]}],
  "tools": [{"operationId": "listOfferings", "description": "Search public products by keyword.", "auth": {"mode": "NONE"}, "startMessage": "One moment while I look that up."},
            {"operationId": "prepareAction", "description": "Prepare a transfer for confirmation.", "auth": {"mode": "BEARER_ENV", "env": "SC_SERVICE_TOKEN"}, "confirmBeforeCall": true,
             "extract": {"proposalId": "{{proposalId}}"}}],
  "knowledge": {"includeSourceDocuments": true, "includeWebsitePages": true, "includeDomainGuide": true, "excludeLocators": []},
  "assistants": [{"id": "primary", "name": "Standard Charter Assistant", "systemPrompt": "…", "firstMessage": "Thanks for calling Standard Charter. How can I help?",
                  "jobs": ["job:compare-products"], "tools": ["listOfferings", "prepareAction"], "knowledge": true}],
  "tests": [{"id": "test:compare", "scenario": "Product comparison", "callerOpening": "What savings accounts do you offer?", "expect": ["calls listOfferings", "names products from the result"], "mustNot": ["quotes a rate not in the result"]}],
  "exclusions": [{"what": "Investment advice", "why": "No authoritative source and out of policy."}]
}
```

## Deciding
- **Jobs** come from goals with enough support. `handling`: `ANSWER` (knowledge only), `GUIDED_PROCESS` (a procedure without tools), `TOOL_ACTION` (calls an operation), `HANDOFF` (another assistant or a human), `DECLINE` (say it is out of scope and why). List the knowledge records that justify each job; the compiler puts them in the domain guide the agent searches.
- **Tools** are the operations the agent may call. Include only what a job needs. The check fails on operations classified PRIVILEGED (admin, reset, internal) unless `agent.allowPrivileged` is true and the user asked for it. Every non-read operation (anything but GET) must have `confirmBeforeCall: true`, except login-style calls; the compiled prompt then requires a read-back and an explicit yes before the call.
- **Auth** per tool: `NONE`; `VAPI_CREDENTIAL` with a `credentialId` the user already has in Vapi; or `BEARER_ENV` with the name of an environment variable that will hold a bearer token when `apply` runs (a Vapi bearer credential is created from it; the value never touches disk). If an API needs a session token from a login call, give the login tool an `extract` map (Liquid over the response, e.g. `{"token": "{{token}}"}`) and reference `{{token}}` where later tools need it.
- **Knowledge base**: source documents (including PDFs), website pages as Markdown, and the generated domain guide. Exclude internal or employee-only documents with `excludeLocators` when the audience is customers. Transcripts are never included.
- **Assistants**: one is usually enough. Use several plus a `squad` only when jobs need different prompts or tool sets; each `handoffTo` entry becomes a handoff tool and squad destination. Prompts should name the jobs, the tone, what to verify before disclosing anything, and when to hand off. The compiler appends knowledge, tool, handoff, and exclusion sections automatically.
- **Tests**: a handful of caller openings with observable expectations, optionally `followUps` (later caller turns in the same chat). Prefer scenarios drawn from transcripts, without copying a caller's personal details. `test` runs them through Vapi chat after `apply`.

`check plan` resolves everything, fills runtime defaults, and prints the exact operations the agent will be able to call. Read that list to the user before `approve plan`.
