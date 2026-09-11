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
            {"operationId": "prepareAction", "description": "Prepare a transfer for confirmation.", "auth": {"mode": "HEADER_ENV", "env": "SC_SERVICE_TOKEN"}, "confirmBeforeCall": true,
             "extract": {"proposalId": "{{proposalId}}"}},
            {"operationId": "customerLogin", "description": "Sign the caller in.", "auth": {"mode": "NONE"}, "skipConfirmationReason": "Login call; nothing to read back.",
             "extract": {"sessionToken": "{{token}}"}},
            {"operationId": "getCustomer", "description": "Read the signed-in customer's profile.", "auth": {"mode": "NONE"}, "headers": {"Authorization": "Bearer {{sessionToken}}"}}],
  "knowledge": {"includeSourceDocuments": true, "includeWebsitePages": true, "includeDomainGuide": true, "excludeLocators": []},
  "assistants": [{"id": "primary", "name": "Standard Charter Assistant", "systemPrompt": "You are the Standard Charter Bank assistant. Help customers compare products, answer policy questions from the knowledge base, and prepare transfers only after verification and an explicit confirmation. Be concise and warm.", "firstMessage": "Thanks for calling Standard Charter. How can I help?",
                  "jobs": ["job:compare-products"], "tools": ["listOfferings", "prepareAction"], "knowledge": true}],
  "tests": [{"id": "test:compare", "scenario": "Product comparison", "callerOpening": "What savings accounts do you offer?", "expect": ["calls listOfferings", "names products from the result"], "mustNot": ["quotes a rate not in the result"]}],
  "exclusions": [{"what": "Investment advice", "why": "No authoritative source and out of policy."}]
}
```

## Deciding
- **Jobs** come from goals with enough support. `handling`: `ANSWER` (knowledge only), `GUIDED_PROCESS` (a procedure without tools), `TOOL_ACTION` (calls an operation), `HANDOFF` (another assistant or a human), `DECLINE` (say it is out of scope and why). Everything you write on a job is compiled into the system prompt of each assistant that owns it: a “Jobs you handle” section with the steps, slots, safeguards, escalation, up to two examples, and the text of every `knowledge` record it lists. The domain guide in the knowledge base carries the whole approved ontology regardless.
- **Tools** are the operations the agent may call. Include only what a job needs. The check fails on operations classified PRIVILEGED (admin, reset, internal) unless `agent.allowPrivileged` is true and the user asked for it. Every non-GET operation must carry `confirmBeforeCall: true`; the compiled prompt then requires a read-back and an explicit yes. The only way out is `skipConfirmationReason` (a login-style call with nothing to read back), which the check prints as "NO read-back" so the user sees it at Gate 2.
- **Auth** per tool: `NONE`; `VAPI_CREDENTIAL` with the `credentialId` of a credential the user already created in Vapi; or `HEADER_ENV` with `env`, the name of a variable the user has saved in `~/.config/vapi-build/env` (the same file as the Vapi key). At apply time its value is sent as a fixed header, `Authorization: Bearer <token>` by default; set `headerName` and `prefix` for APIs that want `X-API-Key: <token>`. Never name a platform secret (`VAPI_*`, `AWS_*`, anything with `SECRET`); the check refuses those. For a session token that a login operation returns, give the login tool an `extract` map (Liquid over its response, e.g. `{"sessionToken": "{{token}}"}`) and put `"headers": {"Authorization": "Bearer {{sessionToken}}"}` on the tools that need it; `headers` values are fixed or Liquid, never model-generated.
- **Knowledge base**: source documents (including PDFs), website pages as Markdown, and the generated domain guide. Exclude internal or employee-only documents with `excludeLocators` when the audience is customers. Transcripts are never included.
- **Assistants**: one is usually enough; names are at most 40 characters and unique. Use several plus a `squad` only when jobs need different prompts or tool sets; each `handoffTo` entry becomes a handoff tool and squad destination. Prompts should name the jobs, the tone, what to verify before disclosing anything, and when to hand off. The compiler appends knowledge, tool, handoff, and exclusion sections automatically.
- **Tests**: a handful of caller openings with observable expectations, optionally `followUps` (later caller turns in the same chat). Prefer scenarios drawn from transcripts, without copying a caller's personal details. `test` runs them through Vapi chat after `apply`.

`check plan` resolves everything, fills runtime defaults, and prints the exact operations the agent will be able to call. Read that list to the user before `approve plan`.
