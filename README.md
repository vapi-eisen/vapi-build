# vapi-build

An [Agent Skill](https://agentskills.io/specification) for [Vapi](https://vapi.ai) that builds a complete, working voice agent from an organization's own raw material:

- a public **website** (crawled within its host),
- **knowledge** articles from local files, URLs, or S3 (Markdown, text, YAML, JSON, HTML; PDF and DOCX are uploaded to the knowledge base as-is),
- sampled **call transcripts** from local files, URLs, or S3 (CSV including nested-transcript columns, JSON/JSONL, text), and
- an **OpenAPI** document describing what the agent may do, with the live backend URL you choose.

The AI agent running the skill is the ontologist and planner. A small Python CLI shipped inside the skill is the deterministic host: it fetches and pins the sources, verifies everything the agent writes against the evidence, renders one review page, and creates the Vapi resources. The result is an evidence-linked ontology, a reviewed plan (a single assistant or a squad with a front-door authenticator, structured outputs for every call, a simulation suite), a knowledge base, API Request tools for approved operations, and the applied assistants, squad, structured outputs, and simulations.

The repository is laid out like [VapiAI/skills](https://github.com/VapiAI/skills) so the `vapi-build/` folder can be merged there as an experimental reference workflow. Sources are specified in the conversation, never checked into this repo. Project workspaces default to `~/vapi-build-projects/<slug>`.

## Install

```bash
npx skills add vapi-eisen/vapi-build --skill vapi-build
```

Or symlink the skill folder into your agent's skills directory, for example `~/.claude/skills/vapi-build` for Claude Code, and install the one required Python dependency:

```bash
pip install jsonschema            # plus: pip install pyyaml (YAML sources), pip install boto3 (S3 sources)
```

The skill's CLI runs through `vapi-build/scripts/vapi-build`, which needs nothing but Python 3.11 and works from any directory.

## Use it

Say `/vapi-build` in any session and name your material. The skill asks for what it needs (sources and their roles, transcript privacy, the API base URL, audience, auth, whether callers arrive by phone), then does every step itself and stops for two yes/no gates:

1. **Review page.** One HTML page with an **Ontology** tab (graph, browse, and every record's quoted evidence), a **Plan** tab (assistants and the single-versus-squad decision, jobs, the exact operations the agent may call with their risk, structured outputs, simulation scenarios), and a **Build** tab that fills in after compile. The page is served locally and refreshes itself whenever the skill re-renders it, so the tab you have open always shows the latest revision. One yes approves the ontology and the plan together.
2. **Build.** After `compile`, the Build tab lists exactly what will be created. On yes the skill applies it to Vapi, runs the plan's chat tests and the simulation suite, and re-renders the page with resource IDs, transcripts, and every evaluation.

You never leave the conversation. The Vapi private key is read from `VAPI_API_KEY` or found where it already lives (a shell profile, a `.env` file) and copied into `~/.config/vapi-build/env` by the CLI; values are never printed or pasted into chat.

## What the plan decides

- **Topology.** Whether one assistant suffices or the application should be a squad of specialists: distinct domains or personas, different tool or credential access, isolated context. When callers arrive by phone and the API can identify them, a **front-door** member looks the caller up by ANI (`{{customer.number}}`), asks for their PIN, and hands off with the verified customer id. The checker flags crowded single assistants and over-split squads.
- **Structured outputs.** What every call yields: a call-outcome record at least, one output per confirmed write, and the fields the business needs downstream. Created in Vapi and attached to the assistants.
- **Simulations.** AI-caller personalities drawn from the transcripts, one smoke scenario per job, evaluations judged through structured outputs, and mocks for every write tool a scenario could reach. Created as a Vapi simulation suite and run once on request.

## Stages and files

| Stage | Command | Writes |
|---|---|---|
| register + fetch | `init`, `add`, `fetch` | `project.json`, `raw/<source>/…` with an inventory and digests |
| extract | `extract` | `evidence/ledger.json`, `evidence/segments/*.txt`, `evidence/packets/*.md`, `evidence/capabilities.json` |
| ontology | the agent writes `ontology/ontology.json`; `check ontology` | `ontology/candidate.json`, `check.json` |
| plan | the agent writes `plan/plan.json`; `check plan`, `render`, `open`, `approve plan` | `plan/candidate.json`, `check.json`, `review.html`, `ontology/approval.json`, `plan/approval.json` |
| build | `compile`, `apply --yes`, `test`, `simulate --yes`, `verify`, `teardown --yes` | `vapi/build.json`, `vapi/knowledge/`, `vapi/summary.md`, `vapi/receipts.json`, `vapi/test-results.json`, `vapi/simulation-results.json` |
| fan-out | subagents write `ontology/fragments/*.json`; `merge` | `ontology/ontology.json` |

Every evidence ID the agent cites is an exact character span in a pinned segment; the checker rejects citations that do not exist, facts that rest only on transcripts, rules without an authoritative source, dangling references, type cycles, unknown operations, invalid output schemas, mismatched evaluation types, and simulation scenarios that would hit a live write unmocked. Approvals are bound to content digests, so a changed ontology invalidates the plan and a changed plan invalidates the build.

## Safety defaults

- Transcripts require a privacy attestation (`synthetic`, `redacted`, or `raw`); raw transcripts are never shown to the model or uploaded. A pattern scan reports emails, phone numbers, card-like and SSN-like strings.
- Every OpenAPI operation starts disabled. Administrative operations are refused unless explicitly allowed; every non-read operation must be marked `confirmBeforeCall`, which the compiled prompt turns into a read-back and explicit confirmation, unless the plan states a reason to skip it (a login or PIN check) and the user sees that on the review page.
- API keys and tokens are read from the environment or `~/.config/vapi-build/env` at apply time, injected into request headers only in the live request, and never written into build files, receipts, or output. A plan cannot name a platform secret, and a token equal to the Vapi key is refused.
- Simulations never reach a live write: the checker requires a mock for every write tool a scenario can reach, and running the suite needs an explicit yes because it uses credits.
- Builds are digest-bound: a changed ontology invalidates the plan, a changed plan or re-extracted evidence invalidates the build, and `apply` refuses stale builds.
- HTTPS only for remote sources, no private-network hosts, bounded page counts, object counts, and byte budgets. The review-page server binds to localhost only and serves one file.

## Layout

```
vapi-build/                 the skill, as VapiAI/skills expects it
  SKILL.md                  instructions for the agent
  references/               ontology, plan, and Vapi guides
  scripts/vapi-build        launcher
  scripts/vapi_build/       the Python CLI and JSON schemas
  tests/                    pytest suite (fictional ferry operator, fake Vapi transport)
scripts/                    the upstream repository's skill validator, copied verbatim
```

## Develop

```bash
python3 -m pytest -q
python3 -m ruff check vapi-build/scripts/vapi_build vapi-build/tests
python3 scripts/validate-agent-skills.py
```

Tests touch no network. The validator is the same one VapiAI/skills runs, so a passing tree here merges cleanly there.

## License

MIT
