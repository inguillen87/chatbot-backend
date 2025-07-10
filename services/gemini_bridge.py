import json
# Importar GenerativeModel si se va a usar directamente, o el cliente de Vertex AI
# from vertexai.preview.generative_models import GenerativeModel 
# Por ahora, como no tenemos credenciales/API real, lo mockearemos.

JULES_SYSTEM_PROMPT = """Sos el asistente IA de una plataforma multi-entidad que atiende a Municipios y Pymes. 
Tu tarea es recibir y entender mensajes de ciudadanos o clientes, interpretar reclamos, consultas o pedidos, y devolver siempre un JSON estructurado y profesional para que el backend ejecute la acción adecuada.

### Qué hacés
- Comprendés todo tipo de reclamos o pedidos, desde problemas de luminarias, tránsito, residuos, hasta consultas comerciales (“quiero un préstamo”, “me falta stock”, etc).
- Pedís datos faltantes de manera proactiva y natural, sin fricción.
- Respondés de forma personalizada según si el usuario es un vecino del municipio o un cliente/usuario de una PyME.
- Cuando el mensaje es ambiguo, pedís aclaraciones de inmediato, nunca “trabas” la conversación.
- Si es necesario, sugerís que la persona envíe una foto, audio, o geolocalización (ej: para un bache o reclamo complejo).
- Si detectás que el mensaje aplica tanto a pyme como municipio, devolvés un campo “target” en el JSON para que el backend derive.

### Entrada SIEMPRE
- mensaje_usuario: Texto plano.
- usuario: Objeto JSON (nombre, tipo_entidad, ubicación, contacto, etc).
- historial: Array JSON de turns previos (“pregunta”, “respuesta”, etc).

### SALIDA SIEMPRE (en JSON, nunca en texto ni en código Python):

{
  "respuesta_usuario": "...respuesta profesional y directa...",
  "accion_backend": "...crear_reclamo | consulta_estado | info_tramite | info_producto | consulta_credito | derivar_humano...",
  "datos_estructura": {
    "categoria": "...",
    "descripcion": "...",
    "ubicacion": "...",
    "coordenadas": "...",
    "usuario": "...",
    "telefono": "...",
    "target": "municipio | pyme"
  },
  "pedir_info": null | "ubicacion" | "categoria" | "id_reclamo" | "producto" | ...,
  "botones": [ { "texto": "..." }, ... ]
}

### Ejemplo
Si el usuario dice “necesito un préstamo para terminar la finca”, respondés:
{
  "respuesta_usuario": "Te ayudo a gestionar tu pedido de crédito para tu finca. ¿Podés decirme el monto y el destino del préstamo?",
  "accion_backend": "consulta_credito",
  "datos_estructura": {
    "categoria": "Crédito PyME",
    "descripcion": "Pedido de préstamo para terminar la finca",
    "usuario": "Juan Pérez",
    "telefono": "+54...",
    "target": "pyme"
  },
  "pedir_info": "monto",
  "botones": [ { "texto": "Solicitar préstamo" }, { "texto": "Cancelar" } ]
}

Si dice “se quemó la luz en la calle Mitre y Belgrano”:
{
  "respuesta_usuario": "Registré tu reclamo por luminaria quemada en Mitre y Belgrano. El municipio lo atenderá pronto.",
  "accion_backend": "crear_reclamo",
  "datos_estructura": {
    "categoria": "Alumbrado Público",
    "descripcion": "Luz quemada",
    "ubicacion": "Mitre y Belgrano",
    "coordenadas": null,
    "usuario": "Pedro Gómez",
    "telefono": "+54...",
    "target": "municipio"
  },
  "pedir_info": null,
  "botones": [ { "texto": "Consultar estado" }, { "texto": "Nuevo reclamo" } ]
}

Siempre devolvé el JSON, nunca texto plano, nunca código.
"""

