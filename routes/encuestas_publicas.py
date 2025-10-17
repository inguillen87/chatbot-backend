from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from flask import Blueprint, jsonify, request, current_app

from models import (
    db,
    PublicSurvey,
    PublicSurveyQuestion,
    PublicSurveyOption,
    PublicSurveyResponse,
    PublicSurveyAnswer,
)
from utils.auth_helpers import (
    admin_o_empleado_requerido,
    get_or_create_anon_id,
    token_requerido,
)


encuestas_admin_bp = Blueprint("encuestas_admin", __name__, url_prefix="/admin/encuestas")
encuestas_public_bp = Blueprint("encuestas_public", __name__, url_prefix="/public/encuestas")


@encuestas_admin_bp.route("", methods=["OPTIONS"], provide_automatic_options=False)
@encuestas_admin_bp.route("/", methods=["OPTIONS"], provide_automatic_options=False)
@encuestas_admin_bp.route("/<path:anything>", methods=["OPTIONS"], provide_automatic_options=False)
def encuestas_admin_preflight(anything: Optional[str] = None):
    """Allow unauthenticated CORS preflight checks for the legacy admin API."""

    return current_app.make_default_options_response()


STATUS_ALIASES = {
    "draft": "draft",
    "borrador": "draft",
    "published": "published",
    "publicada": "published",
    "archived": "archived",
    "archivada": "archived",
    "cerrada": "archived",
}

QUESTION_TYPE_ALIASES = {
    "single_choice": "single_choice",
    "opcion_unica": "single_choice",
    "radio": "single_choice",
    "multiple_choice": "multiple_choice",
    "opcion_multiple": "multiple_choice",
    "checkbox": "multiple_choice",
    "text": "text",
    "texto": "text",
    "open_text": "text",
    "abierta": "text",
}


def _slugify() -> str:
    return secrets.token_hex(3)


