"""Sample and normalize call transcripts from whatever shape they arrive in.

Supported shapes: a CSV with one conversation per row (including a nested-CSV transcript
column), a CSV of utterance rows grouped by a conversation id, JSON or JSONL objects with a
turns/messages/utterances array or a transcript string, and plain text files (one
conversation each). Large CSV objects are streamed up to a byte budget and sampled with a
seeded reservoir, so a multi-gigabyte corpus costs a bounded download.
"""
from __future__ import annotations

import codecs
import csv
import io
import json
import random
import re
from typing import Any, Callable, Iterable, Iterator

from .workspace import BuildError

SPEAKER_COLUMNS = ("speaker", "role", "party", "speaker_role", "speaker_name", "from")
TEXT_COLUMNS = ("text", "content", "utterance", "message", "transcript", "body")
ID_COLUMNS = ("conversation_id", "call_id", "conversationid", "callid", "session_id", "id", "transcript_id")
csv.field_size_limit(64 * 1024 * 1024)

Conversation = dict[str, Any]


class _Lines:
    """Yield decoded lines from a byte stream while counting bytes; stops at a byte budget."""

    def __init__(self, reader: Any, limit: int) -> None:
        self.reader, self.limit, self.read_bytes, self.truncated = reader, limit, 0, False
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.buffer = ""

    def __iter__(self) -> Iterator[str]:
        while True:
            remaining = self.limit - self.read_bytes
            if remaining <= 0:
                # Budget spent: truncated only if the source actually has more bytes.
                self.truncated = bool(self.reader.read(1))
                break
            chunk = self.reader.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            self.read_bytes += len(chunk)
            self.buffer += self.decoder.decode(chunk)
            lines = self.buffer.split("\n")
            self.buffer = lines.pop()
            for line in lines:
                yield line + "\n"
        self.buffer += self.decoder.decode(b"", final=True)
        if self.buffer and not self.truncated:
            yield self.buffer
        self.buffer = ""


def _pick(header: Iterable[str], candidates: Iterable[str]) -> str | None:
    lowered = {h.casefold().strip(): h for h in header}
    for name in candidates:
        if name in lowered:
            return lowered[name]
    return None


def _nested_csv_column(row: dict[str, str]) -> str | None:
    for key, value in row.items():
        if not isinstance(value, str) or "\n" not in value:
            continue
        first = value.split("\n", 1)[0].casefold()
        if any(s in first for s in SPEAKER_COLUMNS) and any(t in first for t in TEXT_COLUMNS):
            return key
    return None


def _turns_from_rows(rows: list[dict[str, str]], speaker_col: str | None, text_col: str) -> list[dict[str, str]]:
    turns = []
    for row in rows:
        text = (row.get(text_col) or "").strip()
        if not text:
            continue
        turns.append({"speaker": (row.get(speaker_col) or "unknown").strip() if speaker_col else "unknown", "text": text})
    return turns


def conversation_from_row(row: dict[str, str], locator: str, index: int) -> Conversation | None:
    nested = _nested_csv_column(row)
    id_col = _pick(row.keys(), ID_COLUMNS)
    conversation_id = str(row.get(id_col) or index) if id_col else str(index)
    fields = {k: v for k, v in row.items() if k != nested and isinstance(v, str) and v and len(v) <= 200}
    if nested:
        inner = list(csv.DictReader(io.StringIO(row[nested])))
        if not inner:
            return None
        speaker_col, text_col = _pick(inner[0].keys(), SPEAKER_COLUMNS), _pick(inner[0].keys(), TEXT_COLUMNS)
        if not text_col:
            return None
        turns = _turns_from_rows(inner, speaker_col, text_col)
    else:
        text_col = _pick(row.keys(), TEXT_COLUMNS)
        if text_col and row.get(text_col):
            turns = [{"speaker": (row.get(_pick(row.keys(), SPEAKER_COLUMNS) or "") or "unknown"), "text": row[text_col].strip()}]
        else:
            turns = [{"speaker": "record", "text": "\n".join(f"{k}: {v}" for k, v in row.items() if v)}]
    if not turns:
        return None
    return {"id": conversation_id, "locator": locator, "turns": turns, "fields": fields}


def conversations_from_csv(lines: Iterable[str], locator: str) -> Iterator[Conversation]:
    reader = csv.DictReader(lines)
    header = reader.fieldnames or []
    speaker_col, text_col, id_col = _pick(header, SPEAKER_COLUMNS), _pick(header, TEXT_COLUMNS), _pick(header, ID_COLUMNS)
    utterance_rows = bool(text_col and id_col and speaker_col)
    if utterance_rows:
        current_id, rows = None, []
        for row in reader:
            row_id = row.get(id_col) or ""
            if current_id is not None and row_id != current_id and rows:
                yield {"id": current_id, "locator": locator, "turns": _turns_from_rows(rows, speaker_col, text_col), "fields": {}}
                rows = []
            current_id = row_id
            rows.append(row)
        if rows and current_id is not None:
            yield {"id": current_id, "locator": locator, "turns": _turns_from_rows(rows, speaker_col, text_col), "fields": {}}
        return
    for index, row in enumerate(reader, start=1):
        conversation = conversation_from_row(row, locator, index)
        if conversation:
            yield conversation


