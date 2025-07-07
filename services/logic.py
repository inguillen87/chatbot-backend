import logging
from flask import current_app # Para logging y config
from models import ArchivoAdjunto, AnalisisArchivo, db # db para la sesión
from services.interpretacion_service import interpretacion_service
from services.archivo_service import archivo_service
# servicio_tickets se importa/usa en los handlers específicos (municipios.py, pymes.py)

logger = logging.getLogger(__name__)

# Rubros que deben usar la lógica de municipio/ente público
RUBROS_PUBLICOS = {
    "municipio",
    "municipios",
    "ong",
    "gobierno",
    "hospital_publico",
    "entidad_publica",
    # Agregá acá los que consideres públicos
}

def normalizar_rubro(rubro) -> str:
    """Devuelve el nombre del rubro en minúsculas."""
    if not rubro:
        return ""
    if isinstance(rubro, str):
        return rubro.strip().lower()
    if hasattr(rubro, "nombre") and getattr(rubro, "nombre"):
        return str(rubro.nombre).strip().lower()
    if hasattr(rubro, "clave") and getattr(rubro, "clave"):
        return str(rubro.clave).strip().lower()
    return str(rubro).strip().lower()


def es_rubro_publico(rubro) -> bool:
    """Indica si un rubro pertenece a ``RUBROS_PUBLICOS``."""
    return normalizar_rubro(rubro) in RUBROS_PUBLICOS

try:
    from services.cohere_ai import get_cohere_response
except Exception:  # pragma: no cover - fallback for tests
    from services.cohere_ai import robust_chat as get_cohere_response

# --- Utilidades para small talk ---
PROMPT_DETECT_SMALL_TALK = """
Analiza la FRASE DEL USUARIO y responde únicamente "SI" o "NO".
Responde "SI" si la frase es simplemente una charla casual o un saludo sin una
solicitud específica. Responde "NO" en caso contrario.

FRASE DEL USUARIO: "{pregunta_usuario}"
"""

PROMPT_RESPUESTA_SMALL_TALK = """
Responde de manera cordial y breve en español a la FRASE DEL USUARIO y luego
ofrece tu ayuda.

FRASE DEL USUARIO: "{pregunta_usuario}"
"""


def detectar_small_talk_con_llm(pregunta: str) -> bool:
    """Devuelve ``True`` si la pregunta parece small talk según el LLM."""
    prompt = PROMPT_DETECT_SMALL_TALK.format(pregunta_usuario=pregunta)
    try:
        decision = get_cohere_response(
            message=prompt,
            preamble="Eres un clasificador de small talk. Responde solo SI o NO.",
        )
        return decision.strip().upper().startswith("SI")
    except Exception as e:
        logger.error(f"[SMALL_TALK] Error detectando small talk: {e}")
        return False


def generar_respuesta_small_talk(pregunta: str) -> str:
    """Genera una respuesta cordial para una frase de small talk."""
    prompt = PROMPT_RESPUESTA_SMALL_TALK.format(pregunta_usuario=pregunta)
    try:
        return get_cohere_response(
            message=prompt,
            preamble="Eres un asistente amigable que mantiene charlas casuales.",
        ).strip()
    except Exception as e:
        logger.error(f"[SMALL_TALK] Error generando respuesta: {e}")
        return "¡Hola! ¿En qué puedo ayudarte?"

