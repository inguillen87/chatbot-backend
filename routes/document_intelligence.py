import io
from typing import Any, List

import pandas as pd
import pdfplumber
from flask import Blueprint, jsonify, request

from routes.auth import token_requerido


document_intelligence_bp = Blueprint(
    "document_intelligence",
    __name__,
    url_prefix="/api/pymes/<int:pyme_id>/document-intelligence",
)
document_intelligence_public_bp = Blueprint(
    "document_intelligence_public",
    __name__,
    url_prefix="/pymes/<int:pyme_id>/document-intelligence",
)


def _build_columns(columns: List[Any]) -> List[dict[str, str]]:
    parsed: List[dict[str, str]] = []
    for col in columns:
        name = "" if col is None else str(col)
        parsed.append({"key": name, "name": name})
    return parsed


@document_intelligence_bp.route("/preview", methods=["OPTIONS"])
def document_intelligence_preview_options(pyme_id: int):
    """Handle CORS preflight requests for the preview endpoint."""

    return "", 204


def _document_intelligence_preview(current_user, pyme_id: int):
    """Return a lightweight preview of the uploaded spreadsheet or CSV file."""

    # Relax check to allow pyme_id=0 (generic context) or ID match
    if pyme_id != 0 and getattr(current_user, "id", None) != pyme_id:
        return (
            jsonify({"error": "Solo podés previsualizar archivos de tu propia PYME."}),
            403,
        )

    uploaded = request.files.get("file") or request.files.get("archivo")
    if not uploaded:
        return jsonify({"error": "Archivo requerido."}), 400

    content = uploaded.read()
    df = None

    sheet = request.form.get("sheet")
    header_row = request.form.get("headerRow", type=int)
    header_index = 0 if header_row is None else header_row

    filename = uploaded.filename.lower() if uploaded.filename else ""

    if filename.endswith(".pdf"):
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                # Intenta extraer tablas de la primera página
                page = pdf.pages[0]
                tables = page.extract_tables()

                if tables and tables[0]:
                    # Usar la primera tabla encontrada
                    table_data = tables[0]
                    # Asumir que la primera fila es el encabezado si hay más de 1 fila
                    if len(table_data) > 1:
                        headers = table_data[0]
                        rows = table_data[1:]
                    else:
                        headers = [f"Columna {i+1}" for i in range(len(table_data[0]))]
                        rows = table_data

                    df = pd.DataFrame(rows, columns=headers)
                else:
                    # Si no hay tablas, extraer texto simple
                    text = page.extract_text() or ""
                    # Crear un DF dummy con el texto
                    df = pd.DataFrame([{"Contenido": line} for line in text.split('\n') if line.strip()])

        except Exception as exc:
            return (
                jsonify({
                    "error": "No se pudo procesar el PDF.",
                    "details": str(exc),
                }),
                400,
            )
    else:
        try:
            df = pd.read_excel(io.BytesIO(content), sheet_name=sheet, header=header_index)
        except Exception:
            try:
                df = pd.read_csv(io.BytesIO(content), header=header_index)
            except Exception as exc:
                return (
                    jsonify({
                        "error": "No se pudo leer el archivo. Usa CSV, Excel o PDF.",
                        "details": str(exc),
                    }),
                    400,
                )

    df = df.dropna(how="all")

    max_rows = request.form.get("maxRows", type=int) or 50
    preview_df = df.head(max_rows).fillna("")

    # Ensure all column names are strings to avoid JSON serialization issues (e.g. sorting keys)
    preview_df.columns = preview_df.columns.map(lambda x: str(x) if x is not None else "")

    # Deduplicate columns to avoid UserWarning and potential serialization issues
    preview_df = preview_df.loc[:, ~preview_df.columns.duplicated()]

    # Fill NaN/None values in the data to ensure clean JSON
    preview_df = preview_df.fillna("")

    response_payload = {
        "pymeId": pyme_id,
        "totalRows": int(len(df.index)),
        "columns": _build_columns(list(preview_df.columns)),
        "rows": preview_df.to_dict(orient="records"),
    }

    if sheet:
        response_payload["sheetName"] = sheet

    if header_row is not None:
        response_payload["headerRow"] = header_row

    return jsonify(response_payload)


@document_intelligence_bp.route("/preview", methods=["POST"])
@token_requerido
def document_intelligence_preview(current_user, pyme_id: int):
    return _document_intelligence_preview(current_user, pyme_id)


@document_intelligence_public_bp.route("/preview", methods=["OPTIONS"])
def document_intelligence_preview_options_public(pyme_id: int):
    """Public alias for OPTIONS preflight when hitting /pymes/... paths."""

    return "", 204


@document_intelligence_public_bp.route("/preview", methods=["POST"])
@token_requerido
def document_intelligence_preview_public(current_user, pyme_id: int):
    """Public alias that reuses the API handler for /pymes/... requests."""

    return _document_intelligence_preview(current_user, pyme_id)

