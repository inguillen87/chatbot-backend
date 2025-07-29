from flask import Blueprint, request, jsonify, current_app
from models import User, PlantillasRespuesta
from services.embedding_service import embed_textos_gemini
from extensions import db
from .auth import token_requerido
from models import User, PlantillasRespuesta, db
from services.common_utils import cosine_similarity
from utils.permissions import require_role


ai_bp = Blueprint('ai_bp', __name__, url_prefix='/api/ai')

MIN_SIMILARITY_THRESHOLD = 0.5

@ai_bp.route('/suggest-templates', methods=['POST'])
@token_requerido
@require_role('admin', 'empleado')
def suggest_templates_route(current_user: User):
    data = request.get_json()

    if not data or not data.get('asunto'):
        return jsonify({"error": "El campo 'asunto' es obligatorio."}), 400

    asunto = data['asunto']
    contexto_ticket = data.get('contexto_ticket', '')
    top_n = data.get('top_n', 3)

    if not isinstance(asunto, str) or not asunto.strip():
        return jsonify({"error": "El campo 'asunto' debe ser un string no vacío."}), 400
    if not isinstance(contexto_ticket, str):
        return jsonify({"error": "El campo 'contexto_ticket' debe ser un string."}), 400
    if not isinstance(top_n, int) or top_n <= 0:
        return jsonify({"error": "El campo 'top_n' debe ser un entero positivo."}), 400

    current_app.logger.info(
        f"[SUGGEST_TEMPLATES] User {current_user.id} (Rol {current_user.rol}). Asunto: '{asunto[:50]}...', Contexto: '{contexto_ticket[:50]}...', Top_n: {top_n}"
    )

    texto_consulta = asunto
    if contexto_ticket.strip():
        texto_consulta = f"{asunto}\n\n{contexto_ticket}"

    if not texto_consulta.strip():
        return jsonify({"sugerencias": [], "message": "El texto de consulta combinado (asunto/contexto) está vacío."}), 200

    try:
        query_embedding_list = embed_textos_gemini(textos=[texto_consulta], input_type="search_query")
        if not query_embedding_list or not query_embedding_list[0]:
            current_app.logger.error(f"[SUGGEST_TEMPLATES] No se pudo generar embedding para la consulta: '{texto_consulta[:100]}...'")
            return jsonify({"error": "Error al generar el embedding para la consulta."}), 500
        query_embedding = query_embedding_list[0]
    except Exception as e:
        current_app.logger.error(f"[SUGGEST_TEMPLATES] Excepción al generar embedding para la consulta: {e}", exc_info=True)
        return jsonify({"error": "Excepción al procesar la consulta con IA."}), 500

    try:
        plantillas_activas = PlantillasRespuesta.query.filter(
            PlantillasRespuesta.is_active == True,
            PlantillasRespuesta.embedding != None
        ).all()
    except Exception as e:
        current_app.logger.error(f"[SUGGEST_TEMPLATES] Error al consultar plantillas en la BD: {e}", exc_info=True)
        return jsonify({"error": "Error al obtener plantillas de la base de datos."}), 500

    if not plantillas_activas:
        current_app.logger.info("[SUGGEST_TEMPLATES] No hay plantillas activas con embeddings disponibles.")
        return jsonify({"sugerencias": [], "message": "No hay plantillas de respuesta activas configuradas con embeddings."}), 200

    sugerencias_con_score = []
    for plantilla in plantillas_activas:
        if not plantilla.embedding or not isinstance(plantilla.embedding, list):
            current_app.logger.warning(f"[SUGGEST_TEMPLATES] Plantilla ID {plantilla.id} ('{plantilla.name}') no tiene un embedding válido, saltando.")
            continue

        try:
            score = cosine_similarity(query_embedding, plantilla.embedding)
        except Exception as e:
            current_app.logger.error(f"[SUGGEST_TEMPLATES] Error calculando similaridad para plantilla ID {plantilla.id}: {e}", exc_info=True)
            score = 0.0

        if score >= MIN_SIMILARITY_THRESHOLD:
            sugerencias_con_score.append({
                "id_plantilla": str(plantilla.id),
                "name": plantilla.name,
                "text": plantilla.text,
                "score": round(score, 4)
            })

    sugerencias_ordenadas = sorted(sugerencias_con_score, key=lambda x: x['score'], reverse=True)

    final_sugerencias = sugerencias_ordenadas[:top_n]

    current_app.logger.info(f"[SUGGEST_TEMPLATES] Devolviendo {len(final_sugerencias)} sugerencias. Scores: {[s['score'] for s in final_sugerencias]}")

    return jsonify({"sugerencias": final_sugerencias}), 200

# Aquí podrían ir otras rutas relacionadas con IA en el futuro.
