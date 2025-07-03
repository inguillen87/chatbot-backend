from flask import Blueprint, request, jsonify, current_app
from models import User, PlantillasRespuesta # Importar PlantillasRespuesta
from services.cohere_ai import robust_embed
from services.qdrant_search import buscar_catalogo_qdrant # Podríamos necesitar algo similar o adaptado
from extensions import db
from .auth import token_requerido # Asumiendo que admin_o_empleado_requerido está aqui o se importa desde auth
# Si admin_o_empleado_requerido es un decorador separado:
# from utils.permissions import admin_o_empleado_requerido # O la ruta correcta a este decorador

from models import User, PlantillasRespuesta, db # Asegurar db está importado
from services.cohere_ai import robust_embed
from services.common_utils import cosine_similarity # Importar cosine_similarity
from utils.permissions import require_role


ai_bp = Blueprint('ai_bp', __name__, url_prefix='/api/ai')

MIN_SIMILARITY_THRESHOLD = 0.5 # Umbral mínimo de similaridad para considerar una plantilla

@ai_bp.route('/suggest-templates', methods=['POST'])
@token_requerido
@require_role('admin', 'empleado') # Solo admins o empleados pueden acceder
def suggest_templates_route(current_user: User):
    data = request.get_json()

    if not data or not data.get('asunto'):
        return jsonify({"error": "El campo 'asunto' es obligatorio."}), 400

    asunto = data['asunto']
    contexto_ticket = data.get('contexto_ticket', '') # Opcional
    top_n = data.get('top_n', 3) # Opcional, default 3

    if not isinstance(asunto, str) or not asunto.strip():
        return jsonify({"error": "El campo 'asunto' debe ser un string no vacío."}), 400
    if not isinstance(contexto_ticket, str):
        return jsonify({"error": "El campo 'contexto_ticket' debe ser un string."}), 400
    if not isinstance(top_n, int) or top_n <= 0:
        return jsonify({"error": "El campo 'top_n' debe ser un entero positivo."}), 400

    current_app.logger.info(
        f"[SUGGEST_TEMPLATES] User {current_user.id} (Rol {current_user.rol}). Asunto: '{asunto[:50]}...', Contexto: '{contexto_ticket[:50]}...', Top_n: {top_n}"
    )

    # 1. Combinar asunto y contexto_ticket para formar la consulta.
    #    Se da más peso al asunto si ambos están presentes, o se concatenan.
    #    Una estrategia simple es concatenarlos.
    texto_consulta = asunto
    if contexto_ticket.strip():
        texto_consulta = f"{asunto}\n\n{contexto_ticket}"

    if not texto_consulta.strip():
        return jsonify({"sugerencias": [], "message": "El texto de consulta combinado (asunto/contexto) está vacío."}), 200

    # 2. Generar embedding para la consulta usando cohere_ai.robust_embed().
    #    robust_embed espera una lista de textos.
    try:
        query_embedding_list = robust_embed(textos=[texto_consulta], input_type="search_query")
        if not query_embedding_list or not query_embedding_list[0]:
            current_app.logger.error(f"[SUGGEST_TEMPLATES] No se pudo generar embedding para la consulta: '{texto_consulta[:100]}...'")
            return jsonify({"error": "Error al generar el embedding para la consulta."}), 500
        query_embedding = query_embedding_list[0]
    except Exception as e:
        current_app.logger.error(f"[SUGGEST_TEMPLATES] Excepción al generar embedding para la consulta: {e}", exc_info=True)
        return jsonify({"error": "Excepción al procesar la consulta con IA."}), 500

    # 3. Obtener todas las plantillas activas que tengan un embedding.
    try:
        plantillas_activas = PlantillasRespuesta.query.filter(
            PlantillasRespuesta.is_active == True,
            PlantillasRespuesta.embedding != None # Asegurar que el embedding no sea NULL
        ).all()
    except Exception as e:
        current_app.logger.error(f"[SUGGEST_TEMPLATES] Error al consultar plantillas en la BD: {e}", exc_info=True)
        return jsonify({"error": "Error al obtener plantillas de la base de datos."}), 500

    if not plantillas_activas:
        current_app.logger.info("[SUGGEST_TEMPLATES] No hay plantillas activas con embeddings disponibles.")
        return jsonify({"sugerencias": [], "message": "No hay plantillas de respuesta activas configuradas con embeddings."}), 200

    # 4. Calcular similaridad coseno.
    sugerencias_con_score = []
    for plantilla in plantillas_activas:
        if not plantilla.embedding or not isinstance(plantilla.embedding, list):
            current_app.logger.warning(f"[SUGGEST_TEMPLATES] Plantilla ID {plantilla.id} ('{plantilla.name}') no tiene un embedding válido, saltando.")
            continue

        try:
            score = cosine_similarity(query_embedding, plantilla.embedding)
        except Exception as e:
            current_app.logger.error(f"[SUGGEST_TEMPLATES] Error calculando similaridad para plantilla ID {plantilla.id}: {e}", exc_info=True)
            score = 0.0 # No considerar esta plantilla si hay error

        if score >= MIN_SIMILARITY_THRESHOLD: # Aplicar umbral mínimo
            sugerencias_con_score.append({
                "id_plantilla": str(plantilla.id), # Convertir a string si es UUID o similar
                "name": plantilla.name,
                "text": plantilla.text,
                "score": round(score, 4) # Redondear para mejor presentación
            })

    # 5. Ordenar por similaridad y devolver top_n.
    sugerencias_ordenadas = sorted(sugerencias_con_score, key=lambda x: x['score'], reverse=True)

    final_sugerencias = sugerencias_ordenadas[:top_n]

    current_app.logger.info(f"[SUGGEST_TEMPLATES] Devolviendo {len(final_sugerencias)} sugerencias. Scores: {[s['score'] for s in final_sugerencias]}")

    return jsonify({"sugerencias": final_sugerencias}), 200

# Aquí podrían ir otras rutas relacionadas con IA en el futuro.
