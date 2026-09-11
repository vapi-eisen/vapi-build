# vapi-build

A Claude Code skill that builds a complete, working [Vapi](https://vapi.ai) voice agent from an organization's own raw material:

- a public **website** (crawled within its host),
- **knowledge** articles from local files, URLs, or S3 (Markdown, text, YAML, JSON, HTML; PDF and DOCX are uploaded to the knowledge base as-is),
- sampled **call transcripts** from local files, URLs, or S3 (CSV incl. nested-transcript columns, JSON/JSONL, text), and
- an **OpenAPI** document describing what the agent may do, with the live backend URL you choose.

Claude is the ontologist and planner. A small Python CLI is the deterministic host: it fetches and pins the sources, verifies everything Claude writes against the evidence, and creates the Vapi resources. The result is an evidence-linked ontology, a reviewed plan, a v2 knowledge base, API Request tools for approved operations, one or more assistants, and a squad when needed.

Sources are specified in the conversation, never checked into this repo. Project workspaces default to `~/vapi-build-projects/<slug>`.

## Use it

Say `/vapi-build` in any Claude Code session and name your material. The skill asks for what it needs (sources and their roles, transcript privacy, the API base URL, audience, auth), then does every step itself and stops only for three yes/no gates: the ontology, the plan with the exact operations the agent may call, and the build. It then creates the Vapi resources and runs the plan's test scenarios through Vapi chat.

The one thing you do by hand, once: save your Vapi private key where the CLI reads it.

```bash
mkdir -p ~/.config/vapi-build && echo 'VAPI_API_KEY=<your private key>' > ~/.config/vapi-build/env && chmod 600 ~/.config/vapi-build/env
```

(Exporting `VAPI_API_KEY` in the shell you start Claude Code from also works.) S3 sources use your default AWS credentials or a profile you name.

The skill is installed for all sessions by symlinking its folder into `~/.claude/skills/vapi-build`; it ships a launcher, `vapi-build`, that runs the CLI from any directory. Inside this repo the same skill is available project-locally.

## Stages and files

| Stage | Command | Writes |
|---|---|---|
| register + fetch | `init`, `add`, `fetch` | `project.json`, `raw/<source>/…` with an inventory and digests |
| extract | `extract` | `evidence/ledger.json`, `evidence/segments/*.txt`, `evidence/packets/*.md`, `evidence/capabilities.json` |
| ontology | Claude writes `ontology/ontology.json`; `check`, `summarize`, `approve` | `ontology/candidate.json`, `check.json`, `approval.json` |
| plan | Claude writes `plan/plan.json`; `check`, `summarize`, `approve` | `plan/candidate.json`, `check.json`, `approval.json` |
| build | `compile`, `apply --yes`, `test`, `verify`, `teardown --yes` | `vapi/build.json`, `vapi/knowledge/`, `vapi/summary.md`, `vapi/receipts.json`, `vapi/test-results.json` |
| fan-out | subagents write `ontology/fragments/*.json`; `merge` | `ontology/ontology.json` |

Every evidence ID Claude cites is an exact character span in a pinned segment; the checker rejects citations that do not exist, facts that rest only on transcripts, rules without an authoritative source, dangling references, type cycles, and unknown operations. Approvals are bound to content digests, so a changed ontology invalidates the plan and a changed plan invalidates the build.

## Safety defaults

- Transcripts require a privacy attestation (`synthetic`, `redacted`, or `raw`); raw transcripts are never shown to the model or uploaded. A pattern scan reports emails, phone numbers, card-like and SSN-like strings.
- Every OpenAPI operation starts disabled. Administrative operations are refused unless explicitly allowed; write, financial, and destructive operations must be marked `confirmBeforeCall`, which the compiled prompt turns into a read-back and explicit confirmation.
- API keys and bearer tokens come from the environment at apply time and are never written to disk or printed.
- HTTPS only for remote sources, no private-network hosts, bounded page counts, object counts, and byte budgets.

## Develop

```bash
python3 -m pytest -q
python3 -m ruff check vapi_build tests
```

Tests use a fictional ferry operator and a fake Vapi transport; nothing touches the network.

## Lineage

The evidence discipline (host-pinned segments, model-authored proposals, independent checks, disabled-by-default capabilities, observational transcripts) is a compact descendant of the ontology contract and Agent Builder in `vapi-sales-demo-platform`. This repo trades that platform's console, queues, and release signing for a single skill and CLI.
