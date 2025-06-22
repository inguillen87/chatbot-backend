from flask import Blueprint, jsonify, request
from models import User, Recordatorio
from extensions import db
from routes.auth import token_requerido
from datetime import datetime

recordatorios_bp = Blueprint('recordatorios', __name__, url_prefix='/recordatorios')

@recordatorios_bp.route('', methods=['GET'])
@token_requerido
def listar_recordatorios(current_user: User):
    """Lista los recordatorios del usuario actual (empresa)."""
    records = Recordatorio.query.filter_by(empresa_id=current_user.id).order_by(Recordatorio.fecha_vencimiento.asc()).all()
    return jsonify([
        {
            'id': r.id,
            'cliente_id': r.cliente_id,
            'tipo': r.tipo,
            'descripcion': r.descripcion,
            'fecha_vencimiento': r.fecha_vencimiento.isoformat(),
            'enviado': r.enviado,
        }
        for r in records
    ])

@recordatorios_bp.route('', methods=['POST'])
@token_requerido
def crear_recordatorio(current_user: User):
    data = request.get_json(silent=True) or {}
    cliente_id = data.get('cliente_id')
    tipo = data.get('tipo')
    fecha = data.get('fecha_vencimiento')
    if not cliente_id or not tipo or not fecha:
        return jsonify({'error': 'Datos incompletos'}), 400
    try:
        fecha_dt = datetime.fromisoformat(fecha)
    except ValueError:
        return jsonify({'error': 'fecha_vencimiento invalida'}), 400
    rec = Recordatorio(
        empresa_id=current_user.id,
        cliente_id=cliente_id,
        tipo=tipo,
        descripcion=data.get('descripcion'),
        fecha_vencimiento=fecha_dt,
    )
    db.session.add(rec)
    db.session.commit()
    return jsonify({'id': rec.id}), 201
