"""Verbatim document records and deterministic paragraph chunks (no LLM calls)."""

from __future__ import annotations

from datetime import date
import hashlib
import json
import re
from urllib.parse import quote

from .document_identity import parse_metadata, resolve_item_identity

CHUNK_CHARS = 900  # Below the synthesis budget; HTTP previews have a separate cap.


def original_document(metadata: dict) -> tuple[str | None, dict]:
    original = parse_metadata(metadata.get("original_data")) or metadata
    for key in ("source_content", "content", "text", "正文", "原文"):
        if isinstance(original.get(key), str) and original[key].strip():
            return original[key], original
    return None, original


def document_metadata(original: dict, source_id: str, content: str | None) -> dict:
    version = original.get("document_version", original.get("version"))
    version = str(version).strip() if version is not None else None
    document_source_id = source_id + "@v:" + quote(version, safe="") if version else source_id
    meta = {
        "document_id": source_id, "document_source_id": document_source_id,
        "title": str(original.get("title") or original.get("标题") or ""),
        "content_origin": "original" if content is not None else "legacy_unverified",
    }
    if version:
        meta["document_version"] = version
    for key in ("effective_from", "effective_to"):
        if original.get(key):
            meta[key] = date.fromisoformat(str(original[key])).isoformat()
    if meta.get("effective_from") and meta.get("effective_to") and meta["effective_from"] > meta["effective_to"]:
        raise ValueError("effective_to precedes effective_from")
    for key in ("scope", "updated_at", "company_name", "企业名称"):
        if original.get(key) is not None:
            meta[key] = original[key]
    if content is not None:
        meta["source_content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return meta


def paragraph_chunks(text: str, max_chars: int = CHUNK_CHARS) -> list[tuple[int, int]]:
    """Keep paragraphs (and a Markdown heading with its paragraph) intact.

    Oversized individual clauses are rejected before any writes, rather than
    silently dropping their conditions/exceptions or pretending to parse them.
    Adjacent spans reconstruct the exact original including whitespace.
    """
    if len(text) <= max_chars:
        return [(0, len(text))]
    # CSV/Excel cells may preserve Windows CRLF. Match paragraph boundaries
    # without normalizing the source, so offsets and evidence remain verbatim.
    ends = [m.end() for m in re.finditer(r"\r?\n[ \t]*\r?\n|\r[ \t]*\r", text)] + [len(text)]
    blocks, start = [], 0
    for end in ends:
        if end > start:
            blocks.append([start, end])
        start = end
    grouped, index = [], 0
    while index < len(blocks):
        first, last = blocks[index]
        if text[first:last].strip().startswith("#") and len(text[first:last].strip().splitlines()) == 1 and index + 1 < len(blocks):
            index += 1
            last = blocks[index][1]
        if last - first > max_chars:
            raise ValueError(f"A document clause exceeds {max_chars} characters; provide paragraph/clause boundaries")
        grouped.append((first, last))
        index += 1
    chunks = []
    for first, last in grouped:
        if chunks and last - chunks[-1][0] <= max_chars:
            chunks[-1] = (chunks[-1][0], last)
        else:
            chunks.append((first, last))
    return chunks


def prepare_document_records(item: dict) -> list[dict]:
    domain, dataset_id, source_id, metadata = resolve_item_identity(item)
    raw, original = original_document(metadata)
    meta = document_metadata(original, source_id, raw)
    explicit_document = raw is not None
    if raw is None and item.get("original_data"):
        # Generic tabular records retain an exact structured snapshot, not a
        # generated prose answer. Do not infer a document version from a row.
        raw = json.dumps(item["original_data"], ensure_ascii=False, sort_keys=True, default=str)
        meta["content_origin"] = "row_snapshot"
    spans = paragraph_chunks(raw) if explicit_document else [(0, len(raw or ""))]
    parent = meta["document_source_id"]
    result = []
    for index, (first, last) in enumerate(spans):
        body = raw[first:last] if raw is not None else None
        chunk_id = parent if len(spans) == 1 else f"{parent}#chunk:{index + 1:04d}"
        chunk_meta = dict(meta, chunk_index=index, chunk_count=len(spans),
                          section_title=" / ".join(s.lstrip("# ") for s in (body or "").splitlines() if s.startswith("#")) or meta["title"],
                          start_char=first, end_char=last)
        if body is not None:
            chunk_meta["source_content_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        prepared = dict(item, domain=domain, dataset_id=dataset_id, source_id=chunk_id,
                        source_content=body, _document_meta=chunk_meta)
        if len(spans) > 1:
            # Chunk embeddings use the complete clause, with title/section as
            # retrieval context. Generated summaries never replace evidence.
            prepared["rewritten_content"] = meta["title"] + "\n" + body
        if explicit_document:
            prepared["_document_group"] = (domain, dataset_id, parent)
        result.append(prepared)
    return result


def lexical_text(text: str) -> str:
    """Shared Latin/identifier tokens and Han bigrams, without new dependencies.

    This is literal recall, not language understanding or BM25. Both indexing
    and querying use the same normalization; dense retrieval covers paraphrase.
    """
    tokens = []
    for identifier in re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", text.casefold()):
        parts = re.findall(r"[a-z0-9]+", identifier)
        tokens.extend(parts)
        if len(parts) > 1:
            tokens.append("".join(parts))
    for span in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(span[n:n + 2] for n in range(len(span) - 1))
        if len(span) == 1:
            tokens.append(span)
    return " ".join(dict.fromkeys(tokens))


def lexical_query(text: str) -> str:
    # Only normalized terms reach the FTS parser; user operators never become
    # SQL or tool-selection rules. OR gives recall before rank fusion.
    terms = lexical_text(text).split()[:64]
    return " OR ".join('"' + re.sub(r"[-_.]", " ", term) + '"' for term in terms)
