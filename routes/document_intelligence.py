import io
from typing import Any, List

import pandas as pd
from flask import Blueprint, jsonify, request

from routes.auth import token_requerido


document_intelligence_bp = Blueprint(
    "document_intelligence",
    __name__,
    url_prefix="/api/pymes/<int:pyme_id>/document-intelligence",
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


@document_intelligence_bp.route("/preview", methods=["POST"])
@token_requerido
def document_intelligence_preview(current_user, pyme_id: int):
    """Return a lightweight preview of the uploaded spreadsheet or CSV file."""

    if getattr(current_user, "id", None) != pyme_id:
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

    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name=sheet, header=header_index)
    except Exception:
        try:
            df = pd.read_csv(io.BytesIO(content), header=header_index)
        except Exception as exc:
            return (
                jsonify({
                    "error": "No se pudo leer el archivo. Usa CSV o Excel.",
                    "details": str(exc),
                }),
                400,
            )

    df = df.dropna(how="all")

    max_rows = request.form.get("maxRows", type=int) or 50
    preview_df = df.head(max_rows).fillna("")

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

