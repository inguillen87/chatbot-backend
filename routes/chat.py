# routes/chat.py
from flask import Blueprint, request, jsonify, session
import logging

# Asegúrate de que todas las importaciones de modelos y servicios sean correctas
from models import Rubro, User
from services.logic import responder_chatboc
from services.municipios import responder_municipio

chat_bp = Blueprint("chat_bp", __name__)
logger = logging.getLogger(__name__)

@chat_bp.route("/ask", methods=["POST"])
def ask():
    """
    Endpoint principal para procesar todas las preguntas del chat.
    Determina inteligentemente el rubro del usuario y deriva la solicitud
    a la lógica de backend correspondiente (PyME genérica o Municipio con estado).
    """
    try:
        # 1. EXTRACCIÓN Y VALIDACIÓN DE DATOS DE LA SOLICITUD
        data = request.get_json()
        if not data:
            logger.warning("Solicitud a /ask sin datos JSON o JSON vacío.")
            return jsonify({"error": "Falta el cuerpo de la solicitud o está vacío."}), 400

        pregunta = data.get("question") or data.get("pregunta")
        if not pregunta:
            logger.warning("Pregunta faltante en la solicitud a /ask.")
            return jsonify({"error": "Falta la pregunta"}), 400

        # Obtiene el token de autorización de forma segura
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.split(" ")[1].strip() if auth_header.startswith("Bearer ") else None
        
        # Obtiene el nombre del rubro enviado desde el frontend (si existe)
        rubro_nombre_frontend = (data.get("rubro") or "").strip().lower()

        logger.info(f"▶️  Iniciando /ask para pregunta: '{pregunta[:50]}...' (Token: {str(token)[:15] if token else 'N/A'}, Rubro Frontend: {rubro_nombre_frontend})")

        # 2. BÚSQUEDA DE ENTIDADES (USUARIO Y RUBRO)
        user_obj = User.query.filter_by(token=token).first() if token and not token.startswith("demo") else None
        if user_obj:
            logger.info(f"👤 Usuario autenticado encontrado: {user_obj.email} (ID: {user_obj.id})")

        # Se determina el rubro autoritativo. La información del usuario en la DB tiene prioridad.
        rubro_autoritativo = None
        if user_obj and user_obj.rubro:
            rubro_autoritativo = user_obj.rubro
            logger.info(f"📚 Rubro determinado por perfil de usuario: '{rubro_autoritativo.nombre}' (ID: {rubro_autoritativo.id})")
        elif rubro_nombre_frontend:
            rubro_autoritativo = Rubro.query.filter(Rubro.nombre.ilike(rubro_nombre_frontend)).first()
            if rubro_autoritativo:
                logger.info(f"📚 Rubro determinado por parámetro frontend: '{rubro_autoritativo.nombre}' (ID: {rubro_autoritativo.id})")
        
        # 3. ENRUTAMIENTO INTELIGENTE AL BACKEND CORRECTO
        
        # Comprobamos si el rubro final es 'municipios' para usar la lógica con memoria
        if rubro_autoritativo and rubro_autoritativo.nombre.lower().strip() == 'municipios':
            
            logger.info(f"🧠 Derivando al flujo de MUNICIPIOS para el usuario.")
            
            # Llamamos a la función especializada en municipios, pasando la sesión para la memoria
            resultado = responder_municipio(
                pregunta=pregunta,
                user_obj=user_obj,
                rubro_obj=rubro_autoritativo,
                session_obj=session  # <-- ¡CRUCIAL! Pasamos el objeto de sesión
            )
        else:
            # Para cualquier otro caso, usamos la lógica genérica para PyMEs
            logger.info(f"🧠 Derivando al flujo general (PyME) para el usuario.")

            resultado = responder_chatboc(
                pregunta=pregunta,
                token=token,
                rubro_nombre_frontend=rubro_nombre_frontend
            )
            
        logger.info(f"✅ Solicitud /ask procesada exitosamente. Fuente de respuesta: '{resultado.get('fuente', 'desconocida')}'")
        return jsonify(resultado), 200

    except Exception as e:
        logger.error(f"❌ Error crítico en el endpoint /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al procesar tu pregunta."}), 500

@chat_bp.route("/ping", methods=["GET"])
def ping():
    """Endpoint de prueba para verificar que el servicio está activo."""
    return jsonify({"status": "ok", "message": "pong"}), 200