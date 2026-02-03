import io
from typing import Any, List

import pandas as pd
import pdfplumber
from flask import Blueprint, jsonify, request

from routes.auth import token_requerido
from services.vision_fallback_service import analyze_image_structured


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
                page = pdf.pages[0]
                table_data = None
                for page in pdf.pages[:2]:
                    table = page.extract_table(
                        {
                            "vertical_strategy": "lines",
                            "horizontal_strategy": "lines",
                            "snap_tolerance": 3,
                            "join_tolerance": 3,
                        }
                    )
                    if table:
                        table_data = table
                        break
                    tables = page.extract_tables() or []
                    if tables:
                        table_data = max(tables, key=len)
                        break

                if table_data:
                    headers = []
                    rows = []
                    header_index = None
                    header_tokens = ("marca", "varietal", "precio", "caja", "botella", "pallet")
                    for idx, row in enumerate(table_data):
                        joined = " ".join(str(cell or "").lower() for cell in row)
                        if any(token in joined for token in header_tokens):
                            header_index = idx
                            break

                    if header_index is None:
                        header_index = 0

                    headers = [str(cell or "").strip() for cell in table_data[header_index]]
                    rows = table_data[header_index + 1 :]

                    if not any(headers):
                        headers = [f"Columna {i+1}" for i in range(len(rows[0]))] if rows else []

                    df = pd.DataFrame(rows, columns=headers)
                if df is None or df.empty:
                    try:
                        image = page.to_image(resolution=300).original
                        buffer = io.BytesIO()
                        image.save(buffer, format="JPEG")
                        prompt = (
                            "Extrae la tabla del catálogo en JSON con claves "
                            "'columns' (lista de strings) y 'rows' (lista de objetos con esas columnas). "
                            "Incluye columnas como Marca, Varietal, Unidades/Caja, Pallet, "
                            "Precio Caja, Precio Botella, Sugerido Público si están presentes. "
                            "No inventes datos, deja vacío si no se ve."
                        )
                        vision = analyze_image_structured(buffer.getvalue(), prompt)
                        if isinstance(vision, dict) and vision.get("rows") and vision.get("columns"):
                            df = pd.DataFrame(vision["rows"], columns=vision["columns"])
                    except Exception:
                        df = None

                if df is None or df.empty:
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
