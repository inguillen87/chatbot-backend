import sys

file_path = 'routes/document_intelligence.py'

new_llm_prompt = '''def _catalog_llm_prompt(rubro: Optional[str] = None) -> str:
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
        "1. Normaliza los precios a float. Si dice '.500', es 1500.0. "
        "2. Detecta variantes implícitas si es necesario. "
        "3. Si hay info en 'extra_metadata', usa claves en snake_case."
    )'''

with open(file_path, 'r') as f:
    lines = f.readlines()

# Correct indices (0-based)
start_llm = 171 # Line 172
end_llm = 184   # Line 185

start_items = 186 # Line 187
end_items = 200   # Line 201

if "def _catalog_llm_prompt" not in lines[start_llm]:
    print(f"Error: Line {start_llm} is not _catalog_llm_prompt. content: {lines[start_llm]}")
    sys.exit(1)

if "def _catalog_items_prompt" not in lines[start_items]:
    print(f"Error: Line {start_items} is not _catalog_items_prompt. content: {lines[start_items]}")
    sys.exit(1)

# Replace in reverse order to preserve indices for the first replacement
lines[start_items:end_items+1] = [new_items_prompt + "\n"]
lines[start_llm:end_llm+1] = [new_llm_prompt + "\n"]

with open(file_path, 'w') as f:
    f.writelines(lines)
print("Updated routes/document_intelligence.py")
