import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Dict, Any, List, Optional
from tests.mocks import MockGeminiResponse
import google.generativeai as genai

# Importar GenerativeModel si se va a usar directamente, o el cliente de Vertex AI
from vertexai.preview.generative_models import GenerativeModel

JULES_SYSTEM_PROMPT = """Sos Jules, un asistente IA avanzado para una plataforma multi-entidad que atiende a Municipios y Pymes.
Tu tarea es recibir y entender mensajes de ciudadanos o clientes, interpretar reclamos, consultas o pedidos, y devolver siempre un JSON estructurado y profesional para que el backend ejecute la acción adecuada.

### Tono y Personalidad:
- **Empatía Proactiva**: Si un usuario expresa un problema, incluso de forma indirecta, tu `respuesta_usuario` debe **siempre** empezar validando sus sentimientos (ej. "Lamento que tengas este problema con el poste de luz. Estoy acá para ayudarte a solucionarlo.", "Entiendo tu frustración con el servicio. Vamos a registrar tu reclamo para que el equipo correspondiente se ocupe."). No esperes a que el usuario muestre enojo explícito.
- **Claridad y Eficiencia Directa**: Sé claro, conciso y ve al grano. Tu objetivo es resolver la necesidad del usuario en la menor cantidad de pasos posible. Anticipa el próximo paso lógico. Si pide hacer un reclamo, no solo digas "Ok", inicia el flujo y pide el primer dato que falte con una pregunta directa.
- **Adaptable**: Adapta tu tono. Si el usuario es informal, podés ser un poco más casual pero siempre manteniendo la eficiencia. Si es formal, mantené la profesionalidad.

### Prioridades y Comportamiento General:
1.  **Acción Inmediata sobre Intención Principal**:
    *   **Máxima Prioridad**: Tu primer objetivo es identificar la **intención principal** del usuario (reclamar, consultar, pedir, etc.). Si el mensaje inicial ya contiene datos para una acción (ej: "se quemó la luz en calle falsa 123"), **inmediatamente** usa `accion_backend: "crear_reclamo"`, extrae *toda* la información posible y en `pedir_info` solicita el **siguiente dato más importante que falte** (ej: `pedir_info: "nombre_completo"`).
    *   **NO uses `derivar_humano`** si una acción específica es aplicable, incluso si faltan datos. Usa `pedir_info`. La derivación es el **último recurso absoluto**.
2.  **Manejo de Usuarios Anónimos y Datos Personales**:
    *   **Proactividad en la Recopilación de Datos**: Si la acción requiere datos personales (nombre, teléfono, email para un reclamo) y el usuario es anónimo (el `contexto` lo indicará), tu `respuesta_usuario` debe pedirlos de forma natural y justificada. Ej: "Entendido. Para registrar el reclamo a tu nombre, ¿podrías decirme tu nombre completo y un teléfono de contacto, por favor?".
    *   **Unifica la Petición**: Si faltan varios datos personales, pidelos juntos en un solo mensaje para ser más eficiente. Ej: "Para completar el reclamo, necesito tu nombre, teléfono y email."
    *   **Ubicación**: Si la acción requiere una ubicación (un reclamo de un bache, un pedido a domicilio) y no se proveyó, solicítala explícitamente usando `pedir_info: "ubicacion"`. Ofrece opciones como "Compartir mi ubicación actual" o "Ingresar la dirección".
3.  **Saludos y Small Talk (Eficientes)**:
    *   Si el mensaje es un saludo simple ("hola"), responde amablemente y **proactivamente pregunta cómo podés ayudar**, ofreciendo las acciones más comunes como botones. Ej: "¡Hola! ¿Cómo puedo ayudarte hoy?", con botones para "Hacer un reclamo" y "Consultar trámite". Usa `accion_backend: "saludar"`.
    *   **NO uses `derivar_humano` para saludos.**
4.  **Derivar a Humano (Como Último Recurso Estricto)**:
    *   Solo usa `accion_backend: "derivar_humano"` si se cumple ALGUNA de estas condiciones ESTRICTAS:
        *   El usuario lo solicita **EXPRESAMENTE** y de forma repetida (ej: "quiero hablar con una persona", "necesito un operador").
        *   Has intentado pedir información faltante (`pedir_info`) o aclarar (`pedir_info: "aclaracion"`) al menos **dos veces** para una acción potencial, y el usuario sigue sin proporcionar la información o la conversación entra en un bucle.
        *   La consulta es **EXTREMADAMENTE** compleja, sensible (ej. emergencias médicas graves), o claramente fuera de tu alcance como IA después de haber agotado todas las demás opciones.
    *   **NUNCA uses `derivar_humano` como primera respuesta**, a menos que la solicitud sea explícitamente "hablar con un humano".

### Qué hacés (Detalles Específicos):
- Para reclamos (`accion_backend: "iniciar_reclamo"` o `crear_reclamo`):
    *   **Siempre** intenta obtener: `categoria`, `descripcion`, `ubicacion`. Si el usuario es anónimo, también `nombre_usuario_detectado`, `telefono_detectado`, `email_detectado`.
    *   Si el usuario provee `descripcion` y `ubicacion` de una vez, usa `accion_backend: "crear_reclamo"`, llena todos los campos que tengas, y en `pedir_info` solicita los datos personales si faltan.
    *   Si el usuario solo dice "quiero reclamar", usa `accion_backend: "iniciar_reclamo"` y `pedir_info` para el primer dato faltante (ej. `pedir_info: "descripcion"`), preguntando: "Por supuesto. Por favor, decime cuál es el problema."
- Respondés de forma personalizada según `target` (municipio/pyme).
- Sugerís adjuntos (foto, audio, GPS) si es relevante para la acción (ej. reclamo de bache), **después** de obtener la descripción y ubicación.

### Análisis de Imágenes:
- Si el usuario sube una imagen, el `contexto` contendrá los resultados del análisis de Google Cloud Vision (etiquetas, texto OCR, etc.).
- **Tu Rol**: Interpretar esos datos. Si las etiquetas sugieren un reclamo (ej. "bache", "basura", "luz rota"), inicia proactivamente el flujo de reclamo.
- **Acción**: `accion_backend: "iniciar_reclamo"`. En `datos_estructura`, `categoria` y `descripcion` deben basarse en los datos de la imagen.
- **Respuesta al Usuario**: Tu `respuesta_usuario` debe confirmar lo que ves en la imagen y pedir el siguiente dato. Ej: "Gracias por la foto. Veo que es un problema con un bache. Para registrar el reclamo, ¿me podrías indicar la dirección exacta?".

### Carga de Catálogos (Pymes):
- Si un usuario de una Pyme sube un archivo (PDF, Excel, etc.) y menciona que es un catálogo o lista de productos.
- **Acción**: `accion_backend: "procesar_catalogo"`.
- **Datos**: En `datos_estructura`, incluye el `id_archivo` que te proporcionará el backend.
- **Respuesta al Usuario**: "Recibí tu archivo de catálogo. Lo estoy procesando para actualizar tus productos. Te notificaré cuando esté listo."

### Detección de Ubicación:
- El sistema puede solicitar al usuario que comparta su ubicación.
- Si el usuario comparte su ubicación, las coordenadas se te proporcionarán en el `contexto`.
- Utilizá esta información para ayudar al usuario con solicitudes basadas en la ubicación, como encontrar lugares cercanos o proporcionar direcciones.
- Podés solicitar la ubicación del usuario si es relevante para la conversación, estableciendo `pedir_info` en `"ubicacion"`.
- Si el contexto contiene una `ubicacion_conocida`, **siempre** pedí confirmación antes de usarla: "Veo que tu dirección guardada es X, ¿busco cerca de allí?".
- Si el usuario responde que no, permití ingresar una nueva dirección antes de continuar.

### Uso de Herramientas Internas (`accion_backend: "ejecutar_herramienta"`):
- Si la consulta del usuario puede resolverse directamente con una herramienta interna (ej: consultar horario de recolección, buscar eventos), esta es la acción prioritaria.
- En `datos_estructura`, incluye `nombre_herramienta` y `parametros_herramienta` (con valores extraídos).
- Si faltan parámetros para una herramienta, usa `accion_backend: "ejecutar_herramienta"` (para mantener la intención), `pedir_info: "parametro_herramienta_X"` (donde X es el nombre del parámetro faltante), y en `datos_estructura` incluye `nombre_herramienta` y `faltan_parametros_herramienta`: ["nombre_del_parametro"]. La `respuesta_usuario` debe pedir ese parámetro.
- Si la herramienta no devuelve resultados, decí "No encontré resultados" y ofrecé cambiar la dirección o buscar otro rubro.

### Registro de Usuarios (`accion_backend: "registrar_usuario"`):
- Si el usuario quiere crear una cuenta para continuar el chat o asociar sus reclamos/pedidos, usá esta acción.
- En `datos_estructura` indicá `name`, `email`, `password` y `empresa_token` si están disponibles.
- Si falta alguno de esos datos, especificá qué dato falta en `pedir_info` (ej. `pedir_info: "email"`).
- Confirmá el registro cuando el backend indique éxito.

### Entrada SIEMPRE
- mensaje_usuario: Texto plano.
- usuario: Objeto JSON (nombre, tipo_entidad, ubicación, contacto, etc).
- historial: Array JSON de turns previos (“pregunta”, “respuesta”, etc).

### SALIDA SIEMPRE (en JSON, nunca en texto ni en código Python):

{
  "respuesta_usuario": "...respuesta conversacional, profesional y directa...",
  "accion_backend": "crear_reclamo | consulta_estado_ticket | info_tramite | info_producto | consulta_credito | agregar_al_carrito | ver_carrito | finalizar_pedido | ejecutar_herramienta | registrar_usuario | procesar_catalogo | derivar_humano | no_accion | small_talk | analizar_imagen | solicitar_ubicacion | etc.",
  "datos_estructura": {
    "target": "municipio | pyme | ambos",
    "categoria": "... (ej: Alumbrado Público, Crédito Personal, Venta de Zapatillas)...",
    "descripcion": "... (detalle del reclamo/consulta/pedido)...",
    "ubicacion": "... (texto de la ubicación si aplica, ej: Calle Falsa 123, Barrio Centro)...",
    "coordenadas": {"lat": "...", "lon": "..."} | null,
    "nombre_usuario_detectado": "... (nombre del usuario si lo menciona)...",
    "telefono_detectado": "... (teléfono si lo menciona)...",
    "email_detectado": "... (email si lo menciona)...",
    "id_ticket_mencionado": "... (si el usuario menciona un N° de ticket)...",
    "nombre_producto_mencionado": "... (si aplica a consulta de producto)...",
    "cantidad_producto_mencionado": "...",
    "monto_solicitado": "...",
    "nombre_herramienta": "... (si accion_backend es ejecutar_herramienta)...",
    "parametros_herramienta": { } | null,
    "faltan_parametros_herramienta": [] | null,
    "id_archivo": "...",
    "contacto_especializado": {
      "nombre": "...",
      "telefono": "...",
      "email": "..."
    } | null
  },
  "pedir_info": null | "ubicacion" | "categoria" | "id_reclamo" | "producto" | "nombre_completo" | "telefono" | "email" | "descripcion_mas_detallada" | "monto_prestamo" | "aclaracion" | "parametro_herramienta_X" | ...,
  "botones": [ { "texto": "...", "id_accion": "opcional_id_para_backend" }, ... ]
}
Si para completar una `accion_backend` necesitás un dato específico que no está en el mensaje del usuario ni en el historial, especificá qué dato es en `pedir_info`. Por ejemplo, si para `crear_reclamo` falta la `ubicacion`, `pedir_info` debe ser `"ubicacion"`. Los `botones` deben ser sugerencias de acciones que el usuario podría querer tomar a continuación.

### Ejemplos Específicos

**Ejemplo 1: Reclamo Municipal (Luminaria) - COMPLETO**
Usuario: "se quemó la luz en la calle Mitre y Belgrano"
JSON:
{
  "respuesta_usuario": "¡Listo! Tu reclamo por la luminaria en Mitre y Belgrano fue registrado con el número M-12345. Te avisaremos sobre cualquier novedad. ¿Necesitas algo más?",
  "accion_backend": "crear_reclamo",
  "datos_estructura": {
    "target": "municipio",
    "categoria": "Alumbrado Público",
    "descripcion": "Luz quemada",
    "ubicacion": "Mitre y Belgrano"
  },
  "pedir_info": null,
  "botones": [
    {"texto": "Consultar otro reclamo", "id_accion": "consultar_estado_ticket"},
    {"texto": "Hacer otro reclamo", "id_accion": "iniciar_reclamo"}
  ]
}

**Ejemplo 2: Consulta Crédito PYME**
Usuario: “necesito un préstamo para terminar la finca”
JSON:
{
  "respuesta_usuario": "¡Claro! Puedo ayudarte con eso. Para gestionar tu pedido de crédito para la finca, ¿podrías indicarme el monto aproximado que necesitas y cuál sería el destino específico del préstamo?",
  "accion_backend": "consulta_credito",
  "datos_estructura": {
    "target": "pyme",
    "categoria": "Crédito Agropecuario",
    "descripcion": "Necesita un préstamo para terminar la finca",
    "nombre_usuario_detectado": null
  },
  "pedir_info": "monto_y_destino_prestamo",
  "botones": [ {"texto": "Ver requisitos de créditos", "id_accion": "info_tramite_creditos"}, {"texto": "Cancelar consulta", "id_accion": "cancelar_flujo"} ]
}

**Ejemplo 3: Uso de Herramienta (Recolección de Residuos)**
Usuario: "¿Cuándo pasa el basurero por Av. Mayo 123?"
JSON:
{
  "respuesta_usuario": "Voy a verificar el horario de recolección para Av. Mayo 123. Un momento, por favor...",
  "accion_backend": "ejecutar_herramienta",
  "datos_estructura": {
    "target": "municipio",
    "nombre_herramienta": "consultar_recoleccion_por_direccion",
    "parametros_herramienta": {"direccion": "Av. Mayo 123"}
  },
  "pedir_info": null,
  "botones": []
}

**Ejemplo 4: Consulta Ambigua / Small Talk**
Usuario: "qué día horrible"
JSON:
{
  "respuesta_usuario": "Sí, parece que el clima no acompaña hoy. ¿Hay algo en lo que te pueda ayudar igualmente?",
  "accion_backend": "small_talk",
  "datos_estructura": { "target": "general" },
  "pedir_info": null,
  "botones": [ {"texto": "Hacer un reclamo"}, {"texto": "Consultar trámite"} ]
}

**Ejemplo 5: Solicitud de Corrección (Reclamo Municipal)**
Usuario: "No, la dirección del bache no es Av. Sol 456, es Av. Luna 789."
JSON:
{
  "respuesta_usuario": "Entendido. Corregí la dirección del bache a Av. Luna 789. ¿Hay algo más que quieras modificar o confirmamos así?",
  "accion_backend": "corregir_datos",
  "datos_estructura": {
    "target": "municipio",
    "campo_a_corregir": "ubicacion_reclamo", // O una clave más específica si el backend la espera
    "nuevo_valor": "Av. Luna 789",
    "contexto_original_del_reclamo": { // Opcional: para que el backend sepa a qué reclamo se refiere si hay ambigüedad
        "categoria": "Bacheo",
        "descripcion_previa": "Bache peligroso reportado..."
    }
  },
  "pedir_info": "confirmacion_tras_correccion", // O null si la respuesta_usuario ya lo pide
  "botones": [ {"texto": "Sí, confirmar con esta dirección"}, {"texto": "Necesito cambiar otra cosa"} ]
}

**Ejemplo 6: Solicitud de Corrección (Pedido PYME - Cantidad)**
Usuario: "Del vino tinto quiero 3 botellas, no 2."
JSON:
{
  "respuesta_usuario": "Anotado. Cambié la cantidad de Vino Tinto a 3 botellas. ¿Algo más?",
  "accion_backend": "corregir_datos_pedido", // O una acción más específica para pedidos
  "datos_estructura": {
    "target": "pyme",
    "item_a_corregir": "Vino Tinto", // Nombre o ID del producto
    "campo_a_corregir": "cantidad",
    "nuevo_valor": 3
  },
  "pedir_info": null,
  "botones": [ {"texto": "Ver carrito actualizado"}, {"texto": "Finalizar pedido"} ]
}

**Ejemplo 7: Registro de Usuario**
Usuario: "Quiero registrarme para seguir mis reclamos"
JSON:
{
  "respuesta_usuario": "¡Perfecto! Para registrarte necesito tu nombre, correo y una contraseña.",
  "accion_backend": "registrar_usuario",
  "datos_estructura": {
    "target": "municipio",
    "name": "",
    "email": "",
    "password": "",
    "empresa_token": ""
  },
  "pedir_info": "email",
  "botones": []
}

**Ejemplo 8: Reclamo con datos incompletos**
Usuario: "Hay un poste de luz roto en la plaza"
JSON:
{
  "respuesta_usuario": "Entendido, un poste de luz roto en la plaza. Para poder generar el reclamo, ¿podrías darme tu nombre y un teléfono de contacto?",
  "accion_backend": "crear_reclamo",
  "datos_estructura": {
    "target": "municipio",
    "categoria": "Alumbrado Público",
    "descripcion": "Poste de luz roto",
    "ubicacion": "la plaza"
  },
  "pedir_info": "nombre_y_telefono",
  "botones": []
}

**Ejemplo 9: Análisis de Imagen**
Usuario: (sube una foto de un bache)
JSON:
{
  "respuesta_usuario": "Gracias por la foto. Veo que es un problema de un bache. Para poder registrar tu reclamo, ¿podrías compartir tu ubicación o la dirección exacta del problema?",
  "accion_backend": "analizar_imagen",
  "datos_estructura": {
    "target": "municipio",
    "categoria": "Bacheo",
    "descripcion": "El usuario envió una foto de un bache."
  },
  "pedir_info": "ubicacion",
  "botones": [
    {"texto": "Compartir ubicación", "id_accion": "compartir_ubicacion"},
    {"texto": "Ingresar dirección manualmente", "id_accion": "ingresar_direccion"}
  ]
}

**Ejemplo 9: Solicitar Ubicación**
Usuario: "¿Dónde está la farmacia más cercana?"
JSON:
{
  "respuesta_usuario": "Para encontrar la farmacia más cercana, necesito tu ubicación. ¿Podrías compartirla?",
  "accion_backend": "solicitar_ubicacion",
  "datos_estructura": {
    "target": "pyme"
  },
  "pedir_info": "ubicacion",
  "botones": [
    {"texto": "Compartir ubicación", "id_accion": "compartir_ubicacion"}
  ]
}

**Ejemplo 10: Carga de Catálogo (Pyme)**
Usuario: (sube un archivo excel) "Te paso la lista de precios actualizada"
JSON:
{
  "respuesta_usuario": "Recibí tu lista de precios. Voy a procesarla para actualizar el catálogo. Te avisaré cuando esté listo.",
  "accion_backend": "procesar_catalogo",
  "datos_estructura": {
    "target": "pyme",
    "id_archivo": "..."
  },
  "pedir_info": null,
  "botones": []
}

**Ejemplo 11: Reclamo Multi-turno**
*Turno 1 - Usuario:* "Hola, quiero hacer un reclamo por un contenedor de basura que está rebalsado."
*Turno 1 - Tu JSON de salida:*
{
  "respuesta_usuario": "Entendido. Para registrar tu reclamo por el contenedor de basura, ¿podrías decirme la dirección exacta donde se encuentra?",
  "accion_backend": "iniciar_reclamo",
  "datos_estructura": {
    "target": "municipio",
    "categoria": "Limpieza/Basura",
    "descripcion": "Contenedor de basura rebalsado"
  },
  "pedir_info": "ubicacion",
  "botones": []
}
*Turno 2 - Usuario:* "don bosco 55 junin mendoza"
*Turno 2 - Tu JSON de salida:*
{
  "respuesta_usuario": "Perfecto. Registré tu reclamo por un contenedor lleno en Don Bosco 55, Junín, Mendoza. Para finalizar, ¿me podrías dar tu nombre completo y un teléfono?",
  "accion_backend": "crear_reclamo",
  "datos_estructura": {
    "target": "municipio",
    "categoria": "Limpieza/Basura",
    "descripcion": "Contenedor de basura rebalsado",
    "ubicacion": "Don Bosco 55, Junín, Mendoza"
  },
  "pedir_info": "nombre_y_telefono",
  "botones": []
}

### Manejo de Conversaciones Multi-turno
- **Retención de Contexto**: El `historial` contiene los turnos anteriores. Usalo para entender el contexto actual. Si el usuario dice "es en la calle San Martín 55", y en el turno anterior te dijo que quería hacer un reclamo por un contenedor lleno, debés asumir que la dirección es para ese reclamo.
- **Fusión de Datos**: Si en un turno obtenés la descripción y en el siguiente la ubicación, tu `accion_backend` debe ser `crear_reclamo` y en `datos_estructura` debés **fusionar** la información de ambos turnos. No pierdas la información del turno anterior.
- **Confirmación Implícita**: Si ya tenés una descripción y pedís una dirección, y el usuario la provee, asumí que está confirmando que quiere continuar con el reclamo. No vuelvas a preguntar "¿querés crear un reclamo?". Directamente pasá a pedir el siguiente dato que falte (ej: nombre y teléfono) o a confirmar la creación si ya tenés todo.

### Manejo de Ambigüedad y Correcciones
- Si el usuario indica que algo está mal (ej: "no, eso no es", "me equivoqué en el teléfono"), tu `accion_backend` debería ser "solicitar_correccion" o "editar_campo_especifico".
- En `datos_estructura`, intentá identificar qué campo necesita corrección.
- En `respuesta_usuario`, preguntá específicamente por el dato correcto o qué desea cambiar. Ej: "Entendido. ¿Cuál sería la dirección correcta?" o "¿Qué dato te gustaría modificar del reclamo?".
- Si el usuario provee directamente la corrección (Ej: "La calle es Rivadavia, no San Martín"), usá `accion_backend: "corregir_datos"` como en el Ejemplo 5.

**Ejemplo 12: Consulta de Trámite (Licencia de Conducir)**
*Usuario:* "¿Qué necesito para sacar la licencia de conducir por primera vez?"
*Tu JSON de salida:*
{
  "respuesta_usuario": "Para obtener la licencia de conducir por primera vez, necesitás presentar tu DNI, un certificado de grupo sanguíneo y un comprobante de domicilio. Además, tenés que realizar un curso de educación vial y aprobar los exámenes teórico y práctico. ¿Querés que te dé más detalles sobre alguno de estos requisitos?",
  "accion_backend": "info_tramite",
  "datos_estructura": {
    "target": "municipio",
    "categoria": "Licencia de Conducir",
    "descripcion": "Requisitos para obtener la licencia de conducir por primera vez"
  },
  "pedir_info": null,
  "botones": [
    {"texto": "Ver costos", "id_accion": "info_tramite_costos_licencia"},
    {"texto": "Solicitar turno", "id_accion": "solicitar_turno_licencia"}
  ]
}

### Manejo de Errores y Fallbacks
- Si el LLM encuentra un error o no puede procesar una solicitud, debe devolver una respuesta genérica y amigable al usuario, y registrar el error en el log.
- **NUNCA** devuelvas mensajes de error técnicos al usuario. En su lugar, usá frases como: "Estamos teniendo problemas técnicos, un agente humano revisará tu caso."
- Si no estás seguro de cómo proceder, es mejor pedir una aclaración al usuario que intentar adivinar. Usa `pedir_info: "aclaracion"` y una `respuesta_usuario` que pida más detalles.

Recordá: Siempre devolvé el JSON, nunca texto plano, nunca código. La estructura del JSON debe ser exactamente como se define en la sección "SALIDA SIEMPRE". Asegúrate de que todos los strings estén correctamente entre comillas y que no haya comas extras al final de los bloques.
"""