# Puedes ajustar este prompt según las intenciones que quieras clasificar
PROMPT_CLASIFICACION_INTENCION = """
Analiza la siguiente PREGUNTA DEL USUARIO y clasifica su INTENCIÓN.
Responde ÚNICAMENTE con una de las INTENCIONES POSIBLES de la lista.
Si la pregunta no encaja claramente en ninguna de las categorías específicas, clasifícala como 'general'.

INTENCIONES POSIBLES:
- iniciar_reclamo: El usuario expresa deseo de presentar una queja, problema o denuncia.
    Ejemplos: "quiero reclamar por una luz quemada", "hay mucha basura en la esquina de mi casa", "el servicio de agua no funciona", "tengo un problema con el pavimento"
- consultar_estado_ticket: El usuario quiere saber el estado o progreso de un ticket, reclamo o trámite ya iniciado.
    Ejemplos: "cómo va mi reclamo 12345?", "quisiera saber el estado de mi gestión", "alguna novedad sobre el ticket M-5567?"
- consultar_impuestos: El usuario pregunta sobre impuestos municipales, tasas, facturas, boletas o formas de pago relacionadas.
    Ejemplos: "quiero pagar mis impuestos", "cómo pago la tasa municipal?", "dónde puedo ver mi boleta de ABL?", "cuánto debo de patentes?"
- consultar_tramite: El usuario pregunta sobre cómo realizar un trámite, requisitos, horarios o lugares para trámites municipales.
    Ejemplos: "requisitos para licencia de conducir", "cómo se hace la habilitación comercial?", "dónde saco el certificado de domicilio?", "horario para renovar DNI"
- hacer_sugerencia: El usuario quiere proponer una idea, mejora o dar una opinión constructiva.
    Ejemplos: "deberían poner más bancos en la plaza", "sugiero que mejoren la iluminación del parque", "tengo una idea para el tránsito"
- hablar_con_agente: El usuario solicita explícitamente hablar con una persona, empleado o representante.
    Ejemplos: "necesito hablar con alguien", "quiero hablar con una persona", "pasame con un humano", "me pasas con un operador?", "quiero un representante"
- general: Cualquier otra consulta que no encaje en las anteriores, o preguntas generales sobre el municipio.
    Ejemplos: "cuál es el teléfono del intendente?", "historia de la ciudad", "eventos culturales este fin de semana"

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

INTENCIÓN: """

def _clasificar_intencion_con_llm(pregunta: str) -> str:
    """
    Clasifica la intención de la pregunta del usuario utilizando un modelo de lenguaje.
    """
    logger.info(f"[CLASIFICADOR INTENCION] Clasificando intención para: '{pregunta}'")

    prompt = PROMPT_CLASIFICACION_INTENCION.format(pregunta_usuario=pregunta)

    try:
        # Asegúrate de que get_cohere_response esté correctamente configurado
        # para tu API de Cohere o el LLM que estés usando.
        # El preamble aquí es opcional, pero ayuda a guiar el LLM.
        intencion = get_cohere_response(
            message=prompt, 
            preamble="Eres un clasificador de intención de usuario. Responde solo con la intención clasificada."
        )
        # Limpia cualquier espacio en blanco o caracter especial
        intencion_limpia = intencion.strip().lower()
        logger.info(f"[CLASIFICADOR INTENCION] Intención detectada: '{intencion_limpia}'")
        return intencion_limpia
    except Exception as e:
        logger.error(f"[CLASIFICADOR INTENCION] Error al clasificar intención con LLM: {e}")
        return "general" # Retorna una intención por defecto en caso de error

