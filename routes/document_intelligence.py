import io
import re
from typing import Any, List, Optional

import pandas as pd
import pdfplumber
from flask import Blueprint, jsonify, request

from models import CatalogoItem, CatalogUpload, db
from routes.auth import token_requerido
from services.embedding_service import embed_textos_llm
from services.llm_utils import llamar_llm_para_json_estructurado
from services.qdrant_service import index_catalog_item
from services.vision_fallback_service import (
    analyze_image_structured,
    analyze_image_text,
    analyze_text_structured,
)


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



def parse_price(v: Any) -> float:
    """Robust price parsing for Argentine format."""
    if v is None:
        return 0.0
    s = str(v).strip()
    # Remove currency symbols and whitespace
    s = re.sub(r"[^\d.,-]", "", s)

    if not s:
        return 0.0

    # Argentine format: 1.500,00 -> 1500.00
    # US format: 1,500.00 -> 1500.00

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."): # 1.500,00
            s = s.replace(".", "").replace(",", ".")
        else: # 1,500.00
            s = s.replace(",", "")
    elif "," in s: # 1500,00 or 1,500 (ambiguous, assume decimal if 2 digits after, else thousands?)
        # Simple heuristic: if comma is close to end, it's decimal
        if len(s) - s.rfind(",") <= 3:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")

    try:
        return float(s)
    except ValueError:
        return 0.0

def _map_columns_heuristically(df: pd.DataFrame) -> pd.DataFrame:
    """Map columns to standard names using regex synonyms."""
    if df is None or df.empty:
        return df

    mapping = {}
    synonyms = {
        "sku": ["codigo", "id", "ref", "cod", "articulo"],
        "nombre": ["producto", "descripcion", "detalle", "item", "nombre"],
        "precio": ["valor", "importe", "costo", "precio venta", "precio unitario", "p.unit", "precio_lista"],
        "stock": ["cantidad", "existencia", "disponible", "cant"],
        "marca": ["brand", "fabricante", "bodega"],
        "categoria": ["rubro", "familia", "tipo", "seccion"],
        "unidad": ["medida", "uni", "pres"],
        "moneda": ["divisa"]
    }

    used_cols = set()

    for col in df.columns:
        col_lower = str(col).lower().strip()
        mapped = None

        # Exact match
        if col_lower in synonyms:
            mapped = col_lower

        # Synonym match
        if not mapped:
            for standard, syns in synonyms.items():
                if any(s in col_lower for s in syns):
                    mapped = standard
                    break

        if mapped and mapped not in used_cols:
            mapping[col] = mapped
            used_cols.add(mapped)

    if mapping:
        # Create a copy with renamed columns for the preview,
        # but maybe we should keep original names and just suggest mapping?
        # The frontend expects "columns" list.
        # For the preview logic which expects specific keys in the 'rows' for the commit phase,
        # we might want to rename.
        # But wait, the commit phase uses `columns` sent back from frontend.
        # If we rename here, the frontend shows mapped names.
        return df.rename(columns=mapping)

    return df


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
    rubro_hint = f"Contexto del negocio: {rubro}. " if rubro else ""
    return (
        "Analiza el documento o texto y extrae la información de productos en formato estructurado JSON. "
        "El objetivo es crear una vista previa fiel del catálogo. "
        "Formato de respuesta esperado (JSON Object): "
        "{ "
        "  \"columns\": [\"Nombre\", \"Precio\", \"Marca\", ...], "
        "  \"rows\": [[\"Vino Malbec\", \"1500.00\", \"Rutini\", ...], ...] "
        "} "
        f"{rubro_hint}"
        "Instrucciones clave: "
        "1. Identifica los encabezados reales. Si no hay encabezados, infiérelos (ej: Producto, Precio, SKU, Variedad). "
        "2. Extrae TODOS los productos visibles. "
        "3. Maneja formatos de precios argentinos (ej: 1.500,00 es mil quinientos; 1500 es mil quinientos). "
        "4. Si hay múltiples columnas de precios (ej: Precio Caja, Precio Unitario), inclúyelas todas en 'columns' y 'rows'. "
        "5. No inventes datos. Si una celda está vacía, usa string vacío \"\". "
        "6. Si la imagen contiene promociones o listas complejas, intenta tabularlas lo mejor posible. "
        "7. Responde SOLO el JSON."
    )


