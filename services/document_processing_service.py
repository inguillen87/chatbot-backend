from __future__ import annotations

import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Tuple

import pdfplumber
import requests
from docx import Document

from models import ArchivoAdjunto, db
from services.llm_utils import llamar_llm_para_json_estructurado
from utils.lazy_module import LazyModule

pd = LazyModule("pandas")

logger = logging.getLogger(__name__)


_HEADER_KEYWORDS = {
    "producto",
    "descripcion",
    "descripción",
    "precio",
    "unidad",
    "cantidad",
    "stock",
    "sku",
    "codigo",
    "código",
    "marca",
    "presentacion",
    "presentación",
    "volumen",
    "item",
    "nombre",
}


class DocumentProcessingService:
    """Utility to transform raw documents into structured purchase data."""

    MAX_TEXT_CHARS = 18000
    MAX_TABLE_ROWS = 40

    def process_document(
        self,
        file_content: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> Dict[str, Any]:
        """Extract plain text and structured information from a document."""

        if not file_content:
            return {"success": False, "error": "Contenido vacío."}

        mime_type = (mime_type or "").lower()
        logger.info("Processing document with mime type %s", mime_type or "desconocido")

        extractor = None
        if "pdf" in mime_type:
            extractor = self._extract_from_pdf
        elif "spreadsheet" in mime_type or "excel" in mime_type or mime_type.endswith("csv"):
            extractor = self._extract_from_spreadsheet
        elif "word" in mime_type or mime_type.endswith("msword"):
            extractor = self._extract_from_word
        elif mime_type.startswith("text/") or mime_type in {"application/json"}:
            extractor = self._extract_from_text
        else:
            logger.warning("Unsupported MIME type for document processing: %s", mime_type)
            return {"success": False, "error": f"Tipo de archivo no soportado: {mime_type}"}

        try:
            text_content, table_records, metadata = extractor(file_content, filename)
        except Exception as exc:
            logger.warning(
                "Unable to extract document content for mime type %s: %s",
                mime_type or "desconocido",
                exc,
            )
            return {
                "success": False,
                "error": "No se pudo procesar el archivo. Verificá que no esté dañado y volvé a intentarlo.",
            }

        if not text_content and not table_records:
            return {"success": False, "error": "No se pudo extraer información del documento."}

        structured = self._build_structured_response(text_content, table_records, filename)

        return {
            "success": True,
            "texto_extraido": text_content,
            "datos_estructurados": structured,
            "metadata": metadata,
        }

    def process_document_by_id(self, archivo_id: int) -> Dict[str, Any]:
        """Helper that fetches the ``ArchivoAdjunto`` bytes and processes them."""

        archivo = db.session.get(ArchivoAdjunto, archivo_id)
        if not archivo:
            logger.error("ArchivoAdjunto with ID %s not found.", archivo_id)
            return {"success": False, "error": "Archivo no encontrado."}

        try:
            file_bytes = self._download_file_bytes(archivo)
        except Exception as exc:  # pragma: no cover - network/path errors are hard to simulate consistently
            logger.error("Unable to download file %s: %s", archivo.url, exc, exc_info=True)
            return {"success": False, "error": "No se pudo descargar el archivo para analizarlo."}

        return self.process_document(file_bytes, archivo.mime or "", archivo.nombre_original or archivo.filename)

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------
    def _extract_from_pdf(self, file_content: bytes, filename: str | None = None) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        text_chunks: List[str] = []
        table_records: List[Dict[str, Any]] = []

        with pdfplumber.open(io.BytesIO(file_content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_chunks.append(page_text.strip())

                try:
                    tables = page.extract_tables() or []
                except Exception:  # pragma: no cover
                    tables = []

                for tbl in tables:
                    table_records.extend(self._table_to_records(tbl))

        full_text = "\n\n".join(text_chunks)
        metadata = {"tables_detected": len(table_records)}
        return full_text, table_records, metadata

    def _extract_from_spreadsheet(self, file_content: bytes, filename: str | None = None) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        buffer = io.BytesIO(file_content)
        csv_text: str | None = None

        try:
            df_raw = pd.read_excel(buffer, header=None, dtype=str, keep_default_na=False)

            def loader(header: int) -> pd.DataFrame:
                inner_buffer = io.BytesIO(file_content)
                return pd.read_excel(inner_buffer, header=header, dtype=str, keep_default_na=False)

        except ValueError:
            csv_text = file_content.decode("utf-8", errors="replace")
            df_raw = pd.read_csv(
                io.StringIO(csv_text),
                header=None,
                dtype=str,
                keep_default_na=False,
                sep=None,
                engine="python",
            )

            def loader(header: int) -> pd.DataFrame:
                return pd.read_csv(
                    io.StringIO(csv_text),
                    header=header,
                    dtype=str,
                    keep_default_na=False,
                    sep=None,
                    engine="python",
                )

        header_row = self._detect_header_row(df_raw)
        df = loader(header_row)
        df = df.applymap(lambda val: val.strip() if isinstance(val, str) else val)
        df = df.replace("", pd.NA).dropna(how="all").fillna("")

        df.columns = [self._clean_header(str(col)) for col in df.columns]
        df = df.loc[:, ~df.columns.str.contains(r"^unnamed", case=False)]

        records = df.to_dict(orient="records")
        trimmed_records = records[: self.MAX_TABLE_ROWS]

        csv_buffer = io.StringIO()
        df.head(self.MAX_TABLE_ROWS).to_csv(csv_buffer, index=False)

        metadata = {
            "header_row_index": header_row,
            "columnas_detectadas": list(df.columns),
            "total_filas": len(df),
            "formato_fuente": "csv" if csv_text is not None else "excel",
        }

        return csv_buffer.getvalue(), trimmed_records, metadata

    def _extract_from_word(self, file_content: bytes, filename: str | None = None) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        document = Document(io.BytesIO(file_content))
        paragraphs = [para.text.strip() for para in document.paragraphs if para.text.strip()]
        text = "\n".join(paragraphs)
        return text, [], {}

    def _extract_from_text(self, file_content: bytes, filename: str | None = None) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        text = file_content.decode("utf-8", errors="replace")
        return text, [], {}

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _download_file_bytes(self, archivo: ArchivoAdjunto) -> bytes:
        """Download file bytes from local storage or remote URL."""

        url = archivo.url or ""
        if not url:
            raise FileNotFoundError("Archivo sin URL asociada")

        if url.startswith("http://") or url.startswith("https://"):
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            return response.content

        local_path = url
        if os.path.isabs(local_path) and os.path.exists(local_path):
            with open(local_path, "rb") as file_handle:
                return file_handle.read()

        raise FileNotFoundError(f"No se pudo ubicar el archivo en {url}")

    def _table_to_records(self, table: List[List[Any]]) -> List[Dict[str, Any]]:
        if not table or len(table) < 2:
            return []

        headers = [self._clean_header(str(cell)) for cell in table[0]]
        records: List[Dict[str, Any]] = []
        for row in table[1:]:
            record = {}
            for idx, cell in enumerate(row):
                header = headers[idx] if idx < len(headers) else f"columna_{idx}"
                value = "" if cell is None else str(cell).strip()
                record[header] = value
            if any(record.values()):
                records.append(record)
        return records

    def _clean_header(self, header: str) -> str:
        header = header.strip()
        header = header.replace("\n", " ")
        header = re.sub(r"\s+", " ", header)
        return header.lower()

    def _detect_header_row(self, df: pd.DataFrame) -> int:
        best_row = 0
        best_score = float("-inf")

        for idx, row in df.iterrows():
            score = 0.0
            for cell in row.tolist():
                value = str(cell).strip()
                if not value or value.lower() in {"nan", "none"}:
                    continue
                score += 1.0
                if any(keyword in value.lower() for keyword in _HEADER_KEYWORDS):
                    score += 2.0
                if re.match(r"^[0-9.,]+$", value):
                    score -= 0.3
            if score > best_score:
                best_score = score
                best_row = idx

        return int(best_row)

    def _build_structured_response(
        self,
        text_content: str,
        table_records: List[Dict[str, Any]],
        filename: str | None,
    ) -> Dict[str, Any] | None:
        if not text_content and not table_records:
            return None

        trimmed_text = (text_content or "")[: self.MAX_TEXT_CHARS]
        records_json = json.dumps(table_records[: self.MAX_TABLE_ROWS], ensure_ascii=False)

        system_prompt = (
            "Eres un asistente experto en interpretar documentos comerciales de pymes. "
            "Debes producir JSON estricto que describa pedidos o catálogos."
        )

        user_prompt = (
            "Analiza la información extraída de un documento (catálogo, nota de pedido, factura o lista de precios).\n"
            "Genera un JSON con esta estructura exacta:\n"
            "{\n"
            "  \"resumen\": string,\n"
            "  \"items\": [\n"
            "    {\n"
            "      \"nombre\": string,\n"
            "      \"descripcion\": string,\n"
            "      \"unidad\": string,\n"
            "      \"cantidad\": string,\n"
            "      \"precio_unitario\": string,\n"
            "      \"moneda\": string,\n"
            "      \"subtotal_estimado\": string\n"
            "    }\n"
            "  ],\n"
            "  \"totales\": {\"moneda\": string, \"total_estimado\": string},\n"
            "  \"contacto\": {\"nombre\": string, \"telefono\": string, \"email\": string}\n"
            "}\n"
            "Usa cadenas vacías si un dato no aparece. No inventes información.\n"
            f"Nombre del archivo (si disponible): {filename or 'desconocido'}.\n"
            "Texto extraído (recortado si es largo):\n"
            f'"""{trimmed_text}"""\n\n'
            "Filas detectadas (formato JSON):\n"
            f"{records_json}\n"
            "Devuelve solamente el JSON final."
        )

        llm_response = llamar_llm_para_json_estructurado(system_prompt, user_prompt)
        if isinstance(llm_response, dict):
            return llm_response
        return None


document_processing_service = DocumentProcessingService()
