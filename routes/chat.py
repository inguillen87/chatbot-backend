# routes/chat.py
import logging
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func
from models import User, Rubro
from services.logic import (
    responder_chatboc,
    RUBROS_PUBLICOS,
    normalizar_rubro,
    es_rubro_publico,
)
from .auth import anon_o_token_requerido


from flask import make_response

def cors_options_response():
    resp = make_response('', 200)
    # OJO: Si en producción no querés exponerlo a todos, cambiá el "*" por tu dominio
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, Origin, Accept, Anon-Id, x-entity-token'
    resp.headers['Access-Control-Allow-Credentials'] = 'true'
    return resp


chat_bp = Blueprint("chat_bp", __name__)


def _parse_request(tipo_chat_fijo: str | None = None):
    """Obtiene y valida los campos comunes del cuerpo JSON."""
    def _normalizar_tipo_chat(valor: str | None) -> str | None:
        if not valor:
            return None
        valor = str(valor).strip().lower()
        sinonimos = {
            "pymes": "pyme",
            "pyme": "pyme",
            "municipios": "municipio",
            "municipio": "municipio",
            "muni": "municipio",
        }
        return sinonimos.get(valor)

    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise TypeError("El cuerpo debe ser JSON")

        pregunta = data.get("pregunta")
        if not pregunta:
            raise ValueError("Falta el campo 'pregunta'")

        if tipo_chat_fijo:
            tipo_chat = tipo_chat_fijo
        else:
            tipo_chat = _normalizar_tipo_chat(data.get("tipo_chat"))
            if tipo_chat not in ("pyme", "municipio"):
                raise ValueError("'tipo_chat' debe ser 'pyme' o 'municipio'")

        contexto_previo = data.get("contexto_previo")  # Leemos la mochila
        rubro_id = data.get("rubro_id")
        rubro_clave = data.get("rubro_clave")

        return pregunta, contexto_previo, tipo_chat, rubro_id, rubro_clave, None

    except (TypeError, ValueError) as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
            None,
            None,
            None,
            None,
            None,
            jsonify({"error": str(e)}),
        )
    except Exception as e:
        current_app.logger.error(f"Error inesperado al parsear /ask: {e}")
        return (
            None,
            None,
            None,
            None,
            None,
            jsonify({"error": "Formato JSON inválido"}),
        )


def _authenticate_and_get_user():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token:
            return User.query.filter_by(token=token).first()
    return None


