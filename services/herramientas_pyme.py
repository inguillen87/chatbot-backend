import sys
import os
import json
import logging

project_root_herramientas_pyme = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_herramientas_pyme not in sys.path:
    sys.path.insert(0, project_root_herramientas_pyme)
import random
import difflib
import re # Import re for stock number extraction
from datetime import datetime
from models import CatalogoItem, User # Added User import

logger = logging.getLogger(__name__)


def consultar_horario_actual(user_id: int): # Changed to accept user_id
    user = User.query.get(user_id)
    if not user or not getattr(user, "horario", None):
        return json.dumps({"respuesta": "No tengo información sobre el horario de atención en este momento."})
    horario = user.horario
    # now = datetime.now() # Not used
    mensaje = f"Nuestro horario de atención es: {horario}."
    return json.dumps({"respuesta": mensaje})


def verificar_stock_producto(nombre_producto: str, user_id: int): # Renamed nombre to nombre_producto for clarity
    """Busca stock del producto con coincidencia difusa y sugiere alternativas."""
    if not nombre_producto or not user_id:
        return json.dumps({"respuesta": "Por favor, dime qué producto te interesa."})

    items = CatalogoItem.query.filter(CatalogoItem.user_id == user_id).all()
    if not items:
        return json.dumps({"respuesta": "No encontré productos en el catálogo para verificar stock."})

    nombres_catalogo = [it.nombre for it in items if getattr(it, "nombre", None)]
    if not nombres_catalogo: # Handle case where items exist but have no names
        return json.dumps({"respuesta": "No hay productos con nombre en el catálogo para verificar."})

    coincidencias = difflib.get_close_matches(nombre_producto, nombres_catalogo, n=3, cutoff=0.6)

    if coincidencias:
        item_encontrado = next((i for i in items if i.nombre == coincidencias[0]), None)
        if item_encontrado:
            stock_disponible = getattr(item_encontrado, "cantidad", None) # 'cantidad' seems to be used for stock

            # Check if stock is explicitly "0", "0.0", "agotado", "no disponible"
            stock_agotado_keywords = {"0", "0.0", "agotado", "no disponible"}
            if stock_disponible is not None and str(stock_disponible).strip().lower() in stock_agotado_keywords:
                # Stock is explicitly zero or marked as unavailable
                pass # Fall through to "no stock" logic
            elif stock_disponible is not None and str(stock_disponible).strip(): # If stock has some value
                # Try to parse stock as number for better comparison if it's string like "10 unidades"
                stock_str_cleaned = str(stock_disponible).strip()
                stock_num_match = re.match(r"(\d+)", stock_str_cleaned) # Try to get leading digits
                if stock_num_match:
                    try:
                        stock_num = int(stock_num_match.group(1))
                        if stock_num > 0:
                            return json.dumps({"respuesta": f"¡Sí! Tenemos {stock_disponible} de {item_encontrado.nombre}."})
                        # If stock_num is 0 after parsing, it means agotado, fall through
                    except ValueError: # Not a simple number at the start
                        return json.dumps({"respuesta": f"Tenemos disponibilidad de {item_encontrado.nombre} (Stock actual: {stock_disponible})."})
                else: # Stock is some text like "Disponible"
                    return json.dumps({"respuesta": f"Tenemos disponibilidad de {item_encontrado.nombre} (Stock actual: {stock_disponible})."})

            # No stock or stock is "0" / "agotado"
            alternativas = [c for c in coincidencias[1:] if c != item_encontrado.nombre] # Get other close matches as alternatives
            if alternativas:
                return json.dumps({"respuesta": f"Lo siento, no tenemos stock de {item_encontrado.nombre} en este momento. Quizás te interesen: {', '.join(alternativas)}."})
            return json.dumps({"respuesta": f"Lo siento, no tenemos stock de {item_encontrado.nombre} en este momento."})

    # No direct coincidence found
    sugerencias_catalogo = ", ".join(nombres_catalogo[:3])
    if sugerencias_catalogo:
        return json.dumps({"respuesta": f"No encontré '{nombre_producto}' exactamente. Algunos productos disponibles son: {sugerencias_catalogo}. ¿Te interesa alguno de estos o querés que busque otra cosa?"})
    return json.dumps({"respuesta": f"No pude encontrar información de stock para '{nombre_producto}'."})


