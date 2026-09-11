from __future__ import annotations

import io
import json

import pytest

from vapi_build import openapi, sources, transcripts, website
from vapi_build.workspace import BuildError
from .conftest import nested_csv


def test_website_parser_drops_hidden_and_scripts_keeps_visible():
    page = website.extract_page('<html><head><title>T</title><script>alert(1)</script></head><body><nav><a href="/x">Nav</a></nav>'
                                '<h1>Hello</h1><div hidden>SECRET</div><p style="display:none">HIDDEN</p><p>Visible <b>bold</b> text.</p>'
                                '<form><input value="PRIVATE"><textarea>NOTES</textarea></form><ul><li>One</li><li>Two</li></ul></body></html>')
    texts = [b["text"] for b in page["blocks"]]
    assert page["title"] == "T"
    assert texts == ["Hello", "Visible bold text.", "One", "Two"]


def test_nested_csv_and_utterance_rows_and_json_and_text():
    conversations = list(transcripts.conversations_from_csv(io.StringIO(nested_csv().decode()), "x.csv"))
    assert [c["id"] for c in conversations] == ["c1", "c2", "c3"]
    assert conversations[0]["turns"][0] == {"speaker": "Customer", "text": "I need to cancel my crossing tomorrow and get my money back."}
    assert conversations[0]["fields"]["reason"] == "Refund"
    flat = "conversation_id,speaker,text\n1,Customer,Hi\n1,Agent,Hello\n2,Customer,Bye\n"
    grouped = list(transcripts.conversations_from_csv(io.StringIO(flat), "flat.csv"))
    assert [len(c["turns"]) for c in grouped] == [2, 1]
    from_json = transcripts.conversations_from_json(json.dumps({"call_id": "k", "turns": [{"speaker": "A", "text": "x"}, "plain line"]}).encode(), "k.json")
    assert from_json[0]["id"] == "k" and len(from_json[0]["turns"]) == 2
    assert transcripts._turns_from_text("Agent: hi\nthere\nCustomer: yo")[0]["text"] == "hi there"


def test_reservoir_sampling_is_seeded_and_bounded():
    rows = "conversation_id,text\n" + "".join(f"{i},hello number {i}\n" for i in range(200))
    objects = [("big.csv", lambda: io.BytesIO(rows.encode()), len(rows))]
    first = transcripts.sample_from_objects(objects, sample=5, seed=7, scan_bytes=10 ** 9)
    second = transcripts.sample_from_objects(objects, sample=5, seed=7, scan_bytes=10 ** 9)
    assert [c["id"] for c in first["conversations"]] == [c["id"] for c in second["conversations"]]
    assert first["sampling"]["candidatesSeen"] == 200 and first["sampling"]["sampled"] == 5
    truncated = transcripts.sample_from_objects(objects, sample=5, seed=7, scan_bytes=500)
    assert truncated["sampling"]["scanTruncated"] and truncated["sampling"]["candidatesSeen"] < 200


def test_pii_scan_counts_patterns():
    scan = transcripts.scan_conversations([{"id": "1", "locator": "x", "turns": [{"speaker": "c", "text": "mail me at a@b.co or call 555-123-4567, card 4111111111111111"}], "fields": {}}])
    assert scan["conversationsWithHits"] == 1 and scan["patternHits"]["email"] == 1 and scan["patternHits"]["longDigits"] == 1


def test_openapi_schema_projection_and_names():
    document = {"openapi": "3.0.3", "paths": {"/things/{id}": {"put": {"operationId": "update-thing", "requestBody": {"content": {"application/json": {"schema": {
        "allOf": [{"$ref": "#/components/schemas/A"}, {"type": "object", "properties": {"extra": {"oneOf": [{"type": "integer"}, {"type": "null"}]}}}]}}}},
        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}], "responses": {"200": {"description": "ok"}}}}},
        "components": {"schemas": {"A": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string", "maxLength": 5}, "tags": {"type": "array", "items": {"type": ["string", "null"]}}}}}}}
    result = openapi.inventory(document, server_url="https://api.example")
    op = result["operations"][0]
    schema = op["toolSchema"]
    assert schema["properties"]["id"] == {"type": "string", "format": "uuid"}
    assert schema["properties"]["name"] == {"type": "string"} and schema["properties"]["tags"]["items"] == {"type": "string"}
    assert schema["properties"]["extra"]["type"] == "integer" and schema["required"] == ["id", "name"]
    assert op["classification"]["risk"] == "MEDIUM" and op["classification"]["confirmBeforeCall"] is True  # every write confirms
    taken = set()
    assert openapi.tool_name("update-thing", taken) == "update-thing"
    assert openapi.tool_name("update-thing", taken) == "update-thing_2"
    assert len(openapi.tool_name("x" * 60, set())) == 40


def test_openapi_parse_rejects_non_openapi_and_external_refs():
    with pytest.raises(BuildError):
        sources.parse_openapi(b'{"swagger": "2.0"}')
    with pytest.raises(BuildError, match="not local"):
        sources.parse_openapi(json.dumps({"openapi": "3.1.0", "paths": {"/a": {"get": {"responses": {"200": {"$ref": "https://x/y"}}}}}}).encode())
    parsed = sources.parse_openapi(json.dumps({"openapi": "3.1.0", "servers": [{"url": "/api"}], "paths": {}}).encode(), document_url="https://host.example/spec.json")
    assert parsed["serverUrl"] == "https://host.example/api"


def test_source_registration_guards(tmp_path):
    from vapi_build.workspace import Workspace

    workspace = Workspace.create(tmp_path / "w", "Guards")
    with pytest.raises(BuildError, match="privacy attestation"):
        sources.add_source(workspace, "transcripts", str(tmp_path))
    with pytest.raises(BuildError, match="HTTPS"):
        sources.add_source(workspace, "website", "http://insecure.example")
    with pytest.raises(BuildError, match="does not exist"):
        sources.add_source(workspace, "knowledge", str(tmp_path / "missing"))
    sources.add_source(workspace, "knowledge", "s3://bucket/prefix/")
    with pytest.raises(BuildError, match="already registered"):
        sources.add_source(workspace, "knowledge", "s3://bucket/prefix/")


def test_crawl_stays_on_host_and_records_failures():
    pages = {"https://a.example/": (b'<a href="/b">b</a><a href="https://other.example/">o</a><a href="/c.png">img</a>', "text/html"),
             "https://a.example/b": (b"<p>B</p>", "text/html")}

    def fetch(url):
        if url not in pages:
            raise BuildError("404")
        return pages[url][0], pages[url][1], url

    crawled = sources.crawl_site("https://a.example/", fetch, max_pages=10)
    assert [p["locator"] for p in crawled] == ["https://a.example/", "https://a.example/b"]