def _catalog_items_prompt(rubro: Optional[str] = None) -> str:
    rubro_hint = f"Contexto del negocio: {rubro}. " if rubro else ""
    return (
        "Eres un asistente experto en normalizar catálogos para una plataforma de e-commerce y IA. "
        "Tu tarea es convertir datos tabulares (columns/rows) en una lista de objetos JSON estandarizados. "
        f"{rubro_hint}"
        "Output esperado: JSON con clave 'items', que es una lista de objetos. "
        "Cada objeto debe tener: "
        " - nombre (string, obligatorio) "
        " - sku (string, opcional) "
        " - marca (string, opcional) "
        " - categoria (string, opcional) "
        " - precio (number/float, extrae solo el valor numérico, ej: 1500.00) "
        " - moneda (string, ej: ARS, USD) "
        " - stock (number, opcional) "
        " - unidad (string, ej: un, kg, lt) "
        " - presentacion (string, ej: Caja x 6, Botella 750ml) "
        " - descripcion (string, detalles adicionales) "
        " - extra_metadata (dict, para cualquier otro campo: varietal, anada, region, etc.) "
        "REGLAS: "
        "1. Normaliza los precios a float. Si dice '$1.500', es 1500.0. "
        "2. Detecta variantes implícitas si es necesario. "
        "3. Si hay info en 'extra_metadata', usa claves en snake_case."
    )


def _normalize_catalog_items_with_llm(columns: List[str], rows: List[dict], rubro: Optional[str]) -> List[dict]:
    system_prompt = _catalog_items_prompt(rubro)
    user_prompt = (
        "Columnas detectadas:\n"
        f"{columns}\n\n"
        "Filas detectadas (objetos con columnas):\n"
        f"{rows[:200]}"
    )
    response = llamar_llm_para_json_estructurado(system_prompt=system_prompt, user_prompt=user_prompt)
    if isinstance(response, dict):
        items = response.get("items") or response.get("productos") or response.get("catalogo") or []
        return items if isinstance(items, list) else []
    if isinstance(response, list):
        return response
    return []


def _sanitize_sku(raw: Optional[str], fallback: str) -> str:
    value = (raw or "").strip() or fallback
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or fallback


