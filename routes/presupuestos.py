from flask import Blueprint, request, jsonify
from services.presupuesto_pdf import generar_presupuesto_pdf
from services.email_service import enviar_email_con_adjunto
from routes.auth import token_requerido

presupuesto_bp = Blueprint('presupuesto_bp', __name__, url_prefix='/presupuestos')


@presupuesto_bp.route('/generar', methods=['POST'])
@token_requerido
def generar_presupuesto(user):
    data = request.get_json() or {}
    items = data.get('items') or []
    if not items:
        return jsonify({'error': 'Items requeridos'}), 400
    email = data.get('email_cliente')
    if not email:
        return jsonify({'error': 'Email requerido'}), 400
    cliente = {
        'nombre': data.get('nombre_cliente'),
    }
    pdf_bytes = generar_presupuesto_pdf(items, cliente)
    asunto = 'Presupuesto solicitado'
    cuerpo = '<p>Adjuntamos el presupuesto solicitado.</p>'
    ok = enviar_email_con_adjunto(email, asunto, cuerpo, 'presupuesto.pdf', pdf_bytes)
    if ok:
        return jsonify({'mensaje': 'Presupuesto enviado'}), 200
    return jsonify({'error': 'No se pudo enviar el presupuesto'}), 500
