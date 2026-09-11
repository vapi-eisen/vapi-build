"""Render the checked ontology, plan, and build as one interactive HTML page each.

The page has a graph of the records (d3 force layout), a browse view for everything that does not belong
on a graph (claims, observations, issues, tests), and a detail panel that opens on click and shows every
record's evidence as the quoted source text from the ledger. It is written for the user, who reads it
at each gate instead of a wall of Markdown. Data is embedded as JSON; the only external asset is d3 from
cdnjs, which the Artifact content-security policy admits.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .extract import load_ledger
from .workspace import BuildError, Workspace, read_json

D3_SRC = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"
QUOTE_LIMIT = 900

# --------------------------------------------------------------------------- evidence


def evidence_index(workspace: Workspace, ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every evidence ID with the quoted source text and where it came from."""
    segments = {s["id"]: s for s in ledger["segments"]}
    sources = {s["id"]: s for s in ledger["sources"]}
    texts: dict[str, str] = {}
    out: dict[str, dict[str, Any]] = {}
    for item in ledger["evidence"]:
        segment = segments.get(item["segment"])
        if not segment:
            continue
        if segment["id"] not in texts:
            path = workspace.path("evidence", *segment["file"].split("/"))
            texts[segment["id"]] = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        quote = texts[segment["id"]][item["start"]:item["end"]].strip()
        if len(quote) > QUOTE_LIMIT:
            quote = quote[:QUOTE_LIMIT].rstrip() + " …"
        source = sources.get(segment["source"], {})
        out[item["id"]] = {
            "id": item["id"], "segment": segment["id"], "title": segment.get("title") or segment["id"],
            "role": segment.get("role"), "authority": source.get("authority"), "locator": segment.get("locator"),
            "label": item.get("label"), "text": quote,
        }
    return out


# --------------------------------------------------------------------------- ontology model


def _ids(value: Any) -> list[str]:
    """Schema fields that may hold one ID or a list of IDs."""
    if not value:
        return []
    return [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)]


def _link(record_id: str, label: str | None = None) -> dict[str, str]:
    return {"id": record_id, "label": label or record_id}


