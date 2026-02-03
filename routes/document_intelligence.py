import io
from typing import Any, List, Optional

import pandas as pd
import pdfplumber
from flask import Blueprint, jsonify, request

from routes.auth import token_requerido
from services.vision_fallback_service import analyze_image_structured, analyze_text_structured


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


def _looks_like_flat_pdf_table(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return False

    column_count = len(df.columns)
    if column_count >= 3:
        return False

    sample_rows = df.head(6).fillna("")
    header_tokens = ("marca", "varietal", "precio", "caja", "botella", "pallet", "unidad")
    token_hits = 0
    long_row_hits = 0
    mostly_empty_second_column = False
    if column_count == 2:
        second_values = sample_rows.iloc[:, 1].astype(str).str.strip()
        empty_ratio = (second_values == "").mean() if len(second_values) else 0
        mostly_empty_second_column = empty_ratio >= 0.6
    for _, row in sample_rows.iterrows():
        cell_text = " ".join(str(value) for value in row if value is not None).strip()
        if not cell_text:
            continue
        lowered = cell_text.lower()
        token_hits += sum(token in lowered for token in header_tokens)
        if len(cell_text) > 80:
            long_row_hits += 1

    flat_by_tokens = token_hits >= 2 or long_row_hits >= 2
    if column_count <= 1:
        return flat_by_tokens
    return mostly_empty_second_column and flat_by_tokens


def _looks_like_placeholder_columns(columns: List[Any]) -> bool:
    if not columns:
        return True
    normalized = [str(col or "").strip().lower() for col in columns]
    if all(not col for col in normalized):
        return True
    placeholder_hits = 0
    for col in normalized:
        if col.startswith("col_") or col.startswith("columna") or col.startswith("column"):
            placeholder_hits += 1
    return placeholder_hits >= max(1, int(len(normalized) * 0.6))


def _build_df_from_vision(vision: dict) -> Optional[pd.DataFrame]:
    if not isinstance(vision, dict):
        return None
    columns = vision.get("columns") or []
    rows = vision.get("rows") or []
    if not rows:
        return None
    if rows and isinstance(rows[0], dict):
        df = pd.DataFrame(rows)
        if columns:
            df = df.reindex(columns=columns)
        return df
    if columns:
        return pd.DataFrame(rows, columns=columns)
    return pd.DataFrame(rows)


@document_intelligence_bp.route("/preview", methods=["OPTIONS"])
def document_intelligence_preview_options(pyme_id: int):
    """Handle CORS preflight requests for the preview endpoint."""

    return "", 204


def _catalog_llm_prompt() -> str:
    return (
        "Extrae la tabla del catálogo en JSON con claves "
        "'columns' (lista de strings) y 'rows' (lista de listas ordenadas según columns). "
        "Incluye columnas como Marca, Varietal, Unidades/Caja, Pallet, "
        "Precio Caja, Precio Botella, Sugerido Público si están presentes. "
        "No inventes datos, deja vacío si no se ve. "
        "Mantén los valores numéricos tal como aparecen (puntos para miles, comas decimales)."
    )


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
                page_for_image = pdf.pages[0]
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
                        page_for_image = page
                        break
                    tables = page.extract_tables() or []
                    if tables:
                        table_data = max(tables, key=len)
                        page_for_image = page
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
                try:
                    image = page_for_image.to_image(resolution=300).original
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG")
                    vision = analyze_image_structured(buffer.getvalue(), _catalog_llm_prompt())
                    vision_df = _build_df_from_vision(vision)
                    if vision_df is not None and not vision_df.empty:
                        df = vision_df
                except Exception:
                    pass

                use_pdf_fallback = (
                    df is None
                    or df.empty
                    or _looks_like_flat_pdf_table(df)
                    or _looks_like_placeholder_columns(list(df.columns) if df is not None else [])
                )
                if use_pdf_fallback:
                    df = df if df is not None and not df.empty else None

                if df is None or df.empty:
                    text = page_for_image.extract_text() or ""
                    structured = analyze_text_structured(text, _catalog_llm_prompt())
                    structured_df = _build_df_from_vision(structured)
                    if structured_df is not None and not structured_df.empty:
                        df = structured_df
                    else:
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
    csv_sample = preview_df.to_csv(index=False)
    structured = analyze_text_structured(csv_sample, _catalog_llm_prompt())
    structured_df = _build_df_from_vision(structured)
    if structured_df is not None and not structured_df.empty:
        preview_df = structured_df.head(max_rows).fillna("")

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
