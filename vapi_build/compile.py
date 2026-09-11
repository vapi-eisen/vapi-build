"""Compile the approved plan into exact Vapi payloads and knowledge-base files. No network."""
from __future__ import annotations

import re
import shutil
from collections import defaultdict
from typing import Any

from . import sources
from .extract import load_ledger, segment_text
from .ontology import approved_ontology
from .plan import approved_plan
from .workspace import BuildError, Workspace, slug, utc_now, write_json

TEXT_LIKE = {"markdown", "text", "yaml", "json", "csv", "pdf", "docx", "html"}


def _locators(ledger: dict[str, Any]) -> dict[str, str]:
    segment_locator = {s["id"]: s["locator"] for s in ledger["segments"]}
    return {e["id"]: segment_locator[e["segment"]] for e in ledger["evidence"]}


def render_domain_guide(ontology: dict[str, Any], ledger: dict[str, Any]) -> str:
    locator_of = _locators(ledger)
    by_id = {r["id"]: r for group in ("types", "entities", "claims", "rules", "procedures", "goals", "observations", "capabilities") for r in ontology.get(group, [])}
    label = lambda ref: by_id.get(ref, {}).get("label") or by_id.get(ref, {}).get("operationId") or ref  # noqa: E731

    def cite(record: dict[str, Any]) -> str:
        seen = []
        for ref in record.get("evidence", []):
            locator = locator_of.get(ref)
            if locator and locator not in seen:
                seen.append(locator)
        return f" (source: {'; '.join(seen[:2])})" if seen else ""

    lines = [f"# {ontology['domain']['name']}: domain guide", "", ontology["domain"]["summary"], "",
             "This guide was compiled from the organization's own material. Every statement carries its source.", ""]
    lines += ["## Glossary", ""]
    for record in ontology["types"]:
        lines.append(f"- **{record['label']}**: {record['definition']}")
    for record in ontology["entities"]:
        aliases = f" Also called: {', '.join(record['aliases'])}." if record.get("aliases") else ""
        lines.append(f"- **{record['label']}** ({', '.join(label(t) for t in record['types'])}): {record['definition']}{aliases}")
    if ontology["claims"]:
        lines += ["", "## Facts", ""]
        grouped = defaultdict(list)
        for claim in ontology["claims"]:
            grouped[claim["subject"]].append(claim)
        for subject, claims in grouped.items():
            lines.append(f"### {label(subject)}")
            for claim in claims:
                prefix = "Not the case: " if claim.get("polarity") == "NEGATIVE" else ""
                condition = f" Applies when: {claim['conditions']}." if claim.get("conditions") else ""
                status = " (inferred, not stated verbatim)" if claim.get("status") == "INFERRED" else " (hypothesis, unverified)" if claim.get("status") == "HYPOTHESIS" else ""
                lines.append(f"- {prefix}{claim['text']}{condition}{status}{cite(claim)}")
            lines.append("")
    if ontology["rules"]:
        lines += ["## Rules and policies", ""]
        for rule in ontology["rules"]:
            actors = f" For {', '.join(label(a) for a in rule['actors'])}:" if rule.get("actors") else ""
            applies = f" Applies: {rule['applies']}." if rule.get("applies") else ""
            exceptions = f" Exceptions: {'; '.join(rule['exceptions'])}." if rule.get("exceptions") else ""
            lines.append(f"- {rule['modality'].replace('_', ' ')}:{actors} {rule['text']}{applies}{exceptions}{cite(rule)}")
        lines.append("")
    if ontology["procedures"]:
        lines += ["## Procedures", ""]
        for procedure in ontology["procedures"]:
            goals = f" Serves: {', '.join(label(g) for g in procedure['goals'])}." if procedure.get("goals") else ""
            lines.append(f"### {procedure['label']}{goals}{cite(procedure)}")
            for index, step in enumerate(procedure["steps"], start=1):
                tool = f" [uses {label(step['capability'])}]" if step.get("capability") else ""
                lines.append(f"{index}. {step['instruction']}{tool}")
            lines.append("")
    lines += ["## What callers ask for", ""]
    for goal in ontology["goals"]:
        phrases = f" Callers say things like: “{'”, “'.join(goal['callerPhrases'][:5])}”." if goal.get("callerPhrases") else ""
        lines.append(f"- **{goal['label']}**: {goal['definition']}{phrases}")
    if ontology["observations"]:
        lines += ["", "## Observed in conversations (context, not policy)", ""]
        for observation in ontology["observations"]:
            count = f" Seen in {observation['count']} of {observation['sampleSize']} sampled conversations." if "count" in observation and "sampleSize" in observation else ""
            lines.append(f"- {observation['text']}{count}")
    return "\n".join(lines).rstrip() + "\n"