def _build_extra_metadata(item: dict) -> dict:
    base_keys = {
        "nombre",
        "title",
        "sku",
        "marca",
        "brand",
        "categoria",
        "category",
        "precio",
        "price",
        "moneda",
        "currency",
        "stock",
        "unidad",
        "unit",
        "presentacion",
        "pack",
        "descripcion",
        "description",
        "extra_metadata",
    }
    extra = {}
    for key, value in item.items():
        if key in base_keys or value in (None, "", []):
            continue
        extra[key] = value
    explicit = {
        "varietal": item.get("varietal"),
        "anada": item.get("anada") or item.get("añada"),
        "pallet": item.get("pallet"),
        "caja": item.get("caja") or item.get("box"),
        "unidades_por_caja": item.get("unidades_por_caja") or item.get("unidad_por_caja"),
        "precio_por_caja": item.get("precio_por_caja"),
        "litros": item.get("litros"),
        "ml": item.get("ml"),
        "kg": item.get("kg"),
        "gramos": item.get("gramos"),
        "bolsa": item.get("bolsa"),
        "medida": item.get("medida"),
        "alto": item.get("alto"),
        "ancho": item.get("ancho"),
        "largo": item.get("largo"),
        "peso": item.get("peso"),
    }
    for key, value in explicit.items():
        if value not in (None, "", []):
            extra.setdefault(key, value)
    return extra


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

    if filename.endswith(".pdf"):
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
                        vision = analyze_image_structured(
                            buffer.getvalue(),
                            _catalog_llm_prompt(rubro_hint),
                        )
                        vision_df = _build_df_from_vision(vision)
                        if vision_df is not None and not vision_df.empty:
                            vision_tables.append(vision_df)
                        else:
                            ocr_text = analyze_image_text(buffer.getvalue())
                            if ocr_text:
                                ocr_texts.append(ocr_text)
                                structured = analyze_text_structured(ocr_text, _catalog_llm_prompt(rubro_hint))
                                structured_df = _build_df_from_vision(structured)
                                if structured_df is not None and not structured_df.empty:
                                    text_tables.append(structured_df)
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
                            structured = analyze_text_structured(text, _catalog_llm_prompt(rubro_hint))
                            structured_df = _build_df_from_vision(structured)
                            if structured_df is not None and not structured_df.empty:
                                text_tables.append(structured_df)
                    except Exception:
                        pass

                df = _merge_dataframes(vision_tables)
                if df is None or df.empty:
                    df = _merge_dataframes(table_tables)
                if df is None or df.empty:
                    df = _merge_dataframes(text_tables)
                if df is None or df.empty:
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

    if df.empty:
        return jsonify({
            "error": "No se pudieron detectar datos en el archivo.",
            "details": "El análisis automático no encontró tablas válidas. Intente subir un CSV o Excel estándar.",
            "retry_with_csv": True
        }), 422

    max_rows = request.form.get("maxRows", type=int) or 50
    preview_df = df.head(max_rows).fillna("")
    if not preview_df.empty and len(preview_df.columns) > 0:
        # HEURISTIC MAPPING INSTEAD OF LLM FOR TABULAR DATA
        preview_df = _map_columns_heuristically(preview_df)
        # csv_sample = preview_df.to_csv(index=False)
        # structured = analyze_text_structured(csv_sample, _catalog_llm_prompt(rubro_hint))
        # structured_df = _build_df_from_vision(structured)
        # if structured_df is not None and not structured_df.empty:
        #    preview_df = structured_df.head(max_rows).fillna("")

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

    payload = request.get_json(silent=True) or {}
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    if not columns or not rows:
        return jsonify({"error": "Columns y rows son requeridos."}), 400

    rubro_hint = payload.get("rubro") or payload.get("rubroSlug")
    replace_catalog = payload.get("replaceCatalog", True)
    upload_id = payload.get("catalogUploadId")

    items = _normalize_catalog_items_with_llm(columns, rows, rubro_hint)
    if not items:
        return jsonify({"error": "No se pudieron normalizar items."}), 400

    tenant_id = getattr(current_user, "tenant_id", None)
    if replace_catalog:
        query = CatalogoItem.query.filter_by(user_id=current_user.id)
        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)
        query.delete()

    texts_to_embed = []
    normalized_items = []
    for idx, item in enumerate(items, start=1):
        nombre = (item.get("nombre") or item.get("title") or "").strip()
        if not nombre:
            continue
        sku = _sanitize_sku(item.get("sku"), f"item-{idx}-{nombre}")
        precio = parse_price(item.get("precio") or item.get("price"))
        normalized_items.append({
            "nombre": nombre,
            "sku": sku,
            "marca": item.get("marca") or item.get("brand"),
            "categoria": item.get("categoria") or item.get("category"),
            "precio": precio,
            "moneda": item.get("moneda") or item.get("currency"),
            "stock": item.get("stock"),
            "unidad": item.get("unidad") or item.get("unit"),
            "presentacion": item.get("presentacion") or item.get("pack"),
            "descripcion": item.get("descripcion") or item.get("description"),
            "extra_metadata": _build_extra_metadata(item),
        })
        texts_to_embed.append(f"{nombre} {item.get('categoria') or ''} {precio or ''}")

    embeddings = embed_textos_llm(texts_to_embed) if texts_to_embed else []
    count = 0
    for idx, item in enumerate(normalized_items):
        catalog_item = CatalogoItem(
            user_id=current_user.id,
            tenant_id=tenant_id,
            nombre=item["nombre"],
            sku=item["sku"],
            marca=item.get("marca"),
            categoria=item.get("categoria"),
            precio=str(item.get("precio") or ""),
            moneda=item.get("moneda"),
            cantidad=str(item.get("stock") or "") if item.get("stock") is not None else None,
            unidad=item.get("unidad"),
            descripcion_corta=item.get("presentacion"),
            descripcion=item.get("descripcion"),
            extra_metadata=item.get("extra_metadata") or None,
            modalidad="venta",
            disponible=True,
        )
        db.session.add(catalog_item)
        db.session.flush()

        embedding = embeddings[idx] if idx < len(embeddings) else None
        if embedding:
            index_catalog_item(
                tenant_id or current_user.id,
                {
                    "id": catalog_item.id,
                    "nombre": catalog_item.nombre,
                    "descripcion": catalog_item.descripcion,
                    "precio": catalog_item.precio,
                    "rubro": rubro_hint or "general",
                    "stock": item.get("stock") or 0,
                    "extra_metadata": item.get("extra_metadata") or {},
                },
                embedding,
            )
        count += 1

    if upload_id:
        upload_rec = _catalog_upload_from_request(upload_id)
        if upload_rec:
            upload_rec.preview_data = {"columns": columns, "rows": rows}
            upload_rec.stats = {"items": count}
            upload_rec.status = "committed"
    db.session.commit()

    return jsonify({"success": True, "items": count})


@document_intelligence_public_bp.route("/preview", methods=["OPTIONS"])
def document_intelligence_preview_options_public(pyme_id: int):
    """Public alias for OPTIONS preflight when hitting /pymes/... paths."""

    return "", 204


@document_intelligence_public_bp.route("/preview", methods=["POST"])
@token_requerido
def document_intelligence_preview_public(current_user, pyme_id: int):
    """Public alias that reuses the API handler for /pymes/... requests."""

    return _document_intelligence_preview(current_user, pyme_id)
