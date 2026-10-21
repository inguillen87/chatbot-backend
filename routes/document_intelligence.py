from __future__ import annotations

import io
import logging
import uuid
from typing import Any, List, Optional

from flask import Blueprint, jsonify, request

from models import CatalogUpload, db
from routes.auth import token_requerido
from services.vision_fallback_service import (
    analyze_image_structured,
    analyze_image_text,
    analyze_text_structured,
)
from utils.lazy_module import LazyModule

pd = LazyModule("pandas")
pdfplumber = LazyModule("pdfplumber")

logger = logging.getLogger(__name__)


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


def _merge_dataframes(dfs: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if not dfs:
        return None
    columns: List[str] = []
    for df in dfs:
        if df is None or df.empty:
            continue
        for col in df.columns:
            col_str = str(col)
            if col_str not in columns:
                columns.append(col_str)
    if not columns:
        return None
    normalized = []
    for df in dfs:
        if df is None or df.empty:
            continue
        normalized.append(df.reindex(columns=columns))
    if not normalized:
        return None
    return pd.concat(normalized, ignore_index=True)


def _catalog_upload_from_request(upload_id: Optional[int]) -> Optional[CatalogUpload]:
    if not upload_id:
        return None
    return CatalogUpload.query.filter_by(id=upload_id).first()


def _read_tabular_file(
    content: bytes,
    filename: str,
    sheet: Optional[str],
    header_index: int,
) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    if filename.endswith(".csv") or filename.endswith(".txt"):
        return pd.read_csv(io.BytesIO(content), header=header_index), None

    engine = None
    if filename.endswith(".xls"):
        engine = "xlrd"
    elif filename.endswith((".xlsx", ".xlsm")):
        engine = "openpyxl"

    sheet_name = sheet if sheet is not None else 0
    try:
        df = pd.read_excel(
            io.BytesIO(content),
            sheet_name=sheet_name,
            header=header_index,
            engine=engine,
        )
        if isinstance(df, dict):
            df = next(iter(df.values()), pd.DataFrame())
        return df, None
    except Exception:
        try:
            return pd.read_csv(io.BytesIO(content), header=header_index), None
        except Exception as exc:
            return None, str(exc)


@document_intelligence_bp.route("/preview", methods=["OPTIONS"])
def document_intelligence_preview_options(pyme_id: int):
    """Handle CORS preflight requests for the preview endpoint."""

    return "", 204


def _catalog_llm_prompt(rubro: Optional[str] = None) -> str:
    rubro_hint = f"Rubro sugerido: {rubro}. " if rubro else ""
    return (
        "Extrae la tabla del catálogo en JSON con claves "
        "'columns' (lista de strings) y 'rows' (lista de listas ordenadas según columns). "
        "Detecta los encabezados reales del documento (no fuerces columnas fijas) y "
        "respeta el orden original. "
        f"{rubro_hint}"
        "Si el catálogo trae columnas de marca, presentación, variedad, unidades, medidas, "
        "precio unitario, precio por caja, moneda, SKU, stock u otros campos, inclúyelos tal cual. "
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

    rubro_hint = request.form.get("rubro") or request.form.get("rubroSlug")

    is_pdf = filename.endswith(".pdf")

    structured_attempts = 0
    structured_failures = 0
    debug_id = str(uuid.uuid4())

    if is_pdf:
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                max_pages = request.form.get("maxPages", type=int) or 3
                vision_tables: List[pd.DataFrame] = []
                text_tables: List[pd.DataFrame] = []
                table_tables: List[pd.DataFrame] = []
                ocr_texts: List[str] = []

                for page in pdf.pages[:max_pages]:
                    try:
                        image = page.to_image(resolution=300).original
                        buffer = io.BytesIO()
                        image.save(buffer, format="JPEG")
                        structured_attempts += 1
                        vision = analyze_image_structured(
                            buffer.getvalue(),
                            _catalog_llm_prompt(rubro_hint),
                        )
                        vision_df = _build_df_from_vision(vision)
                        if vision_df is not None and not vision_df.empty:
                            vision_tables.append(vision_df)
                        else:
                            structured_failures += 1
                            ocr_text = analyze_image_text(buffer.getvalue())
                            if ocr_text:
                                ocr_texts.append(ocr_text)
                                structured_attempts += 1
                                structured = analyze_text_structured(ocr_text, _catalog_llm_prompt(rubro_hint))
                                structured_df = _build_df_from_vision(structured)
                                if structured_df is not None and not structured_df.empty:
                                    text_tables.append(structured_df)
                                else:
                                    structured_failures += 1
                    except Exception:
                        pass

                    try:
                        table_data = page.extract_table(
                            {
                                "vertical_strategy": "lines",
                                "horizontal_strategy": "lines",
                                "snap_tolerance": 3,
                                "join_tolerance": 3,
                            }
                        )
                        if table_data:
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
                            table_df = pd.DataFrame(rows, columns=headers)
                            if table_df is not None and not table_df.empty:
                                table_tables.append(table_df)
                        else:
                            tables = page.extract_tables() or []
                            if tables:
                                table_data = max(tables, key=len)
                                if table_data:
                                    headers = [str(cell or "").strip() for cell in table_data[0]]
                                    rows = table_data[1:]
                                    table_df = pd.DataFrame(rows, columns=headers)
                                    if table_df is not None and not table_df.empty:
                                        table_tables.append(table_df)
                    except Exception:
                        pass

                    try:
                        text = page.extract_text() or ""
                        if text.strip():
                            structured_attempts += 1
                            structured = analyze_text_structured(text, _catalog_llm_prompt(rubro_hint))
                            structured_df = _build_df_from_vision(structured)
                            if structured_df is not None and not structured_df.empty:
                                text_tables.append(structured_df)
                            else:
                                structured_failures += 1
                    except Exception:
                        pass

                df = _merge_dataframes(vision_tables)
                if df is None or df.empty:
                    df = _merge_dataframes(table_tables)
                if df is None or df.empty:
                    df = _merge_dataframes(text_tables)
                if df is None or df.empty:
                    if structured_attempts > 0 and structured_attempts == structured_failures:
                        return (
                            jsonify({
                                "ok": False,
                                "error": "preview_failed",
                                "detail": "No se pudo interpretar el PDF con IA.",
                                "debug_id": debug_id,
                                "actions": ["retry_ocr", "download_template", "open_manual_editor"],
                            }),
                            422,
                        )
                    fallback_text = ""
                    if ocr_texts:
                        fallback_text = "\n".join(ocr_texts)
                    elif pdf.pages:
                        fallback_text = pdf.pages[0].extract_text() or ""
                    df = pd.DataFrame(
                        [{"Contenido": line} for line in fallback_text.split("\n") if line.strip()]
                    )

        except Exception:
            df = pd.DataFrame()
    else:
        df, error = _read_tabular_file(content, filename, sheet, header_index)
        if error:
            return (
                jsonify({
                    "error": "No se pudo leer el archivo. Usa CSV, Excel o PDF.",
                    "details": error,
                }),
                400,
            )

    df = df if df is not None else pd.DataFrame()
    df = df.dropna(how="all")

    max_rows = request.form.get("maxRows", type=int) or 50
    preview_df = df.head(max_rows).fillna("")
    if (
        not preview_df.empty
        and len(preview_df.columns) > 0
        and is_pdf
    ):
        csv_sample = preview_df.to_csv(index=False)
        structured_attempts += 1
        structured = analyze_text_structured(csv_sample, _catalog_llm_prompt(rubro_hint))
        structured_df = _build_df_from_vision(structured)
        if structured_df is not None and not structured_df.empty:
            preview_df = structured_df.head(max_rows).fillna("")
        else:
            structured_failures += 1
            return (
                jsonify({
                    "ok": False,
                    "error": "preview_failed",
                    "detail": "No se pudo interpretar la tabla con IA.",
                    "debug_id": debug_id,
                    "actions": ["retry_ocr", "download_template", "open_manual_editor"],
                }),
                422,
            )

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

    upload_id = request.form.get("catalogUploadId", type=int)
    upload_rec = _catalog_upload_from_request(upload_id)
    if upload_rec:
        upload_rec.preview_data = response_payload
        upload_rec.stats = {
            "rows": int(len(df.index)),
            "preview_rows": int(len(preview_df.index)),
        }
        upload_rec.status = "preview_ready"
        db.session.commit()

    return jsonify(response_payload)


@document_intelligence_bp.route("/preview", methods=["POST"])
@token_requerido
def document_intelligence_preview(current_user, pyme_id: int):
    return _document_intelligence_preview(current_user, pyme_id)


@document_intelligence_bp.route("/commit", methods=["POST"])
@token_requerido
def document_intelligence_commit(current_user, pyme_id: int):
    if pyme_id != 0 and getattr(current_user, "id", None) != pyme_id:
        return (
            jsonify({"error": "Solo podés confirmar catálogos de tu propia PYME."}),
            403,
        )

    # This legacy commit path could replace SQL rows before durable storage and
    # vector readback, and it had no exact tenant authorization contract.  Keep
    # preview available, but route all publication through the active,
    # tenant-scoped import workflow.
    return (
        jsonify(
            {
                "contract_version": "document_intelligence.commit_retired.v1",
                "codigo": "document_intelligence_catalog_commit_retired",
                "mensaje": "Confirmá el catálogo desde la importación administrada.",
                "writes_performed": False,
                "replacement": {
                    "create": "/api/admin/catalog/import",
                    "commit": "/api/admin/catalog/import/{upload_id}/commit",
                },
            }
        ),
        410,
    )


@document_intelligence_public_bp.route("/preview", methods=["OPTIONS"])
def document_intelligence_preview_options_public(pyme_id: int):
    """Public alias for OPTIONS preflight when hitting /pymes/... paths."""

    return "", 204


@document_intelligence_public_bp.route("/preview", methods=["POST"])
@token_requerido
def document_intelligence_preview_public(current_user, pyme_id: int):
    """Public alias that reuses the API handler for /pymes/... requests."""

    return _document_intelligence_preview(current_user, pyme_id)
