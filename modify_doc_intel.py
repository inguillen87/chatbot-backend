import re

file_path = 'routes/document_intelligence.py'

# Helper functions to add
parse_price_code = '''
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
'''

with open(file_path, 'r') as f:
    original = f.read()

# Insert helper functions before `_build_columns`
insert_point = original.find("def _build_columns")
if insert_point != -1:
    new_content = original[:insert_point] + parse_price_code + "\n\n" + original[insert_point:]
else:
    print("Could not find insertion point")
    sys.exit(1)

# Modify _document_intelligence_preview
# Find the block where LLM is called for CSV/Excel
# Pattern: if not preview_df.empty and len(preview_df.columns) > 0:
# ... csv_sample ...
# ... analyze_text_structured ...

# We will replace that block.
block_start = new_content.find('    if not preview_df.empty and len(preview_df.columns) > 0:')
if block_start == -1:
    print("Could not find preview logic block")
    # It might have different indentation or newlines?
    # Let's try to replace based on the LLM call
    pass

# Direct replacement of the LLM call block
llm_call_pattern = r'''        csv_sample = preview_df.to_csv\(index=False\)
        structured = analyze_text_structured\(csv_sample, _catalog_llm_prompt\(rubro_hint\)\)
        structured_df = _build_df_from_vision\(structured\)
        if structured_df is not None and not structured_df.empty:
            preview_df = structured_df.head\(max_rows\).fillna\(""\)'''

replacement = r'''        # HEURISTIC MAPPING INSTEAD OF LLM FOR TABULAR DATA
        preview_df = _map_columns_heuristically(preview_df)
        # csv_sample = preview_df.to_csv(index=False)
        # structured = analyze_text_structured(csv_sample, _catalog_llm_prompt(rubro_hint))
        # structured_df = _build_df_from_vision(structured)
        # if structured_df is not None and not structured_df.empty:
        #    preview_df = structured_df.head(max_rows).fillna("")'''

import re
new_content_mod = re.sub(llm_call_pattern, replacement, new_content, flags=re.DOTALL)

# Add error handling for PDF/Vision path
# Find where `df = _merge_dataframes(...)` happens and fallback logic ends.
# Then check if df is empty.

# Look for `        except Exception:
#            df = pd.DataFrame()`
# And the `else` block for tabular.

# We want to change:
#    df = df if df is not None else pd.DataFrame()
#    df = df.dropna(how="all")
#    max_rows = ...
#    preview_df = df.head(max_rows)...
#    if not preview_df.empty ...

# To check if empty and return error.

final_check_pattern = r'''    df = df if df is not None else pd.DataFrame\(\)
    df = df.dropna\(how="all"\)

    max_rows'''

final_check_replacement = r'''    df = df if df is not None else pd.DataFrame()
    df = df.dropna(how="all")

    if df.empty:
        return jsonify({
            "error": "No se pudieron detectar datos en el archivo.",
            "details": "El análisis automático no encontró tablas válidas. Intente subir un CSV o Excel estándar.",
            "retry_with_csv": True
        }), 422

    max_rows'''

new_content_mod = re.sub(final_check_pattern, final_check_replacement, new_content_mod, flags=re.DOTALL)


with open(file_path, 'w') as f:
    f.write(new_content_mod)

print("Modified routes/document_intelligence.py")
