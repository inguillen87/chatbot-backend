# routes/ai_templates.py

from flask import Blueprint, jsonify, request, current_app
from models import PlantillasRespuesta, db # db será necesario para las operaciones de escritura/actualización
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from services.embedding_service import embed_textos_llm

# Definir el Blueprint con el prefijo de URL /api/ai
ai_templates_bp = Blueprint('ai_templates', __name__, url_prefix='/api/ai')

@ai_templates_bp.route('/templates', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def get_all_templates(user): # 'user' es inyectado por @token_requerido
    """
    Endpoint para listar todas las plantillas de respuesta.
    Autenticación: Requerida (admin/empleado).
    """
    try:
        # Filtrar por usuario si en el futuro se decide vincular plantillas a usuarios/empresas
        # Por ahora, se asume que las plantillas son globales para todos los admins/empleados
        # if user.rol == 'admin':
        #     plantillas = PlantillasRespuesta.query.order_by(PlantillasRespuesta.name.asc()).all()
        # else: # empleado, podría tener acceso solo a las de su empresa si user_id estuviera en PlantillasRespuesta
        #     plantillas = PlantillasRespuesta.query.filter_by(user_id=user.empresa_id or user.id).order_by(PlantillasRespuesta.name.asc()).all()

        plantillas = PlantillasRespuesta.query.order_by(PlantillasRespuesta.name.asc()).all()

        lista_plantillas = []
        for plantilla in plantillas:
            lista_plantillas.append({
                "id": plantilla.id,
                "name": plantilla.name,
                "text": plantilla.text,
                "keywords": plantilla.keywords if plantilla.keywords else [], # Asegurar que sea una lista
                "is_active": plantilla.is_active,
                "created_at": plantilla.created_at.isoformat() if plantilla.created_at else None,
                "updated_at": plantilla.updated_at.isoformat() if plantilla.updated_at else None
            })

        return jsonify({"plantillas": lista_plantillas}), 200
    except Exception as e:
        current_app.logger.error(f"Error al obtener todas las plantillas para el usuario {user.id if user else 'desconocido'}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener las plantillas."}), 500