def _normalize_slug_value(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    slug = str(raw).strip()
    if not slug:
        return None
    return slug.lower()


def _normalize_status(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return STATUS_ALIASES.get(raw.lower().strip())


def _normalize_question_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return QUESTION_TYPE_ALIASES.get(raw.lower().strip())


def _serialize_option(option: PublicSurveyOption) -> Dict[str, Any]:
    return {
        "id": option.id,
        "texto": option.texto,
        "valor": option.valor,
        "orden": option.orden,
    }


def _serialize_question(question: PublicSurveyQuestion) -> Dict[str, Any]:
    return {
        "id": question.id,
        "titulo": question.titulo,
        "descripcion": question.descripcion,
        "tipo": question.tipo,
        "obligatoria": question.obligatoria,
        "orden": question.orden,
        "opciones": [_serialize_option(opt) for opt in question.opciones],
    }


def _serialize_survey(survey: PublicSurvey, include_questions: bool = True) -> Dict[str, Any]:
    data = {
        "id": survey.id,
        "slug": survey.slug,
        "titulo": survey.titulo,
        "descripcion": survey.descripcion,
        "estado": survey.estado,
        "estado_publico": survey.estado_publico(),
        "created_at": survey.created_at.isoformat() if survey.created_at else None,
        "updated_at": survey.updated_at.isoformat() if survey.updated_at else None,
        "published_at": survey.published_at.isoformat() if survey.published_at else None,
        "archived_at": survey.archived_at.isoformat() if survey.archived_at else None,
    }
    if include_questions:
        data["preguntas"] = [_serialize_question(q) for q in survey.preguntas]
    return data


def _ensure_slug_unique(slug: Optional[str], survey_id: Optional[int] = None) -> bool:
    if not slug:
        return True

    query = PublicSurvey.query.filter_by(slug=slug)
    if survey_id is not None:
        query = query.filter(PublicSurvey.id != survey_id)
    return not db.session.query(query.exists()).scalar()


def _update_options(question: PublicSurveyQuestion, options_payload: Iterable[Dict[str, Any]]) -> None:
    existing = {opt.id: opt for opt in question.opciones if opt.id is not None}
    seen_options: set[PublicSurveyOption] = set()
    for index, option_data in enumerate(options_payload or []):
        opt_id = option_data.get("id") or option_data.get("option_id")
        if opt_id:
            opt = existing.get(opt_id)
            if not opt:
                raise ValueError(f"Opción {opt_id} no pertenece a la pregunta {question.id}")
        else:
            opt = PublicSurveyOption(question=question)
            db.session.add(opt)

        opt.texto = option_data.get("texto") or option_data.get("label") or option_data.get("nombre")
        if not opt.texto:
            raise ValueError("Las opciones deben incluir 'texto' o 'label'")
        opt.valor = option_data.get("valor") or option_data.get("value")
        opt.orden = option_data.get("orden") if option_data.get("orden") is not None else index
        seen_options.add(opt)

    for opt in list(question.opciones):
        if opt not in seen_options:
            db.session.delete(opt)


def _update_questions(survey: PublicSurvey, questions_payload: Iterable[Dict[str, Any]]) -> None:
    existing = {question.id: question for question in survey.preguntas}
    seen_ids: set[int] = set()
    for index, question_data in enumerate(questions_payload or []):
        question_id = (
            question_data.get("id")
            or question_data.get("pregunta_id")
            or question_data.get("question_id")
        )
        if question_id:
            question = existing.get(question_id)
            if not question:
                raise ValueError(f"Pregunta {question_id} no pertenece a la encuesta")
        else:
            question = PublicSurveyQuestion(survey=survey)
            db.session.add(question)

        question.titulo = (
            question_data.get("titulo")
            or question_data.get("title")
            or question_data.get("pregunta")
        )
        if not question.titulo:
            raise ValueError("Cada pregunta debe incluir un 'titulo' o 'title'")

        question.descripcion = question_data.get("descripcion") or question_data.get("description")

        tipo_raw = question_data.get("tipo") or question_data.get("type")
        normalized_type = _normalize_question_type(tipo_raw)
        if not normalized_type:
            raise ValueError(f"Tipo de pregunta inválido: {tipo_raw}")
        question.tipo = normalized_type

        question.obligatoria = bool(question_data.get("obligatoria") or question_data.get("required"))
        question.orden = question_data.get("orden") if question_data.get("orden") is not None else index

        _update_options(question, question_data.get("opciones") or question_data.get("options") or [])
        db.session.flush()
        seen_ids.add(question.id)

    for question in list(survey.preguntas):
        if question.id not in seen_ids:
            db.session.delete(question)


def _apply_status_transition(survey: PublicSurvey, desired_status: Optional[str]) -> None:
    if not desired_status:
        return
    now = datetime.now(timezone.utc)
    previous = survey.estado
    survey.estado = desired_status
    survey.updated_at = now
    if desired_status == "published" and survey.published_at is None:
        survey.published_at = now
    if desired_status == "archived" and survey.archived_at is None:
        survey.archived_at = now
    if desired_status == "draft":
        survey.archived_at = None
        if previous != "published":
            survey.published_at = None


def _query_surveys_for_admin(user) -> Any:
    query = PublicSurvey.query
    municipio_id = getattr(user, "municipio_id", None)
    if municipio_id is not None:
        query = query.filter(PublicSurvey.municipio_id == municipio_id)
    return query


@encuestas_admin_bp.route("/", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def listar_encuestas(current_user):
    encuestas = (
        _query_surveys_for_admin(current_user)
        .order_by(PublicSurvey.created_at.desc())
        .all()
    )
    data = [_serialize_survey(encuesta, include_questions=False) for encuesta in encuestas]
    return jsonify({"encuestas": data})


@encuestas_admin_bp.route("/", methods=["POST"])
@token_requerido
@admin_o_empleado_requerido
def crear_encuesta(current_user):
    payload = request.get_json(force=True, silent=True) or {}

    titulo = payload.get("titulo") or payload.get("title")
    if not titulo:
        return jsonify({"error": "El campo 'titulo' es obligatorio"}), 400

    slug = (
        _normalize_slug_value(payload.get("slug") or payload.get("codigo"))
        or _slugify()
    )
    if not _ensure_slug_unique(slug):
        return jsonify({"error": "Ya existe una encuesta con ese slug"}), 409

    encuesta = PublicSurvey(
        titulo=titulo,
        descripcion=payload.get("descripcion") or payload.get("description"),
        slug=slug,
        estado="draft",
        created_by_id=current_user.id,
        municipio_id=getattr(current_user, "municipio_id", None),
    )
    db.session.add(encuesta)
    db.session.flush()

    try:
        _update_questions(encuesta, payload.get("preguntas") or payload.get("questions") or [])
    except ValueError as exc:
        current_app.logger.warning("Error creando encuesta: %s", exc)
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    desired_status = None
    if payload.get("publicar") or payload.get("publish"):
        desired_status = "published"
    else:
        desired_status = _normalize_status(payload.get("estado") or payload.get("status")) or "draft"

    _apply_status_transition(encuesta, desired_status)
    db.session.commit()
    return jsonify(_serialize_survey(encuesta)), 201


@encuestas_admin_bp.route("/<int:encuesta_id>", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def obtener_encuesta(current_user, encuesta_id: int):
    encuesta = (
        _query_surveys_for_admin(current_user)
        .filter_by(id=encuesta_id)
        .first_or_404()
    )
    return jsonify(_serialize_survey(encuesta))


@encuestas_admin_bp.route("/<int:encuesta_id>", methods=["PUT"])
@token_requerido
@admin_o_empleado_requerido
def actualizar_encuesta(current_user, encuesta_id: int):
    encuesta = (
        _query_surveys_for_admin(current_user)
        .filter_by(id=encuesta_id)
        .first_or_404()
    )
    payload = request.get_json(force=True, silent=True) or {}

    nuevo_titulo = payload.get("titulo") or payload.get("title")
    if nuevo_titulo:
        encuesta.titulo = nuevo_titulo

    encuesta.descripcion = payload.get("descripcion") or payload.get("description") or encuesta.descripcion

    if "slug" in payload or "codigo" in payload:
        raw_slug = payload.get("slug") if "slug" in payload else payload.get("codigo")
        nuevo_slug = _normalize_slug_value(raw_slug)
        if nuevo_slug:
            if nuevo_slug != encuesta.slug:
                if not _ensure_slug_unique(nuevo_slug, encuesta.id):
                    return jsonify({"error": "Ya existe una encuesta con ese slug"}), 409
            encuesta.slug = nuevo_slug
        elif isinstance(raw_slug, str) and raw_slug.strip() == "":
            return jsonify({"error": "El slug no puede quedar vacío"}), 400

    try:
        if "preguntas" in payload or "questions" in payload:
            _update_questions(encuesta, payload.get("preguntas") or payload.get("questions") or [])
    except ValueError as exc:
        current_app.logger.warning("Error actualizando encuesta: %s", exc)
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    desired_status = None
    if payload.get("publicar") or payload.get("publish"):
        desired_status = "published"
    elif payload.get("archivar") or payload.get("cerrar"):
        desired_status = "archived"
    elif payload.get("estado") or payload.get("status"):
        desired_status = _normalize_status(payload.get("estado") or payload.get("status"))

    _apply_status_transition(encuesta, desired_status)

    db.session.commit()
    return jsonify(_serialize_survey(encuesta))


@encuestas_public_bp.route("/<string:slug>", methods=["GET"])
def encuesta_publica(slug: str):
    encuesta = PublicSurvey.query.filter_by(slug=slug, estado="published").first()
    if not encuesta:
        return jsonify({"error": "Encuesta no encontrada"}), 404
    return jsonify(_serialize_survey(encuesta))


def _extract_answer_question_id(item: Dict[str, Any]) -> Optional[int]:
    for key in ("question_id", "pregunta_id", "id"):
        if item.get(key) is not None:
            try:
                return int(item[key])
            except (TypeError, ValueError):
                return None
    return None


def _parse_answers(payload: Dict[str, Any], encuesta: PublicSurvey) -> List[Dict[str, Any]]:
    raw_answers = payload.get("answers") or payload.get("respuestas")
    items: List[Dict[str, Any]] = []

    if raw_answers is None:
        return items

    if isinstance(raw_answers, dict):
        for key, value in raw_answers.items():
            try:
                question_id = int(key)
            except (TypeError, ValueError):
                continue
            items.append({"question_id": question_id, "value": value})
    elif isinstance(raw_answers, list):
        for element in raw_answers:
            if isinstance(element, dict):
                items.append(element)
    return items


def _store_answers(
    encuesta: PublicSurvey,
    answers_payload: List[Dict[str, Any]],
    anon_id: Optional[str],
    metadata: Optional[Dict[str, Any]],
) -> PublicSurveyResponse:
    response = PublicSurveyResponse(
        survey=encuesta,
        anon_id=anon_id,
        metadata_json=metadata,
    )
    db.session.add(response)
    db.session.flush()

    preguntas_por_id = {preg.id: preg for preg in encuesta.preguntas}

    for raw_answer in answers_payload:
        question_id = _extract_answer_question_id(raw_answer)
        if not question_id or question_id not in preguntas_por_id:
            raise ValueError("Identificador de pregunta inválido")
        pregunta = preguntas_por_id[question_id]

        selected_options: List[int] = []
        valor_libre: Optional[str] = None

        if pregunta.tipo == "text":
            valor_libre = raw_answer.get("valor") or raw_answer.get("value") or raw_answer.get("texto")
            if valor_libre is None and isinstance(raw_answer.get("value"), (int, float)):
                valor_libre = str(raw_answer.get("value"))
        else:
            opciones_ids = raw_answer.get("opciones") or raw_answer.get("options") or raw_answer.get("option_ids") or raw_answer.get("opciones_ids")
            if opciones_ids is None:
                opcion_id = raw_answer.get("opcion_id") or raw_answer.get("option_id")
                if opcion_id is not None:
                    opciones_ids = [opcion_id]
            if opciones_ids is None and raw_answer.get("valor"):
                opciones_ids = [raw_answer.get("valor")]

            if opciones_ids is None:
                raise ValueError("Falta seleccionar opciones para la pregunta")

            if not isinstance(opciones_ids, (list, tuple)):
                opciones_ids = [opciones_ids]

            selected_options = []
            opciones_por_id = {op.id: op for op in pregunta.opciones}
            for opcion_raw in opciones_ids:
                try:
                    opcion_id = int(opcion_raw)
                except (TypeError, ValueError):
                    continue
                if opcion_id not in opciones_por_id:
                    raise ValueError("La opción seleccionada no pertenece a la pregunta")
                selected_options.append(opcion_id)

        if pregunta.tipo == "text":
            answer = PublicSurveyAnswer(
                response=response,
                question=pregunta,
                valor=valor_libre,
            )
            db.session.add(answer)
        elif pregunta.tipo == "single_choice":
            if not selected_options:
                raise ValueError("Debe seleccionarse una opción")
            opcion_id = selected_options[0]
            answer = PublicSurveyAnswer(
                response=response,
                question=pregunta,
                option_id=opcion_id,
            )
            db.session.add(answer)
        else:  # multiple_choice
            for opcion_id in selected_options:
                answer = PublicSurveyAnswer(
                    response=response,
                    question=pregunta,
                    option_id=opcion_id,
                )
                db.session.add(answer)

    return response


@encuestas_public_bp.route("/<string:slug>/respuestas", methods=["POST"])
def enviar_respuesta(slug: str):
    encuesta = PublicSurvey.query.filter_by(slug=slug, estado="published").first()
    if not encuesta:
        return jsonify({"error": "Encuesta no encontrada o no publicada"}), 404

    payload = request.get_json(force=True, silent=True) or {}
    respuestas = _parse_answers(payload, encuesta)

    anon_id = payload.get("anon_id") or payload.get("anonId") or payload.get("respondent_id")
    if not anon_id:
        anon_id = get_or_create_anon_id()

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None

    try:
        response = _store_answers(encuesta, respuestas, anon_id=anon_id, metadata=metadata)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    db.session.commit()
    return jsonify({"success": True, "respuesta_id": response.id, "anon_id": anon_id}), 201