def conversations_from_json(data: bytes, locator: str) -> list[Conversation]:
    text = data.decode("utf-8-sig", errors="replace")
    records: list[Any] = []
    try:
        loaded = json.loads(text)
        records = loaded if isinstance(loaded, list) else [loaded]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    conversations = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        turns_source = next((record[key] for key in ("turns", "messages", "utterances", "transcript", "dialogue") if isinstance(record.get(key), list)), None)
        if turns_source is not None:
            turns = []
            for turn in turns_source:
                if isinstance(turn, dict):
                    text_value = next((turn[k] for k in ("text", "content", "utterance", "message") if isinstance(turn.get(k), str)), "")
                    speaker = next((turn[k] for k in ("speaker", "role", "party", "from") if isinstance(turn.get(k), str)), "unknown")
                    if text_value.strip():
                        turns.append({"speaker": speaker, "text": text_value.strip()})
                elif isinstance(turn, str) and turn.strip():
                    turns.append({"speaker": "unknown", "text": turn.strip()})
        elif isinstance(record.get("transcript"), str):
            turns = _turns_from_text(record["transcript"])
        else:
            continue
        if turns:
            identity = str(record.get("id") or record.get("conversation_id") or record.get("call_id") or index)
            fields = {k: v for k, v in record.items() if isinstance(v, (str, int, float)) and k not in ("transcript",) and len(str(v)) <= 200}
            conversations.append({"id": identity, "locator": locator, "turns": turns, "fields": fields})
    return conversations


def _turns_from_text(text: str) -> list[dict[str, str]]:
    turns = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9 _.-]{0,30}):\s*(.+)$", line)
        if match:
            turns.append({"speaker": match.group(1).strip(), "text": match.group(2).strip()})
        elif turns:
            turns[-1]["text"] += " " + line
        else:
            turns.append({"speaker": "unknown", "text": line})
    return turns


def sample_from_objects(objects: list[tuple[str, Callable[[], Any], int]], *, sample: int, seed: int, scan_bytes: int) -> dict[str, Any]:
    """objects: (locator, open() -> bytes or binary reader, size). Returns sampled normalized conversations."""
    rng = random.Random(seed)
    reservoir: list[Conversation] = []
    seen = 0
    notes: list[str] = []
    truncated = False

    def consider(conversation: Conversation) -> None:
        nonlocal seen
        seen += 1
        if len(reservoir) < sample:
            reservoir.append(conversation)
        else:
            slot = rng.randrange(seen)
            if slot < sample:
                reservoir[slot] = conversation

    many_small = len(objects) > 1 and all(size <= 2 * 1024 * 1024 for _, _, size in objects)
    order = list(objects)
    if many_small and len(order) > sample * 4:
        rng.shuffle(order)
        order = order[: sample * 4]
        notes.append(f"Scanned a seeded subset of {len(order)} of {len(objects)} objects.")
    for locator, opener, size in order:
        if len(reservoir) >= sample and many_small:
            break
        handle = opener()
        name = locator.casefold()
        if name.endswith(".csv") or (size > 2 * 1024 * 1024 and not name.endswith((".json", ".jsonl", ".txt", ".md"))):
            reader = handle if hasattr(handle, "read") else io.BytesIO(handle)
            lines = _Lines(reader, scan_bytes)
            for conversation in conversations_from_csv(lines, locator):
                consider(conversation)
            if lines.truncated:
                truncated = True
                notes.append(f"{locator}: scanned the first {lines.read_bytes // (1024 * 1024)} MB only; sample drawn from that prefix.")
            continue
        data = handle.read() if hasattr(handle, "read") else handle
        if name.endswith((".json", ".jsonl")):
            for conversation in conversations_from_json(data, locator):
                consider(conversation)
        else:
            turns = _turns_from_text(data.decode("utf-8-sig", errors="replace"))
            if turns:
                consider({"id": locator.rsplit("/", 1)[-1], "locator": locator, "turns": turns, "fields": {}})
    if not reservoir:
        raise BuildError("No conversations could be parsed from the transcript source.")
    reservoir.sort(key=lambda c: (c["locator"], c["id"]))
    return {"conversations": reservoir, "notes": notes,
            "sampling": {"method": "seeded-reservoir", "seed": seed, "requested": sample, "sampled": len(reservoir), "candidatesSeen": seen, "scanTruncated": truncated}}


def conversation_text(conversation: Conversation) -> str:
    return "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in conversation["turns"])


PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)"),
    "longDigits": re.compile(r"(?<!\d)\d{13,19}(?!\d)"),
    "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
}


def scan_conversations(conversations: list[Conversation]) -> dict[str, Any]:
    counts = {name: 0 for name in PII_PATTERNS}
    flagged = 0
    for conversation in conversations:
        text = conversation_text(conversation)
        hit = False
        for name, pattern in PII_PATTERNS.items():
            found = len(pattern.findall(text))
            counts[name] += found
            hit = hit or found > 0
        flagged += hit
    return {"conversationsScanned": len(conversations), "conversationsWithHits": flagged, "patternHits": counts,
            "note": "Pattern counts only; synthetic data trips these too. Not a redaction certificate."}
