from flask import Blueprint, request, jsonify
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from models import User
from utils.whatsapp import enviar_imagen_whatsapp
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

whatsapp_promocionar_bp = Blueprint('whatsapp_promocionar', __name__, url_prefix='/api/whatsapp')

RATE_LIMIT_DIR = Path('logs')


def _rate_limit_file(empresa_id: Optional[int]) -> Path:
    name = 'last_whatsapp_promocion_global.txt' if empresa_id is None else f'last_whatsapp_promocion_{empresa_id}.txt'
    return RATE_LIMIT_DIR / name


def _check_file(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        last = datetime.fromisoformat(path.read_text().strip())
        return datetime.utcnow() - last >= timedelta(days=1)
    except Exception:
        return True


def _puede_enviar(empresa_id: Optional[int], scope_all: bool = False) -> bool:
    global_file = _rate_limit_file(None)
    if not _check_file(global_file):
        return False
    if scope_all:
        return True
    return _check_file(_rate_limit_file(empresa_id))


def _registrar_envio(empresa_id: Optional[int], scope_all: bool = False):
    path = _rate_limit_file(None if scope_all else empresa_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.utcnow().isoformat())


def _ultimo_envio(empresa_id: Optional[int]):
    path = _rate_limit_file(empresa_id)
    if not path.exists():
        return None
    try:
        return datetime.fromisoformat(path.read_text().strip())
    except Exception:
        return None


@whatsapp_promocionar_bp.route('/promocionar', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def promocionar_whatsapp(current_user):
    data = request.get_json() or {}
    # The frontend may send a pre-built `mensaje` or individual fields
    # to compose one. Prefer explicit pieces so employees don't have to
    # manually craft the WhatsApp text.
    mensaje = data.get('mensaje')
    titulo = data.get('titulo')
    descripcion = data.get('descripcion')
    link = data.get('link')
    url_imagen = data.get('url_imagen')

    if not mensaje:
        if titulo and descripcion and link:
            mensaje = f"{titulo}\n\n{descripcion}\n{link}"
        else:
            return jsonify({'error': 'titulo, descripcion y link son requeridos si no se envía mensaje.'}), 400

    if not url_imagen:
        return jsonify({'error': 'url_imagen es requerido.'}), 400

    scope_all = bool(data.get('todos') or request.args.get('todos'))
    if scope_all and current_user.rol != 'super_admin':
        scope_all = False
    query = User.query.filter(
        User.telefono.isnot(None),
        User.acepta_marketing.is_(True)
    )

    if current_user.rol == 'super_admin' and scope_all:
        empresa_id = None
        usuarios = query.all()
    else:
        empresa_id = current_user.id if current_user.rol == 'admin' and current_user.empresa_id is None else current_user.empresa_id
        if not empresa_id:
            return jsonify({'error': 'No se pudo determinar la empresa del usuario.'}), 403
        usuarios = query.filter(User.empresa_id == empresa_id).all()

    if not _puede_enviar(empresa_id, scope_all):
        return jsonify({'error': 'Solo se permite un envío por día.'}), 429

    enviados = 0
    for usuario in usuarios:
        if enviar_imagen_whatsapp(usuario.telefono, mensaje, url_imagen):
            enviados += 1

    _registrar_envio(empresa_id, scope_all)
    return jsonify({'enviados': enviados}), 200


@whatsapp_promocionar_bp.route('/promocionar', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def estado_promocion(current_user):
    scope_all = bool(request.args.get('todos')) and current_user.rol == 'super_admin'
    empresa_id = None if scope_all else (
        current_user.id if current_user.rol == 'admin' and current_user.empresa_id is None else current_user.empresa_id
    )
    last = _ultimo_envio(empresa_id)
    return jsonify({
        'puede_enviar': _puede_enviar(empresa_id, scope_all),
        'ultimo_envio': last.isoformat() if last else None
    })
