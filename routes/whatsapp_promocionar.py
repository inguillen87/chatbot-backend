from flask import Blueprint, request, jsonify
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from models import User
from utils.whatsapp import enviar_imagen_whatsapp
from datetime import datetime, timedelta
from pathlib import Path

whatsapp_promocionar_bp = Blueprint('whatsapp_promocionar', __name__, url_prefix='/api/whatsapp')

RATE_LIMIT_FILE = Path('logs/last_whatsapp_promocion.txt')


def _puede_enviar() -> bool:
    if not RATE_LIMIT_FILE.exists():
        return True
    try:
        last = datetime.fromisoformat(RATE_LIMIT_FILE.read_text().strip())
        return datetime.utcnow() - last >= timedelta(days=1)
    except Exception:
        return True


def _registrar_envio():
    RATE_LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RATE_LIMIT_FILE.write_text(datetime.utcnow().isoformat())


@whatsapp_promocionar_bp.route('/promocionar', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def promocionar_whatsapp(current_user):
    if not _puede_enviar():
        return jsonify({'error': 'Solo se permite un envío por día.'}), 429

    data = request.get_json() or {}
    mensaje = data.get('mensaje')
    url_imagen = data.get('url_imagen')
    if not mensaje or not url_imagen:
        return jsonify({'error': 'mensaje y url_imagen son requeridos.'}), 400

    usuarios = User.query.filter(
        User.telefono.isnot(None),
        User.acepta_marketing.is_(True)
    ).all()

    enviados = 0
    for usuario in usuarios:
        if enviar_imagen_whatsapp(usuario.telefono, mensaje, url_imagen):
            enviados += 1

    _registrar_envio()
    return jsonify({'enviados': enviados}), 200