@ai_templates_bp.route('/templates', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def create_template(user):
    """
    Endpoint para crear una nueva plantilla de respuesta.
    Autenticación: Requerida (admin/empleado).
    Genera embedding para el campo text.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON"}), 400

    name = data.get('name')
    text = data.get('text')
    keywords = data.get('keywords') # Opcional
    is_active = data.get('is_active', True) # Opcional, default True

    if not name or not isinstance(name, str) or not name.strip():
        return jsonify({"error": "El campo 'name' es requerido y debe ser un string no vacío."}), 400
    if not text or not isinstance(text, str) or not text.strip():
        return jsonify({"error": "El campo 'text' es requerido y debe ser un string no vacío."}), 400
    if keywords is not None and not isinstance(keywords, list):
        return jsonify({"error": "El campo 'keywords' debe ser una lista de strings si se proporciona."}), 400
    if keywords:
        for kw in keywords:
            if not isinstance(kw, str):
                return jsonify({"error": "Todos los elementos en 'keywords' deben ser strings."}), 400
    if not isinstance(is_active, bool):
        return jsonify({"error": "El campo 'is_active' debe ser booleano."}), 400

    # Generar embedding para el texto de la plantilla
    embedding_vector = None
    if text:
        try:
            # embed_textos_llm espera una lista de textos y devuelve una lista de embeddings
            embeddings_list = embed_textos_llm(textos=[text.strip()], input_type="search_document")
            if embeddings_list and len(embeddings_list) > 0:
                embedding_vector = embeddings_list[0]
            else:
                current_app.logger.warning(f"No se pudo generar embedding para la plantilla '{name.strip()}' texto: '{text.strip()[:50]}...'")
                # Decidir si fallar o continuar sin embedding. El prompt indica "Importante: Generar..."
                # Por ahora, se registrará la advertencia y se guardará sin embedding si falla.
                # Si es crítico, se podría devolver un error 500 aquí.
        except Exception as e:
            current_app.logger.error(f"Error al generar embedding para la plantilla '{name.strip()}': {e}", exc_info=True)
            # Similar al caso anterior, se podría devolver un error aquí.

    try:
        nueva_plantilla = PlantillasRespuesta(
            name=name.strip(),
            text=text.strip(),
            keywords=keywords if keywords else [],
            is_active=is_active,
            embedding=embedding_vector # Guardar el embedding
            # created_at y updated_at se manejan por defecto en el modelo
            # user_id podría asignarse aquí si se implementa la lógica de plantillas por usuario/empresa
            # Ejemplo: user_id = user.empresa_id if user.rol == 'empleado' and user.empresa_id else user.id
        )
        db.session.add(nueva_plantilla)
        db.session.commit()

        current_app.logger.info(f"Plantilla '{nueva_plantilla.name}' creada con ID {nueva_plantilla.id} por usuario {user.id}.")

        return jsonify({
            "id": nueva_plantilla.id,
            "name": nueva_plantilla.name,
            "text": nueva_plantilla.text,
            "keywords": nueva_plantilla.keywords,
            "is_active": nueva_plantilla.is_active,
            "embedding_generated": bool(embedding_vector), # Informar si se generó el embedding
            "created_at": nueva_plantilla.created_at.isoformat(),
            "updated_at": nueva_plantilla.updated_at.isoformat()
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al guardar nueva plantilla '{name.strip()}' para usuario {user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar la plantilla."}), 500

from sqlalchemy.orm.attributes import flag_modified # Para notificar a SQLAlchemy de cambios en JSON/Pickle

@ai_templates_bp.route('/templates/<string:template_id>', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def update_template(user, template_id):
    """
    Endpoint para actualizar una plantilla existente.
    Autenticación: Requerida (admin/empleado).
    Regenera embedding si el campo text cambia.
    """
    plantilla = PlantillasRespuesta.query.get(template_id)
    if not plantilla:
        return jsonify({"error": "Plantilla no encontrada."}), 404

    # TODO: Considerar lógica de permisos si las plantillas se vinculan a user_id/empresa_id
    # if user.rol == 'empleado' and hasattr(plantilla, 'user_id') and plantilla.user_id != user.empresa_id:
    #     return jsonify({"error": "Permiso denegado."}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON y no estar vacío."}), 400

    updated_fields = []
    text_changed = False

    if 'name' in data:
        new_name = data['name']
        if not isinstance(new_name, str) or not new_name.strip():
            return jsonify({"error": "El campo 'name' debe ser un string no vacío si se proporciona."}), 400
        if plantilla.name != new_name.strip():
            plantilla.name = new_name.strip()
            updated_fields.append('name')

    if 'text' in data:
        new_text = data['text']
        if not isinstance(new_text, str) or not new_text.strip():
            return jsonify({"error": "El campo 'text' debe ser un string no vacío si se proporciona."}), 400
        if plantilla.text != new_text.strip():
            plantilla.text = new_text.strip()
            text_changed = True
            # No agregar a updated_fields aún, se maneja por text_changed para el embedding

    if 'keywords' in data:
        new_keywords = data['keywords']
        if not isinstance(new_keywords, list):
            return jsonify({"error": "El campo 'keywords' debe ser una lista de strings si se proporciona."}), 400
        for kw in new_keywords:
            if not isinstance(kw, str):
                return jsonify({"error": "Todos los elementos en 'keywords' deben ser strings."}), 400
        # Comparar listas correctamente
        if sorted(plantilla.keywords if plantilla.keywords else []) != sorted(new_keywords):
            plantilla.keywords = new_keywords
            flag_modified(plantilla, "keywords") # Importante para tipos JSON mutables
            updated_fields.append('keywords')

    if 'is_active' in data:
        new_is_active = data['is_active']
        if not isinstance(new_is_active, bool):
            return jsonify({"error": "El campo 'is_active' debe ser booleano si se proporciona."}), 400
        if plantilla.is_active != new_is_active:
            plantilla.is_active = new_is_active
            updated_fields.append('is_active')

    if not updated_fields and not text_changed:
        return jsonify({"mensaje": "No se proporcionaron campos para actualizar o los valores son los mismos que los actuales."}), 200

    embedding_regenerated = False
    if text_changed:
        updated_fields.append('text') # Ahora sí lo agregamos a los campos actualizados
        current_app.logger.info(f"El texto de la plantilla '{plantilla.id}' ha cambiado. Regenerando embedding.")
        try:
            embeddings_list = embed_textos_llm(textos=[plantilla.text], input_type="search_document")
            if embeddings_list and len(embeddings_list) > 0:
                plantilla.embedding = embeddings_list[0]
                embedding_regenerated = True
                flag_modified(plantilla, "embedding") # Importante para tipos PickleType
                current_app.logger.info(f"Embedding regenerado para la plantilla '{plantilla.id}'.")
            else:
                current_app.logger.warning(f"No se pudo regenerar embedding para la plantilla '{plantilla.id}'. Se eliminará el embedding existente.")
                plantilla.embedding = None
                flag_modified(plantilla, "embedding")
        except Exception as e:
            current_app.logger.error(f"Error al regenerar embedding para la plantilla '{plantilla.id}': {e}", exc_info=True)
            # No se detiene la actualización de otros campos, pero el embedding puede quedar nulo/viejo.

    try:
        # updated_at se actualiza automáticamente por onupdate=datetime.utcnow en el modelo
        db.session.commit()
        current_app.logger.info(f"Plantilla '{plantilla.id}' actualizada por usuario {user.id}. Campos actualizados: {', '.join(updated_fields) if updated_fields else 'ninguno (solo embedding)'}. Embedding regenerado: {embedding_regenerated}")

        return jsonify({
            "id": plantilla.id,
            "name": plantilla.name,
            "text": plantilla.text,
            "keywords": plantilla.keywords,
            "is_active": plantilla.is_active,
            "embedding_regenerated": embedding_regenerated,
            "created_at": plantilla.created_at.isoformat(),
            "updated_at": plantilla.updated_at.isoformat()
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar plantilla '{plantilla.id}' por usuario {user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al actualizar la plantilla."}), 500

@ai_templates_bp.route('/templates/<string:template_id>', methods=['DELETE'])
@token_requerido
@admin_o_empleado_requerido # Opcionalmente solo_admin_requerido si se prefiere
def delete_template(user, template_id):
    """
    Endpoint para eliminar una plantilla existente.
    Autenticación: Requerida (admin/empleado).
    """
    plantilla = PlantillasRespuesta.query.get(template_id)
    if not plantilla:
        return jsonify({"error": "Plantilla no encontrada."}), 404

    # TODO: Considerar lógica de permisos si las plantillas se vinculan a user_id/empresa_id
    # if user.rol == 'empleado' and hasattr(plantilla, 'user_id') and plantilla.user_id != user.empresa_id:
    #     return jsonify({"error": "Permiso denegado para eliminar esta plantilla."}), 403

    try:
        nombre_plantilla_eliminada = plantilla.name # Guardar nombre para el log/mensaje
        db.session.delete(plantilla)
        db.session.commit()
        current_app.logger.info(f"Plantilla '{nombre_plantilla_eliminada}' (ID: {template_id}) eliminada por usuario {user.id}.")
        return jsonify({"mensaje": f"Plantilla '{nombre_plantilla_eliminada}' eliminada correctamente."}), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al eliminar plantilla '{template_id}' por usuario {user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al eliminar la plantilla."}), 500

from services.llm_bridge import llamar_llm_para_generacion_texto

@ai_templates_bp.route('/generate-template-text', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido # Asumiendo mismos permisos que para gestionar plantillas
def generate_template_text_from_prompt(user):
    """
    Genera texto para una plantilla usando Cohere Generate a partir de un prompt.
    Autenticación: Requerida (admin/empleado).
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON"}), 400

    prompt_usuario = data.get('prompt')
    if not prompt_usuario or not isinstance(prompt_usuario, str) or not prompt_usuario.strip():
        return jsonify({"error": "El campo 'prompt' es requerido y debe ser un string no vacío."}), 400

    try:
        # Podríamos agregar un preámbulo o instrucciones adicionales si es necesario,
        # por ejemplo, para guiar el tono o formato de la plantilla.
        # Ejemplo de preámbulo:
        # preamble = "Eres un asistente experto en redactar plantillas de respuesta concisas y profesionales para atención al cliente. " \
        #            "El texto debe ser adecuado para ser usado directamente como una plantilla de respuesta rápida."

        MAX_PROMPT_LENGTH = 2000
        if len(prompt_usuario) > MAX_PROMPT_LENGTH:
            return jsonify({"error": f"El prompt excede la longitud máxima de {MAX_PROMPT_LENGTH} caracteres."}), 400

        current_app.logger.info(f"Usuario {user.id} solicitando generación de texto para plantilla con prompt: '{prompt_usuario[:100]}...'")

        # Usar el servicio LLM para generación de texto
        system_prompt_generacion = "Eres un asistente experto en redactar plantillas de respuesta. Genera un texto basado en la siguiente solicitud del usuario."
        generated_text = llamar_llm_para_generacion_texto(
            system_prompt_especifico=system_prompt_generacion,
            user_prompt=prompt_usuario,
            temperature=0.7 # Puede ajustarse para más creatividad
        )

        if generated_text:
            current_app.logger.info(f"Texto generado por LLM para prompt de usuario {user.id}: '{generated_text[:100]}...'")
            return jsonify({"generated_text": generated_text.strip()}), 200
        else:
            current_app.logger.error(f"El LLM no devolvió texto para el prompt del usuario {user.id}: '{prompt_usuario[:100]}...'.")
            return jsonify({"error": "No se pudo generar el texto de la plantilla en este momento. Intente más tarde."}), 503 # Service Unavailable

    except Exception as e:
        current_app.logger.error(f"Error al generar texto de plantilla (LLM) para usuario {user.id} con prompt '{prompt_usuario[:100]}...': {e}", exc_info=True)
        return jsonify({"error": "Error interno al procesar la solicitud de generación de texto."}), 500

@ai_templates_bp.route('/improve-template-text', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido # Asumiendo mismos permisos
def improve_template_text(user):
    """
    Mejora el texto de una plantilla existente usando el LLM.
    Autenticación: Requerida (admin/empleado).
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON"}), 400

    text_to_improve = data.get('text_to_improve')
    if not text_to_improve or not isinstance(text_to_improve, str) or not text_to_improve.strip():
        return jsonify({"error": "El campo 'text_to_improve' es requerido y debe ser un string no vacío."}), 400

    try:
        MAX_TEXT_LENGTH = 4000 # Limitar la longitud del texto a mejorar
        if len(text_to_improve) > MAX_TEXT_LENGTH:
            return jsonify({"error": f"El texto a mejorar excede la longitud máxima de {MAX_TEXT_LENGTH} caracteres."}), 400

        # Construir el prompt para Cohere
        # Prompt mejorado para solicitar específicamente el texto mejorado y mantener el significado.
        prompt_para_cohere = (
            f"Por favor, reescribe el siguiente texto para que sea más claro, conciso y profesional, manteniendo el significado original. "
            f"El resultado debe ser únicamente el texto mejorado, sin introducciones ni comentarios adicionales. "
            f"Texto a mejorar:\n\"\"\"\n{text_to_improve}\n\"\"\""
        )

        # Podríamos tener un preámbulo general también para esta tarea si se desea
        # preamble_mejorar = "Eres un asistente experto en refinar y mejorar textos para plantillas de comunicación profesional."

        current_app.logger.info(f"Usuario {user.id} solicitando mejora de texto para plantilla: '{text_to_improve[:100]}...'")

        # El prompt_para_cohere ya está bien formulado para ser un user_prompt para el LLM.
        # El system_prompt puede ser más genérico o específico para la tarea de mejora.
        system_prompt_mejora = "Eres un asistente experto en refinar y mejorar textos para plantillas de comunicación profesional. Responde únicamente con el texto mejorado."
        improved_text = llamar_llm_para_generacion_texto(
            system_prompt_especifico=system_prompt_mejora,
            user_prompt=prompt_para_cohere, # prompt_para_cohere ya contiene la instrucción y el texto
            temperature=0.5 # Temperatura moderada para mejora
        )

        if improved_text:
            cleaned_text = improved_text.strip()
            # La limpieza adicional que se hacía para Cohere podría no ser necesaria o ser diferente para el LLM.
            # Se deja como está por ahora, pero se podría revisar si el LLM añade prefijos/sufijos no deseados.
            current_app.logger.info(f"Texto mejorado por LLM para usuario {user.id}: '{cleaned_text[:100]}...'")
            return jsonify({"improved_text": cleaned_text}), 200
        else:
            current_app.logger.error(f"El LLM no devolvió texto mejorado para el input del usuario {user.id}: '{text_to_improve[:100]}...'.")
            return jsonify({"error": "No se pudo mejorar el texto de la plantilla en este momento. Intente más tarde."}), 503

    except Exception as e:
        current_app.logger.error(f"Error al mejorar texto de plantilla (LLM) para usuario {user.id} con texto '{text_to_improve[:100]}...': {e}", exc_info=True)
        return jsonify({"error": "Error interno al procesar la solicitud de mejora de texto."}), 500