def llamar_gemini(mensaje_usuario: str, usuario: dict, historial: list) -> dict:
    """
    Simula una llamada a la API de Gemini y devuelve una respuesta JSON estructurada.
    En una implementación real, aquí se haría la llamada a la API de Gemini.
    """
    # prompt_completo = f"""{JULES_SYSTEM_PROMPT}

    # MENSAJE DEL USUARIO: "{mensaje_usuario}"
    # USUARIO: {json.dumps(usuario, ensure_ascii=False, indent=2)}
    # HISTORIAL: {json.dumps(historial, ensure_ascii=False, indent=2)}
    # """
    # print("--- PROMPT ENVIADO A GEMINI (SIMULADO) ---") # Mantener comentado el print del prompt completo
    # print(prompt_completo)
    # print("------------------------------------------")

    # --- INICIO: LLAMADA REAL A GEMINI ---
    prompt_final_para_api = f"""{JULES_SYSTEM_PROMPT}

MENSAJE DEL USUARIO: "{mensaje_usuario}"
USUARIO: {json.dumps(usuario, ensure_ascii=False)}
HISTORIAL: {json.dumps(historial, ensure_ascii=False)}
"""
    # Configuración para asegurar que la respuesta sea JSON
    # Esto puede variar ligeramente según la versión de la librería o el modelo exacto.
    # Para gemini-1.5-pro-preview y la librería actual, se puede guiar por prompt
    # o usar generation_config si está disponible y bien documentado para JSON mode.
    # Por ahora, confiaremos en el prompt que explícitamente pide JSON.
    
    # Descomentar la siguiente línea e inicializar el modelo de Vertex AI
    # from vertexai.preview.generative_models import GenerativeModel, GenerationConfig # Añadir GenerationConfig
    
    # model = GenerativeModel("gemini-1.5-pro-preview") 
    # Si se quiere forzar JSON output con GenerationConfig (si el modelo y SDK lo soportan bien):
    # generation_config = GenerationConfig(
    #     response_mime_type="application/json",
    # )
    # response = model.generate_content(prompt_final_para_api, generation_config=generation_config)
    
    # Llamada estándar (confiando en el prompt para el formato JSON):
    # response = model.generate_content(prompt_final_para_api)

    # --- INICIO BLOQUE SIMULADO (REEMPLAZAR CON LLAMADA REAL) ---
    # Este bloque es para que el código siga funcionando sin la API real configurada.
    # ELIMINAR O COMENTAR ESTE BLOQUE CUANDO SE USE LA API REAL.
    print("--- ADVERTENCIA: USANDO RESPUESTA SIMULADA DE GEMINI ---")
    logger = logging.getLogger(__name__)
    logger.warning("LLAMADA A GEMINI SIMULADA. Reemplazar con la llamada real a la API.")
    
    mensaje_lower = mensaje_usuario.lower()
    if "préstamo" in mensaje_lower or "credito" in mensaje_lower:
        simulated_response_text = json.dumps({
            "respuesta_usuario": "Te ayudo a gestionar tu pedido de crédito (simulado). ¿Podés decirme el monto y el destino del préstamo?",
            "accion_backend": "consulta_credito",
            "datos_estructura": {"categoria": "Crédito PyME", "descripcion": mensaje_usuario, "usuario": usuario.get("nombre", "N/A"), "target": "pyme"},
            "pedir_info": "monto", "botones": [ { "texto": "Solicitar préstamo" }, { "texto": "Cancelar" } ]})
    elif ("luz" in mensaje_lower and ("quemada" in mensaje_lower or "rota" in mensaje_lower or "calle" in mensaje_lower or "esquina" in mensaje_lower)) or "luminaria" in mensaje_lower:
        simulated_response_text = json.dumps({
            "respuesta_usuario": "Registré tu reclamo por luminaria quemada (simulado). El municipio lo atenderá pronto.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"categoria": "Alumbrado Público", "descripcion": mensaje_usuario, "ubicacion": "Mitre y Belgrano (ejemplo simulado)", "usuario": usuario.get("nombre", "N/A"), "telefono": usuario.get("contacto", {}).get("telefono", "N/A"), "target": "municipio"},
            "pedir_info": None, "botones": [ { "texto": "Consultar estado" }, { "texto": "Nuevo reclamo" } ]})
    else:
        simulated_response_text = json.dumps({
            "respuesta_usuario": "No entendí bien tu consulta (simulado). ¿Podrías reformularla?",
            "accion_backend": "derivar_humano", "datos_estructura": {"descripcion": mensaje_usuario, "usuario": usuario.get("nombre", "N/A"), "target": usuario.get("tipo_entidad", "municipio")},
            "pedir_info": "aclaracion", "botones": []})
    
    # Simular el objeto response que tendría un atributo .text
    class SimulatedResponse:
        def __init__(self, text):
            self.text = text
    response = SimulatedResponse(simulated_response_text)
    # --- FIN BLOQUE SIMULADO ---

    try:
        # Limpiar espacios antes/después y parsear
        # El prompt JULES pide explícitamente un JSON, así que response.text debería serlo.
        # A veces los LLMs pueden añadir ```json\n ... \n```. strip() ayuda con espacios,
        # pero el parseo de ```json ... ``` necesitaría un manejo más específico si ocurre consistentemente.
        
        respuesta_texto_crudo = response.text.strip()
        # Intento básico de limpiar ```json ... ``` si está presente
        if respuesta_texto_crudo.startswith("```json"):
            respuesta_texto_crudo = respuesta_texto_crudo[7:]
            if respuesta_texto_crudo.endswith("```"):
                respuesta_texto_crudo = respuesta_texto_crudo[:-3]
        
        return json.loads(respuesta_texto_crudo)
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error parseando respuesta de Gemini: {e}\nRespuesta cruda: {response.text}")
        # Fallback a un error simple o una acción segura
        return {
            "respuesta_usuario": "Lo siento, hubo un error técnico al procesar tu solicitud. Un humano revisará tu caso.",
            "accion_backend": "derivar_humano", # Acción segura
            "datos_estructura": {"error_detalle": f"Fallo al parsear LLM: {str(e)}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, 
            "botones": [] 
        }
    # --- FIN: LLAMADA REAL A GEMINI ---

if __name__ == '__main__':
    # Ejemplos de uso para prueba rápida
    usuario_ejemplo_pyme = {
        "nombre": "Juan Pérez",
        "tipo_entidad": "pyme",
        "ubicacion": "Calle Falsa 123",
        "contacto": {"email": "juan.perez@example.com", "telefono": "+5491122334455"}
    }
    historial_ejemplo = [
        {"role": "user", "parts": [{"text": "Hola"}]},
        {"role": "model", "parts": [{"text": "Hola Juan, ¿en qué puedo ayudarte?"}]}
    ]

    mensaje1 = "necesito un préstamo para terminar la finca"
    respuesta1 = llamar_gemini(mensaje1, usuario_ejemplo_pyme, historial_ejemplo)
    print(f"Mensaje: {mensaje1}\nRespuesta LLM (mock): {json.dumps(respuesta1, indent=2, ensure_ascii=False)}\n")

    usuario_ejemplo_municipio = {
        "nombre": "Ana Gómez",
        "tipo_entidad": "municipio",
        "ubicacion": "Barrio Sol",
        "contacto": {"web": "ana.gomez.vecinos.com"}
    }
    mensaje2 = "Hay una luz quemada en la esquina de San Martín y Rivadavia"
    respuesta2 = llamar_gemini(mensaje2, usuario_ejemplo_municipio, [])
    print(f"Mensaje: {mensaje2}\nRespuesta LLM (mock): {json.dumps(respuesta2, indent=2, ensure_ascii=False)}\n")

    mensaje3 = "Quiero saber el estado de mi reclamo"
    respuesta3 = llamar_gemini(mensaje3, usuario_ejemplo_municipio, [])
    print(f"Mensaje: {mensaje3}\nRespuesta LLM (mock): {json.dumps(respuesta3, indent=2, ensure_ascii=False)}\n")

    mensaje4 = "vender cosas"
    respuesta4 = llamar_gemini(mensaje4, usuario_ejemplo_pyme, [])
    print(f"Mensaje: {mensaje4}\nRespuesta LLM (mock): {json.dumps(respuesta4, indent=2, ensure_ascii=False)}\n")

"""
Este script incluye:
- El `JULES_SYSTEM_PROMPT`.
- La función `llamar_gemini` que actualmente está *mockeada*. Devuelve diferentes respuestas estructuradas basadas en keywords simples en el mensaje del usuario. Esto nos permitirá probar el flujo sin una API real de Gemini por ahora.
- Una sección `if __name__ == '__main__':` para probar rápidamente la función `llamar_gemini` con algunos ejemplos.

**Importante sobre el mock:**
El mock actual es muy básico. En una fase de desarrollo más avanzada, podríamos querer:
- Usar una librería de mocking más sofisticada.
- Tener un conjunto más amplio de ejemplos de entrada/salida.
- Simular errores de la API de Gemini (ej. timeouts, respuestas malformadas).

Por ahora, esto debería ser suficiente para continuar con la integración.
Una vez que tengamos acceso real y configuración para la API de Gemini, la sección mockeada se reemplazará con la llamada real usando `vertexai` o la librería correspondiente.
"""