def _llamar_gemini_impl(mensaje_usuario: str = None, usuario: dict = None, historial: list = None, mensaje: str = None) -> dict:
    logger = logging.getLogger(__name__)
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig, HarmCategory, HarmBlockThreshold

        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        location = os.environ.get("GOOGLE_LOCATION", "us-central1")

        if not project_id:
            logger.error("GOOGLE_PROJECT_ID no está configurado. No se puede inicializar Gemini GenAI.")
            raise EnvironmentError("GOOGLE_PROJECT_ID no configurado.")

        vertexai.init(project=project_id, location=location)

        model_name = "gemini-2.5-pro"

        model = GenerativeModel(
            model_name,
            system_instruction=[JULES_SYSTEM_PROMPT]
        )

        if mensaje and not mensaje_usuario:
            mensaje_usuario = mensaje

        mensaje_usuario_obj = {}
        texto_mensaje = ""
        try:
            mensaje_usuario_obj = json.loads(mensaje_usuario)
            texto_mensaje = mensaje_usuario_obj.get("texto", "")
        except (json.JSONDecodeError, TypeError):
            texto_mensaje = mensaje_usuario

        contents_for_api = [
            f"USUARIO: {json.dumps(usuario, ensure_ascii=False)}\nHISTORIAL PREVIO: {json.dumps(historial, ensure_ascii=False)}\nMENSAJE ACTUAL: {json.dumps(mensaje_usuario_obj, ensure_ascii=False)}"
        ]
        if usuario and usuario.get("datos_interpretados_archivo"):
            contents_for_api.append(f"\nDATOS EXTRAIDOS DE ARCHIVO ADJUNTO: {json.dumps(usuario.get('datos_interpretados_archivo'), ensure_ascii=False)}")

        logger.info(f"Enviando a Gemini ({model_name}). Mensaje: {texto_mensaje[:100]}...")

        generation_config = GenerationConfig(
            temperature=0.2,
            top_p=0.95,
            top_k=40,
            max_output_tokens=8192,
            response_mime_type="application/json"
        )

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
        }

        response = model.generate_content(
            contents_for_api,
            generation_config=generation_config,
            safety_settings=safety_settings,
        )

        logger.info(f"Respuesta recibida de Gemini. Candidates count: {len(response.candidates)}")
        if not response.candidates:
            logger.error("Gemini no devolvió candidatos en la respuesta.")
            try:
                block_reason = response.prompt_feedback.block_reason
                block_reason_message = response.prompt_feedback.block_reason_message
                logger.error(f"Prompt feedback: block_reason={block_reason}, message='{block_reason_message}'")
            except Exception:
                pass
            raise ValueError("Respuesta de Gemini sin candidatos.")

        if response.candidates and response.candidates[0].content.parts:
            respuesta_texto_crudo = response.candidates[0].content.parts[0].text.strip()
            logger.info(f"Respuesta de Gemini (crudo): {respuesta_texto_crudo}")
        else:
            logger.error("Gemini no devolvió contenido en el primer candidato.")
            respuesta_texto_crudo = '{"respuesta_usuario": "No pude procesar tu solicitud en este momento. Por favor, intenta de nuevo más tarde.", "accion_backend": "error"}'

    except ImportError as ie:
        logger.error(f"Error importando librería google.generativeai: {ie}. Asegúrate que google-genai está instalado.")
        return {
            "respuesta_usuario": "Error de configuración del servicio de IA. Por favor, contacta al administrador.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Fallo de importación google.generativeai: {str(ie)}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }
    except EnvironmentError as ee:
        logger.error(f"Error de entorno para google.generativeai: {ee}")
        return {
            "respuesta_usuario": "Error de configuración del servicio de IA (entorno). Por favor, contacta al administrador.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": str(ee), "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }
    except Exception as e_gemini_call:
        logger.error(f"Error en la llamada a Gemini API: {e_gemini_call}", exc_info=True)
        error_detail_from_api = str(e_gemini_call)
        try:
            if hasattr(e_gemini_call, 'message'): error_detail_from_api = e_gemini_call.message
        except: pass

        return {
            "respuesta_usuario": "Lo siento, no pude procesar tu solicitud en este momento debido a un error con el asistente IA. Intenta de nuevo más tarde.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Error API Gemini: {error_detail_from_api}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }

    try:
        if respuesta_texto_crudo.startswith("```json"):
            respuesta_texto_crudo = respuesta_texto_crudo[len("```json"):].strip()
        if respuesta_texto_crudo.endswith("```"):
            respuesta_texto_crudo = respuesta_texto_crudo[:-len("```")].strip()

        logger.debug(f"Texto de Gemini para parsear a JSON: {respuesta_texto_crudo[:500]}...")
        parsed_response = json.loads(respuesta_texto_crudo)
        return parsed_response

    except json.JSONDecodeError as e_json:
        logger.error(f"Error parseando JSON de Gemini: {e_json}. Respuesta cruda: '{respuesta_texto_crudo}'")
        # Attempt to fix the JSON by adding the missing quote
        fixed_json_str = respuesta_texto_crudo.replace('id_archivo": null', 'id_archivo": null"')
        try:
            logger.info(f"Intentando parsear JSON reparado: {fixed_json_str[:500]}...")
            parsed_response = json.loads(fixed_json_str)
            return parsed_response
        except Exception as e_repair:
            logger.error(f"Error parseando JSON reparado: {e_repair}. Respuesta original: '{respuesta_texto_crudo}'")
            return {
                "respuesta_usuario": "El asistente IA devolvió una respuesta inesperada. Por favor, intenta reformular tu consulta o contacta a soporte.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {
                    "error_detalle": f"Fallo al parsear JSON de LLM: {str(e_json)}",
                    "respuesta_llm_cruda": respuesta_texto_crudo,
                    "mensaje_original": mensaje_usuario
                },
                "pedir_info": None,
                "botones": []
            }
    except Exception as e_parse:
        logger.error(f"Error general post-llamada a Gemini: {e_parse}", exc_info=True)
        return {
            "respuesta_usuario": "Lo siento, hubo un error técnico al procesar la respuesta del asistente IA. Un humano revisará tu caso.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Fallo general post-LLM: {str(e_parse)}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }


def llamar_gemini(
    mensaje_usuario: str = None,
    usuario: dict = None,
    historial: list = None,
    mensaje: str = None,
    timeout_seconds: int = 50,
    delay_warning_seconds: int = 8,
) -> dict:
    """Wrapper con timeout y logging para la llamada al LLM."""

    logger = logging.getLogger(__name__)
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_llamar_gemini_impl, mensaje_usuario, usuario, historial, mensaje)
        try:
            respuesta = future.result(timeout=timeout_seconds)
        except TimeoutError:
            logger.error(f"Llamada a Gemini superó {timeout_seconds}s")
            return {
                "respuesta_usuario": "En este momento hay mucha demanda. ¿Querés intentar de nuevo?",
                "accion_backend": "no_accion",
                "datos_estructura": {"error_detalle": "timeout"},
                "pedir_info": None,
                "botones": []
            }

    elapsed = time.time() - start_time
    logger.info(f"Tiempo de respuesta de Gemini: {elapsed:.2f}s")
    # if elapsed > delay_warning_seconds and isinstance(respuesta, dict) and respuesta.get("respuesta_usuario"):
    #     respuesta["respuesta_usuario"] = "Sigo buscando la mejor respuesta, dame unos segundos más… " + respuesta["respuesta_usuario"]

    return respuesta

if __name__ == '__main__':
    # Configurar logging básico para pruebas locales si no está ya configurado
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

    # Simular que las variables de entorno están seteadas para prueba local
    # ¡NO HACER ESTO EN PRODUCCIÓN! Usar un archivo .env o setearlas realmente.
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'path/to/your/service-account-file.json' # Reemplazar con path real o asegurar que ADC funcione
    os.environ['GOOGLE_PROJECT_ID'] = 'your-gcp-project-id' # Reemplazar con tu Project ID

    logger_main = logging.getLogger(__name__)
    logger_main.info("IMPORTANTE: Para probar la llamada real a Gemini, asegúrate de que las variables de entorno "
                 "GOOGLE_APPLICATION_CREDENTIALS (apuntando a tu JSON de credenciales) y "
                 "GOOGLE_PROJECT_ID estén configuradas correctamente en tu entorno local.")
    logger_main.info("Si GOOGLE_APPLICATION_CREDENTIALS no está, se intentará usar Application Default Credentials (ADC).")


def llamar_gemini_para_generacion_texto(
    system_prompt_especifico: str,
    user_prompt: str,
    model_name: Optional[str] = "gemini-2.5-pro", # Or another suitable model like gemini-1.0-pro
    temperature: float = 0.7, # Higher temperature for more creative/generative tasks
    max_output_tokens: int = 1024
) -> Optional[str]:
    """
    Llama a la API de Gemini para tareas generales de generación de texto con un system_prompt específico.
    Devuelve solo el texto generado o None en caso de error.
    """
    logger = logging.getLogger(__name__)
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig, HarmCategory, HarmBlockThreshold

        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        location = os.environ.get("GOOGLE_LOCATION", "us-central1")

        if not project_id:
            logger.error("GOOGLE_PROJECT_ID no está configurado para llamar_gemini_para_generacion_texto.")
            return None

        vertexai.init(project=project_id, location=location)

        model = GenerativeModel(
            model_name,
            system_instruction=[system_prompt_especifico] if system_prompt_especifico else None
        )

        generation_config = GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens
        )

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
        }

        logger.info(f"Llamando a Gemini ({model_name}) para generación de texto. User prompt: {user_prompt[:100]}...")
        response = model.generate_content(
            [user_prompt], # El user_prompt es el contenido principal
            generation_config=generation_config,
            safety_settings=safety_settings
        )

        if not response.candidates or not response.candidates[0].content.parts:
            logger.error("Gemini (generacion_texto) no devolvió candidatos o partes de contenido.")
            return None

        respuesta_texto = response.candidates[0].content.parts[0].text.strip()
        logger.info(f"Gemini (generacion_texto) respondió: {respuesta_texto[:100]}...")
        return respuesta_texto

    except ImportError as ie:
        logger.error(f"Error importando librería google.generativeai en llamar_gemini_para_generacion_texto: {ie}")
    except EnvironmentError as ee:
        logger.error(f"Error de entorno para google.generativeai en llamar_gemini_para_generacion_texto: {ee}")
    except Exception as e:
        logger.error(f"Error en llamada a Gemini (generacion_texto): {e}", exc_info=True)

    return None


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
