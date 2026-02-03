import sys

file_path = 'routes/document_intelligence.py'

new_llm_prompt = '''def _catalog_llm_prompt(rubro: Optional[str] = None) -> str:
    rubro_hint = f"Contexto del negocio: {rubro}. " if rubro else ""
    return (
        "Analiza el documento o texto y extrae la información de productos en formato estructurado JSON. "
        "El objetivo es crear una vista previa fiel del catálogo. "
        "Formato de respuesta esperado (JSON Object): "
        "{ "
        "  \\"columns\\": [\\"Nombre\\", \\"Precio\\", \\"Marca\\", ...], "
        "  \\"rows\\": [[\\"Vino Malbec\\", \\"1500.00\\", \\"Rutini\\", ...], ...] "
        "} "
        f"{rubro_hint}"
        "Instrucciones clave: "
        "1. Identifica los encabezados reales. Si no hay encabezados, infiérelos (ej: Producto, Precio, SKU, Variedad). "
        "2. Extrae TODOS los productos visibles. "
        "3. Maneja formatos de precios argentinos (ej: 1.500,00 es mil quinientos; 1500 es mil quinientos). "
        "4. Si hay múltiples columnas de precios (ej: Precio Caja, Precio Unitario), inclúyelas todas en 'columns' y 'rows'. "
        "5. No inventes datos. Si una celda está vacía, usa string vacío \\"\\". "
        "6. Si la imagen contiene promociones o listas complejas, intenta tabularlas lo mejor posible. "
        "7. Responde SOLO el JSON."
    )'''

new_items_prompt = '''def _catalog_items_prompt(rubro: Optional[str] = None) -> str:
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
    )'''

with open(file_path, 'r') as f:
    lines = f.readlines()

# Correct indices (0-based) based on previous check
# _catalog_llm_prompt was at line 172 (index 171)
# _catalog_items_prompt was at line 187 (index 186)
start_llm = 171
end_llm = 184

start_items = 186
end_items = 200

# Verify again just in case previous patch shifted things or content is broken
if "def _catalog_llm_prompt" not in lines[start_llm]:
    # Fallback search
    for i, line in enumerate(lines):
        if "def _catalog_llm_prompt" in line:
            start_llm = i
            break
    # Approximate end finding
    for i in range(start_llm, len(lines)):
        if lines[i].strip() == ")":
             end_llm = i
             break

if "def _catalog_items_prompt" not in lines[start_items]:
    # Fallback search
    for i, line in enumerate(lines):
        if "def _catalog_items_prompt" in line:
            start_items = i
            break
    # Approximate end finding
    for i in range(start_items, len(lines)):
        if lines[i].strip() == ")":
             end_items = i
             break

print(f"Replacing _catalog_llm_prompt at {start_llm}-{end_llm}")
print(f"Replacing _catalog_items_prompt at {start_items}-{end_items}")

# Replace in reverse order
lines[start_items:end_items+1] = [new_items_prompt + "\n"]
lines[start_llm:end_llm+1] = [new_llm_prompt + "\n"]

with open(file_path, 'w') as f:
    f.writelines(lines)
print("Fixed routes/document_intelligence.py")
