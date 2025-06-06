# routes/chat.py (Versión Mejorada)

from flask import Blueprint, request, jsonify, session, current_app
import logging

# Importamos los modelos y servicios necesarios
from models import Rubro, User
from services.logic import responder_chatboc
from services.municipios import responder_municipio
# --- PASO 1: IMPORTAMOS NUESTRO NUEVO SERVICIO DE TICKETS ---
from services.ticket_service import servicio_tickets, SQLAlchemyError


chat_bp = Blueprint("chat_bp", __name__)
# Usamos current_app.logger para consistencia con el logging de app.py
# logger = logging.getLogger(__name__) # Ya no es necesario, usaremos current_app.logger

@chat_bp.route("/ask", methods=["POST"])
def ask():
    """
    Endpoint principal para procesar todas las preguntas del chat.
    Determina inteligentemente el rubro del usuario y deriva la solicitud
    a la lógica de backend correspondiente.
    """
    try:
        data = request.get_json()
        if not data or not data.get("question"):
            current_app.logger.warning("Solicitud a /ask inválida (sin JSON o sin 'question').")
            return jsonify({"error": "Falta la pregunta en la solicitud."}), 400

        pregunta = data.get("question")
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.split(" ")[1] if auth_header.startswith("Bearer ") else None
        
        current_app.logger.info(f"▶️  Iniciando /ask para pregunta: '{pregunta[:50]}...' (Token: {str(token)[:15] if token else 'N/A'})")

        # 1. BÚSQUEDA DE ENTIDADES (se mantiene, está bien hecho)
        user_obj = User.query.filter_by(token=token).first() if token else None
        rubro_autoritativo = user_obj.rubro if user_obj and user_obj.rubro else None

        if not rubro_autoritativo and data.get("rubro"):
             rubro_autoritativo = Rubro.query.filter(Rubro.nombre.ilike(data.get("rubro").strip().lower())).first()

        if user_obj:
            current_app.logger.info(f"👤 Usuario autenticado: {user_obj.email} (ID: {user_obj.id})")
        if rubro_autoritativo:
            current_app.logger.info(f"📚 Rubro determinado: '{rubro_autoritativo.nombre}'")

        # 2. ENRUTAMIENTO INTELIGENTE AL BACKEND CORRECTO
        # La decisión de a qué servicio llamar se basa en el nombre del rubro.
        if rubro_autoritativo and rubro_autoritativo.nombre.lower().strip() == 'municipios':
            current_app.logger.info("🧠 Derivando al flujo de MUNICIPIOS.")
            resultado = responder_municipio(
                pregunta=pregunta,
                user_obj=user_obj,
                rubro_obj=rubro_autoritativo,
                session_obj=session
            )
        else:
            current_app.logger.info("🧠 Derivando al flujo general (PyME).")
            # --- MEJORA: Unificamos la firma de la llamada ---
            # Ahora pasamos los objetos completos, haciendo el servicio más eficiente y limpio.
            resultado = responder_chatboc(
                pregunta=pregunta,
                user_obj=user_obj,
                rubro_obj=rubro_autoritativo,
                session_obj=session # Pasamos la sesión por si la lógica de PyME también necesita memoria
            )
            
        current_app.logger.info(f"✅ Solicitud /ask procesada. Fuente: '{resultado.get('fuente', 'desconocida')}'")
        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en el endpoint /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al procesar tu pregunta."}), 500


# --- PASO 2: NUEVO ENDPOINT PARA USAR EL SERVICIO DE TICKETS ---
@chat_bp.route("/ticket", methods=["POST"])
def crear_ticket_desde_chat():
    """
    Endpoint dedicado para crear un ticket. El frontend llamaría a esta ruta
    cuando el usuario confirma una acción (ej. 'Confirmar Pedido').
    """
    data = request.get_json()
    if not data or not data.get("tipo_ticket") or not data.get("pregunta"):
        return jsonify({"error": "Faltan datos requeridos (tipo_ticket, pregunta)."}), 400
    
    current_app.logger.info(f"▶️  Recibida solicitud para crear ticket tipo '{data['tipo_ticket']}'")

    try:
        # Empaquetamos los datos que vienen del frontend en un diccionario
        ticket_data = {
            "pregunta": data.get("pregunta"),
            "comentario": data.get("comentario"),
            "user_id": data.get("user_id"),
            "rubro_id": data.get("rubro_id"),
            "telefono": data.get("telefono"),
            "email": data.get("email"),
            "dni": data.get("dni")
        }

        # Usamos nuestro nuevo y flamante servicio de tickets
        numero_ticket = servicio_tickets.crear_nuevo_ticket(
            tipo_ticket=data["tipo_ticket"],
            ticket_data=ticket_data
        )

        return jsonify({
            "mensaje": "Ticket creado exitosamente.", 
            "numero_ticket": numero_ticket
        }), 201

    except ValueError as e:
        # Este error lo lanzamos en el servicio si el tipo de ticket es inválido
        return jsonify({"error": str(e)}), 400
    except SQLAlchemyError:
        # El servicio ya logueó el error detallado, aquí solo devolvemos una respuesta genérica
        return jsonify({"error": "No se pudo crear el ticket debido a un error en la base de datos."}), 500
    except Exception as e:
        current_app.logger.error(f"❌ Error inesperado en /ticket: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor."}), 500


@chat_bp.route("/ping", methods=["GET"])
def ping():
    """Endpoint de prueba para verificar que el servicio está activo."""
    return jsonify({"status": "ok", "message": "pong"}), 200