def _tool_payload(tool: dict[str, Any], server_url: str) -> dict[str, Any]:
    operation = tool["operation"]
    url = server_url.rstrip("/") + re.sub(r"\{([^}]+)\}", r"{{\1}}", operation["path"])
    if operation["queryParams"]:
        url += "?" + "&".join(f"{name}={{{{{name}}}}}" for name in operation["queryParams"])
    payload: dict[str, Any] = {"type": "apiRequest", "name": tool["name"], "description": tool["description"], "method": operation["method"], "url": url}
    if operation["toolSchema"].get("properties"):
        payload["body"] = operation["toolSchema"]
    if tool.get("timeoutSeconds"):
        payload["timeoutSeconds"] = tool["timeoutSeconds"]
    if tool.get("startMessage"):
        payload["messages"] = [{"type": "request-start", "content": tool["startMessage"]}]
    if tool.get("extract"):
        payload["variableExtractionPlan"] = {"aliases": [{"key": key, "value": value} for key, value in tool["extract"].items()]}
    if tool.get("staticParameters"):
        payload["parameters"] = [{"key": key, "value": value} for key, value in tool["staticParameters"].items()]
    if tool["auth"]["mode"] == "VAPI_CREDENTIAL":
        payload["credentialId"] = tool["auth"]["credentialId"]
    return payload


def _system_prompt(assistant: dict[str, Any], plan: dict[str, Any], tools: dict[str, dict[str, Any]]) -> str:
    parts = [assistant["systemPrompt"].strip()]
    if assistant.get("knowledge", True):
        parts.append("# Knowledge\nBefore answering a factual question, search the knowledge base and answer only from what it returns. "
                     "If it has nothing relevant, say so plainly and offer the next step; never invent facts, prices, policies, or eligibility.")
    used = [tools[name] for name in assistant.get("tools", []) if name in tools]
    if used:
        lines = ["# Tools"]
        for tool in used:
            lines.append(f"- {tool['name']}: {tool['description']}")
            if tool.get("confirmBeforeCall"):
                lines.append("  Before calling it, read back every value you will send and wait for an explicit yes. Call it once; do not retry on your own.")
        lines.append("Only pass values the caller gave you or that an earlier tool returned. Never guess identifiers, amounts, or account details.")
        parts.append("\n".join(lines))
    if assistant.get("handoffTo"):
        lines = ["# Handoffs"]
        for handoff in assistant["handoffTo"]:
            lines.append(f"- Hand off to {handoff['assistant']} when {handoff['when']}")
        parts.append("\n".join(lines))
    if plan.get("exclusions"):
        parts.append("# Out of scope\n" + "\n".join(f"- {item['what']}: {item['why']}" for item in plan["exclusions"]))
    return "\n\n".join(parts)