def calcular_costo_envio(ciudad_destino: str, user_id: int): # Added user_id for potential future per-user logic
    if not ciudad_destino:
        return json.dumps({"respuesta": "Por favor, indícame la ciudad de destino para calcular el envío."})
    # Simple random cost for now, could be more sophisticated
    costo = random.randint(300, 2500) # Adjusted range
    return json.dumps({"respuesta": f"El costo estimado de envío a {ciudad_destino} es ${costo}. Este valor es aproximado."})


TOOL_REGISTRY_PYME = {
    "consultar_horario": {
        "funcion": consultar_horario_actual,
        "descripcion": "Informa el horario de atención de la empresa. Útil si preguntan 'a qué hora abren?', 'están abiertos?', 'horarios'.",
        "parametros": {
            "user_id": {"type": "integer", "description": "ID del usuario/pyme dueña del bot. Este dato se obtiene del contexto, no se pide al usuario final."}
        },
         "roles_permitidos": ["usuario", "empleado", "admin_pyme"] # Assuming similar role structure
    },
    "verificar_stock_producto": {
        "funcion": verificar_stock_producto,
        "descripcion": "Verifica la disponibilidad o stock de un producto específico en el catálogo. Útil para preguntas como 'tienen stock de X?', 'hay Y producto?'.",
        "parametros": {
            "nombre_producto": {"type": "string", "description": "El nombre del producto que el usuario quiere consultar."},
            "user_id": {"type": "integer", "description": "ID del usuario/pyme dueña del bot. Se obtiene del contexto."}
        },
         "roles_permitidos": ["usuario", "empleado", "admin_pyme"]
    },
    "calcular_costo_envio": {
        "funcion": calcular_costo_envio,
        "descripcion": "Calcula un costo estimado de envío a una ciudad o localidad dada por el usuario. Útil para 'cuánto sale el envío a X?'.",
        "parametros": {
            "ciudad_destino": {"type": "string", "description": "La ciudad o localidad a la que se enviaría el producto."},
            "user_id": {"type": "integer", "description": "ID del usuario/pyme dueña del bot. Se obtiene del contexto."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_pyme"]
    },
}

def crear_prompt_decision_herramienta_pyme(pregunta_usuario: str) -> str:
    """Crea un prompt para que el LLM decida si usar una herramienta Pyme y extraiga parámetros."""

    herramientas_para_prompt = {}
    for nombre, detalles in TOOL_REGISTRY_PYME.items():
        # Solo incluir parámetros que el LLM debe extraer del usuario.
        params_para_llm = {
            k: v for k, v in detalles["parametros"].items()
            if k != "user_id" # user_id is context, not from user query
        }

        entry = {"descripcion": detalles["descripcion"]}
        if params_para_llm: # Solo añadir 'parametros_a_extraer_del_usuario' si hay alguno
            entry["parametros_a_extraer_del_usuario"] = params_para_llm
        herramientas_para_prompt[nombre] = entry

    prompt = f"""
Eres un despachador de herramientas inteligente para un chatbot de una PyME (pequeña o mediana empresa).
Analiza la PREGUNTA DEL USUARIO y decide si alguna de las HERRAMIENTAS DISPONIBLES puede resolverla.

HERRAMIENTAS DISPONIBLES:
{json.dumps(herramientas_para_prompt, indent=2, ensure_ascii=False)}

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

INSTRUCCIONES DE RESPUESTA:
1. Si una herramienta aplica y TIENE parámetros que deben extraerse de la pregunta del usuario:
   - Si TODOS los parámetros necesarios para esa herramienta están presentes en la pregunta, devuelve un JSON con "herramienta" y "parametros".
     Ejemplo: {{"herramienta": "verificar_stock_producto", "parametros": {{"nombre_producto": "tornillos de media pulgada"}}}}
   - Si FALTAN parámetros que la herramienta necesita de la pregunta del usuario, devuelve un JSON con "herramienta" y "faltan_parametros_del_usuario".
     Ejemplo: {{"herramienta": "calcular_costo_envio", "faltan_parametros_del_usuario": ["ciudad_destino"]}}
2. Si una herramienta aplica y NO tiene parámetros que extraer de la pregunta del usuario (ej. consultar_horario):
   - Devuelve un JSON solo con "herramienta".
     Ejemplo: {{"herramienta": "consultar_horario"}}
3. Si NINGUNA herramienta aplica directamente, devuelve la palabra 'null' (sin comillas).

Considera el contexto de una PyME: los usuarios preguntarán por productos, stock, horarios, envíos, etc.
Sé preciso al identificar la herramienta y los parámetros. No inventes parámetros que no estén en la pregunta.
"""
    return prompt
