import io
from typing import Any, List, Optional

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
    for _, row in sample_rows.iterrows():
        cell_text = " ".join(str(value) for value in row if value is not None).strip()
        if not cell_text:
            continue
        lowered = cell_text.lower()
        token_hits += sum(token in lowered for token in header_tokens)
        if len(cell_text) > 80:
            long_row_hits += 1

    return column_count <= 1 and (token_hits >= 2 or long_row_hits >= 2)


def _group_words_by_line(
    words: List[dict[str, Any]], y_tolerance: float = 3.0
) -> List[dict[str, Any]]:
    if not words:
        return []

    sorted_words = sorted(words, key=lambda w: (w.get("top", 0), w.get("x0", 0)))
    lines: List[dict[str, Any]] = []
    current_line: List[dict[str, Any]] = []
    current_top = None

    for word in sorted_words:
        top = float(word.get("top", 0))
        if current_top is None or abs(top - current_top) <= y_tolerance:
            current_line.append(word)
            current_top = top if current_top is None else current_top
        else:
            lines.append(
                {
                    "top": current_top,
                    "words": sorted(current_line, key=lambda w: w.get("x0", 0)),
                }
            )
            current_line = [word]
            current_top = top

    if current_line:
        lines.append(
            {"top": current_top, "words": sorted(current_line, key=lambda w: w.get("x0", 0))}
        )

    return lines


def _build_columns_from_header_words(
    header_words: List[dict[str, Any]], gap_tolerance: float = 12.0
) -> List[dict[str, Any]]:
    if not header_words:
        return []

    segments: List[dict[str, Any]] = []
    current_segment = {
        "words": [header_words[0]],
        "x0": float(header_words[0]["x0"]),
        "x1": float(header_words[0]["x1"]),
    }

    for word in header_words[1:]:
        gap = float(word["x0"]) - current_segment["x1"]
        if gap > gap_tolerance:
            segments.append(current_segment)
            current_segment = {
                "words": [word],
                "x0": float(word["x0"]),
                "x1": float(word["x1"]),
            }
        else:
            current_segment["words"].append(word)
            current_segment["x1"] = max(current_segment["x1"], float(word["x1"]))

    segments.append(current_segment)

    return [
        {
            "name": " ".join(word["text"] for word in segment["words"]).strip(),
            "x0": segment["x0"],
            "x1": segment["x1"],
        }
        for segment in segments
    ]


def _assign_word_to_column(
    word: dict[str, Any], column_boundaries: List[float]
) -> int:
    center = (float(word["x0"]) + float(word["x1"])) / 2
    for idx, boundary in enumerate(column_boundaries):
        if center <= boundary:
            return idx
    return len(column_boundaries)


def _extract_table_from_words(page: pdfplumber.page.Page) -> Optional[pd.DataFrame]:
    words = page.extract_words(
        x_tolerance=2,
        y_tolerance=2,
        keep_blank_chars=False,
        extra_attrs=["x0", "x1", "top", "bottom"],
    )
    if not words:
        return None

    lines = _group_words_by_line(words)
    if not lines:
        return None

    header_tokens = ("marca", "varietal", "precio", "caja", "botella", "pallet", "unidad")
    header_idx = None
    header_hits = 0
    for idx, line in enumerate(lines):
        line_text = " ".join(word["text"].lower() for word in line["words"])
        hits = sum(token in line_text for token in header_tokens)
        if hits > header_hits:
            header_hits = hits
            header_idx = idx

    if header_idx is None or header_hits < 2:
        return None

    header_lines = [lines[header_idx]]
    if header_idx + 1 < len(lines):
        next_line = lines[header_idx + 1]
        if abs(next_line["top"] - lines[header_idx]["top"]) <= 10:
            header_lines.append(next_line)

    header_words = sorted(
        [word for line in header_lines for word in line["words"]],
        key=lambda w: w.get("x0", 0),
    )
    column_defs = _build_columns_from_header_words(header_words)
    if not column_defs:
        return None

    columns = [col["name"] or f"Columna {idx + 1}" for idx, col in enumerate(column_defs)]
    centers = [(col["x0"] + col["x1"]) / 2 for col in column_defs]
    boundaries = [
        (centers[idx] + centers[idx + 1]) / 2 for idx in range(len(centers) - 1)
    ]

    header_bottom = max(line["top"] for line in header_lines) + 4
    rows: List[List[str]] = []
    for line in lines[header_idx + len(header_lines) :]:
        if line["top"] <= header_bottom:
            continue
        row_cells = [""] * len(columns)
        for word in line["words"]:
            col_idx = _assign_word_to_column(word, boundaries)
            existing = row_cells[col_idx]
            row_cells[col_idx] = f"{existing} {word['text']}".strip() if existing else word["text"]
        if any(cell.strip() for cell in row_cells):
            rows.append(row_cells)

    if not rows:
        return None

    return pd.DataFrame(rows, columns=columns)


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
                        header_tokens = (
                            "marca",
                            "varietal",
                            "precio",
                            "caja",
                            "botella",
                            "pallet",
                        )
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
                            headers = (
                                [f"Columna {i+1}" for i in range(len(rows[0]))] if rows else []
                            )

                        df = pd.DataFrame(rows, columns=headers)
                use_vision = df is None or df.empty or _looks_like_flat_pdf_table(df)
                if use_vision:
                    structured_df = _extract_table_from_words(page_for_image)
                    if structured_df is not None and not structured_df.empty:
                        df = structured_df
                        use_vision = False
                if use_vision:
                    try:
                        image = page_for_image.to_image(resolution=300).original
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
                        df = df if df is not None and not df.empty else None

                if df is None or df.empty:
                    # Si no hay tablas, extraer texto simple
                    text = page_for_image.extract_text() or ""
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