def compile_build(workspace: Workspace) -> dict[str, Any]:
    plan = approved_plan(workspace)
    ontology = approved_ontology(workspace)
    ledger = load_ledger(workspace)
    out = workspace.path("vapi")
    knowledge_dir = out / "knowledge"
    if knowledge_dir.exists():
        shutil.rmtree(knowledge_dir)
    knowledge_dir.mkdir(parents=True)
    project_slug = workspace.project["slug"]
    selection = plan["knowledge"]
    excluded = set(selection.get("excludeLocators", []))
    files: list[dict[str, Any]] = []
    taken_names: set[str] = set()

    def unique(name: str) -> str:
        base, ext = (name.rsplit(".", 1) + [""])[:2] if "." in name else (name, "")
        candidate, counter = name, 2
        while candidate in taken_names:
            candidate = f"{base}-{counter}" + (f".{ext}" if ext else "")
            counter += 1
        taken_names.add(candidate)
        return candidate

    if selection["includeDomainGuide"]:
        name = unique(f"{project_slug}-domain-guide.md")
        (knowledge_dir / name).write_text(render_domain_guide(ontology, ledger), encoding="utf-8")
        files.append({"path": f"knowledge/{name}", "name": name, "origin": "generated", "locator": "ontology"})
    if selection["includeWebsitePages"]:
        for segment in ledger["segments"]:
            if segment["role"] != "website" or segment["locator"] in excluded:
                continue
            name = unique(f"{segment['id'].split(':', 1)[1]}.md")
            body = f"# {segment['title']}\n\nSource: {segment['locator']}\n\n{segment_text(workspace, segment)}\n"
            (knowledge_dir / name).write_text(body, encoding="utf-8")
            files.append({"path": f"knowledge/{name}", "name": name, "origin": "website", "locator": segment["locator"]})
    if selection["includeSourceDocuments"]:
        for source in workspace.sources("knowledge"):
            inventory = sources.load_inventory(workspace, source)
            raw = sources.raw_dir(workspace, source)
            for item in inventory["items"]:
                if item["locator"] in excluded or item["kind"] not in TEXT_LIKE and item["kind"] != "other":
                    continue
                original = item["locator"].rstrip("/").rsplit("/", 1)[-1] or item["file"]
                if item["kind"] == "html":
                    segment = next((s for s in ledger["segments"] if s.get("item") == item["id"] and s["source"] == source["id"]), None)
                    if segment is None:
                        continue
                    name = unique(slug(original.rsplit(".", 1)[0], 50) + ".md")
                    (knowledge_dir / name).write_text(f"# {segment['title']}\n\nSource: {segment['locator']}\n\n{segment_text(workspace, segment)}\n", encoding="utf-8")
                else:
                    name = unique(re.sub(r"[^A-Za-z0-9._-]+", "-", original)[:80] or item["file"])
                    shutil.copyfile(raw / item["file"], knowledge_dir / name)
                files.append({"path": f"knowledge/{name}", "name": name, "origin": "source", "locator": item["locator"], "kind": item["kind"]})
    if not files:
        raise BuildError("The plan selects no knowledge files; enable at least the domain guide or source documents.")

    server_url = plan["runtime"].get("serverUrl") or ""
    tools_by_name = {tool["name"]: tool for tool in plan["resolvedTools"]}
    by_operation = {tool["operationId"]: tool for tool in plan["resolvedTools"]}
    credentials: dict[str, dict[str, Any]] = {}
    tool_records = []
    for tool in plan["resolvedTools"]:
        credential_ref = None
        if tool["auth"]["mode"] == "BEARER_ENV":
            credential_ref = f"credential:{tool['auth']['env']}"
            credentials[credential_ref] = {"ref": credential_ref, "env": tool["auth"]["env"], "name": f"{project_slug}-{tool['auth']['env'].lower()}"[:40],
                                           "headerName": tool["auth"].get("headerName", "Authorization")}
        tool_records.append({"ref": f"tool:{tool['name']}", "operationId": tool["operationId"], "payload": _tool_payload(tool, server_url), "credentialRef": credential_ref})

    assistant_records = []
    names = {assistant["id"]: assistant["name"] for assistant in plan["assistants"]}
    for assistant in plan["assistants"]:
        tool_names = [by_operation[op]["name"] for op in assistant.get("tools", []) if op in by_operation]
        prompt = _system_prompt({**assistant, "tools": tool_names, "handoffTo": [{**h, "assistant": names[h["assistant"]]} for h in assistant.get("handoffTo", [])]}, plan, tools_by_name)
        model = {**plan["runtime"]["model"], "messages": [{"role": "system", "content": prompt}]}
        if assistant.get("handoffTo"):
            model["tools"] = [{
                "type": "handoff",
                "destinations": [{"type": "assistant", "assistantName": names[h["assistant"]], "description": h["when"], "contextEngineeringPlan": {"type": "userAndAssistantMessages"}}
                                 for h in assistant["handoffTo"]],
            }]
        payload = {
            "name": assistant["name"],
            "firstMessageMode": "assistant-speaks-first" if assistant.get("firstMessage") else "assistant-speaks-first-with-model-generated-message",
            "model": model,
            "voice": plan["runtime"]["voice"],
            "transcriber": plan["runtime"]["transcriber"],
            "metadata": {"managedBy": "vapi-build", "project": project_slug, "planDigest": plan["digest"], "ontologyDigest": plan["ontologyDigest"]},
        }
        if assistant.get("firstMessage"):
            payload["firstMessage"] = assistant["firstMessage"]
        assistant_records.append({"ref": f"assistant:{assistant['id']}", "payload": payload, "toolRefs": [f"tool:{name}" for name in tool_names],
                                  "knowledge": assistant.get("knowledge", True)})
    squad = None
    if len(plan["assistants"]) > 1:
        entry = plan["squad"]["entry"]
        ordered = sorted(plan["assistants"], key=lambda a: a["id"] != entry)
        squad = {"ref": "squad:main", "payload": {"name": plan["squad"].get("name") or f"{plan['agent']['name']} squad"},
                 "members": [{"assistantRef": f"assistant:{a['id']}",
                              "assistantDestinations": [{"type": "assistant", "assistantName": names[h["assistant"]], "description": h["when"], "contextEngineeringPlan": {"type": "userAndAssistantMessages"}}
                                                        for h in a.get("handoffTo", [])]} for a in ordered]}
    build = {
        "compiledAt": utc_now(), "project": workspace.project["name"], "projectSlug": project_slug,
        "planDigest": plan["digest"], "ontologyDigest": plan["ontologyDigest"],
        "knowledgeBase": {"name": (selection.get("name") or f"{plan['agent']['name']} knowledge")[:80],
                          "description": f"Compiled by vapi-build from {len(files)} files; plan {plan['digest'][:23]}"[:1000], "files": files},
        "credentials": list(credentials.values()), "tools": tool_records, "assistants": assistant_records, "squad": squad,
        "tests": plan.get("tests", []),
    }
    write_json(out / "build.json", build)
    (out / "summary.md").write_text(render_build_summary(build), encoding="utf-8")
    return build


