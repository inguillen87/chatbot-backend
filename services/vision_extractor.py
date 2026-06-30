from __future__ import annotations

import json
import logging
import io
import re
from typing import List, Optional

import openai

from services.vision_fallback_service import analyze_image_smart, analyze_image_structured, analyze_text_structured

logger = logging.getLogger(__name__)


_IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",  # jpeg
    b"\x89PNG\r\n\x1a\n",
    b"RIFF",  # webp starts with RIFF....WEBP
)
_BINARY_DOCUMENT_SIGNATURES = (
    b"%PDF",
    b"PK\x03\x04",  # docx, xlsx, odt and other zip-based office files
    b"\xd0\xcf\x11\xe0",  # legacy Office compound documents
)


def _client() -> object:
    ctor = getattr(openai, "OpenAI", None)
    if ctor:
        return ctor()
    return openai


def _looks_like_image(file_bytes: bytes) -> bool:
    if not file_bytes:
        return False
    if file_bytes.startswith(b"RIFF") and b"WEBP" in file_bytes[:16]:
        return True
    return any(file_bytes.startswith(signature) for signature in _IMAGE_SIGNATURES if signature != b"RIFF")


def _decode_text(file_bytes: bytes) -> Optional[str]:
    if not file_bytes:
        return None
    if any(file_bytes.startswith(signature) for signature in _BINARY_DOCUMENT_SIGNATURES):
        return None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = file_bytes.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
        if text and sum(ch.isprintable() or ch.isspace() for ch in text) / max(len(text), 1) > 0.86:
            return text
    return None


def _unescape_pdf_literal(value: str) -> str:
    value = value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
    value = value.replace(r"\n", "\n").replace(r"\r", "\n").replace(r"\t", "\t")
    return value


def _clean_pdf_text_candidate(text: str) -> Optional[str]:
    cleaned = re.sub(r"%PDF-[^\r\n]*", " ", text)
    cleaned = re.sub(r"\b(?:obj|endobj|stream|endstream|xref|trailer|startxref)\b", " ", cleaned)
    cleaned = re.sub(r"[/<>[\]{}]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 6:
        return None
    alpha_count = sum(ch.isalpha() for ch in cleaned)
    if alpha_count < 4:
        return None
    return cleaned


def _decode_pdf_text(file_bytes: bytes) -> Optional[str]:
    if not file_bytes.startswith(b"%PDF"):
        return None

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(file_bytes))  # type: ignore[name-defined]
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        cleaned = _clean_pdf_text_candidate(extracted)
        if cleaned:
            return cleaned
    except Exception:
        pass

    raw = file_bytes.decode("latin-1", errors="ignore")
    literal_chunks = [
        _unescape_pdf_literal(match)
        for match in re.findall(r"\(((?:\\.|[^\\()]){2,})\)\s*(?:Tj|TJ|'|\")", raw)
    ]
    if literal_chunks:
        cleaned = _clean_pdf_text_candidate("\n".join(chunk.strip() for chunk in literal_chunks))
        if cleaned:
            return cleaned

    return _clean_pdf_text_candidate(raw)


def _normalize_row(row: object, columns: list[str] | None = None) -> Optional[dict]:
    if isinstance(row, dict):
        return {str(key): value for key, value in row.items() if value is not None}
    if isinstance(row, (list, tuple)):
        if columns:
            return {
                str(columns[index] if index < len(columns) else f"col_{index + 1}"): value
                for index, value in enumerate(row)
                if value is not None
            }
        return {f"col_{index + 1}": value for index, value in enumerate(row) if value is not None}
    if row is None:
        return None
    label = str(row).strip()
    return {"nombre": label, "cantidad": 1} if label else None


def _coerce_rows(payload: object) -> Optional[List[dict]]:
    if not payload:
        return None

    if isinstance(payload, dict):
        rows = payload.get("items") or payload.get("productos") or payload.get("rows") or payload.get("data")
        columns = payload.get("columns") if isinstance(payload.get("columns"), list) else None
    else:
        rows = payload
        columns = None

    if not isinstance(rows, list):
        return None

    normalized = [_normalize_row(row, columns) for row in rows]
    cleaned = [row for row in normalized if row]
    return cleaned or None


def _table_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "Devuelve un JSON con `columns` y `rows`. "
        "Usa columnas como sku, nombre, cantidad, unidad, descripcion, direccion, concepto, importe o vencimiento "
        "segun corresponda. No inventes datos; si un renglon es dudoso, mantenelo como nombre o descripcion para revision humana."
    )


def _looks_like_delimited_table(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()][:4]
    if len(lines) < 2:
        return False
    delimiters = (",", ";", "\t", "|")
    return any(sum(line.count(delimiter) for line in lines) >= len(lines) for delimiter in delimiters)


def _rows_from_plain_text(text: str) -> Optional[List[dict]]:
    if not text or _looks_like_delimited_table(text):
        return None
    rows = []
    for line in text.splitlines()[:50]:
        cleaned = line.strip(" -\t")
        if not cleaned:
            continue
        match = re.match(r"^(?P<cantidad>\d+(?:[\.,]\d+)?)\s+(?P<nombre>.+)$", cleaned)
        if match:
            rows.append(
                {
                    "nombre": match.group("nombre").strip(),
                    "cantidad": match.group("cantidad").replace(",", "."),
                }
            )
            continue
        rows.append({"nombre": cleaned, "cantidad": 1})
    return rows or None


def _extract_with_project_fallbacks(file_bytes: bytes, prompt: str) -> Optional[List[dict]]:
    structured_prompt = _table_prompt(prompt)

    if _looks_like_image(file_bytes):
        structured = analyze_image_structured(file_bytes, structured_prompt)
        rows = _coerce_rows(structured)
        if rows:
            return rows

        smart_result = analyze_image_smart(file_bytes, prompt=structured_prompt)
        ocr_text = ((smart_result or {}).get("full_text_annotation") or {}).get("description")
        if isinstance(ocr_text, str) and ocr_text.strip():
            structured_from_text = analyze_text_structured(ocr_text, structured_prompt)
            rows = _coerce_rows(structured_from_text) or _rows_from_plain_text(ocr_text)
            if rows:
                return rows

    decoded_text = _decode_text(file_bytes)
    if decoded_text:
        structured_from_text = analyze_text_structured(decoded_text, structured_prompt)
        rows = _coerce_rows(structured_from_text) or _rows_from_plain_text(decoded_text)
        if rows:
            return rows

    pdf_text = _decode_pdf_text(file_bytes)
    if pdf_text:
        structured_from_text = analyze_text_structured(pdf_text, structured_prompt)
        rows = _coerce_rows(structured_from_text) or _rows_from_plain_text(pdf_text)
        if rows:
            return rows

    return None


def extract_table_from_file(file_bytes: bytes, prompt: str, model: str = "gpt-4.1-mini") -> Optional[List[dict]]:
    fallback_rows = _extract_with_project_fallbacks(file_bytes, prompt)
    if fallback_rows:
        return fallback_rows

    try:
        client = _client()
        response = client.responses.create(  # type: ignore[attr-defined]
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "input_file", "input_file": file_bytes},
                    ],
                }
            ],
            format={"type": "json_object"},
        )
        message = response.output[0].content[0].text  # type: ignore[index]

        data = json.loads(message)
        rows = _coerce_rows(data)
        if rows:
            return rows
    except Exception as exc:  # pragma: no cover - best effort
        logger.error("Error usando OpenAI Vision: %s", exc)
    return None