# ... otras funciones que ya tengas en logic.py (como responder_chatboc)
def responder_chatboc(
    pregunta,
    owner_user=None,
    current_user=None,
    rubro_obj=None,
    session_obj=None,
    rubro_nombre_frontend=None,
    tipo_chat=None,
    anon_id=None,
    chat_session_uuid=None, # Nuevo parámetro
    **kwargs,
):
    """Envía la consulta al handler correcto según el rubro y tipo de chat."""
    logger.debug(f"[responder_chatboc] START - Args: pregunta='{pregunta}', owner_user_id='{getattr(owner_user, 'id', 'N/A')}', current_user_id='{getattr(current_user, 'id', 'N/A')}', anon_id='{anon_id}', tipo_chat_inicial='{tipo_chat}', rubro_obj_id='{getattr(rubro_obj, 'id', 'N/A')}'")

    # 1. Determinar el 'effective_owner_user' (la entidad o bot dueño)
    effective_owner_user = owner_user
    if not effective_owner_user and current_user and current_user.empresa_id:
        from models import User # Local import
        logger.debug(f"[responder_chatboc] No owner_user arg, but current_user ({current_user.id}) has empresa_id ({current_user.empresa_id}). Attempting to load owner.")
        potential_owner = User.query.get(current_user.empresa_id)
        if potential_owner:
            effective_owner_user = potential_owner
            logger.info(f"[responder_chatboc] Set effective_owner_user to User ID {effective_owner_user.id} (Name: {effective_owner_user.nombre_empresa}) via current_user.empresa_id.")
        else: #pragma: no cover
            logger.warning(f"[responder_chatboc] current_user.empresa_id ({current_user.empresa_id}) did not resolve to a valid User. effective_owner_user remains None.")

    if not effective_owner_user:
        logger.warning(f"[responder_chatboc] 'effective_owner_user' could not be determined. This is critical for context-specific logic (e.g., for /ask/municipio). Check if a valid entity token is being passed for the bot instance.")

    # 2. Detectar nombre de rubro (universal)
    rubro_nombre = ""
    fuente = ""
    if rubro_obj:
        if getattr(rubro_obj, "nombre", None):
            rubro_nombre = str(rubro_obj.nombre).strip().lower()
            fuente = "rubro_obj.nombre"
        elif getattr(rubro_obj, "clave", None):
            rubro_nombre = str(rubro_obj.clave).strip().lower()
            fuente = "rubro_obj.clave"
    elif owner_user and getattr(owner_user, "rubro", None):
        rubro_value = owner_user.rubro
        if hasattr(rubro_value, "nombre") and rubro_value.nombre:
            rubro_nombre = str(rubro_value.nombre).strip().lower()
            fuente = "owner_user.rubro.nombre"
        elif hasattr(rubro_value, "clave") and rubro_value.clave:
            rubro_nombre = str(rubro_value.clave).strip().lower()
            fuente = "owner_user.rubro.clave"
        else:
            rubro_nombre = str(rubro_value).strip().lower()
            fuente = "owner_user.rubro (str)"
    elif rubro_nombre_frontend:
        rubro_nombre = str(rubro_nombre_frontend).strip().lower()
        fuente = "rubro_nombre_frontend"
    else:
        rubro_nombre = ""
        fuente = "no_encontrado"

    # logger.info(  # Comentado para reducir verbosidad, la siguiente línea es más completa.
    #     f"[LOGIC] Usando rubro: '{rubro_nombre}' (fuente: {fuente}, user: {getattr(owner_user, 'id', None)})"
    # )

    # Si el rubro indica un tipo específico de lógica, lo usamos siempre
    if rubro_nombre:
        esperado = "municipio" if es_rubro_publico(rubro_nombre) else "pyme"
        if tipo_chat and tipo_chat != esperado:
            logger.info(
                "Ajustando tipo_chat de '%s' a '%s' por rubro público '%s'",
                tipo_chat,
                esperado,
                rubro_nombre,
            )
        tipo_chat = esperado
    elif tipo_chat not in ("municipio", "pyme"):
        raise ValueError(f"Tipo de chat inválido: {tipo_chat}")

    if not tipo_chat:
        raise ValueError("tipo_chat requerido")

    # ... (lógica existente para determinar rubro_nombre y tipo_chat) ...
    # Esta parte permanece igual.
    rubro_nombre = ""
    fuente = ""
    if rubro_obj:
        if getattr(rubro_obj, "nombre", None):
            rubro_nombre = str(rubro_obj.nombre).strip().lower()
            fuente = "rubro_obj.nombre"
        elif getattr(rubro_obj, "clave", None):
            rubro_nombre = str(rubro_obj.clave).strip().lower()
            fuente = "rubro_obj.clave"
    elif owner_user and getattr(owner_user, "rubro", None):
        rubro_value = owner_user.rubro
        # Asegurarse de acceder a .clave si rubro_value es un objeto Rubro
        rubro_clave_o_nombre = getattr(rubro_value, 'clave', None) or getattr(rubro_value, 'nombre', None)
        if rubro_clave_o_nombre:
            rubro_nombre = str(rubro_clave_o_nombre).strip().lower()
            fuente = f"owner_user.rubro.{'clave' if getattr(rubro_value, 'clave', None) else 'nombre'}"
        else: # Si no tiene clave ni nombre, convertir a string (caso raro)
            rubro_nombre = str(rubro_value).strip().lower()
            fuente = "owner_user.rubro (str)"

    elif rubro_nombre_frontend:
        rubro_nombre = str(rubro_nombre_frontend).strip().lower()
        fuente = "rubro_nombre_frontend"
    else:
        rubro_nombre = ""
        fuente = "no_encontrado"

    logger.info(
        f"[LOGIC] Usando rubro: '{rubro_nombre}' (fuente: {fuente}, user: {getattr(owner_user, 'id', None)})"
    )

    # Si el rubro indica un tipo específico de lógica, lo usamos siempre
    if rubro_nombre:
        # Usar la clave del rubro para es_rubro_publico si rubro_nombre vino de un objeto rubro con clave
        # Esto es importante porque RUBROS_PUBLICOS se basa en claves normalizadas.
        clave_para_chequeo_publico = rubro_nombre
        if rubro_obj and hasattr(rubro_obj, 'clave') and rubro_obj.clave:
            clave_para_chequeo_publico = rubro_obj.clave.strip().lower()
        elif owner_user and hasattr(owner_user, 'rubro') and owner_user.rubro and hasattr(owner_user.rubro, 'clave') and owner_user.rubro.clave:
             clave_para_chequeo_publico = owner_user.rubro.clave.strip().lower()


        esperado = "municipio" if es_rubro_publico(clave_para_chequeo_publico) else "pyme"
        if tipo_chat and tipo_chat != esperado:
            logger.info( # Este log es importante si hay un ajuste
                f"Ajustando tipo_chat de '{tipo_chat}' a '{esperado}' basado en rubro '{rubro_nombre}' (clave chequeada: '{clave_para_chequeo_publico}')"
            )
        tipo_chat = esperado
    elif tipo_chat not in ("municipio", "pyme"): # Si no hay rubro, el tipo_chat debe ser válido
        # Esta condición podría necesitar revisión. Si no hay rubro Y no hay tipo_chat válido,
        # es un error. Pero si tipo_chat es válido y no hay rubro, podría ser un chat genérico.
        # Por ahora, mantenemos: si no hay rubro, tipo_chat debe ser explícito y válido.
        logger.error(f"Tipo de chat inválido ('{tipo_chat}') o no determinable sin un rubro claro.")
        raise ValueError(f"Tipo de chat inválido o no determinable sin rubro: {tipo_chat}")

    if not tipo_chat: # Si después de todo no se pudo determinar
        logger.error("Error crítico: tipo_chat no pudo ser determinado.")
        raise ValueError("tipo_chat requerido y no pudo ser determinado.")

    logger.info(
        f"[LOGIC_DELEGATION_PREP] Preparando para delegar. "
        f"OwnerUserID: {getattr(owner_user, 'id', 'N/A')}, "
        f"ViewerUserID: {getattr(current_user, 'id', 'N/A')}, "
        f"AnonID: {anon_id if anon_id else 'N/A'}, "
        f"ChatSessionUUID: {chat_session_uuid if chat_session_uuid else 'N/A'}, "
        f"RubroEfectivo: '{rubro_nombre}' (detectado de: {fuente}), "
        f"RubroObjectID: {getattr(rubro_obj, 'id', 'N/A')}, "
        f"TipoChatFinal: {tipo_chat}."
    )

    # --- Inicio: Lógica de manejo de archivo adjunto y su análisis ---
    uploaded_file_info = kwargs.get("uploaded_file_info")
    datos_interpretados_de_archivo = None
    archivo_id_para_asociar_al_ticket = None
    procesamiento_archivo_en_curso = False # Nueva bandera

    if uploaded_file_info and isinstance(uploaded_file_info, dict) and uploaded_file_info.get("id"):
        archivo_id = uploaded_file_info.get("id")
        current_app.logger.info(f"[LOGIC] Procesando uploaded_file_info para ArchivoAdjunto ID: {archivo_id}")

        # Usar db.session del contexto de la aplicación actual
        archivo_obj = db.session.query(ArchivoAdjunto).get(archivo_id)

        if archivo_obj:
            archivo_id_para_asociar_al_ticket = archivo_id # Guardar para asociar incluso si el análisis está pendiente

            if archivo_obj.analisis: # Si existe un registro de análisis
                estado_analisis_actual = archivo_obj.analisis.estado_analisis
                current_app.logger.info(f"[LOGIC] ArchivoAdjunto ID: {archivo_id} tiene análisis con estado: {estado_analisis_actual}")

                if estado_analisis_actual == "completado":
                    user_id_actual = owner_user.id if owner_user else None
                    datos_interpretados_de_archivo = interpretacion_service.interpretar_analisis_para_datos_ticket(
                        analisis_archivo=archivo_obj.analisis,
                        tipo_contexto=tipo_chat, # tipo_chat ya está corregido según el rubro
                        user_id=user_id_actual
                    )
                    if datos_interpretados_de_archivo:
                        current_app.logger.info(f"[LOGIC] Datos interpretados del archivo: {datos_interpretados_de_archivo}")
                    else:
                        current_app.logger.info(f"[LOGIC] Análisis completado pero sin datos interpretables para el contexto {tipo_chat}.")
                        # Podríamos querer enviar un mensaje genérico si el análisis no produjo nada útil aquí.
                        # Por ejemplo, si es una imagen que no es un reclamo.
                        if archivo_obj.analisis.tipo_analisis == 'imagen_general_vision_v1':
                             datos_interpretados_de_archivo = {
                                 "_mensaje_bot": "He procesado la imagen, pero no parece ser un reclamo o pedido claro. ¿Podrías describirme qué necesitas o qué ves en la imagen?",
                                 "es_reclamo": False # Asegurar que se marque como no reclamo
                             }


                elif estado_analisis_actual in ["pendiente", "procesando"]:
                    current_app.logger.info(f"[LOGIC] Análisis para ArchivoAdjunto ID: {archivo_id} aún está '{estado_analisis_actual}'.")
                    procesamiento_archivo_en_curso = True
                    # Guardar en contexto de sesión que hay un archivo procesándose
                    if session_obj: # session_obj es la sesión de Flask
                         session_obj[f'archivo_procesando_{chat_session_uuid}'] = archivo_id
                    # Devolver respuesta indicando que se está procesando
                    return {
                        "respuesta": "Estoy analizando el archivo que subiste. Te avisaré en cuanto termine. Mientras tanto, ¿puedo ayudarte con otra cosa o prefieres esperar?",
                        "contexto_pyme": kwargs.get("contexto_previo"), # Devolver contexto sin cambios
                        "contexto_municipio": kwargs.get("contexto_previo"),
                        "botones": [{"texto": "Esperar resultado", "payload": f"consultar_analisis:{archivo_id}"}],
                        # Otros campos que devuelve normalmente tu API
                        "fuente": "sistema",
                        "tipo_respuesta": "espera_analisis_archivo"
                    }
                elif estado_analisis_actual == "error":
                    current_app.logger.error(f"[LOGIC] Análisis para ArchivoAdjunto ID: {archivo_id} resultó en error: {archivo_obj.analisis.error_analisis}")
                    # Informar al usuario del error
                    return {
                        "respuesta": f"Hubo un problema al analizar el archivo: {archivo_obj.analisis.error_analisis}. Por favor, intenta subirlo de nuevo o describe tu consulta.",
                        "contexto_pyme": kwargs.get("contexto_previo"),
                        "contexto_municipio": kwargs.get("contexto_previo"),
                        "fuente": "sistema_error",
                        "tipo_respuesta": "error_analisis_archivo"
                    }
                else: # Otros estados o si no hay datos interpretados
                    current_app.logger.info(f"[LOGIC] Análisis para ArchivoAdjunto ID: {archivo_id} en estado '{estado_analisis_actual}' no produjo datos directamente utilizables en este flujo.")

            else: # No hay registro de AnalisisArchivo (esto no debería ocurrir si se crea en /subir)
                current_app.logger.warning(f"[LOGIC] No existe registro AnalisisArchivo para ArchivoAdjunto ID: {archivo_id}. Esto es inesperado si el análisis se inicia al subir.")
                # Podríamos forzar una respuesta de "procesando" si esto ocurre, asumiendo que la tarea se está ejecutando.
                # O considerarlo un error si siempre debería existir.
                # Por ahora, trataremos como si estuviera pendiente.
                procesamiento_archivo_en_curso = True
                if session_obj:
                    session_obj[f'archivo_procesando_{chat_session_uuid}'] = archivo_id
                return {
                    "respuesta": "Estoy preparando tu archivo para el análisis. Te avisaré en breve. Mientras tanto, ¿puedo ayudarte con otra cosa?",
                    "contexto_pyme": kwargs.get("contexto_previo"),
                    "contexto_municipio": kwargs.get("contexto_previo"),
                    "botones": [{"texto": "Esperar resultado", "payload": f"consultar_analisis:{archivo_id}"}],
                    "fuente": "sistema",
                    "tipo_respuesta": "espera_analisis_archivo"
                }
        else:
            current_app.logger.warning(f"[LOGIC] No se encontró ArchivoAdjunto con ID: {archivo_id} desde uploaded_file_info.")
            # No podemos asociar si no encontramos el archivo, y no hay nada que procesar.
            # Esto podría ser un error en el ID enviado por el frontend.

    # Si el usuario envía un payload "consultar_analisis:ID_ARCHIVO"
    if pregunta.startswith("consultar_analisis:"):
        id_archivo_a_consultar = pregunta.split(":")[1]
        current_app.logger.info(f"[LOGIC] Usuario consulta estado de análisis para archivo ID: {id_archivo_a_consultar}")
        # Aquí se re-ejecutaría la lógica de verificación de estado de arriba.
        # Esto requiere que el frontend envíe este payload como una "pregunta".
        # Este es un re-entry point.
        # Para evitar duplicar código, podríamos refactorizar la lógica de chequeo de estado.
        # Por ahora, si llega aquí, se volverá a chequear el estado del archivo_id_a_consultar
        # si el frontend lo reenvía en uploaded_file_info (o si lo pasamos de otra forma).
        # Mejor: si el payload es `consultar_analisis:ID`, forzamos uploaded_file_info.
        if not uploaded_file_info: # Si no vino en el request original, lo simulamos para el chequeo
             kwargs["uploaded_file_info"] = {"id": id_archivo_a_consultar}
             # Y volvemos a llamar a responder_chatboc recursivamente (cuidado con bucles infinitos)
             # O, mejor, refactorizar la lógica de chequeo.
             # Por ahora, si se llega aquí con ese payload, el flujo de arriba lo manejará si
             # el ID se vuelve a poner en uploaded_file_info.
             # Esto es un poco enrevesado. Una mejor solución sería tener un endpoint específico para consultar estado.
             # O que el handler de municipio/pyme maneje este payload.
             # ----
             # Simplificación: El chequeo de `uploaded_file_info` ya está arriba. Si el frontend envía
             # `pregunta = "consultar_analisis:ID"` Y `uploaded_file_info = {"id": ID}`, la lógica de arriba
             # se activará y chequeará el estado. El `pregunta` en sí mismo no se usará para el LLM en ese caso.
             pass


    # Actualizar kwargs para pasar la información a los handlers específicos
    kwargs["datos_interpretados_archivo"] = datos_interpretados_de_archivo
    kwargs["archivo_id_para_asociar"] = archivo_id_para_asociar_al_ticket

    if "uploaded_file_info" in kwargs: # Limpiar para no pasarlo si ya se usó.
        del kwargs["uploaded_file_info"]
    # --- Fin: Lógica de manejo de archivo adjunto ---

    # Si un archivo está en proceso, no pasamos al LLM de intención general ni small talk.
    # La respuesta ya se dio arriba.
    # No, esto no es correcto. Si el archivo se está procesando, ya retornamos.
    # Si llegamos aquí, o no hubo archivo, o el análisis se completó (y datos_interpretados_archivo está poblado o es None).

    # --- Inicio: Intento de búsqueda y uso de Plantillas de Respuesta ---
    # respuesta_con_plantilla = None
    # if not procesamiento_archivo_en_curso: # No buscar plantillas si estamos esperando análisis de archivo
    #     try:
    #         from services.template_service import buscar_plantillas_relevantes, formatear_plantilla
    #
    #         # Construir un contexto básico para formatear plantillas
    #         # Este contexto se puede enriquecer mucho más en los handlers específicos (pyme/municipio)
    #         # si deciden usar una plantilla.
    #         contexto_para_plantilla = {
    #             "usuario": owner_user, # El objeto User completo
    #             "pregunta_usuario": pregunta,
    #             "datos_archivo": datos_interpretados_de_archivo, # Si los hay
    #             # Se podrían añadir más datos generales aquí si son útiles para plantillas genéricas
    #         }
    #
    #         plantillas_encontradas = buscar_plantillas_relevantes(texto_consulta=pregunta, top_n=1)
    #
    #         if plantillas_encontradas:
    #             plantilla_seleccionada = plantillas_encontradas[0]
    #             logger.info(f"Plantilla relevante encontrada: '{plantilla_seleccionada.name}' (ID: {plantilla_seleccionada.id})")
    #
    #             # Aquí es donde la lógica se puede complicar:
    #             # 1. ¿La plantilla es suficiente por sí misma?
    #             # 2. ¿Necesita datos adicionales que solo el handler pyme/municipio puede proveer?
    #             # 3. ¿Debería el handler pyme/municipio ser responsable de llamar a formatear_plantilla?
    #
    #             # Opción A: Si la plantilla es muy genérica y se puede formatear con contexto_para_plantilla:
    #             # texto_respuesta_plantilla = formatear_plantilla(plantilla_seleccionada.text, contexto_para_plantilla)
    #             # respuesta_con_plantilla = {
    #             #     "respuesta": texto_respuesta_plantilla,
    #             #     "fuente": f"plantilla:{plantilla_seleccionada.id}",
    #             #     "tipo_respuesta": "plantilla_directa",
    #             #     # ... otros campos necesarios como contexto_pyme/municipio ...
    #             # }
    #             # logger.info(f"Respondiendo directamente con plantilla formateada ID {plantilla_seleccionada.id}")
    #
    #             # Opción B: Pasar la plantilla seleccionada al handler (pyme/municipio) para que decida.
    #             # El handler puede entonces enriquecer el contexto y llamar a formatear_plantilla.
    #             # Esto parece más flexible.
    #             kwargs["plantilla_sugerida"] = plantilla_seleccionada
    #             logger.info(f"Pasando plantilla sugerida '{plantilla_seleccionada.name}' al handler {tipo_chat}.")
    #
    #     except ImportError:
    #         logger.warning("Modulo template_service no encontrado. Búsqueda de plantillas desactivada.") # This will no longer be hit
    #     except Exception as e_template:
    #         logger.error(f"Error durante la búsqueda o formateo inicial de plantillas: {e_template}", exc_info=True)
    #         # Continuar sin plantilla si hay error aquí.
    #
    # # Si ya tenemos una respuesta de plantilla directa (Opción A), podríamos retornarla aquí.
    # # if respuesta_con_plantilla:
    # #    # Asegurarse de que el contexto de sesión (contexto_pyme/municipio) se actualice y devuelva correctamente.
    # #    # Esto es complejo si la plantilla es genérica y no actualiza el contexto específico.
    # #    # Por ahora, preferimos Opción B (pasar al handler).
    # #    pass
    # --- Fin: Intento de búsqueda y uso de Plantillas de Respuesta ---


    # Pasar el contexto_previo correcto al handler
    # El contexto_previo que llega a kwargs es el genérico.
    # Los handlers esperan "contexto_pyme" o "contexto_municipio".
    # El session_obj (Flask session) se usa para almacenar el contexto entre llamadas.
    # El `contexto_previo` en kwargs debería ser el específico del tipo_chat.

    # La variable `session_obj` que se pasa a los handlers es la sesión de Flask.
    # Los handlers (responder_municipio, responder_pyme) son responsables de cargar/guardar
    # su propio contexto de esa sesión Flask usando chat_session_uuid.

    if tipo_chat == "municipio":
        from services.municipios import responder_municipio
        return responder_municipio(
            pregunta_original=pregunta, # La pregunta original del usuario
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=current_user,
            session_obj=session_obj, # Flask session
            anon_id=anon_id,
            chat_session_uuid=chat_session_uuid,
            **kwargs, # Contiene datos_interpretados_archivo y archivo_id_para_asociar
        )
    elif tipo_chat == "pyme":
        from services.pymes import responder_pyme
        return responder_pyme(
            pregunta=pregunta,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=current_user,
            session_obj=session_obj, # Flask session
            anon_id=anon_id,
            chat_session_uuid=chat_session_uuid,
            **kwargs,
        )
    else:
        # Esto no debería ocurrir debido a las validaciones previas de tipo_chat
        logger.error(f"Error crítico: tipo_chat '{tipo_chat}' no es ni 'municipio' ni 'pyme' en la parte final de responder_chatboc.")
        return {"respuesta": "Error interno: tipo de chat no configurado correctamente.", "fuente": "sistema_error"}