def render_build_summary(build: dict[str, Any]) -> str:
    lines = [f"# Vapi build for {build['project']}", "", f"Plan {build['planDigest'][:23]} · ontology {build['ontologyDigest'][:23]}", "",
             f"## Knowledge base “{build['knowledgeBase']['name']}” ({len(build['knowledgeBase']['files'])} files)"]
    for file in build["knowledgeBase"]["files"]:
        lines.append(f"- {file['name']} ({file['origin']}) ← {file['locator']}")
    lines += ["", f"## Tools ({len(build['tools'])})"]
    for tool in build["tools"]:
        payload = tool["payload"]
        auth = f" · credential from ${tool['credentialRef'].split(':', 1)[1]}" if tool["credentialRef"] else f" · credentialId {payload['credentialId']}" if payload.get("credentialId") else ""
        lines.append(f"- {payload['name']}: {payload['method']} {payload['url']}{auth}")
    lines += ["", f"## Assistants ({len(build['assistants'])})"]
    for assistant in build["assistants"]:
        lines.append(f"- {assistant['payload']['name']}: {len(assistant['toolRefs'])} API tools" + (" + knowledge base" if assistant["knowledge"] else ""))
    if build["squad"]:
        lines.append(f"- Squad “{build['squad']['payload']['name']}” starting with {build['squad']['members'][0]['assistantRef']}")
    if build["credentials"]:
        lines += ["", "## Credentials created at apply time (values read from your environment, never stored)"]
        for credential in build["credentials"]:
            lines.append(f"- ${credential['env']} → bearer credential “{credential['name']}”")
    if build["tests"]:
        lines += ["", f"## Test scenarios ({len(build['tests'])})"]
        for test in build["tests"]:
            lines.append(f"- {test['scenario']}: “{test['callerOpening']}” → {'; '.join(test['expect'])}")
    return "\n".join(lines) + "\n"
