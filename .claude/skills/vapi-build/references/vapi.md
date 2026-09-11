# What `compile` and `apply` do in Vapi

`compile` writes `vapi/build.json` (every payload), `vapi/knowledge/` (the files, each with its digest), and `vapi/summary.md`. `apply --yes` first checks that the build matches the currently approved plan and the current evidence (a stale build is refused with "run `compile` again"), resolves every token it will need from `~/.config/vapi-build/env`, and only then talks to `https://api.vapi.ai` with the private key from `VAPI_API_KEY`/`VAPI_PRIVATE_KEY` in the environment or that same file. Order, with `vapi/receipts.json` written after every step so a rerun resumes instead of duplicating:

1. **Files**: `POST /file` (multipart, `purpose=knowledge-base-v2`) for every knowledge file. A receipted file whose content changed is refused: run `teardown --yes` then `apply --yes`.
2. **Knowledge base**: `POST /v2/knowledge-base`, then `POST /v2/knowledge-base/{id}/file` per file, then poll `GET /v2/knowledge-base/{id}` until every file is `ready` and the base reports its search `toolId` (falls back to listing tools of type `knowledgeBase`).
3. **Tools**: `POST /tool` with `type: apiRequest`, the method, a URL built from the server URL and the operation path with `{param}` rewritten to `{{param}}` (query parameters appended the same way), a `body` schema projected from the OpenAPI parameters and request body, optional `messages`, `variableExtractionPlan`, static `parameters`, `credentialId`, and `headers` whose properties carry a fixed `value`. `HEADER_ENV` tokens are injected into those header values here and nowhere else.
4. **Assistants**: `POST /assistant` with the compiled system prompt, `model.toolIds` = the knowledge-base search tool plus the API tools, handoff tools when the plan has several assistants, voice and transcriber from the plan runtime, and metadata naming the project and digests.
5. **Squad**: `POST /squad` when there is more than one assistant; the entry assistant is first.
6. **Verify**: `GET` every created resource and check the ID matches.

`teardown --yes` deletes in reverse (squad, assistants, tools, knowledge base, files). Anything Vapi refuses to delete (for example a pinned assistant) stays in the receipts and is reported; delete it in the dashboard and run teardown again.

## Testing the agent
`test` sends each plan test to the applied assistant (or squad) through `POST /chat`, chaining `followUps` with `previousChatId`, and saves `vapi/test-results.json`. Judge the transcripts against `expect` and `mustNot` yourself and report per scenario. Chat exercises the model, prompt, knowledge base, and tools, but not voice, so a voice check in the Vapi dashboard (talk button, or a web call with the public key) is still worth one pass. Check that write operations were read back and confirmed before the tool ran.

## Things to verify on a first live build
- URL variables (`{{bookingId}}`) resolve from the body properties the model fills; check the tool response in the call log if a path comes through unrendered.
- Optional query parameters render as empty strings when the model omits them; if the API rejects `?q=`, remove the optional parameter from the plan's tool or ask for it as required.
- Vapi indexes Markdown, text, PDF, and DOCX files. A file stuck in `indexing` past ten minutes stops `apply`; rerun to keep waiting, or remove that file from the plan.
- 401 from the tool means the auth mode is wrong for that API: switch to `HEADER_ENV` or `VAPI_CREDENTIAL`, or model the login flow with `extract` and a Liquid `headers` value.
- Assistant names must be unique in the Vapi organization when handoffs address them by name.
