"""Command line for the vapi-build skill. Every command prints a short, readable result."""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

from . import __version__, compile as compiler, extract, ontology, plan, sources, vapi
from .workspace import BuildError, Workspace, read_json

DEFAULT_ROOT = "~/vapi-build-projects"


def _workspace(args) -> Workspace:
    return Workspace.open(args.workspace)


def cmd_doctor(args) -> int:
    print(f"vapi-build {__version__} · python {sys.version.split()[0]}")
    for module in ("jsonschema", "yaml", "boto3"):
        try:
            importlib.import_module(module)
            print(f"  ok   {module}")
        except ImportError:
            print(f"  MISSING {module}  (pip install {'pyyaml' if module == 'yaml' else module}){'; only needed for s3:// sources' if module == 'boto3' else ''}")
    _, source = vapi.find_key()
    print(f"  {'ok  ' if source else 'unset'} Vapi private key" + (f" from {source}" if source else f" (export VAPI_API_KEY or save VAPI_API_KEY=… in {vapi.KEY_FILE}; needed only for apply/test/teardown)"))
    aws = any(os.environ.get(k) for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID")) or Path("~/.aws/credentials").expanduser().exists() or Path("~/.aws/config").expanduser().exists()
    print(f"  {'ok  ' if aws else 'unset'} AWS credentials (needed only for s3:// sources)")
    return 0


def cmd_init(args) -> int:
    root = args.workspace or str(Path(DEFAULT_ROOT).expanduser() / _slug(args.name))
    workspace = Workspace.create(root, args.name)
    if args.aws_profile:
        workspace.project["awsProfile"] = args.aws_profile
        workspace.save()
    print(f"Created project “{args.name}” at {workspace.root}")
    print("Next: register sources with `add`, then `fetch`.")
    return 0


def _slug(name: str) -> str:
    from .workspace import slug

    return slug(name, 40)


def cmd_add(args) -> int:
    workspace = _workspace(args)
    options = {"maxPages": args.max_pages, "sample": args.sample, "seed": args.seed, "scanMb": args.scan_mb, "serverUrl": args.server_url,
               "allowedHosts": args.allowed_host or None}
    record = sources.add_source(workspace, args.role, args.location, privacy=args.privacy, authority=args.authority, **options)
    print(f"Registered {record['id']} ({record['role']}, authority {record['authority']}): {record['location']}")
    return 0


def cmd_fetch(args) -> int:
    workspace = _workspace(args)
    for inventory in sources.fetch_all(workspace, only=args.only):
        if inventory.get("error"):
            print(f"{inventory['source']}: FAILED — {inventory['error']}")
            continue
        print(f"{inventory['source']}: {inventory['itemCount']} items, {inventory['byteCount']:,} bytes")
        if inventory.get("sampling"):
            s = inventory["sampling"]
            print(f"  sampled {s['sampled']} of {s['candidatesSeen']} conversations seen (seed {s['seed']}{', scan truncated' if s['scanTruncated'] else ''})")
            scan = inventory["piiScan"]
            print(f"  privacy attestation: {inventory['privacy']}; pattern scan flagged {scan['conversationsWithHits']} conversations {scan['patternHits']}")
        if inventory.get("serverUrl"):
            print(f"  tools will call: {inventory['serverUrl']}")
        for note in inventory.get("notes", [])[:8]:
            print(f"  note: {note}")
    return 0


def cmd_extract(args) -> int:
    workspace = _workspace(args)
    summary = extract.extract_all(workspace, batch_size=args.batch_size)
    print(f"{summary['segments']} segments, {summary['evidence']} evidence spans, {summary['operations']} API operations, {summary['gaps']} gaps")
    for source_id, info in summary["bySource"].items():
        print(f"  {source_id}: {info['items']} items → {info['segments']} segments")
    print(f"Read these packets in order, then write {workspace.path('ontology', 'ontology.json')}:")
    for packet in summary["packetFiles"]:
        print(f"  {workspace.path(packet)}")
    if summary["gaps"]:
        print(f"Gaps are listed in {workspace.path('evidence', 'ledger.json')} under `gaps`.")
    for note in summary.get("oversizedPackets", []):
        print(f"  note: {note}")
    return 0


def _print_report(report: dict, workspace: Workspace) -> int:
    print(f"{report['stage']} check: {report['status']}")
    for key, value in report.get("counts", {}).items():
        if value:
            print(f"  {key}: {value}")
    for error in report["errors"]:
        print(f"  ERROR {error}")
    for warning in report["warnings"]:
        print(f"  warn  {warning}")
    if report["stage"] == "plan" and report.get("enabledOperations"):
        print("  operations the agent will be able to call:")
        for operation in report["enabledOperations"]:
            print(f"    - {operation}")
    if report["status"] == "CANDIDATE":
        print(f"  digest {report['digest']}")
        print(f"Next: `summarize {report['stage']}` for the user, then `approve {report['stage']}` once they say yes.")
    return 0 if report["status"] == "CANDIDATE" else 1


def cmd_check(args) -> int:
    workspace = _workspace(args)
    report = ontology.check_ontology(workspace, strict=args.strict) if args.stage == "ontology" else plan.check_plan(workspace)
    return _print_report(report, workspace)


def cmd_summarize(args) -> int:
    workspace = _workspace(args)
    if args.stage == "ontology":
        text = ontology.summarize(ontology.load_candidate(workspace))
    else:
        text = plan.summarize(plan.load_candidate(workspace), ontology.approved_ontology(workspace))
    print(text)
    return 0


def cmd_approve(args) -> int:
    workspace = _workspace(args)
    approval = ontology.approve_ontology(workspace, by=args.by) if args.stage == "ontology" else plan.approve_plan(workspace, by=args.by)
    print(f"Approved {approval['stage']} {approval['digest']} by {approval['by']} at {approval['at']}")
    if approval["stage"] == "plan":
        print("Next: `compile`, review vapi/summary.md with the user, then `apply --yes`.")
    else:
        print(f"Next: write {workspace.path('plan', 'plan.json')} and run `check plan`.")
    return 0


def cmd_compile(args) -> int:
    workspace = _workspace(args)
    build = compiler.compile_build(workspace)
    print(compiler.render_build_summary(build))
    print(f"Payloads: {workspace.path('vapi', 'build.json')}")
    return 0


def cmd_apply(args) -> int:
    workspace = _workspace(args)
    if not args.yes:
        print("apply creates resources in the Vapi organization that owns VAPI_API_KEY. Re-run with --yes after the user confirms.")
        return 1
    client = vapi.client_from_env()
    receipts = vapi.apply(workspace, client)
    print(f"Applied. knowledge base {receipts['knowledgeBase']['id']} (search tool {receipts['knowledgeBase']['toolId']})")
    for ref, identifier in receipts["tools"].items():
        print(f"  {ref} → {identifier}")
    for ref, identifier in receipts["assistants"].items():
        print(f"  {ref} → {identifier}")
    if receipts["squad"].get("id"):
        print(f"  squad → {receipts['squad']['id']}")
    print("Verified every resource by reading it back. Next: `test` runs the plan's scenarios through Vapi chat.")
    return 0


def cmd_merge(args) -> int:
    workspace = _workspace(args)
    report = ontology.merge_fragments(workspace)
    print(f"merge: {report['status']} from {len(report['fragments'])} fragments")
    for error in report.get("errors", []):
        print(f"  ERROR {error}")
    for key, value in report.get("counts", {}).items():
        if value:
            print(f"  {key}: {value}")
    for duplicate in report.get("possibleDuplicates", []):
        print(f"  same label, different ids → merge or distinguish: {duplicate}")
    for warning in report.get("warnings", []):
        print(f"  warn  {warning}")
    if report["status"] == "MERGED":
        print(f"Wrote {workspace.path('ontology', 'ontology.json')}. Next: review duplicates, then `check ontology`.")
    return 0 if report["status"] == "MERGED" else 1


def cmd_test(args) -> int:
    workspace = _workspace(args)
    report = vapi.run_tests(workspace, vapi.client_from_env())
    if not report["results"]:
        print("The plan declares no tests; nothing to run. Talk to the assistant in the Vapi dashboard instead.")
        return 0
    print(vapi.render_test_report(report))
    print(f"Saved {workspace.path('vapi', 'test-results.json')}. Judge each transcript against its expectations and report the verdicts.")
    return 0


def cmd_verify(args) -> int:
    workspace = _workspace(args)
    for line in vapi.verify(workspace, vapi.client_from_env()):
        print(f"  ok {line}")
    return 0


def cmd_status(args) -> int:
    workspace = _workspace(args)
    print(f"Project “{workspace.project['name']}” at {workspace.root}")
    for source in workspace.sources():
        fetched = source.get("fetched")
        print(f"  {source['id']}: {source['location']}" + (f" · fetched {fetched['itemCount']} items" if fetched else " · not fetched"))
    for stage in ("ontology", "plan"):
        check = workspace.path(stage, "check.json")
        approval = workspace.path(stage, "approval.json")
        state = "approved" if approval.exists() else read_json(check)["status"] if check.exists() else "not checked"
        print(f"  {stage}: {state}")
    build_path = workspace.path("vapi", "build.json")
    plan_approval = workspace.path("plan", "approval.json")
    if build_path.exists() and plan_approval.exists():
        stale = read_json(build_path).get("planDigest") != read_json(plan_approval).get("digest")
        print(f"  build: {'STALE (plan changed; run compile)' if stale else 'compiled'}")
    else:
        print(f"  build: {'compiled' if build_path.exists() else 'not compiled'}")
    receipts = vapi.load_receipts(workspace)
    if receipts:
        print(f"  applied: kb {receipts['knowledgeBase'].get('id')}, {len(receipts['tools'])} tools, {len(receipts['assistants'])} assistants, verified={receipts['verified']}")
    else:
        print("  applied: no")
    return 0


def cmd_teardown(args) -> int:
    workspace = _workspace(args)
    if not args.yes:
        print("teardown deletes every Vapi resource listed in vapi/receipts.json. Re-run with --yes after the user confirms.")
        return 1
    removed = vapi.teardown(workspace, vapi.client_from_env())
    for line in removed:
        print(f"  removed {line}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vapi-build", description="Evidence → ontology → plan → Vapi agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check local prerequisites").set_defaults(func=cmd_doctor)

    p = sub.add_parser("init", help="create a project workspace")
    p.add_argument("name")
    p.add_argument("--workspace", help=f"directory for this project (default {DEFAULT_ROOT}/<slug>)")
    p.add_argument("--aws-profile")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="register raw material: website | knowledge | transcripts | openapi")
    p.add_argument("workspace")
    p.add_argument("role", choices=sources.ROLES)
    p.add_argument("location", help="https URL, local file or directory, or s3://bucket/prefix")
    p.add_argument("--authority", choices=("AUTHORITATIVE", "SUPPORTING"), help="knowledge/website only")
    p.add_argument("--privacy", choices=sources.PRIVACY, help="transcripts only: synthetic, redacted, or raw (raw is never shown to the model)")
    p.add_argument("--max-pages", type=int, default=40, help="website crawl limit")
    p.add_argument("--allowed-host", action="append", help="extra hostnames the website crawl may follow")
    p.add_argument("--sample", type=int, default=40, help="transcripts: conversations to sample")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--scan-mb", type=int, default=64, help="transcripts: megabytes of a large CSV to scan before sampling")
    p.add_argument("--server-url", help="openapi: base URL the agent's tools will call")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("fetch", help="download or crawl every registered source")
    p.add_argument("workspace")
    p.add_argument("--only", help="a source id or role")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("extract", help="build the evidence ledger and reading packets")
    p.add_argument("workspace")
    p.add_argument("--batch-size", type=int, default=10, help="conversations per transcript segment")
    p.set_defaults(func=cmd_extract)

    for name, func in (("check", cmd_check), ("summarize", cmd_summarize), ("approve", cmd_approve)):
        p = sub.add_parser(name)
        p.add_argument("stage", choices=("ontology", "plan"))
        p.add_argument("workspace")
        if name == "check":
            p.add_argument("--strict", action="store_true", help="fail when segments are neither cited nor declared uncovered")
        if name == "approve":
            p.add_argument("--by", help="who approved (defaults to git user.email)")
        p.set_defaults(func=func)

    p = sub.add_parser("merge", help="combine ontology/fragments/*.json into ontology/ontology.json")
    p.add_argument("workspace")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("test", help="run the plan's test scenarios against the applied agent through Vapi chat")
    p.add_argument("workspace")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("compile", help="write Vapi payloads and knowledge files from the approved plan")
    p.add_argument("workspace")
    p.set_defaults(func=cmd_compile)

    for name, func, text in (("apply", cmd_apply, "create the Vapi resources"), ("teardown", cmd_teardown, "delete the Vapi resources this project created")):
        p = sub.add_parser(name, help=text)
        p.add_argument("workspace")
        p.add_argument("--yes", action="store_true")
        p.set_defaults(func=func)

    for name, func, text in (("verify", cmd_verify, "read back every applied resource"), ("status", cmd_status, "show where the project stands")):
        p = sub.add_parser(name, help=text)
        p.add_argument("workspace")
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
