import re
from typing import Any, Dict

import pandas as pd

from .base import BaseExtractor
from services.open_source_document_intelligence import extract_document


def _split_markdown_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def parse_markdown_tables(markdown: str) -> list[pd.DataFrame]:
    tables: list[pd.DataFrame] = []
    current: list[list[str]] = []

    def flush_current() -> None:
        nonlocal current
        if len(current) < 2:
            current = []
            return
        header = current[0]
        rows = [row for row in current[1:] if not _is_separator_row(row)]
        if rows and len(header) >= 2:
            normalized_rows = []
            for row in rows:
                if len(row) < len(header):
                    row = row + [""] * (len(header) - len(row))
                normalized_rows.append(row[: len(header)])
            tables.append(pd.DataFrame(normalized_rows, columns=header))
        current = []

    for raw_line in (markdown or "").splitlines():
        line = raw_line.strip()
        if "|" not in line:
            flush_current()
            continue
        cells = _split_markdown_row(line)
        if len(cells) < 2:
            flush_current()
            continue
        current.append(cells)

    flush_current()
    return tables


class DoclingExtractor(BaseExtractor):
    def extract(self, file_content: bytes, filename: str, **kwargs) -> Dict[str, Any]:
        doc = extract_document(file_content, filename)
        if not doc:
            return {
                "columns": [],
                "rows": [],
                "confidence": 0.0,
                "warnings": ["Docling disabled or unsupported file"],
                "metadata": {},
            }

        warnings = list(doc.get("warnings") or [])
        markdown = doc.get("markdown") or doc.get("text") or ""
        tables = parse_markdown_tables(markdown)
        metadata = dict(doc.get("metadata") or {})
        metadata.update({"docling_status": doc.get("status"), "table_count": len(tables)})

        if not tables:
            return {
                "columns": [],
                "rows": [],
                "confidence": 0.0,
                "warnings": warnings + ["Docling no detecto tablas Markdown"],
                "metadata": metadata,
            }

        df = pd.concat(tables, ignore_index=True)
        normalized = self.normalize_dataframe(df)
        normalized["confidence"] = 0.82
        normalized["warnings"] = warnings
        normalized["metadata"] = {**normalized.get("metadata", {}), **metadata}
        return normalized
