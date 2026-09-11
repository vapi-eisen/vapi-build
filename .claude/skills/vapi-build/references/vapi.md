# What `compile` and `apply` do in Vapi

`compile` writes `vapi/build.json` (every payload), `vapi/knowledge/` (the files), and `vapi/summary.md`. `apply --yes` executes it against `https://api.vapi.ai` with the private key found in `VAPI_API_KEY`/`VAPI_PRIVATE_KEY` in the environment or in `~/.config/vapi-build/env` (a `KEY=value` file the user writes themselves), in this order, writing `vapi/receipts.json` after every step so a rerun resumes instead of duplicating:

1. **Credentials**: for each `BEARER_ENV` tool, `POST /credential` with a `custom-credential` bearer plan from the named environment variable.
2. **Files**: `POST /file` (multipart, `purpose=knowledge-base-v2`) for every knowledge file.
3. **Knowledge base**: `POST /v2/knowledge-base`, then `POST /v2/knowledge-base/{id}/file` per file, then poll `GET /v2/knowledge-base/{id}` until every file is `ready` and the base reports its search `toolId` (falls back to listing tools of type `knowledgeBase`).
4. **Tools**: `POST /tool` with `type: apiRequest`, the method, a URL built from the server URL and the operation path with `{param}` rewritten to `{{param}}` (query parameters appended the same way), a `body` schema projected from the OpenAPI parameters and request body, optional `messages`, `variableExtractionPlan`, static `parameters`, and `credentialId`.
5. **Assistants**: `POST /assistant` with the compiled system prompt, `model.toolIds` = the knowledge-base search tool plus the API tools, handoff tools when the plan has several assistants, voice and transcriber from the plan runtime, and metadata naming the project and digests.
6. **Squad**: `POST /squad` when there is more than one assistant; the entry assistant is first.
7. **Verify**: `GET` every created resource and check the ID matches.

`teardown --yes` deletes in reverse (squad, assistants, tools, knowledge base, files, credentials) and removes the receipts.

## Testing the agent
`test` sends each plan test to the applied assistant (or squad) through `POST /chat`, chaining `followUps` with `previousChatId`, and saves `vapi/test-results.json`. Judge the transcripts against `expect` and `mustNot` yourself and report per scenario. Chat exercises the model, prompt, knowledge base, and tools, but not voice, so a voice check in the Vapi dashboard (talk button, or a web call with the public key) is still worth one pass. Check that write operations were read back and confirmed before the tool ran.

## Things to verify on a first live build
- URL variables (`{{bookingId}}`) resolve from the body properties the model fills; check the tool response in the call log if a path comes through unrendered.
- Optional query parameters render as empty strings when the model omits them; if the API rejects `?q=`, remove the optional parameter from the plan's tool or ask for it as required.
- Vapi indexes Markdown, text, PDF, and DOCX files. A file stuck in `indexing` past ten minutes stops `apply`; rerun to keep waiting, or remove that file from the plan.
- 401 from the tool means the auth mode is wrong for that API: switch to `BEARER_ENV`/`VAPI_CREDENTIAL`, or model the login flow with `extract`.
- Assistant names must be unique in the Vapi organization when handoffs address them by name.
