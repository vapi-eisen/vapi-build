"""OpenAPI operation inventory, risk classification, and schema conversion for Vapi tools."""
from __future__ import annotations

import copy
import re
from typing import Any

import yaml

from .workspace import BuildError, slug

HTTP_METHODS = ("get", "put", "post", "delete", "patch")
VAPI_SCHEMA_KEYS = {"type", "items", "properties", "description", "pattern", "format", "required", "enum", "title"}
VAPI_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"}


def resolve(document: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """Inline local $ref pointers recursively (bounded depth to survive cycles)."""
    if depth > 24:
        return {"type": "object", "description": "recursive schema truncated"}
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            target = node["$ref"]
            current: Any = document
            for token in target[2:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                current = current[token] if isinstance(current, dict) else current[int(token)]
            merged = copy.deepcopy(current)
            for key, value in node.items():
                if key != "$ref":
                    merged[key] = value
            return resolve(document, merged, depth + 1)
        return {key: resolve(document, value, depth + 1) for key, value in node.items()}
    if isinstance(node, list):
        return [resolve(document, item, depth + 1) for item in node]
    return node


def classify(method: str, path: str, operation_id: str, operation: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    lowered = f"{path} {operation_id} {operation.get('summary', '')}".casefold()
    admin = "/admin" in path.casefold() or any(t in lowered for t in ("admin", "reset", "restore", "internal", "debug"))
    destructive = method == "DELETE" or any(t in lowered for t in ("delete", "remove", "reset", "close", "cancel", "terminate"))
    financial = any(t in lowered for t in ("transfer", "payment", "pay", "deposit", "withdraw", "refund", "charge", "billing", "purchase", "order"))
    identity = any(t in lowered for t in ("/auth", "login", "token", "password", "otp", "verify", "identity", "/me"))
    security = operation.get("security", document.get("security", []))
    public = not security or "/public/" in path or path in {"/health", "/status"}
    read = method in {"GET", "HEAD"}
    risk = "PRIVILEGED" if admin else "HIGH" if (destructive or financial) else "MEDIUM" if not read else "LOW"
    return {
        "read": read,
        "write": not read,
        "public": public,
        "requiresAuth": not public,
        "adminOrInternal": admin,
        "destructive": destructive,
        "financial": financial,
        "identitySensitive": identity,
        "risk": risk,
        "confirmBeforeCall": (not read) and (destructive or financial or risk in {"HIGH", "PRIVILEGED"}),
    }


def _params(path_item: dict[str, Any], operation: dict[str, Any], document: dict[str, Any]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for scope in (path_item.get("parameters") or [], operation.get("parameters") or []):
        for parameter in scope:
            parameter = resolve(document, parameter)
            if isinstance(parameter, dict) and parameter.get("name") and parameter.get("in"):
                merged[(parameter["in"], parameter["name"])] = parameter
    return list(merged.values())


def _body_schema(operation: dict[str, Any], document: dict[str, Any]) -> dict[str, Any] | None:
    body = resolve(document, operation.get("requestBody"))
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    for media in ("application/json", "application/x-www-form-urlencoded", "*/*"):
        if media in content and isinstance(content[media], dict) and isinstance(content[media].get("schema"), dict):
            return content[media]["schema"]
    for value in content.values():
        if isinstance(value, dict) and isinstance(value.get("schema"), dict):
            return value["schema"]
    return None


def _response_schema(operation: dict[str, Any], document: dict[str, Any]) -> dict[str, Any] | None:
    responses = resolve(document, operation.get("responses") or {})
    for code in sorted(responses):
        if str(code).startswith("2") and isinstance(responses[code], dict):
            content = responses[code].get("content") or {}
            for value in content.values():
                if isinstance(value, dict) and isinstance(value.get("schema"), dict):
                    return value["schema"]
    return None


def to_vapi_schema(schema: Any, depth: int = 0) -> dict[str, Any]:
    """Project an arbitrary JSON Schema onto the subset Vapi's JsonSchema accepts."""
    if not isinstance(schema, dict) or depth > 12:
        return {"type": "string"}
    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            projected = to_vapi_schema(part, depth + 1)
            merged["properties"].update(projected.get("properties", {}))
            merged["required"] += projected.get("required", [])
        if not merged["required"]:
            merged.pop("required")
        return merged
    for key in ("oneOf", "anyOf"):
        if key in schema and isinstance(schema[key], list) and schema[key]:
            first = to_vapi_schema(schema[key][0], depth + 1)
            first.setdefault("description", schema.get("description", ""))
            return first
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind not in {"string", "number", "integer", "boolean", "array", "object"}:
        kind = "object" if "properties" in schema else "array" if "items" in schema else "string"
    out: dict[str, Any] = {"type": kind}
    for key in ("description", "title", "pattern"):
        if isinstance(schema.get(key), str) and schema[key]:
            out[key] = schema[key]
    if isinstance(schema.get("enum"), list) and all(isinstance(v, (str, int, float, bool)) for v in schema["enum"]):
        out["enum"] = [str(v) for v in schema["enum"]]
        out["type"] = "string"
    if schema.get("format") in VAPI_FORMATS and out["type"] == "string":
        out["format"] = schema["format"]
    if kind == "object":
        properties = schema.get("properties") or {}
        out["properties"] = {name: to_vapi_schema(value, depth + 1) for name, value in properties.items()} if isinstance(properties, dict) else {}
        required = [name for name in (schema.get("required") or []) if name in out["properties"]]
        if required:
            out["required"] = required
    if kind == "array":
        out["items"] = to_vapi_schema(schema.get("items") or {"type": "string"}, depth + 1)
    return out


def inventory(document: dict[str, Any], *, server_url: str | None) -> dict[str, Any]:
    operations = []
    seen_ids: set[str] = set()
    for path, path_item in sorted((document.get("paths") or {}).items()):
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("operationId") or "").strip() or f"{method}_{slug(path, 40)}"
            if operation_id in seen_ids:
                raise BuildError(f"Duplicate operationId in the OpenAPI document: {operation_id}")
            seen_ids.add(operation_id)
            parameters = _params(path_item, operation, document)
            body = _body_schema(operation, document)
            response = _response_schema(operation, document)
            classification = classify(method.upper(), path, operation_id, operation, document)
            tool_properties: dict[str, Any] = {}
            required: list[str] = []
            for parameter in parameters:
                if parameter["in"] not in {"path", "query"}:
                    continue
                projected = to_vapi_schema(parameter.get("schema") or {"type": "string"})
                if parameter.get("description") and "description" not in projected:
                    projected["description"] = parameter["description"]
                tool_properties[parameter["name"]] = projected
                if parameter["in"] == "path" or parameter.get("required"):
                    required.append(parameter["name"])
            if body is not None:
                projected = to_vapi_schema(body)
                if projected.get("type") == "object":
                    tool_properties.update(projected.get("properties", {}))
                    required += [name for name in projected.get("required", []) if name not in required]
                else:
                    tool_properties["body"] = projected
                    required.append("body")
            tool_schema: dict[str, Any] = {"type": "object", "properties": tool_properties}
            if required:
                tool_schema["required"] = required
            query_names = [p["name"] for p in parameters if p["in"] == "query"]
            path_names = [p["name"] for p in parameters if p["in"] == "path"]
            body_names = [name for name in tool_properties if name not in query_names and name not in path_names]
            text = yaml.safe_dump({
                "operationId": operation_id, "method": method.upper(), "path": path,
                "summary": operation.get("summary"), "description": operation.get("description"), "tags": operation.get("tags"),
                "security": operation.get("security", document.get("security")),
                "parameters": [{k: v for k, v in resolve(document, p).items() if k in {"name", "in", "required", "description", "schema"}} for p in parameters],
                "requestBody": body, "response": response,
            }, sort_keys=False, allow_unicode=True, width=100).strip()
            operations.append({
                "id": f"capability:{slug(operation_id, 60)}",
                "operationId": operation_id, "method": method.upper(), "path": path,
                "summary": operation.get("summary") or "", "description": operation.get("description") or "",
                "tags": operation.get("tags") or [],
                "classification": classification,
                "toolSchema": tool_schema,
                "pathParams": path_names, "queryParams": query_names, "bodyParams": body_names,
                "responseSchema": to_vapi_schema(response) if response else None,
                "text": text,
            })
    if not operations:
        raise BuildError("The OpenAPI document declares no operations.")
    return {"openapiVersion": str(document.get("openapi")), "title": (document.get("info") or {}).get("title", ""), "serverUrl": server_url,
            "operationCount": len(operations), "operations": operations}


def tool_name(operation_id: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]", "_", operation_id)[:40] or "tool"
    name, counter = base, 2
    while name in taken:
        suffix = f"_{counter}"
        name = base[: 40 - len(suffix)] + suffix
        counter += 1
    taken.add(name)
    return name