def _rule_subjects(rule: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
    """Types and entities a rule is about: found by matching their labels and aliases in the rule text and scope."""
    haystack = f"{rule.get('text', '')} {rule.get('applies', '') or ''}".casefold()
    hits: list[tuple[int, str]] = []
    for rid, rec in by_id.items():
        if not (rid.startswith("type:") or rid.startswith("entity:")):
            continue
        names = [rec.get("label", "")] + list(rec.get("aliases", []))
        for n in names:
            n = n.casefold().strip()
            if len(n) >= 4 and n in haystack:
                hits.append((len(n), rid))
                break
    hits.sort(reverse=True)
    out: list[str] = []
    for _, rid in hits:
        if rid not in out:
            out.append(rid)
    return out[:4]


def ontology_model(candidate: dict[str, Any], evidence: dict[str, dict[str, Any]], ledger: dict[str, Any], check: dict[str, Any] | None) -> dict[str, Any]:
    groups = ("types", "entities", "properties", "relations", "claims", "rules", "procedures", "goals", "capabilities", "observations", "issues")
    by_id: dict[str, dict[str, Any]] = {r["id"]: r for g in groups for r in candidate.get(g, [])}
    kind_of: dict[str, str] = {r["id"]: g[:-1] if g != "properties" else "property" for g in groups for r in candidate.get(g, [])}
    for g in groups:
        for r in candidate.get(g, []):
            kind_of[r["id"]] = {"types": "type", "entities": "entity", "properties": "property", "relations": "relation", "claims": "claim", "rules": "rule",
                                "procedures": "procedure", "goals": "goal", "capabilities": "capability", "observations": "observation", "issues": "issue"}[g]

    def name(record_id: str) -> str:
        r = by_id.get(record_id)
        if not r:
            return record_id
        return r.get("label") or r.get("operationId") or r.get("text", "")[:60] or record_id

    def links(ids: list[str] | None) -> list[dict[str, str]]:
        return [_link(i, name(i)) for i in ids or [] if i in by_id]

    # reverse indexes
    children: dict[str, list[str]] = {}
    instances: dict[str, list[str]] = {}
    claims_about: dict[str, list[dict[str, Any]]] = {}
    rules_for: dict[str, list[str]] = {}
    procedures_for_goal: dict[str, list[str]] = {}
    procedures_using: dict[str, list[str]] = {}
    caps_for_goal: dict[str, list[str]] = {}
    observations_for: dict[str, list[dict[str, Any]]] = {}
    issues_for: dict[str, list[str]] = {}
    properties_of: dict[str, list[str]] = {}
    relations_of: dict[str, list[str]] = {}
    for t in candidate.get("types", []):
        for p in t.get("parents", []):
            children.setdefault(p, []).append(t["id"])
    for e in candidate.get("entities", []):
        for t in e.get("types", []):
            instances.setdefault(t, []).append(e["id"])
    for c in candidate.get("claims", []):
        claims_about.setdefault(c["subject"], []).append(c)
    for r in candidate.get("rules", []):
        for target in r.get("actors") or []:
            rules_for.setdefault(target, []).append(r["id"])
        for subject in _rule_subjects(r, by_id):
            rules_for.setdefault(subject, []).append(r["id"])
    for p in candidate.get("procedures", []):
        for g in p.get("goals", []):
            procedures_for_goal.setdefault(g, []).append(p["id"])
        for s in p.get("steps", []):
            if s.get("capability"):
                procedures_using.setdefault(s["capability"], []).append(p["id"])
    for c in candidate.get("capabilities", []):
        for g in c.get("alignedGoals", []):
            caps_for_goal.setdefault(g, []).append(c["id"])
    for o in candidate.get("observations", []):
        for g in o.get("goals", []):
            observations_for.setdefault(g, []).append(o)
    for i in candidate.get("issues", []):
        for rid in i.get("records", []):
            issues_for.setdefault(rid, []).append(i["id"])
    for p in candidate.get("properties", []):
        for d in p.get("domain", []):
            properties_of.setdefault(d, []).append(p["id"])
    for rel in candidate.get("relations", []):
        for end in _ids(rel.get("from")) + _ids(rel.get("to")):
            relations_of.setdefault(end, []).append(rel["id"])

    def claim_items(subject: str) -> list[dict[str, Any]]:
        return [{"id": c["id"], "text": c["text"], "polarity": c.get("polarity"), "conditions": c.get("conditions"), "status": c.get("status"), "evidence": c.get("evidence", [])}
                for c in claims_about.get(subject, [])]

    def observation_items(goal: str) -> list[dict[str, Any]]:
        return [{"id": o["id"], "text": o["text"], "count": o.get("count"), "sampleSize": o.get("sampleSize"), "evidence": o.get("evidence", [])}
                for o in observations_for.get(goal, [])]

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    records: dict[str, dict[str, Any]] = {}

    def add(record: dict[str, Any], kind: str, label: str, details: list[dict[str, Any]], *, graph: bool, summary: str = "", meta: dict[str, Any] | None = None) -> None:
        rec = {"id": record["id"], "kind": kind, "label": label, "summary": summary, "details": details, "evidence": record.get("evidence", []), "meta": meta or {}}
        records[record["id"]] = rec
        if graph:
            nodes.append({"id": record["id"], "kind": kind, "label": label, "weight": 1 + len(claims_about.get(record["id"], []))})

    def edge(a: str, b: str, kind: str) -> None:
        if a in by_id and b in by_id:
            edges.append({"source": a, "target": b, "kind": kind})

    def common(record: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        if record.get("status"):
            out.append({"label": "Status", "kind": "pills", "value": [record["status"]]})
        if issues_for.get(record["id"]):
            out.append({"label": "Open issues", "kind": "links", "value": links(issues_for[record["id"]])})
        return out

    for t in candidate.get("types", []):
        details = [{"label": "Definition", "kind": "text", "value": t["definition"]}]
        if t.get("parents"):
            details.append({"label": "Is a kind of", "kind": "links", "value": links(t["parents"])})
        if children.get(t["id"]):
            details.append({"label": "Kinds", "kind": "links", "value": links(children[t["id"]])})
        if instances.get(t["id"]):
            details.append({"label": "Named products and things", "kind": "links", "value": links(instances[t["id"]])})
        if properties_of.get(t["id"]):
            details.append({"label": "Attributes", "kind": "links", "value": links(properties_of[t["id"]])})
        if relations_of.get(t["id"]):
            details.append({"label": "Relations", "kind": "links", "value": links(relations_of[t["id"]])})
        if rules_for.get(t["id"]):
            details.append({"label": "Rules that apply", "kind": "links", "value": links(rules_for[t["id"]])})
        if claims_about.get(t["id"]):
            details.append({"label": f"Facts ({len(claims_about[t['id']])})", "kind": "claims", "value": claim_items(t["id"])})
        details += common(t)
        add(t, "type", t["label"], details, graph=True, summary=t["definition"])
        for p in t.get("parents", []):
            edge(t["id"], p, "is-a")

    for e in candidate.get("entities", []):
        details = [{"label": "Definition", "kind": "text", "value": e["definition"]}]
        if e.get("aliases"):
            details.append({"label": "Also called", "kind": "pills", "value": e["aliases"]})
        details.append({"label": "Kind", "kind": "links", "value": links(e["types"])})
        if rules_for.get(e["id"]):
            details.append({"label": "Rules that apply", "kind": "links", "value": links(rules_for[e["id"]])})
        if claims_about.get(e["id"]):
            details.append({"label": f"Facts ({len(claims_about[e['id']])})", "kind": "claims", "value": claim_items(e["id"])})
        if relations_of.get(e["id"]):
            details.append({"label": "Relations", "kind": "links", "value": links(relations_of[e["id"]])})
        details += common(e)
        add(e, "entity", e["label"], details, graph=True, summary=e["definition"])
        for t in e["types"]:
            edge(e["id"], t, "instance-of")

    for p in candidate.get("properties", []):
        details = [{"label": "Definition", "kind": "text", "value": p["definition"]},
                   {"label": "Value", "kind": "pills", "value": [p["valueKind"]]},
                   {"label": "Attribute of", "kind": "links", "value": links(p["domain"])}] + common(p)
        add(p, "property", p["label"], details, graph=False, summary=p["definition"])

    for rel in candidate.get("relations", []):
        details = [{"label": "Definition", "kind": "text", "value": rel["definition"]},
                   {"label": "From", "kind": "links", "value": links(_ids(rel.get("from")))},
                   {"label": "To", "kind": "links", "value": links(_ids(rel.get("to")))}] + common(rel)
        add(rel, "relation", rel["label"], details, graph=False, summary=rel["definition"])
        for a in _ids(rel.get("from")):
            for b in _ids(rel.get("to")):
                edge(a, b, "relation")

    for g in candidate.get("goals", []):
        details = [{"label": "What the caller wants", "kind": "text", "value": g["definition"]}]
        if g.get("callerPhrases"):
            details.append({"label": "How callers say it", "kind": "quotes", "value": g["callerPhrases"]})
        if procedures_for_goal.get(g["id"]):
            details.append({"label": "Procedures", "kind": "links", "value": links(procedures_for_goal[g["id"]])})
        if caps_for_goal.get(g["id"]):
            details.append({"label": "API capabilities", "kind": "links", "value": links(caps_for_goal[g["id"]])})
        if observations_for.get(g["id"]):
            details.append({"label": f"Seen in the calls ({len(observations_for[g['id']])})", "kind": "observations", "value": observation_items(g["id"])})
        details += common(g)
        add(g, "goal", g["label"], details, graph=True, summary=g["definition"], meta={"phrases": len(g.get("callerPhrases", []))})

    for p in candidate.get("procedures", []):
        steps = [{"id": s["id"], "instruction": s["instruction"], "capability": _link(s["capability"], name(s["capability"])) if s.get("capability") in by_id else None,
                  "next": [n for n in s.get("next", [])]} for s in p["steps"]]
        details = []
        if p.get("goals"):
            details.append({"label": "Serves", "kind": "links", "value": links(p["goals"])})
        details.append({"label": f"Steps ({len(steps)})", "kind": "steps", "value": steps})
        details += common(p)
        add(p, "procedure", p["label"], details, graph=True, summary=f"{len(steps)} steps")
        for g in p.get("goals", []):
            edge(p["id"], g, "serves")
        for s in p["steps"]:
            if s.get("capability"):
                edge(p["id"], s["capability"], "uses")

    for c in candidate.get("capabilities", []):
        cls = c.get("classification", {})
        label = c.get("operationId") or c["id"]
        details = [{"label": "Operation", "kind": "mono", "value": f"{c.get('method', '')} {c.get('path', '')}".strip()}]
        pills = [f"risk {cls.get('risk', '?')}"]
        if cls.get("requiresAuth"):
            pills.append("needs a session")
        if cls.get("write"):
            pills.append("writes")
        if cls.get("confirmBeforeCall"):
            pills.append("confirm before calling")
        if cls.get("adminOrInternal"):
            pills.append("admin only")
        details.append({"label": "Classification", "kind": "pills", "value": pills})
        if c.get("description") or c.get("summary"):
            details.append({"label": "Described as", "kind": "text", "value": c.get("description") or c.get("summary")})
        if c.get("alignedGoals"):
            details.append({"label": "Serves goals", "kind": "links", "value": links(c["alignedGoals"])})
        if c.get("preconditions"):
            details.append({"label": "Before calling", "kind": "text", "value": c["preconditions"]})
        if c.get("notes"):
            details.append({"label": "Notes", "kind": "text", "value": c["notes"]})
        if procedures_using.get(c["id"]):
            details.append({"label": "Used by procedures", "kind": "links", "value": links(procedures_using[c["id"]])})
        details.append({"label": "Enabled", "kind": "pills", "value": ["disabled until the plan enables it" if not c.get("enabled") else "enabled"]})
        details += common(c)
        add(c, "capability", label, details, graph=True, summary=f"{c.get('method', '')} {c.get('path', '')}", meta={"risk": cls.get("risk")})
        for g in c.get("alignedGoals", []):
            edge(c["id"], g, "aligned")

    for r in candidate.get("rules", []):
        details = [{"label": "Rule", "kind": "text", "value": r["text"]},
                   {"label": "Modality", "kind": "pills", "value": [r["modality"]]}]
        if r.get("actors"):
            details.append({"label": "Who", "kind": "links", "value": links(r["actors"])})
        if r.get("applies"):
            details.append({"label": "Applies to", "kind": "text", "value": r["applies"]})
        subjects = _rule_subjects(r, by_id)
        if subjects:
            details.append({"label": "About", "kind": "links", "value": links(subjects)})
        if r.get("exceptions"):
            details.append({"label": "Exceptions", "kind": "bullets", "value": r["exceptions"]})
        details += common(r)
        add(r, "rule", r["text"][:90] + ("…" if len(r["text"]) > 90 else ""), details, graph=True, summary=r["text"], meta={"modality": r["modality"]})
        for target in list(dict.fromkeys((r.get("actors") or []) + subjects)):
            edge(r["id"], target, "applies")

    for c in candidate.get("claims", []):
        details = [{"label": "Fact", "kind": "text", "value": c["text"]}, {"label": "About", "kind": "links", "value": links([c["subject"]])}]
        pills = [x for x in (c.get("polarity"), c.get("status")) if x]
        if pills:
            details.append({"label": "Flags", "kind": "pills", "value": pills})
        if c.get("conditions"):
            details.append({"label": "When", "kind": "text", "value": c["conditions"]})
        details += common(c)
        add(c, "claim", c["text"], details, graph=False, summary=name(c["subject"]), meta={"subject": c["subject"], "subjectLabel": name(c["subject"])})

    for o in candidate.get("observations", []):
        details = [{"label": "Observation", "kind": "text", "value": o["text"]}]
        if o.get("count") is not None and o.get("sampleSize"):
            details.append({"label": "Frequency", "kind": "text", "value": f"{o['count']} of {o['sampleSize']} sampled conversations"})
        if o.get("goals"):
            details.append({"label": "Goals", "kind": "links", "value": links(o["goals"])})
        details += common(o)
        add(o, "observation", o["text"][:100] + ("…" if len(o["text"]) > 100 else ""), details, graph=False, summary=o["text"],
            meta={"count": o.get("count"), "sampleSize": o.get("sampleSize")})

    for i in candidate.get("issues", []):
        details = [{"label": "Issue", "kind": "text", "value": i["description"]},
                   {"label": "Kind", "kind": "pills", "value": [i["kind"].replace("_", " ").lower(), i["severity"]]}]
        if i.get("records"):
            details.append({"label": "Records involved", "kind": "links", "value": links(i["records"])})
        add(i, "issue", i["description"][:110] + ("…" if len(i["description"]) > 110 else ""), details, graph=False, summary=i["description"],
            meta={"severity": i["severity"], "issueKind": i["kind"]})

    uncovered = [{"segment": u["segment"], "reason": u["reason"], "title": next((s.get("title") for s in ledger["segments"] if s["id"] == u["segment"]), u["segment"])}
                 for u in candidate.get("uncovered", [])]
    coverage = candidate.get("coverage", {})
    domain = candidate.get("domain", {})
    sources = [{"id": s["id"], "role": s["role"], "authority": s.get("authority"), "location": s.get("location"), "items": s.get("itemCount"), "segments": s.get("segmentCount")}
               for s in ledger["sources"]]
    counts = {g: len(candidate.get(g, [])) for g in groups}
    status = (check or {}).get("status")
    return {
        "page": "ontology",
        "title": domain.get("name") or "Ontology",
        "subtitle": "Evidence-linked ontology for review",
        "summary": domain.get("summary", ""),
        "callerRoles": domain.get("callerRoles", []),
        "status": status, "digest": candidate.get("digest"), "checkedAt": candidate.get("checkedAt"),
        "counts": counts, "coverage": coverage, "uncovered": uncovered, "sources": sources,
        "nodes": nodes, "edges": edges, "records": records, "evidence": evidence,
        "kinds": [
            {"kind": "goal", "label": "Caller goals", "graph": True}, {"kind": "entity", "label": "Products and things", "graph": True},
            {"kind": "type", "label": "Types", "graph": True}, {"kind": "procedure", "label": "Procedures", "graph": True},
            {"kind": "capability", "label": "API capabilities", "graph": True}, {"kind": "rule", "label": "Rules", "graph": True},
            {"kind": "claim", "label": "Facts", "graph": False}, {"kind": "observation", "label": "Call observations", "graph": False},
            {"kind": "issue", "label": "Open issues", "graph": False}, {"kind": "property", "label": "Attributes", "graph": False},
            {"kind": "relation", "label": "Relations", "graph": False},
        ],
    }


# --------------------------------------------------------------------------- plan model


def plan_model(candidate: dict[str, Any], ontology: dict[str, Any], evidence: dict[str, dict[str, Any]], build: dict[str, Any] | None, check: dict[str, Any] | None,
               receipts: dict[str, Any] | None) -> dict[str, Any]:
    o_by_id: dict[str, dict[str, Any]] = {r["id"]: r for g in ("types", "entities", "claims", "rules", "procedures", "goals", "capabilities") for r in ontology.get(g, [])}

    def oname(record_id: str) -> str:
        r = o_by_id.get(record_id)
        return (r.get("label") or r.get("text", "")[:80] or record_id) if r else record_id

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    records: dict[str, dict[str, Any]] = {}
    ids: set[str] = set()

    def add(record_id: str, kind: str, label: str, details: list[dict[str, Any]], *, graph: bool, summary: str = "", evidence_ids: list[str] | None = None, meta: dict[str, Any] | None = None) -> None:
        records[record_id] = {"id": record_id, "kind": kind, "label": label, "summary": summary, "details": details, "evidence": evidence_ids or [], "meta": meta or {}}
        ids.add(record_id)
        if graph:
            nodes.append({"id": record_id, "kind": kind, "label": label, "weight": 1})

    def edge(a: str, b: str, kind: str) -> None:
        edges.append({"source": a, "target": b, "kind": kind})

    def links(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
        return [_link(i, lbl) for i, lbl in pairs]

    tools = {t["operationId"]: t for t in candidate.get("tools", [])}
    resolved = {t["operationId"]: t for t in candidate.get("resolvedTools", [])}
    jobs = {j["id"]: j for j in candidate.get("jobs", [])}
    assistants = {a["id"]: a for a in candidate.get("assistants", [])}
    goal_ids = {g for j in jobs.values() for g in j.get("goals", [])}
    knowledge_ids = {k for j in jobs.values() for k in j.get("knowledge", [])}

    # goals from the ontology that the plan touches
    for gid in sorted(goal_ids):
        g = o_by_id.get(gid, {"id": gid, "label": gid, "definition": ""})
        details = [{"label": "What the caller wants", "kind": "text", "value": g.get("definition", "")}]
        if g.get("callerPhrases"):
            details.append({"label": "How callers say it", "kind": "quotes", "value": g["callerPhrases"]})
        owners = [(j["id"], j["label"]) for j in jobs.values() if gid in j.get("goals", [])]
        details.append({"label": "Handled by", "kind": "links", "value": links(owners)})
        add(gid, "goal", g.get("label", gid), details, graph=True, summary=g.get("definition", ""), evidence_ids=g.get("evidence", []))

    for kid in sorted(knowledge_ids):
        k = o_by_id.get(kid)
        if not k:
            continue
        kind = "rule" if kid.startswith("rule:") else "claim" if kid.startswith("claim:") else "procedure"
        text = k.get("text") or k.get("label") or kid
        details = [{"label": "Text", "kind": "text", "value": text}]
        if k.get("modality"):
            details.append({"label": "Modality", "kind": "pills", "value": [k["modality"]]})
        if k.get("subject"):
            details.append({"label": "About", "kind": "text", "value": oname(k["subject"])})
        if k.get("steps"):
            details.append({"label": "Steps", "kind": "steps", "value": [{"id": s["id"], "instruction": s["instruction"], "capability": None, "next": s.get("next", [])} for s in k["steps"]]})
        details.append({"label": "Compiled into", "kind": "links", "value": links([(j["id"], j["label"]) for j in jobs.values() if kid in j.get("knowledge", [])])})
        add(kid, kind, text[:90] + ("…" if len(text) > 90 else ""), details, graph=False, summary=text, evidence_ids=k.get("evidence", []))

    for op, t in tools.items():
        r = resolved.get(op, {})
        operation = r.get("operation", {})
        cls = operation.get("classification", {})
        auth = t.get("auth", {"mode": "NONE"})
        auth_text = {"NONE": "no authentication", "HEADER_ENV": f"fixed header from key-file variable {auth.get('env')}", "VAPI_CREDENTIAL": f"Vapi credential {auth.get('credentialId')}"}.get(auth.get("mode"), auth.get("mode"))
        details = [{"label": "Operation", "kind": "mono", "value": f"{operation.get('method', '')} {candidate.get('runtime', {}).get('serverUrl', '')}{operation.get('path', '')}".strip()},
                   {"label": "What it does", "kind": "text", "value": t.get("description", "")},
                   {"label": "Authentication", "kind": "text", "value": auth_text}]
        pills = [f"risk {cls.get('risk', '?')}"]
        if t.get("confirmBeforeCall"):
            pills.append("reads back and confirms first")
        elif t.get("skipConfirmationReason"):
            pills.append("NO read-back")
        details.append({"label": "Safety", "kind": "pills", "value": pills})
        if t.get("skipConfirmationReason"):
            details.append({"label": "Why no read-back", "kind": "text", "value": t["skipConfirmationReason"]})
        if t.get("headers"):
            details.append({"label": "Headers", "kind": "mono", "value": "\n".join(f"{k}: {v}" for k, v in t["headers"].items())})
        if t.get("extract"):
            details.append({"label": "Remembers from the response", "kind": "mono", "value": "\n".join(f"{k} ← {v}" for k, v in t["extract"].items())})
        if t.get("startMessage"):
            details.append({"label": "Says while calling", "kind": "quotes", "value": [t["startMessage"]]})
        users = [(a["id"], a["name"]) for a in assistants.values() if op in a.get("tools", [])]
        details.append({"label": "Used by", "kind": "links", "value": links(users)})
        details.append({"label": "Needed for", "kind": "links", "value": links([(j["id"], j["label"]) for j in jobs.values() if op in j.get("tools", [])])})
        add(f"tool:{op}", "tool", op, details, graph=True, summary=f"{operation.get('method', '')} {operation.get('path', '')}", evidence_ids=operation.get("evidence", []),
            meta={"risk": cls.get("risk"), "confirm": bool(t.get("confirmBeforeCall"))})

    for j in jobs.values():
        details = [{"label": "Handling", "kind": "pills", "value": [j["handling"].replace("_", " ").lower()]}]
        details.append({"label": "Goals", "kind": "links", "value": links([(g, oname(g)) for g in j.get("goals", [])])})
        if j.get("steps"):
            details.append({"label": "Steps", "kind": "steps", "value": [{"id": f"{j['id']}-step-{n}", "instruction": s, "capability": None, "next": []} for n, s in enumerate(j["steps"], 1)]})
        if j.get("slots"):
            details.append({"label": "Asks for", "kind": "pills", "value": [f"{s['name']}{' (required)' if s.get('required') else ''}" for s in j["slots"]]})
        if j.get("tools"):
            details.append({"label": "Tools", "kind": "links", "value": links([(f"tool:{t}", t) for t in j["tools"]])})
        if j.get("knowledge"):
            details.append({"label": "Knowledge in the prompt", "kind": "links", "value": links([(k, oname(k)) for k in j["knowledge"] if k in o_by_id])})
        if j.get("safeguards"):
            details.append({"label": "Safeguards", "kind": "bullets", "value": j["safeguards"]})
        if j.get("escalation"):
            details.append({"label": "Escalation", "kind": "text", "value": j["escalation"]})
        if j.get("examples"):
            details.append({"label": "Examples", "kind": "dialog", "value": j["examples"]})
        details.append({"label": "Owned by", "kind": "links", "value": links([(a["id"], a["name"]) for a in assistants.values() if j["id"] in a.get("jobs", [])])})
        add(j["id"], "job", j["label"], details, graph=True, summary=j["handling"], meta={"handling": j["handling"]})
        for g in j.get("goals", []):
            if g in ids:
                edge(j["id"], g, "serves")
        for t in j.get("tools", []):
            if f"tool:{t}" in ids:
                edge(j["id"], f"tool:{t}", "uses")

    for a in assistants.values():
        details = [{"label": "First message", "kind": "quotes", "value": [a["firstMessage"]]} if a.get("firstMessage") else {"label": "First message", "kind": "text", "value": "Generated by the model"},
                   {"label": "System prompt", "kind": "prompt", "value": a["systemPrompt"]},
                   {"label": "Jobs", "kind": "links", "value": links([(jid, jobs[jid]["label"]) for jid in a.get("jobs", []) if jid in jobs])},
                   {"label": "Tools", "kind": "links", "value": links([(f"tool:{t}", t) for t in a.get("tools", [])])},
                   {"label": "Knowledge base", "kind": "text", "value": "Searches the knowledge base" if a.get("knowledge", True) else "No knowledge base"}]
        if a.get("handoffTo"):
            details.append({"label": "Hands off to", "kind": "links", "value": links([(h["assistant"], f"{assistants.get(h['assistant'], {}).get('name', h['assistant'])} — {h['when']}") for h in a["handoffTo"]])})
        add(a["id"], "assistant", a["name"], details, graph=True, summary=a["systemPrompt"][:160])
        for jid in a.get("jobs", []):
            if jid in ids:
                edge(a["id"], jid, "owns")
        for t in a.get("tools", []):
            if f"tool:{t}" in ids:
                edge(a["id"], f"tool:{t}", "uses")
        for h in a.get("handoffTo", []):
            edge(a["id"], h["assistant"], "handoff")

    for t in candidate.get("tests", []):
        details = [{"label": "Caller opens with", "kind": "quotes", "value": [t["callerOpening"]] + list(t.get("followUps", []))},
                   {"label": "Expect", "kind": "bullets", "value": t.get("expect", [])}]
        if t.get("mustNot"):
            details.append({"label": "Must not", "kind": "bullets", "value": t["mustNot"]})
        add(t["id"], "test", t["scenario"], details, graph=False, summary=t["callerOpening"])

    for n, x in enumerate(candidate.get("exclusions", []), 1):
        add(f"exclusion:{n}", "exclusion", x["what"], [{"label": "Why", "kind": "text", "value": x["why"]}], graph=False, summary=x["why"])

    runtime = candidate.get("runtime", {})
    agent = candidate.get("agent", {})
    build_view = None
    if build:
        receipt_ids: dict[str, Any] = {}
        if receipts:
            receipt_ids = {k: v for k, v in receipts.items() if k not in {"files", "updatedAt", "planDigest", "createdAt"} and not isinstance(v, dict)}
            for k, v in receipts.items():
                if isinstance(v, dict) and k != "files":
                    receipt_ids.update({f"{k}.{kk}": (vv.get("id") if isinstance(vv, dict) else vv) for kk, vv in v.items()})
            if receipts.get("files"):
                receipt_ids["files"] = f"{len(receipts['files'])} uploaded"
        build_view = {
            "compiledAt": build.get("compiledAt"),
            "knowledgeBase": {"name": build["knowledgeBase"]["name"], "files": [{"name": f["name"], "origin": f["origin"], "locator": f["locator"], "bytes": f["bytes"]} for f in build["knowledgeBase"]["files"]]},
            "tools": [{"name": t["payload"].get("name") or t["ref"], "method": t["payload"].get("method"), "url": t["payload"].get("url"),
                       "secretHeaders": [h["name"] for h in t.get("secretHeaders", [])]} for t in build.get("tools", [])],
            "assistants": [{"name": a["payload"]["name"], "tools": a.get("toolRefs", []), "knowledge": a.get("knowledge", True), "firstMessage": a["payload"].get("firstMessage")} for a in build.get("assistants", [])],
            "squad": build.get("squad", {}).get("payload", {}).get("name") if build.get("squad") else None,
            "receipts": receipt_ids,
        }
    return {
        "page": "plan",
        "title": agent.get("name") or "Agent plan",
        "subtitle": "Agent plan for review",
        "summary": agent.get("purpose", ""),
        "callerRoles": [agent.get("audience", "customers")],
        "status": (check or {}).get("status"), "digest": candidate.get("digest"), "checkedAt": candidate.get("checkedAt"),
        "counts": {"assistants": len(assistants), "jobs": len(jobs), "tools": len(tools), "goals": len(goal_ids), "tests": len(candidate.get("tests", [])), "exclusions": len(candidate.get("exclusions", []))},
        "runtime": {"serverUrl": runtime.get("serverUrl"), "model": runtime.get("model"), "voice": runtime.get("voice"), "transcriber": runtime.get("transcriber"), "language": agent.get("language", "en")},
        "enabledOperations": candidate.get("enabledOperations", []),
        "build": build_view,
        "nodes": nodes, "edges": edges, "records": records, "evidence": evidence,
        "kinds": [
            {"kind": "assistant", "label": "Assistants", "graph": True}, {"kind": "job", "label": "Jobs", "graph": True},
            {"kind": "goal", "label": "Caller goals", "graph": True}, {"kind": "tool", "label": "Tools", "graph": True},
            {"kind": "test", "label": "Tests", "graph": False}, {"kind": "exclusion", "label": "Out of scope", "graph": False},
            {"kind": "rule", "label": "Rules in prompts", "graph": False}, {"kind": "claim", "label": "Facts in prompts", "graph": False},
            {"kind": "procedure", "label": "Procedures in prompts", "graph": False},
        ],
    }


# --------------------------------------------------------------------------- html


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_page(model: dict[str, Any]) -> str:
    title = html.escape(model["title"])
    data = _json_for_script(model)
    return f"""<meta charset="utf-8">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>
<div id="app" class="app">
  <header class="top">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true"></span>
      <div><h1 id="title">{title}</h1><div class="sub" id="subtitle"></div></div>
    </div>
    <nav class="views" role="tablist">
      <button class="view-btn is-on" data-view="graph" role="tab">Graph</button>
      <button class="view-btn" data-view="browse" role="tab">Browse</button>
      <button class="view-btn" data-view="overview" role="tab">Overview</button>
    </nav>
    <label class="search"><span class="sr">Search</span><input id="search" type="search" placeholder="Search records, facts, phrases…" autocomplete="off"></label>
  </header>
  <div class="body">
    <main class="stage">
      <section id="view-graph" class="view is-on">
        <div class="graph-bar"><div id="legend" class="legend"></div><div class="graph-hint">Drag to move · scroll to zoom · click a node for details and evidence</div></div>
        <svg id="graph" role="img" aria-label="Record graph"></svg>
      </section>
      <section id="view-browse" class="view"><div id="browse"></div></section>
      <section id="view-overview" class="view"><div id="overview"></div></section>
    </main>
    <aside id="detail" class="detail" aria-live="polite">
      <div class="detail-empty"><p>Select a node or a row to see its definition, related records, and the evidence behind it.</p></div>
    </aside>
  </div>
</div>
<script id="data" type="application/json">{data}</script>
<script src="{D3_SRC}"></script>
<script>{JS}</script>
"""


CSS = r"""
:root {
  --bg:#F5F7F6; --surface:#FFFFFF; --surface-2:#EDF1EF; --ink:#16201E; --muted:#5F6C69; --line:#D8DFDC; --line-strong:#B8C3BF;
  --accent:#0E6F66; --accent-ink:#0A5750; --accent-soft:#DDEFEC; --focus:#0E6F66;
  --k-goal:#C2622B; --k-entity:#0E9E8A; --k-type:#3B6FB6; --k-procedure:#7A4FB5; --k-capability:#A8780A; --k-rule:#B03A48;
  --k-claim:#5F6C69; --k-observation:#8A6D3B; --k-issue:#B03A48; --k-property:#3B6FB6; --k-relation:#3B6FB6;
  --k-assistant:#0E6F66; --k-job:#C2622B; --k-tool:#A8780A; --k-test:#7A4FB5; --k-exclusion:#5F6C69;
  --sev-critical:#B3372F; --sev-warning:#B7791F; --sev-info:#4A6FA5;
  --quote-bg:#F7F4EC; --quote-line:#E2D9C2;
  --sans:"IBM Plex Sans", "Helvetica Neue", Arial, sans-serif; --mono:"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#0F1514; --surface:#171F1E; --surface-2:#1F2927; --ink:#E6ECEA; --muted:#93A29D; --line:#2A3634; --line-strong:#3C4B48;
  --accent:#52BBAF; --accent-ink:#8FD8CF; --accent-soft:#163430; --focus:#52BBAF;
  --k-goal:#E48B55; --k-entity:#3FC4B0; --k-type:#6D9BE0; --k-procedure:#A98AE0; --k-capability:#E0B23A; --k-rule:#E0707D;
  --k-claim:#93A29D; --k-observation:#C9A26A; --k-issue:#E0707D; --k-property:#6D9BE0; --k-relation:#6D9BE0;
  --k-assistant:#52BBAF; --k-job:#E48B55; --k-tool:#E0B23A; --k-test:#A98AE0; --k-exclusion:#93A29D;
  --sev-critical:#E06A62; --sev-warning:#E0A53F; --sev-info:#7FA3DA;
  --quote-bg:#1C1F1B; --quote-line:#3A3A2E;
  color-scheme: dark;
}}
:root[data-theme="dark"] {
  --bg:#0F1514; --surface:#171F1E; --surface-2:#1F2927; --ink:#E6ECEA; --muted:#93A29D; --line:#2A3634; --line-strong:#3C4B48;
  --accent:#52BBAF; --accent-ink:#8FD8CF; --accent-soft:#163430; --focus:#52BBAF;
  --k-goal:#E48B55; --k-entity:#3FC4B0; --k-type:#6D9BE0; --k-procedure:#A98AE0; --k-capability:#E0B23A; --k-rule:#E0707D;
  --k-claim:#93A29D; --k-observation:#C9A26A; --k-issue:#E0707D; --k-property:#6D9BE0; --k-relation:#6D9BE0;
  --k-assistant:#52BBAF; --k-job:#E48B55; --k-tool:#E0B23A; --k-test:#A98AE0; --k-exclusion:#93A29D;
  --sev-critical:#E06A62; --sev-warning:#E0A53F; --sev-info:#7FA3DA;
  --quote-bg:#1C1F1B; --quote-line:#3A3A2E;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans); font-size:14px; line-height:1.45; }
.sr { position:absolute; left:-9999px; }
button { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.app { display:flex; flex-direction:column; height:100vh; min-height:560px; }
.top { display:flex; align-items:center; gap:20px; padding:10px 18px; border-bottom:1px solid var(--line); background:var(--surface); flex-wrap:wrap; }
.brand { display:flex; align-items:center; gap:12px; min-width:0; flex:1 1 260px; }
.brand-mark { width:12px; height:28px; background:linear-gradient(180deg,var(--accent),var(--k-goal)); border-radius:2px; flex:none; }
.top h1 { font-size:17px; font-weight:600; margin:0; letter-spacing:-0.01em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.top .sub { font-size:12px; color:var(--muted); }
.views { display:flex; gap:2px; background:var(--surface-2); padding:3px; border-radius:8px; }
.view-btn { border:0; background:transparent; padding:6px 14px; border-radius:6px; cursor:pointer; color:var(--muted); font-weight:500; }
.view-btn.is-on { background:var(--surface); color:var(--ink); box-shadow:0 1px 2px rgba(0,0,0,.08); }
.search { flex:0 1 320px; min-width:180px; }
.search input { width:100%; padding:8px 12px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--ink); font: inherit; }
.search input:focus { border-color:var(--accent); outline:none; box-shadow:0 0 0 3px var(--accent-soft); }
.body { display:flex; flex:1; min-height:0; }
.stage { flex:1; min-width:0; position:relative; display:flex; flex-direction:column; }
.view { display:none; flex:1; min-height:0; flex-direction:column; }
.view.is-on { display:flex; }
.graph-bar { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:8px 14px; flex-wrap:wrap; }
.legend { display:flex; gap:6px; flex-wrap:wrap; }
.legend button { display:inline-flex; align-items:center; gap:6px; padding:4px 10px 4px 6px; border:1px solid var(--line); background:var(--surface); border-radius:999px; cursor:pointer; font-size:12px; }
.legend button .dot { width:10px; height:10px; border-radius:50%; background:var(--c); }
.legend button.is-off { opacity:.45; text-decoration:line-through; }
.legend button .n { color:var(--muted); font-variant-numeric:tabular-nums; }
.graph-hint { font-size:12px; color:var(--muted); }
#graph { flex:1; width:100%; height:100%; display:block; cursor:grab; }
#graph:active { cursor:grabbing; }
#graph .link { stroke:var(--line-strong); stroke-opacity:.6; fill:none; }
#graph .link.dim { stroke-opacity:.08; }
#graph .link.lit { stroke:var(--ink); stroke-opacity:.9; }
#graph .node circle { stroke:var(--surface); stroke-width:1.5px; cursor:pointer; }
#graph .node.dim { opacity:.15; }
#graph .node.is-selected circle { stroke:var(--ink); stroke-width:2.5px; }
#graph .node text { font-size:10px; fill:var(--ink); pointer-events:none; paint-order:stroke; stroke:var(--bg); stroke-width:3px; stroke-linejoin:round; }
#graph .node.small text { display:none; }
#graph .node.quiet text { display:none; }
#graph.zoomed .node.quiet text, #graph .node.quiet.lit-label text { display:block; }
#view-browse, #view-overview { overflow:auto; }
#browse, #overview { padding:14px 18px 40px; max-width:980px; }
.section { margin-bottom:26px; }
.section h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 8px; display:flex; align-items:center; gap:8px; }
.section h2 .dot { width:9px; height:9px; border-radius:50%; background:var(--c); }
.section h2 .n { font-weight:400; font-variant-numeric:tabular-nums; }
.rows { border-top:1px solid var(--line); }
.row { display:grid; grid-template-columns: 1fr auto; gap:6px 14px; padding:9px 6px; border-bottom:1px solid var(--line); cursor:pointer; align-items:start; }
.row:hover { background:var(--surface); }
.row.is-selected { background:var(--accent-soft); }
.row .lbl { font-weight:500; }
.row .sum { color:var(--muted); font-size:12.5px; grid-column:1 / -1; }
.row .sum.mono { font-family:var(--mono); font-size:12px; }
.row .side { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
.group-h { font-size:12px; color:var(--muted); padding:12px 6px 4px; font-weight:500; }
.pill { display:inline-block; font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); color:var(--muted); background:var(--surface); white-space:nowrap; }
.pill.sev-CRITICAL { color:var(--sev-critical); border-color:var(--sev-critical); }
.pill.sev-WARNING { color:var(--sev-warning); border-color:var(--sev-warning); }
.pill.sev-INFO { color:var(--sev-info); border-color:var(--sev-info); }
.pill.kind { color:var(--c); border-color:var(--c); }
.pill.mod-MUST, .pill.mod-MUST_NOT { color:var(--k-rule); border-color:var(--k-rule); }
.pill.NEGATIVE { color:var(--k-rule); border-color:var(--k-rule); }
.empty { color:var(--muted); padding:10px 6px; }
.detail { width:420px; flex:none; border-left:1px solid var(--line); background:var(--surface); overflow:auto; display:flex; flex-direction:column; }
.detail-empty { padding:24px; color:var(--muted); }
.detail-head { padding:14px 18px 10px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--surface); z-index:1; }
.detail-head .crumbs { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
.detail-head .crumbs button { border:1px solid var(--line); background:var(--bg); border-radius:6px; padding:3px 9px; cursor:pointer; font-size:12px; }
.detail-head .crumbs button:disabled { opacity:.4; cursor:default; }
.detail-head h2 { font-size:16px; margin:0 0 6px; line-height:1.3; text-wrap:balance; }
.detail-head .id { font-family:var(--mono); font-size:11px; color:var(--muted); word-break:break-all; }
.detail-body { padding:6px 18px 30px; }
.field { padding:12px 0; border-bottom:1px solid var(--line); }
.field:last-child { border-bottom:0; }
.field h3 { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 6px; font-weight:500; }
.field p { margin:0; max-width:62ch; }
.field .mono { font-family:var(--mono); font-size:12px; white-space:pre-wrap; word-break:break-word; background:var(--surface-2); padding:8px 10px; border-radius:6px; }
.field .prompt { font-family:var(--mono); font-size:12px; white-space:pre-wrap; background:var(--surface-2); padding:10px; border-radius:6px; max-height:320px; overflow:auto; }
.links { display:flex; flex-wrap:wrap; gap:6px; }
.chip { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--line); background:var(--bg); border-radius:6px; padding:3px 9px; cursor:pointer; font-size:12.5px; text-align:left; }
.chip:hover { border-color:var(--c, var(--accent)); }
.chip .dot { width:8px; height:8px; border-radius:50%; background:var(--c, var(--muted)); flex:none; }
.pills { display:flex; flex-wrap:wrap; gap:6px; }
.quotes { margin:0; padding:0; list-style:none; display:flex; flex-direction:column; gap:6px; }
.quotes li { padding:6px 10px 6px 12px; border-left:3px solid var(--k-goal); background:var(--quote-bg); border-radius:0 6px 6px 0; font-style:italic; }
.bullets { margin:0; padding-left:18px; }
.bullets li { margin:3px 0; }
.dialog { margin:0; padding:0; list-style:none; }
.dialog li { margin:6px 0; }
.dialog .who { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
.steps { margin:0; padding-left:0; list-style:none; counter-reset: step; }
.steps li { position:relative; padding:6px 0 6px 30px; counter-increment: step; }
.steps li::before { content: counter(step); position:absolute; left:0; top:6px; width:20px; height:20px; border-radius:50%; background:var(--accent-soft); color:var(--accent-ink); font-size:11px; display:flex; align-items:center; justify-content:center; font-variant-numeric:tabular-nums; }
.steps .cap { margin-top:4px; }
.subrec { padding:8px 0; border-top:1px dashed var(--line); }
.subrec:first-child { border-top:0; }
.subrec p { margin:0 0 4px; }
.subrec .meta { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.freq { display:inline-flex; align-items:center; gap:6px; font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums; }
.freq i { display:inline-block; height:6px; width:60px; background:var(--surface-2); border-radius:3px; overflow:hidden; }
.freq i b { display:block; height:100%; width:var(--w); background:var(--k-observation); }
.ev { display:inline-flex; align-items:center; gap:5px; font-family:var(--mono); font-size:11px; padding:2px 7px; border-radius:5px; border:1px solid var(--line); background:var(--surface); cursor:pointer; color:var(--muted); }
.ev:hover, .ev.is-open { border-color:var(--accent); color:var(--accent-ink); }
.ev .a { width:6px; height:6px; border-radius:50%; background:var(--muted); }
.ev .a.AUTHORITATIVE { background:var(--accent); }
.ev .a.INTERFACE { background:var(--k-capability); }
.ev .a.OBSERVATIONAL { background:var(--k-observation); }
.evs { display:flex; flex-wrap:wrap; gap:5px; margin-top:6px; }
.quote { margin:8px 0 2px; padding:10px 12px; background:var(--quote-bg); border:1px solid var(--quote-line); border-radius:6px; }
.quote .q { font-family:var(--mono); font-size:12px; white-space:pre-wrap; word-break:break-word; margin:0 0 8px; max-height:260px; overflow:auto; }
.quote .src { font-size:11.5px; color:var(--muted); display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.quote .src a { color:var(--accent-ink); word-break:break-all; }
.ov-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:10px; margin:14px 0 22px; }
.tile { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:12px 14px; cursor:pointer; text-align:left; }
.tile .big { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-0.02em; color:var(--c, var(--ink)); }
.tile .lbl { font-size:12px; color:var(--muted); }
.lede { font-size:15.5px; line-height:1.55; max-width:70ch; margin:6px 0 14px; }
.meta-line { color:var(--muted); font-size:12.5px; display:flex; gap:14px; flex-wrap:wrap; font-family:var(--mono); }
table.plain { border-collapse:collapse; width:100%; font-size:13px; }
table.plain th, table.plain td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
table.plain th { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:500; }
table.plain td.mono, .ops li { font-family:var(--mono); font-size:12px; }
.ops { margin:0; padding-left:0; list-style:none; }
.ops li { padding:7px 8px; border-bottom:1px solid var(--line); }
.ops li.risk-PRIVILEGED { color:var(--sev-critical); }
.ops li .flag { color:var(--sev-warning); }
.overflow { overflow-x:auto; }
mark { background:var(--accent-soft); color:inherit; padding:0 1px; border-radius:2px; }
@media (max-width: 900px) {
  .body { flex-direction:column; }
  .detail { width:auto; border-left:0; border-top:1px solid var(--line); max-height:48vh; }
  .app { height:auto; min-height:100vh; }
  .stage { min-height:52vh; }
}
@media (prefers-reduced-motion: reduce) { * { transition:none !important; animation:none !important; } }
"""


JS = r"""
(function () {
  const M = JSON.parse(document.getElementById('data').textContent);
  const R = M.records, EV = M.evidence;
  const kindMeta = Object.fromEntries(M.kinds.map(k => [k.kind, k]));
  const color = k => `var(--k-${k})`;
  const $ = (s, el) => (el || document).querySelector(s);
  const el = (tag, attrs, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') n.className = v; else if (k === 'style') n.style.cssText = v; else if (k.startsWith('on')) n.addEventListener(k.slice(2), v); else if (v != null) n.setAttribute(k, v);
    }
    for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  };
  $('#subtitle').textContent = M.subtitle + (M.status ? ` · check ${M.status}` : '') + (M.digest ? ` · ${M.digest.slice(0, 19)}` : '');

  // ---------- state
  let selected = null, history = [], hidden = new Set(), query = '';
  const hist = { back: [], fwd: [] };

  // ---------- views
  document.querySelectorAll('.view-btn').forEach(b => b.addEventListener('click', () => showView(b.dataset.view)));
  function showView(name) {
    document.querySelectorAll('.view-btn').forEach(b => b.classList.toggle('is-on', b.dataset.view === name));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('is-on', v.id === 'view-' + name));
    if (name === 'graph') resize();
  }

  // ---------- evidence chips
  function evChip(id) {
    const e = EV[id];
    const chip = el('button', { class: 'ev', type: 'button', title: e ? e.title : id }, el('span', { class: 'a ' + (e ? e.authority || '' : '') }), id.replace(/^evidence:/, ''));
    chip.addEventListener('click', () => {
      const open = chip.nextElementSibling && chip.nextElementSibling.classList.contains('quote');
      if (open) { chip.nextElementSibling.remove(); chip.classList.remove('is-open'); return; }
      chip.classList.add('is-open');
      const q = el('div', { class: 'quote' },
        el('p', { class: 'q' }, e ? (e.text || '(no text captured)') : 'Evidence not found in the ledger.'),
        e ? el('div', { class: 'src' }, el('span', { class: 'pill' }, e.role || 'source'), e.authority ? el('span', { class: 'pill' }, e.authority) : null, el('span', null, e.title),
          e.locator ? (/^https?:/.test(e.locator) ? el('a', { href: e.locator, target: '_blank', rel: 'noopener' }, e.locator) : el('span', { class: 'mono-inline' }, e.locator)) : null) : null);
      chip.after(q);
    });
    return chip;
  }
  function evRow(ids) {
    if (!ids || !ids.length) return null;
    const wrap = el('div', { class: 'evs' });
    // Each chip is followed (on click) by its quote; wrap chips in spans so quotes land inline.
    ids.forEach(id => { const s = el('span', { style: 'display:contents' }); s.append(evChip(id)); wrap.append(s); });
    return wrap;
  }

  // ---------- links
  function linkChip(l) {
    const r = R[l.id];
    const k = r ? r.kind : 'claim';
    const c = el('button', { class: 'chip', type: 'button', style: `--c:${color(k)}` }, el('span', { class: 'dot' }), l.label);
    c.addEventListener('click', () => open(l.id));
    return c;
  }

  // ---------- detail panel
  function open(id, opts) {
    const r = R[id];
    if (!r) return;
    if (selected && selected !== id && !(opts && opts.noHistory)) { hist.back.push(selected); hist.fwd = []; }
    selected = id;
    location.hash = encodeURIComponent(id);
    renderDetail(r);
    highlight(id);
    document.querySelectorAll('.row').forEach(x => x.classList.toggle('is-selected', x.dataset.id === id));
  }
  function renderDetail(r) {
    const d = $('#detail'); d.innerHTML = '';
    const back = el('button', { type: 'button', disabled: hist.back.length ? null : 'disabled', onclick: () => { if (!hist.back.length) return; hist.fwd.push(selected); open(hist.back.pop(), { noHistory: true }); } }, '← Back');
    const fwd = el('button', { type: 'button', disabled: hist.fwd.length ? null : 'disabled', onclick: () => { if (!hist.fwd.length) return; hist.back.push(selected); open(hist.fwd.pop(), { noHistory: true }); } }, 'Forward →');
    const head = el('div', { class: 'detail-head' },
      el('div', { class: 'crumbs' }, back, fwd, el('span', { class: 'pill kind', style: `--c:${color(r.kind)}` }, kindMeta[r.kind] ? kindMeta[r.kind].label.replace(/s$/, '') : r.kind)),
      el('h2', null, r.label), el('div', { class: 'id' }, r.id));
    const body = el('div', { class: 'detail-body' });
    for (const f of r.details) body.append(field(f));
    if (r.evidence && r.evidence.length) body.append(el('div', { class: 'field' }, el('h3', null, `Evidence (${r.evidence.length})`), evRow(r.evidence)));
    d.append(head, body);
    d.scrollTop = 0;
  }
  function field(f) {
    const w = el('div', { class: 'field' }, el('h3', null, f.label));
    switch (f.kind) {
      case 'text': w.append(el('p', null, f.value)); break;
      case 'mono': w.append(el('div', { class: 'mono' }, f.value)); break;
      case 'prompt': w.append(el('div', { class: 'prompt' }, f.value)); break;
      case 'links': if (f.value.length) w.append(el('div', { class: 'links' }, f.value.map(linkChip))); else w.append(el('p', { class: 'empty' }, 'none')); break;
      case 'pills': w.append(el('div', { class: 'pills' }, f.value.map(p => el('span', { class: 'pill ' + p.replace(/\s.*/, '') + ' mod-' + p + ' sev-' + p }, p)))); break;
      case 'quotes': w.append(el('ul', { class: 'quotes' }, f.value.map(q => el('li', null, q)))); break;
      case 'bullets': w.append(el('ul', { class: 'bullets' }, f.value.map(q => el('li', null, q)))); break;
      case 'dialog': w.append(el('ul', { class: 'dialog' }, f.value.map(x => [el('li', null, el('div', { class: 'who' }, 'Caller'), x.caller), el('li', null, el('div', { class: 'who' }, 'Agent'), x.agent)]))); break;
      case 'steps': w.append(el('ol', { class: 'steps' }, f.value.map(s => el('li', null, s.instruction, s.capability ? el('div', { class: 'cap' }, linkChip(s.capability)) : null)))); break;
      case 'claims': w.append(...f.value.map(c => el('div', { class: 'subrec' }, el('p', null, c.text), el('div', { class: 'meta' },
          c.polarity === 'NEGATIVE' ? el('span', { class: 'pill NEGATIVE' }, 'not the case') : null, c.status ? el('span', { class: 'pill' }, c.status) : null, c.conditions ? el('span', { class: 'pill' }, 'when: ' + c.conditions) : null),
          evRow(c.evidence)))); break;
      case 'observations': w.append(...f.value.map(o => el('div', { class: 'subrec' }, el('p', null, o.text),
          o.sampleSize ? el('span', { class: 'freq' }, el('i', null, el('b', { style: `--w:${Math.round(100 * (o.count || 0) / o.sampleSize)}%` })), `${o.count} of ${o.sampleSize} calls`) : null, evRow(o.evidence)))); break;
      default: w.append(el('p', null, JSON.stringify(f.value)));
    }
    return w;
  }

  // ---------- browse
  const browse = $('#browse');
  function renderBrowse() {
    browse.innerHTML = '';
    const q = query.trim().toLowerCase();
    const match = r => !q || (r.label + ' ' + r.summary + ' ' + JSON.stringify(r.details)).toLowerCase().includes(q);
    for (const k of M.kinds) {
      let rs = Object.values(R).filter(r => r.kind === k.kind).filter(match);
      if (!rs.length) continue;
      const sec = el('section', { class: 'section' }, el('h2', null, el('span', { class: 'dot', style: `--c:${color(k.kind)}` }), k.label, el('span', { class: 'n' }, `${rs.length}`)));
      const rows = el('div', { class: 'rows' });
      if (k.kind === 'claim') {
        const groups = new Map();
        rs.forEach(r => { const s = r.meta.subjectLabel || '—'; if (!groups.has(s)) groups.set(s, []); groups.get(s).push(r); });
        [...groups.keys()].sort((a, b) => a.localeCompare(b)).forEach(s => { rows.append(el('div', { class: 'group-h' }, s)); groups.get(s).forEach(r => rows.append(row(r))); });
      } else if (k.kind === 'issue') {
        const order = { CRITICAL: 0, WARNING: 1, INFO: 2 };
        rs.sort((a, b) => (order[a.meta.severity] ?? 3) - (order[b.meta.severity] ?? 3)).forEach(r => rows.append(row(r)));
      } else rs.sort((a, b) => a.label.localeCompare(b.label)).forEach(r => rows.append(row(r)));
      sec.append(rows); browse.append(sec);
    }
    if (!browse.children.length) browse.append(el('p', { class: 'empty' }, 'Nothing matches that search.'));
  }
  function row(r) {
    const side = el('div', { class: 'side' });
    if (r.kind === 'issue') side.append(el('span', { class: 'pill sev-' + r.meta.severity }, r.meta.severity), el('span', { class: 'pill' }, (r.meta.issueKind || '').replace(/_/g, ' ').toLowerCase()));
    if (r.kind === 'rule') side.append(el('span', { class: 'pill mod-' + r.meta.modality }, r.meta.modality));
    if (r.kind === 'capability' || r.kind === 'tool') { side.append(el('span', { class: 'pill' }, 'risk ' + (r.meta.risk || '?'))); if (r.meta.confirm) side.append(el('span', { class: 'pill' }, 'confirms first')); }
    if (r.kind === 'goal' && r.meta.phrases) side.append(el('span', { class: 'pill' }, `${r.meta.phrases} caller phrases`));
    if (r.kind === 'job') side.append(el('span', { class: 'pill' }, (r.meta.handling || '').replace(/_/g, ' ').toLowerCase()));
    if (r.kind === 'observation' && r.meta.sampleSize) side.append(el('span', { class: 'freq' }, el('i', null, el('b', { style: `--w:${Math.round(100 * (r.meta.count || 0) / r.meta.sampleSize)}%` })), `${r.meta.count}/${r.meta.sampleSize}`));
    if (r.evidence && r.evidence.length) side.append(el('span', { class: 'pill' }, `${r.evidence.length} evidence`));
    const summary = r.kind === 'claim' || r.kind === 'observation' || r.kind === 'issue' ? null : el('div', { class: 'sum' + (r.kind === 'capability' || r.kind === 'tool' ? ' mono' : '') }, r.summary);
    const n = el('div', { class: 'row', 'data-id': r.id, tabindex: '0', role: 'button' }, el('div', { class: 'lbl' }, r.label), side, summary);
    n.addEventListener('click', () => open(r.id));
    n.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(r.id); } });
    return n;
  }

  // ---------- overview
  function renderOverview() {
    const o = $('#overview'); o.innerHTML = '';
    o.append(el('p', { class: 'lede' }, M.summary));
    if (M.callerRoles && M.callerRoles.length) o.append(el('div', { class: 'meta-line' }, 'callers: ' + M.callerRoles.join(', ')));
    const grid = el('div', { class: 'ov-grid' });
    for (const k of M.kinds) {
      const n = Object.values(R).filter(r => r.kind === k.kind).length;
      if (!n) continue;
      const t = el('button', { class: 'tile', type: 'button', style: `--c:${color(k.kind)}`, onclick: () => { showView('browse'); const h = [...browse.querySelectorAll('.section h2')].find(x => x.textContent.includes(k.label)); if (h) h.scrollIntoView({ block: 'start' }); } },
        el('div', { class: 'big' }, n), el('div', { class: 'lbl' }, k.label));
      grid.append(t);
    }
    o.append(grid);
    if (M.page === 'ontology') {
      const sev = { CRITICAL: 0, WARNING: 0, INFO: 0 };
      Object.values(R).filter(r => r.kind === 'issue').forEach(r => sev[r.meta.severity] = (sev[r.meta.severity] || 0) + 1);
      o.append(section('Issues by severity', el('div', { class: 'pills' }, Object.entries(sev).filter(([, n]) => n).map(([s, n]) => el('span', { class: 'pill sev-' + s }, `${n} ${s.toLowerCase()}`)))));
      if (M.coverage && M.coverage.segments) o.append(section('Evidence coverage', el('p', null, `${M.coverage.cited} of ${M.coverage.segments} source segments are cited by at least one record; ${M.coverage.declaredUncovered} were set aside on purpose; ${(M.coverage.uncited || []).length} are unaccounted for.`)));
      if (M.sources && M.sources.length) o.append(section('Sources', el('div', { class: 'overflow' }, el('table', { class: 'plain' }, el('thead', null, el('tr', null, ['Source', 'Role', 'Authority', 'Items', 'Segments'].map(h => el('th', null, h)))),
        el('tbody', null, M.sources.map(s => el('tr', null, el('td', { class: 'mono' }, s.location || s.id), el('td', null, s.role), el('td', null, s.authority || '—'), el('td', null, s.items ?? '—'), el('td', null, s.segments ?? '—'))))))));
      if (M.uncovered && M.uncovered.length) o.append(section('Set aside on purpose', el('div', { class: 'overflow' }, el('table', { class: 'plain' }, el('thead', null, el('tr', null, ['Segment', 'Reason'].map(h => el('th', null, h)))),
        el('tbody', null, M.uncovered.map(u => el('tr', null, el('td', null, u.title), el('td', null, u.reason))))))));
    }
    if (M.page === 'plan') {
      const rt = M.runtime || {};
      o.append(section('Runtime', el('div', { class: 'overflow' }, el('table', { class: 'plain' }, el('tbody', null,
        [['Tools call', rt.serverUrl], ['Model', rt.model ? `${rt.model.provider} · ${rt.model.model}` + (rt.model.temperature != null ? ` · temperature ${rt.model.temperature}` : '') : '—'],
         ['Voice', rt.voice ? `${rt.voice.provider} · ${rt.voice.voiceId}` : '—'], ['Transcriber', rt.transcriber ? `${rt.transcriber.provider} · ${rt.transcriber.model}` : '—'], ['Language', rt.language]]
          .map(([k, v]) => el('tr', null, el('th', null, k), el('td', { class: 'mono' }, v || '—'))))))));
      if (M.enabledOperations && M.enabledOperations.length) o.append(section('Operations the agent will be able to call', el('ul', { class: 'ops' }, M.enabledOperations.map(op => {
        const li = el('li', { class: /risk PRIVILEGED/.test(op) ? 'risk-PRIVILEGED' : '' });
        const parts = op.split(' · NO read-back'); li.append(parts[0]); if (parts.length > 1) li.append(el('span', { class: 'flag' }, ' · NO read-back'));
        return li; }))));
      if (M.build) {
        const b = M.build;
        o.append(section(`Build · knowledge base “${b.knowledgeBase.name}” (${b.knowledgeBase.files.length} files)`, el('div', { class: 'overflow' }, el('table', { class: 'plain' },
          el('thead', null, el('tr', null, ['File', 'Origin', 'From', 'Bytes'].map(h => el('th', null, h)))),
          el('tbody', null, b.knowledgeBase.files.map(f => el('tr', null, el('td', { class: 'mono' }, f.name), el('td', null, f.origin), el('td', { class: 'mono' }, f.locator), el('td', null, f.bytes))))))));
        o.append(section(`Build · tools (${b.tools.length})`, el('div', { class: 'overflow' }, el('table', { class: 'plain' },
          el('thead', null, el('tr', null, ['Tool', 'Method', 'URL', 'Secret headers'].map(h => el('th', null, h)))),
          el('tbody', null, b.tools.map(t => el('tr', null, el('td', null, t.name), el('td', { class: 'mono' }, t.method), el('td', { class: 'mono' }, t.url), el('td', null, t.secretHeaders.join(', ') || '—'))))))));
        o.append(section(`Build · assistants (${b.assistants.length})` + (b.squad ? ` · squad “${b.squad}”` : ''), el('div', { class: 'overflow' }, el('table', { class: 'plain' },
          el('thead', null, el('tr', null, ['Assistant', 'Tools', 'Knowledge base', 'First message'].map(h => el('th', null, h)))),
          el('tbody', null, b.assistants.map(a => el('tr', null, el('td', null, a.name), el('td', { class: 'mono' }, a.tools.map(x => x.replace(/^tool:/, '')).join(', ')), el('td', null, a.knowledge ? 'yes' : 'no'), el('td', null, a.firstMessage || 'model-generated'))))))));
        const ids = Object.entries(b.receipts || {});
        if (ids.length) o.append(section('Applied resource IDs', el('div', { class: 'overflow' }, el('table', { class: 'plain' }, el('tbody', null, ids.map(([k, v]) => el('tr', null, el('th', null, k), el('td', { class: 'mono' }, typeof v === 'string' ? v : JSON.stringify(v)))))))));
      }
    }
  }
  function section(title, ...kids) { return el('section', { class: 'section' }, el('h2', null, title), ...kids); }

  // ---------- graph
  const svg = d3.select('#graph');
  const gRoot = svg.append('g');
  const gLinks = gRoot.append('g'), gNodes = gRoot.append('g');
  const nodes = M.nodes.map(n => ({ ...n }));
  const nodeById = new Map(nodes.map(n => [n.id, n]));
  const links = M.edges.filter(e => nodeById.has(e.source) && nodeById.has(e.target)).map(e => ({ ...e }));
  const neighbors = new Map();
  links.forEach(l => { (neighbors.get(l.source) || neighbors.set(l.source, new Set()).get(l.source)).add(l.target); (neighbors.get(l.target) || neighbors.set(l.target, new Set()).get(l.target)).add(l.source); });
  const radius = n => Math.min(16, 4 + Math.sqrt(n.weight || 1) * 2.2 + (n.kind === 'goal' || n.kind === 'assistant' ? 3 : 0));
  const sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(l => l.kind === 'instance-of' || l.kind === 'is-a' ? 40 : 70).strength(0.6))
    .force('charge', d3.forceManyBody().strength(-140))
    .force('collide', d3.forceCollide().radius(d => radius(d) + 6))
    .force('center', d3.forceCenter(0, 0))
    .force('x', d3.forceX(0).strength(0.03)).force('y', d3.forceY(0).strength(0.03));
  const link = gLinks.selectAll('line').data(links).join('line').attr('class', 'link').attr('stroke-width', l => l.kind === 'serves' || l.kind === 'owns' ? 1.4 : 1);
  const QUIET = new Set(['type', 'procedure', 'rule', 'claim']);
  const node = gNodes.selectAll('g').data(nodes).join('g').attr('class', d => 'node' + (radius(d) < 6 ? ' small' : '') + (QUIET.has(d.kind) ? ' quiet' : ''))
    .call(d3.drag().on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.25).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; }).on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));
  node.append('circle').attr('r', radius).attr('fill', d => color(d.kind));
  node.append('text').attr('dx', d => radius(d) + 3).attr('dy', '0.35em').text(d => d.label.length > 34 ? d.label.slice(0, 32) + '…' : d.label);
  node.append('title').text(d => d.label);
  node.on('click', (e, d) => { e.stopPropagation(); open(d.id); });
  node.on('mouseenter', (e, d) => { if (!selected) lit(d.id); }).on('mouseleave', () => { if (!selected) lit(null); });
  svg.on('click', () => { /* keep selection */ });
  sim.on('tick', () => {
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('transform', d => `translate(${d.x},${d.y})`);
  });
  const zoom = d3.zoom().scaleExtent([0.2, 4]).on('zoom', e => { gRoot.attr('transform', e.transform); svg.classed('zoomed', e.transform.k >= 1.35); gNodes.selectAll('g').classed('small', d => radius(d) * e.transform.k < 7); });
  svg.call(zoom);
  function resize() {
    const box = $('#graph').getBoundingClientRect();
    if (!box.width) return;
    svg.attr('viewBox', [-box.width / 2, -box.height / 2, box.width, box.height].join(' '));
  }
  window.addEventListener('resize', resize); resize();
  function lit(id) {
    if (!id) { node.classed('dim', false).classed('lit-label', false); link.classed('dim', false).classed('lit', false); return; }
    const near = neighbors.get(id) || new Set();
    node.classed('dim', d => d.id !== id && !near.has(d.id)).classed('lit-label', d => d.id === id || near.has(d.id));
    link.classed('lit', l => l.source.id === id || l.target.id === id).classed('dim', l => l.source.id !== id && l.target.id !== id);
  }
  function highlight(id) {
    node.classed('is-selected', d => d.id === id);
    if (nodeById.has(id)) lit(id); else lit(null);
  }
  function applyHidden() {
    node.style('display', d => hidden.has(d.kind) ? 'none' : null);
    link.style('display', l => hidden.has(l.source.kind) || hidden.has(l.target.kind) ? 'none' : null);
  }
  function applySearchToGraph() {
    const q = query.trim().toLowerCase();
    if (!q) { node.classed('dim', false); return; }
    node.classed('dim', d => !(d.label + ' ' + (R[d.id] ? R[d.id].summary : '')).toLowerCase().includes(q));
  }
  // legend
  const legend = $('#legend');
  for (const k of M.kinds.filter(k => k.graph)) {
    const n = nodes.filter(x => x.kind === k.kind).length;
    if (!n) continue;
    const b = el('button', { type: 'button', style: `--c:${color(k.kind)}` }, el('span', { class: 'dot' }), k.label, el('span', { class: 'n' }, `${n}`));
    b.addEventListener('click', () => { if (hidden.has(k.kind)) hidden.delete(k.kind); else hidden.add(k.kind); b.classList.toggle('is-off', hidden.has(k.kind)); applyHidden(); });
    legend.append(b);
  }

  // ---------- search
  $('#search').addEventListener('input', e => { query = e.target.value; renderBrowse(); applySearchToGraph(); if (query && $('#view-browse').classList.contains('is-on') === false && $('#view-graph').classList.contains('is-on') === false) showView('browse'); });

  renderBrowse(); renderOverview();
  const initial = decodeURIComponent(location.hash.slice(1));
  if (initial && R[initial]) { open(initial); }
})();
"""


# --------------------------------------------------------------------------- entry points


def render_ontology(workspace: Workspace) -> Path:
    from . import ontology as ontology_module

    candidate = ontology_module.load_candidate(workspace)
    ledger = load_ledger(workspace)
    check_path = workspace.path("ontology", "check.json")
    check = read_json(check_path) if check_path.exists() else None
    model = ontology_model(candidate, evidence_index(workspace, ledger), ledger, check)
    out = workspace.path("ontology", "ontology.html")
    out.write_text(render_page(model), encoding="utf-8")
    return out


def render_plan(workspace: Workspace) -> Path:
    from . import ontology as ontology_module
    from . import plan as plan_module

    candidate = plan_module.load_candidate(workspace)
    try:
        approved = ontology_module.approved_ontology(workspace)
    except BuildError:
        approved = ontology_module.load_candidate(workspace)
    ledger = load_ledger(workspace)
    check_path = workspace.path("plan", "check.json")
    check = read_json(check_path) if check_path.exists() else None
    build_path = workspace.path("vapi", "build.json")
    build = read_json(build_path) if build_path.exists() else None
    receipts_path = workspace.path("vapi", "receipts.json")
    receipts = read_json(receipts_path) if receipts_path.exists() else None
    model = plan_model(candidate, approved, evidence_index(workspace, ledger), build, check, receipts)
    out = workspace.path("plan", "plan.html")
    out.write_text(render_page(model), encoding="utf-8")
    return out
