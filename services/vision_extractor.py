from __future__ import annotations

import base64
import logging
import io
import re
import zipfile
from typing import List, Optional

from services import vision_fallback_service
from services.vision_fallback_service import (
    TABLE_SCHEMA_ONLY,
    analyze_image_smart,
    analyze_image_structured,
    analyze_text_structured,
)

logger = logging.getLogger(__name__)

MAX_OPENAI_FILE_BYTES = 20 * 1024 * 1024


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


def _file_input_metadata(file_bytes: bytes) -> tuple[str, str, bool] | None:
    if file_bytes.startswith(b"%PDF"):
        return "document.pdf", "application/pdf", True
    if not file_bytes.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return None
    if any(name.startswith("xl/") for name in names):
        return (
            "document.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            False,
        )
    if any(name.startswith("word/") for name in names):
        return (
            "document.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            False,
        )
    if any(name.startswith("ppt/") for name in names):
        return (
            "document.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            False,
        )
    return None


def _extract_binary_file_with_openai(
    file_bytes: bytes,
    prompt: str,
    model: str,
) -> Optional[List[dict]]:
    metadata = _file_input_metadata(file_bytes)
    if metadata is None or not (0 < len(file_bytes) <= MAX_OPENAI_FILE_BYTES):
        return None
    filename, mime_type, is_pdf = metadata
    try:
        client = vision_fallback_service._get_openai_client()
    except vision_fallback_service.OpenAIConfigurationError:
        return None

    encoded = base64.b64encode(file_bytes).decode("ascii")
    file_part = {
        "type": "input_file",
        "filename": filename,
        "file_data": f"data:{mime_type};base64,{encoded}",
    }
    if is_pdf:
        file_part["detail"] = "auto"
    request = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    file_part,
                    {"type": "input_text", "text": _table_prompt(prompt)},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "file_table",
                "schema": TABLE_SCHEMA_ONLY,
                "strict": True,
            }
        },
        "max_output_tokens": vision_fallback_service._max_output_tokens(),
        "store": False,
    }
    safety_identifier = vision_fallback_service._privacy_safe_identifier(
        vision_fallback_service._content_safety_subject("file", file_bytes)
    )
    if safety_identifier:
        request["safety_identifier"] = safety_identifier
    try:
        response = client.responses.create(**request)
    except Exception as exc:
        logger.warning(
            "OpenAI file extraction failed model=%s error_type=%s",
            model,
            type(exc).__name__,
        )
        return None
    payload = vision_fallback_service._safe_json_loads(
        vision_fallback_service._response_output_text(response)
    )
    return _coerce_rows(payload)


def extract_table_from_file(
    file_bytes: bytes,
    prompt: str,
    model: str | None = None,
) -> Optional[List[dict]]:
    fallback_rows = _extract_with_project_fallbacks(file_bytes, prompt)
    if fallback_rows:
        return fallback_rows

    # Do not submit the same image/text a second time through another API shape.
    if _looks_like_image(file_bytes) or _decode_text(file_bytes) or _decode_pdf_text(file_bytes):
        return None

    resolved_model = model or vision_fallback_service._openai_model()
    return _extract_binary_file_with_openai(file_bytes, prompt, resolved_model)
