"""Check and approve the agent plan against the approved ontology and the OpenAPI inventory."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from . import openapi
from .ontology import approved_ontology, approver, load_capability_inventory
from .vapi import KEY_FILE, KEY_VARIABLES
from .workspace import BuildError, Workspace, digest_json, read_json, utc_now, write_json

RESERVED_ENV = set(KEY_VARIABLES) | {"VAPI_BASE_URL"}

SCHEMA_PATH = Path(__file__).with_name("schemas") / "plan.schema.json"
DEFAULT_RUNTIME = {
    "model": {"provider": "openai", "model": "gpt-4.1", "temperature": 0.2},
    "voice": {"provider": "vapi", "voiceId": "Elliot"},
    "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"},
}
DEFAULT_KNOWLEDGE = {"includeSourceDocuments": True, "includeWebsitePages": True, "includeDomainGuide": True, "excludeLocators": []}


def check_plan(workspace: Workspace) -> dict[str, Any]:
    path = workspace.path("plan", "plan.json")
    if not path.exists():
        raise BuildError(f"Write the plan to {path} first (see the skill's plan guide).")
    plan = read_json(path)
    ontology = approved_ontology(workspace)
    inventory = load_capability_inventory(workspace) or {"operations": [], "serverUrl": None}
    validator = Draft202012Validator(read_json(SCHEMA_PATH))
    errors = [f"/{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in sorted(validator.iter_errors(plan), key=lambda e: list(map(str, e.absolute_path)))]
    warnings: list[str] = []
    if errors:
        return _finish(workspace, plan, None, errors, warnings)

    records = {r["id"]: r for group in ("types", "entities", "claims", "rules", "procedures", "goals", "observations", "capabilities") for r in ontology.get(group, [])}
    operations = {op["operationId"]: op for op in inventory["operations"]}
    for job in plan["jobs"]:
        for ref in job["goals"] + job.get("knowledge", []):
            if ref not in records:
                errors.append(f"{job['id']} references {ref}, which is not in the approved ontology.")
    job_ids = {job["id"] for job in plan["jobs"]}
    if len(job_ids) != len(plan["jobs"]):
        errors.append("Job IDs must be unique.")

    taken: set[str] = set()
    tools_by_operation: dict[str, dict[str, Any]] = {}
    for tool in plan["tools"]:
        operation = operations.get(tool["operationId"])
        if operation is None:
            errors.append(f"Tool {tool['operationId']} is not an operation in the OpenAPI source.")
            continue
        if tool["operationId"] in tools_by_operation:
            errors.append(f"Tool {tool['operationId']} is listed twice.")
        classification = operation["classification"]
        if classification["risk"] == "PRIVILEGED" and not plan["agent"].get("allowPrivileged"):
            errors.append(f"{tool['operationId']} looks administrative or internal ({operation['method']} {operation['path']}); set agent.allowPrivileged only if the user explicitly wants it exposed to callers.")
        if classification["confirmBeforeCall"] and not tool.get("confirmBeforeCall"):
            if tool.get("skipConfirmationReason"):
                warnings.append(f"{tool['operationId']} is a write that will run without a read-back: {tool['skipConfirmationReason']}")
            else:
                errors.append(f"{tool['operationId']} is a {operation['method']} (a write); set confirmBeforeCall: true, or give skipConfirmationReason for a login-style call with nothing to read back.")
        auth = tool.get("auth", {"mode": "NONE"})
        if auth["mode"] == "HEADER_ENV":
            if not auth.get("env"):
                errors.append(f"{tool['operationId']} auth HEADER_ENV needs `env`: the variable name the user saved in {KEY_FILE} holding the token.")
            elif auth["env"] in RESERVED_ENV or auth["env"].startswith(("AWS_", "ANTHROPIC_", "OPENAI_", "GITHUB_", "VAPI_")) or "SECRET" in auth["env"]:
                errors.append(f"{tool['operationId']} names {auth['env']}, which is a platform or provider secret; use a variable the user created for this API.")
        if auth["mode"] == "VAPI_CREDENTIAL" and not auth.get("credentialId"):
            errors.append(f"{tool['operationId']} auth VAPI_CREDENTIAL needs `credentialId` of an existing Vapi credential.")
        if classification["requiresAuth"] and auth["mode"] == "NONE" and not tool.get("headers"):
            warnings.append(f"{tool['operationId']} declares a security requirement but the tool has no auth; calls may fail with 401.")
        if any(name.casefold() == (auth.get("headerName") or "Authorization").casefold() for name in (tool.get("headers") or {})) and auth["mode"] == "HEADER_ENV":
            errors.append(f"{tool['operationId']} sets the same header through `headers` and `auth`; keep one.")
        name = tool.get("name") or openapi.tool_name(tool["operationId"], set(taken))
        if name in taken:
            errors.append(f"Tool name {name} is used twice.")
        taken.add(name)
        tools_by_operation[tool["operationId"]] = {**tool, "name": name, "auth": auth}

    for job in plan["jobs"]:
        for operation_id in job.get("tools", []):
            if operation_id not in tools_by_operation:
                errors.append(f"{job['id']} uses tool {operation_id}, which is not declared under tools.")
        if job["handling"] == "TOOL_ACTION" and not job.get("tools"):
            errors.append(f"{job['id']} is a TOOL_ACTION job but lists no tools.")

    assistant_ids = [assistant["id"] for assistant in plan["assistants"]]
    if len(set(assistant_ids)) != len(assistant_ids):
        errors.append("Assistant IDs must be unique.")
    names = [assistant["name"] for assistant in plan["assistants"]]
    if len(set(names)) != len(names):
        errors.append("Assistant names must be unique (handoffs address assistants by name).")
    covered_jobs: set[str] = set()
    for assistant in plan["assistants"]:
        if len(assistant["name"]) > 40:
            errors.append(f"Assistant name “{assistant['name']}” is longer than Vapi's 40-character limit.")
        for job_id in assistant["jobs"]:
            if job_id not in job_ids:
                errors.append(f"Assistant {assistant['id']} lists unknown {job_id}.")
            covered_jobs.add(job_id)
        for operation_id in assistant.get("tools", []):
            if operation_id not in tools_by_operation:
                errors.append(f"Assistant {assistant['id']} uses undeclared tool {operation_id}.")
        for handoff in assistant.get("handoffTo", []):
            if handoff["assistant"] not in assistant_ids:
                errors.append(f"Assistant {assistant['id']} hands off to unknown assistant {handoff['assistant']}.")
            if handoff["assistant"] == assistant["id"]:
                errors.append(f"Assistant {assistant['id']} cannot hand off to itself.")
    for job_id in job_ids - covered_jobs:
        warnings.append(f"{job_id} is not assigned to any assistant.")
    if len(plan["assistants"]) > 1:
        if "squad" not in plan:
            errors.append("More than one assistant requires a squad with an entry assistant.")
        elif plan["squad"]["entry"] not in assistant_ids:
            errors.append(f"squad.entry {plan['squad']['entry']} is not an assistant.")
    runtime = {**DEFAULT_RUNTIME, **plan.get("runtime", {})}
    server_url = runtime.get("serverUrl") or inventory.get("serverUrl")
    if tools_by_operation and not server_url:
        errors.append("No server URL for the tools: set runtime.serverUrl (for example https://standardcharter.co).")
    if server_url and not str(server_url).startswith("https://"):
        errors.append(f"The tools' server URL must use https, got {server_url}; set runtime.serverUrl.")
    runtime["serverUrl"] = server_url
    tool_operations = {operation_id: operations[operation_id] for operation_id in tools_by_operation}
    used_tools = {t for a in plan["assistants"] for t in a.get("tools", [])}
    for operation_id in tools_by_operation:
        if operation_id not in used_tools:
            warnings.append(f"Tool {operation_id} is declared but no assistant uses it.")
    if not plan.get("tests"):
        warnings.append("No tests declared; add a few caller scenarios so the build can be verified.")
    if errors:
        return _finish(workspace, plan, None, errors, warnings)
    candidate = {
        **plan,
        "runtime": runtime,
        "knowledge": {**DEFAULT_KNOWLEDGE, **plan.get("knowledge", {})},
        "resolvedTools": [{**tools_by_operation[op], "operation": {k: v for k, v in tool_operations[op].items() if k not in {"text"}}} for op in tools_by_operation],
        "ontologyDigest": ontology["digest"],
        "enabledOperations": sorted(f"{tool_operations[op]['method']} {tool_operations[op]['path']} ({op}) · risk {tool_operations[op]['classification']['risk']}"
                                    + (" · confirms first" if tools_by_operation[op].get("confirmBeforeCall") else " · NO read-back" if tool_operations[op]["classification"]["confirmBeforeCall"] else "")
                                    for op in tools_by_operation),
    }
    candidate["digest"] = plan_digest(candidate)
    candidate["checkedAt"] = utc_now()
    return _finish(workspace, plan, candidate, errors, warnings)


def plan_digest(candidate: dict[str, Any]) -> str:
    return digest_json({k: v for k, v in candidate.items() if k not in {"digest", "checkedAt"}})


def _finish(workspace: Workspace, plan: dict[str, Any], candidate: dict[str, Any] | None, errors: list[str], warnings: list[str]) -> dict[str, Any]:
    report = {"stage": "plan", "status": "REJECTED" if errors else "CANDIDATE", "checkedAt": utc_now(), "errors": errors, "warnings": warnings,
              "digest": candidate["digest"] if candidate else None,
              "enabledOperations": candidate["enabledOperations"] if candidate else [],
              "counts": {"jobs": len(plan.get("jobs", [])), "tools": len(plan.get("tools", [])), "assistants": len(plan.get("assistants", [])), "tests": len(plan.get("tests", []))}}
    write_json(workspace.path("plan", "check.json"), report)
    candidate_path = workspace.path("plan", "candidate.json")
    if candidate:
        write_json(candidate_path, candidate)
    elif candidate_path.exists():
        candidate_path.unlink()
    return report


def load_candidate(workspace: Workspace) -> dict[str, Any]:
    path = workspace.path("plan", "candidate.json")
    if not path.exists():
        raise BuildError("No checked plan candidate. Run `check plan` until it passes.")
    return read_json(path)


def summarize(candidate: dict[str, Any], ontology: dict[str, Any]) -> str:
    labels = {r["id"]: r.get("label") or r.get("text", "")[:80] for group in ("goals", "claims", "rules", "procedures", "entities", "types") for r in ontology.get(group, [])}
    lines = [f"# Plan summary: {candidate['agent']['name']}", "", candidate["agent"]["purpose"], ""]
    lines.append(f"**Runtime:** model {candidate['runtime']['model']['provider']}/{candidate['runtime']['model']['model']}, voice {candidate['runtime']['voice']['provider']}/{candidate['runtime']['voice']['voiceId']}, tools call `{candidate['runtime'].get('serverUrl') or 'n/a'}`")
    lines += ["", "## Jobs the agent handles"]
    for job in candidate["jobs"]:
        goals = ", ".join(labels.get(g, g) for g in job["goals"])
        lines.append(f"- **{job['label']}** ({job['handling']}) → {goals}")
        if job.get("tools"):
            lines.append(f"  - tools: {', '.join(job['tools'])}")
        if job.get("knowledge"):
            lines.append(f"  - knowledge: {len(job['knowledge'])} records")
        if job.get("safeguards"):
            lines.append(f"  - safeguards: {'; '.join(job['safeguards'])}")
    lines += ["", "## Tools (operations the agent may call)"]
    if not candidate["resolvedTools"]:
        lines.append("- none")
    for tool in candidate["resolvedTools"]:
        op = tool["operation"]
        confirm = " · confirms before calling" if tool.get("confirmBeforeCall") else ""
        lines.append(f"- `{tool['name']}` → {op['method']} {op['path']} · risk {op['classification']['risk']} · auth {tool['auth']['mode']}{confirm}")
    knowledge = candidate["knowledge"]
    lines += ["", "## Knowledge base", f"- source documents: {'yes' if knowledge['includeSourceDocuments'] else 'no'}; website pages: {'yes' if knowledge['includeWebsitePages'] else 'no'}; generated domain guide: {'yes' if knowledge['includeDomainGuide'] else 'no'}"]
    if knowledge.get("excludeLocators"):
        lines.append(f"- excluded: {', '.join(knowledge['excludeLocators'])}")
    lines += ["", "## Assistants"]
    for assistant in candidate["assistants"]:
        handoffs = f"; hands off to {', '.join(h['assistant'] for h in assistant.get('handoffTo', []))}" if assistant.get("handoffTo") else ""
        lines.append(f"- **{assistant['name']}** ({assistant['id']}): jobs {', '.join(assistant['jobs'])}; tools {', '.join(assistant.get('tools', [])) or 'none'}; knowledge {'on' if assistant.get('knowledge', True) else 'off'}{handoffs}")
    if candidate.get("squad"):
        lines.append(f"- squad entry: {candidate['squad']['entry']}")
    if candidate.get("tests"):
        lines += ["", f"## Tests ({len(candidate['tests'])})"]
        for test in candidate["tests"]:
            lines.append(f"- {test['scenario']}: caller says “{test['callerOpening']}” → expect {'; '.join(test['expect'])}")
    if candidate.get("exclusions"):
        lines += ["", "## Deliberately excluded"]
        for item in candidate["exclusions"]:
            lines.append(f"- {item['what']}: {item['why']}")
    return "\n".join(lines) + "\n"


def approve_plan(workspace: Workspace, *, by: str | None = None) -> dict[str, Any]:
    report_path = workspace.path("plan", "check.json")
    if not report_path.exists():
        raise BuildError("Run `check plan` before approving.")
    report = read_json(report_path)
    if report["status"] != "CANDIDATE":
        raise BuildError("The plan check did not pass; fix the errors first.")
    candidate = load_candidate(workspace)
    if candidate["digest"] != report["digest"]:
        raise BuildError("The plan changed after its last check. Run `check plan` again.")
    approved_ontology(workspace)  # still bound to the same approved ontology
    approval = {"stage": "plan", "digest": candidate["digest"], "ontologyDigest": candidate["ontologyDigest"], "enabledOperations": candidate["enabledOperations"],
                "by": approver(by), "at": utc_now()}
    write_json(workspace.path("plan", "approval.json"), approval)
    return approval


def approved_plan(workspace: Workspace) -> dict[str, Any]:
    approval_path = workspace.path("plan", "approval.json")
    if not approval_path.exists():
        raise BuildError("The plan has not been approved. Run `approve plan` after review.")
    approval = read_json(approval_path)
    candidate = load_candidate(workspace)
    if plan_digest(candidate) != approval["digest"] or candidate.get("digest") != approval["digest"]:
        raise BuildError("The plan candidate differs from what was approved. Run `check plan` and approve it again.")
    ontology = approved_ontology(workspace)
    if ontology["digest"] != approval["ontologyDigest"]:
        raise BuildError("The ontology changed after the plan was approved. Re-check and re-approve the plan.")
    return candidate
