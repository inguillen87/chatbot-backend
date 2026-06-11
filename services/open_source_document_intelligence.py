import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".html",
    ".htm",
    ".md",
    ".txt",
}


def _truthy_env(*names: str) -> bool:
    return any(str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"} for name in names)


def docling_enabled() -> bool:
    return _truthy_env("DOCLING_ENABLED", "OPEN_SOURCE_DOCUMENT_AI_ENABLED")


def _max_file_bytes() -> int:
    try:
        mb = float(os.getenv("DOCLING_MAX_FILE_MB", "15"))
    except ValueError:
        mb = 15.0
    return int(max(mb, 1) * 1024 * 1024)


def _extension(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _get_docling_converter():
    try:
        from docling.document_converter import DocumentConverter
    except Exception as exc:  # pragma: no cover - optional dependency
        raise ConnectionError("Docling is not installed. Set INSTALL_OPEN_SOURCE_AI_EXTRAS=true in the build.") from exc
    return DocumentConverter()


def _export_document(document: Any) -> tuple[str, str]:
    markdown = ""
    plain_text = ""

    if hasattr(document, "export_to_markdown"):
        markdown = document.export_to_markdown() or ""
    elif hasattr(document, "export_to_text"):
        markdown = document.export_to_text() or ""

    if hasattr(document, "export_to_text"):
        plain_text = document.export_to_text() or ""

    if not plain_text:
        plain_text = markdown

    return markdown.strip(), plain_text.strip()


def extract_document(file_content: bytes, filename: str) -> Optional[dict]:
    """Extract document text/markdown using Docling when explicitly enabled."""

    if not docling_enabled():
        return None

    ext = _extension(filename)
    if ext not in SUPPORTED_EXTENSIONS:
        return None

    if not file_content:
        return None

    if len(file_content) > _max_file_bytes():
        logger.warning("Docling skipped for %s: file exceeds DOCLING_MAX_FILE_MB", filename)
        return {
            "engine": "docling",
            "status": "skipped",
            "warnings": ["Archivo supera DOCLING_MAX_FILE_MB"],
            "markdown": "",
            "text": "",
            "metadata": {"filename": filename, "extension": ext},
        }

    temp_path = None
    try:
        converter = _get_docling_converter()
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(file_content)
            temp_path = tmp.name

        result = converter.convert(temp_path)
        document = getattr(result, "document", result)
        markdown, text = _export_document(document)

        if not markdown and not text:
            return {
                "engine": "docling",
                "status": "empty",
                "warnings": ["Docling no devolvio texto util"],
                "markdown": "",
                "text": "",
                "metadata": {"filename": filename, "extension": ext},
            }

        return {
            "engine": "docling",
            "status": "ok",
            "warnings": [],
            "markdown": markdown,
            "text": text,
            "metadata": {
                "filename": filename,
                "extension": ext,
                "text_length": len(text),
                "markdown_length": len(markdown),
            },
        }
    except Exception as exc:
        logger.error("Docling extraction failed for %s: %s", filename, exc, exc_info=True)
        return {
            "engine": "docling",
            "status": "failed",
            "warnings": [f"Docling failed: {exc}"],
            "markdown": "",
            "text": "",
            "metadata": {"filename": filename, "extension": ext},
            "error": str(exc),
        }
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