def _procesar_chat(
    tipo_chat_fijo: str | None = None,
    current_user=None,
    owner_user=None,
    anon_id: str | None = None,
):
    """Procesa una pregunta garantizando coherencia entre rubro y tipo_chat.

    Se usa en /ask, /ask/pyme y /ask/municipio. La estética y la lógica se
    determinan exclusivamente por el rubro y nunca se mezclan las de pyme con
    las de municipio. Si el rubro indica un tipo diferente al recibido, se
    ajusta y se registra un log; si la información es inconsistente, se
    devuelve un error claro.
    """
    try:
        (
            pregunta,
            contexto_previo,
            tipo_chat,
            rubro_id,
            rubro_clave,
            error_response,
        ) = _parse_request(tipo_chat_fijo)
        if error_response:
            return error_response, 400

        owner_obj = owner_user or current_user or _authenticate_and_get_user()
        viewer_obj = current_user
        if not owner_obj and not anon_id:
            return jsonify({"error": "No autenticado."}), 401

        if rubro_id:
            rubro_obj = Rubro.query.get(rubro_id)
        elif rubro_clave:
            rubro_obj = Rubro.query.filter(
                func.lower(Rubro.clave) == rubro_clave.lower()
            ).first()
        else:
            rubro_obj = owner_obj.rubro if owner_obj and owner_obj.rubro else None

        # Logueamos qué rubro se está usando para procesar la pregunta
        if rubro_obj:
            nombre_rubro = getattr(rubro_obj, "nombre", None) or getattr(rubro_obj, "clave", None)
            current_app.logger.info(
                f"Usando rubro ID {rubro_obj.id} - {nombre_rubro}"
            )
        else:
            current_app.logger.info("Sin rubro asociado al usuario o en la petición")

        # --- CONTROL DE PLAN SOLO PARA USUARIOS AUTENTICADOS ---
        if owner_obj:
            if (
                owner_obj.plan != "full"
                and owner_obj.preguntas_usadas >= owner_obj.limite_preguntas
            ):
                return (
                    jsonify(
                        {
                            "error": f"Alcanzaste el límite de preguntas de tu plan ({owner_obj.limite_preguntas}). Mejorá tu plan para seguir consultando."
                        }
                    ),
                    403,
                )

        # Usamos la lógica centralizada que decide según el rubro
        resultado = responder_chatboc(
            pregunta,
            owner_user=owner_obj,
            current_user=viewer_obj,
            rubro_obj=rubro_obj,
            rubro_nombre_frontend=rubro_clave,
            tipo_chat=tipo_chat,
            contexto_previo=contexto_previo,
            anon_id=anon_id,
        )

        # --- Determinar si la conversación debe considerarse pública ---
        rubro_seleccionado = (
            rubro_obj
            or rubro_clave
            or (owner_obj.rubro if owner_obj and getattr(owner_obj, "rubro", None) else None)
        )
        rubro_nombre = normalizar_rubro(rubro_seleccionado)
        es_publico = es_rubro_publico(rubro_seleccionado)

        current_app.logger.info(
            f"[RUBROS] user.rubro={getattr(owner_obj, 'rubro', None)} "
            f"rubroSeleccionado={rubro_seleccionado} "
            f"rubroNormalizado={rubro_nombre} esRubroPublico={es_publico}"
        )


        # --- INCREMENTAR CONTADOR SOLO SI TODO ESTÁ OK ---
        if owner_obj:
            owner_obj.preguntas_usadas += 1
            try:
                from extensions import db

                db.session.commit()
            except Exception as e:
                current_app.logger.error(
                    f"Error al actualizar preguntas_usadas: {e}"
                )
                # NO frena el flujo del bot, pero loguea

        # --- OPCIONAL: DEVOLVER CONTADOR ACTUALIZADO ---
        if isinstance(resultado, dict):
            resultado["es_publico"] = es_publico
            if owner_obj:
                resultado["preguntas_usadas"] = owner_obj.preguntas_usadas
                resultado["limite_preguntas"] = owner_obj.limite_preguntas

        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor."}), 500


# Handler para el preflight de CORS de /ask
@chat_bp.route("/ask", methods=["OPTIONS"])
def ask_options():
    return cors_options_response()


@chat_bp.route("/ask", methods=["POST"])
@anon_o_token_requerido
def ask(current_user=None, anon_id=None, owner_user=None):
    """Endpoint genérico que delega según el rubro.

    El frontend debe enviar siempre el rubro y el tipo de chat correctos.
    Si llegan cruzados, el backend ajustará o lanzará error para evitar
    mezclar la estética de pymes con la de municipios.
    """
    user = owner_user or current_user
    return _procesar_chat(current_user=current_user, owner_user=user, anon_id=anon_id)


# Handler para el preflight de CORS de /ask/pyme
@chat_bp.route("/ask/pyme", methods=["OPTIONS"])
def ask_pyme_options():
    return cors_options_response()


@chat_bp.route("/ask/pyme", methods=["POST"])
@anon_o_token_requerido
def ask_pyme(current_user=None, anon_id=None, owner_user=None):
    """Procesa preguntas para pymes.

    Aunque el endpoint fije el tipo 'pyme', se verificará el rubro para evitar
    mezclar respuestas de municipio. Cualquier inconsistencia se registra y se
    corrige o se devuelve error.
    """
    user = owner_user or current_user
    return _procesar_chat("pyme", current_user=current_user, owner_user=user, anon_id=anon_id)


# Preflight CORS handler for /ask/municipio
@chat_bp.route("/ask/municipio", methods=["OPTIONS"])
def ask_municipio_options():
    return cors_options_response()

@chat_bp.route("/ask/municipio", methods=["POST"])
@anon_o_token_requerido
def ask_municipio(current_user=None, anon_id=None, owner_user=None):
    """Procesa preguntas para municipios.

    Se valida que el rubro corresponda a un ente público y, de no ser así,
    se registrará un error. Esto previene mezclar lógicas de pyme y municipio.
    """
    user = owner_user or current_user
    return _procesar_chat("municipio", current_user=current_user, owner_user=user, anon_id=anon_id)
