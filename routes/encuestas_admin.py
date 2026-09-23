"""Administrative endpoints for managing surveys."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, g

from config.feature_flags import FEATURE_ENCUESTAS
from database import db
from extensions import limiter
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    update_encuesta,
    duplicate_encuesta,
    publicar_encuesta,
    cerrar_encuesta,
    delete_encuesta,
    list_encuestas_page,
    get_encuesta,
    list_respuestas,
    serialize_encuesta,
    serialize_respuesta,
    build_admin_list_payload,
    determine_tenant_id_for_user,
    list_template_catalog,
    build_template_draft_from_slug,
    seed_encuesta_respuestas_demo,
    list_all_comentarios_admin,
    administrar_comentario,
    survey_admin_write_rate_limit_key,
    survey_instrument_max_payload_bytes,
)
from services.survey_deletion_policy import require_admin_deletable_survey
from services.survey_methodology import get_methodology, save_methodology
from services.survey_methodology_contract import CONTRACT as METHODOLOGY_CONTRACT, MAX_BODY_BYTES
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
import json

from services.survey_tenant_scope import (
    SurveyTenantScopeError,
    resolve_survey_storage_tenant_profile,
)
from utils.auth_helpers import token_requerido
from routes.admin_tenant import _is_authorized_for_tenant
from utils.permissions import require_role


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


def _has_explicit_tenant_context() -> bool:
    if request.args.get("tenant") or request.args.get("tenant_slug") or request.args.get("tenant_id"):
        return True
    for header in ("X-Tenant", "X-Tenant-Slug", "X-Chatboc-Tenant", "X-Tenant-Id"):
        if request.headers.get(header):
            return True
    return False


def _create_admin_blueprint(name: str, url_prefix: str) -> Blueprint:
    """Return a blueprint that exposes the admin survey endpoints."""

    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    @bp.before_request
    def _check_feature():  # pragma: no cover - simple guard
        guard = _feature_guard()
        if guard:
            return guard
        return None

    @bp.route("", methods=["OPTIONS"], provide_automatic_options=False)
    @bp.route("/<path:anything>", methods=["OPTIONS"], provide_automatic_options=False)
    def preflight(anything=None):
        """Return a CORS friendly preflight response without auth checks."""

        return current_app.make_default_options_response()

    @bp.route("", methods=["POST"])
    @limiter.shared_limit(
        "120 per minute",
        scope="survey-admin-create",
        key_func=survey_admin_write_rate_limit_key,
    )
    @token_requerido
    @require_role("admin", "super_admin")
    def crear_encuesta_endpoint(current_user):
        max_payload_bytes = survey_instrument_max_payload_bytes()
        if (
            request.content_length is not None
            and request.content_length > max_payload_bytes
        ):
            error = EncuestaError(
                "El instrumento supera el tamaño máximo permitido.",
                status_code=413,
                payload={
                    "contract_version": "surveys.instrument_limits.v1",
                    "reason_code": "survey_instrument_too_large",
                    "retryable": False,
                    "action_hint": "reduce_instrument_size",
                    "field": "payload_bytes",
                    "actual": int(request.content_length),
                    "maximum": max_payload_bytes,
                },
            )
            return jsonify(error.to_dict()), error.status_code
        try:
            encuesta = create_encuesta(request.get_json(force=True), current_user)
        except EncuestaError as err:
            db.session.rollback()
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_encuesta(encuesta)), 201

    @bp.route("/<int:encuesta_id>", methods=["PUT"])
    @token_requerido
    @require_role("admin", "super_admin")
    def editar_encuesta_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = update_encuesta(encuesta_id, request.get_json(force=True), current_user)
        except EncuestaError as err:
            db.session.rollback()
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_encuesta(encuesta)), 200

    @bp.route("/<int:encuesta_id>/duplicar", methods=["POST"])
    @bp.route("/<int:encuesta_id>/duplicate", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def duplicar_encuesta_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = duplicate_encuesta(encuesta_id, request.get_json(silent=True) or {}, current_user)
        except EncuestaError as err:
            db.session.rollback()
            return jsonify(err.to_dict()), err.status_code
        return jsonify({"ok": True, "source_id": encuesta_id, "encuesta": serialize_encuesta(encuesta)}), 201

    @bp.route("/<int:encuesta_id>", methods=["DELETE"])
    @token_requerido
    @require_role("admin", "super_admin")
    def eliminar_encuesta_endpoint(current_user, encuesta_id: int):
        try:
            require_admin_deletable_survey(encuesta_id, current_user)
            delete_encuesta(encuesta_id, current_user)
        except EncuestaError as err:
            db.session.rollback()
            return jsonify(err.to_dict()), err.status_code
        except IntegrityError:
            db.session.rollback()
            return jsonify({"error":"El borrador tiene registros vinculados que deben conservarse.",
                            "reason_code":"survey_delete_linked_history"}),409
        return jsonify({"ok": True, "encuesta_id": encuesta_id}), 200

    def _methodology_response(payload, status=200):
        response=jsonify(payload)
        response.status_code=status
        response.headers['Cache-Control']='private, no-store'
        return response

    @bp.route('/<int:encuesta_id>/methodology', methods=['GET'])
    @token_requerido
    @require_role('admin','super_admin')
    def methodology_get(current_user,encuesta_id):
        try:
            raw=request.args.get('revision')
            if raw is not None and (not raw.isascii() or not raw.isdigit() or len(raw)>10):
                raise EncuestaError('La versi\u00f3n solicitada no es v\u00e1lida.',status_code=400,
                    payload={'reason_code':'methodology_revision_invalid'})
            payload=get_methodology(encuesta_id,current_user,int(raw) if raw is not None else None)
            return _methodology_response(payload)
        except EncuestaError as err:
            db.session.rollback()
            return _methodology_response(err.to_dict(),err.status_code)
        except SQLAlchemyError:
            db.session.rollback()
            return _methodology_response({'contract_version':METHODOLOGY_CONTRACT,
                'reason_code':'methodology_storage_unavailable','error':'La ficha requiere almacenamiento verificado en este entorno.'},503)

    @bp.route('/<int:encuesta_id>/methodology', methods=['PUT'])
    @limiter.shared_limit('60 per minute',scope='survey-methodology-write',key_func=survey_admin_write_rate_limit_key)
    @token_requerido
    @require_role('admin','super_admin')
    def methodology_put(current_user,encuesta_id):
        try:
            # Authorize before reading a body or accessing methodology storage.
            get_encuesta(encuesta_id,user=current_user)
            if request.content_length is not None and request.content_length>MAX_BODY_BYTES:
                raise EncuestaError('La ficha supera el tama\u00f1o permitido.',status_code=413)
            if not request.is_json:
                raise EncuestaError('La ficha debe enviarse como JSON.',status_code=415)
            raw=request.stream.read(MAX_BODY_BYTES+1)
            if len(raw)>MAX_BODY_BYTES:
                raise EncuestaError('La ficha supera el tama\u00f1o permitido.',status_code=413)
            try: data=json.loads(raw.decode('utf-8'))
            except (UnicodeError,ValueError):
                raise EncuestaError('El documento JSON no es v\u00e1lido.',status_code=400) from None
            return _methodology_response(save_methodology(encuesta_id,current_user,data))
        except EncuestaError as err:
            db.session.rollback()
            return _methodology_response(err.to_dict(),err.status_code)
        except SQLAlchemyError:
            db.session.rollback()
            return _methodology_response({'contract_version':METHODOLOGY_CONTRACT,
                'reason_code':'methodology_write_unconfirmed',
                'error':'No pudimos confirmar el guardado. Actualiz\u00e1 la ficha antes de reintentar.'},503)

    @bp.route("/<int:encuesta_id>/publicar", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def publicar_endpoint(current_user, encuesta_id: int):
        try:
            encuesta, link = publicar_encuesta(encuesta_id, current_user)
        except EncuestaError as err:
            db.session.rollback()
            return jsonify(err.to_dict()), err.status_code

        base_url = (
            current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
            or request.host_url.rstrip("/")
        )
        url_publica = f"{base_url}/e/{link.slug_publico}"
        encuesta_payload = serialize_encuesta(encuesta)
        return (
            jsonify(
                {
                    "ok": True,
                    "slug_publico": link.slug_publico,
                    "canonical_slug": link.slug_publico,
                    "url_publica": url_publica,
                    "share_url": url_publica,
                    "public_api_endpoint": f"/api/public/encuestas/v1/{link.slug_publico}",
                    "encuesta": encuesta_payload,
                }
            ),
            200,
        )

    @bp.route("/<int:encuesta_id>/cerrar", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def cerrar_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = cerrar_encuesta(encuesta_id, current_user)
        except EncuestaError as err:
            db.session.rollback()
            return jsonify(err.to_dict()), err.status_code
        return jsonify({"ok": True, "estado": encuesta.estado}), 200

    @bp.route("", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def listar_encuestas_endpoint(current_user):
        estado = request.args.get("estado")

        try:
            tenant_profile = getattr(g, "tenant_profile", None)
            if _has_explicit_tenant_context() and tenant_profile and not _is_authorized_for_tenant(current_user, tenant_profile):
                return jsonify({"error": "No autorizado para este tenant"}), 403

            tenant_id = determine_tenant_id_for_user(current_user)

            page_data = list_encuestas_page(
                tenant_id,
                estado,
                limit=request.args.get("limit"),
                cursor=request.args.get("cursor"),
                page=request.args.get("page"),
            )
            encuestas = page_data["items"]
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        tenant_profile = getattr(g, "tenant_profile", None)
        tenant_slug = (
            getattr(tenant_profile, "slug", None)
            if tenant_profile is not None
            else getattr(current_user, "tenant_slug", None)
        )
        payload = build_admin_list_payload(
            encuestas,
            tenant_id=tenant_id,
            tenant_slug=(str(tenant_slug).strip() if tenant_slug else None),
            pagination=page_data["pagination"],
        )
        if (request.args.get("legacy") or "").lower() in {"1", "true", "yes"}:
            # Preserve the historical top-level array while returning the same
            # bounded, lightweight rows as the versioned envelope.
            response = jsonify(payload["encuestas"])
            pagination = page_data["pagination"]
            response.headers["X-Total-Count"] = str(pagination["total_items"])
            response.headers["X-Page-Limit"] = str(pagination["limit"])
            if pagination.get("next_cursor"):
                response.headers["X-Next-Cursor"] = str(
                    pagination["next_cursor"]
                )
            return response, 200

        return jsonify(payload), 200

    @bp.route("/templates", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def listar_plantillas_endpoint(current_user):
        requested_municipality = (
            request.args.get("municipality") or request.args.get("municipio")
        )
        slugs_param = request.args.get("slugs")
        template_slugs = None
        if slugs_param:
            template_slugs = [slug.strip() for slug in slugs_param.split(",") if slug.strip()]

        include_draft = (request.args.get("include_draft") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        try:
            tenant_id = determine_tenant_id_for_user(current_user)
            tenant_profile = resolve_survey_storage_tenant_profile(tenant_id)
            tenant_config = (
                tenant_profile.configuracion
                if isinstance(tenant_profile.configuracion, dict)
                else {}
            )
            municipality = str(
                tenant_config.get("survey_municipality_label")
                or tenant_profile.nombre
                or ""
            ).strip()
            if not municipality:
                raise EncuestaError(
                    "El tenant no tiene una localidad configurada.",
                    status_code=409,
                    payload={
                        "reason_code": "survey_template_municipality_unconfigured",
                        "action_hint": "configure_server_owned_municipality_label",
                    },
                )
            if (
                requested_municipality
                and str(requested_municipality).strip().casefold()
                != municipality.casefold()
            ):
                raise EncuestaError(
                    "La localidad solicitada contradice al tenant autenticado.",
                    status_code=409,
                    payload={
                        "contract_version": "surveys.jurisdiction_guard.v1",
                        "reason_code": "survey_template_municipality_mismatch",
                        "action_hint": "remove_caller_controlled_municipality",
                    },
                )
            catalog = list_template_catalog(municipality=municipality, template_slugs=template_slugs)
        except SurveyTenantScopeError:
            return (
                jsonify(
                    {
                        "error": "No se pudo resolver el tenant de encuestas.",
                        "reason_code": "survey_tenant_scope_invalid",
                    }
                ),
                403,
            )
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        if include_draft:
            start_at = request.args.get("start_at") or request.args.get("inicio_at")
            end_at = request.args.get("end_at") or request.args.get("fin_at")
            for template in catalog:
                slug = template.get("slug")
                if not slug:
                    continue
                try:
                    draft = build_template_draft_from_slug(
                        slug,
                        municipality,
                        start=start_at,
                        end=end_at,
                    )
                except EncuestaError as err:
                    return jsonify(err.to_dict()), err.status_code
                template["draft"] = draft

        return jsonify({"templates": catalog}), 200

    @bp.route("/<int:encuesta_id>", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def detalle_encuesta_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = get_encuesta(encuesta_id, user=current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_encuesta(encuesta)), 200

    def _run_seed_demo(current_user, encuesta_id: int, data: dict):
        def _parse_bool(value):
            if isinstance(value, bool):
                return value
            if value is None:
                return False
            return str(value).strip().lower() in {"1", "true", "yes", "si", "on"}

        cantidad = data.get("cantidad") or 100
        try:
            cantidad_int = int(cantidad)
        except (TypeError, ValueError):
            return jsonify({"error": "Cantidad inválida"}), 400

        geo_profile_key = data.get("geo_profile_key") or data.get("geo_key")
        municipality_label = data.get("municipality_label") or data.get("municipality")
        seed_value = data.get("seed")
        if seed_value is not None:
            try:
                seed_value = int(seed_value)
            except (TypeError, ValueError):
                return jsonify({"error": "Seed inválido"}), 400

        reset_data = _parse_bool(data.get("reset") or data.get("borrar"))
        scenario = (data.get("scenario") or data.get("mode") or "balanced").strip().lower()

        try:
            result = seed_encuesta_respuestas_demo(
                encuesta_id,
                current_user,
                cantidad=cantidad_int,
                geo_profile_key=geo_profile_key,
                municipality_label=municipality_label,
                seed=seed_value,
                reset_data=reset_data,
                scenario=scenario,
            )
        except EncuestaError as err:
            db.session.rollback()
            return jsonify(err.to_dict()), err.status_code
        return jsonify(result), 200

    @bp.route("/<int:encuesta_id>/seed-demo", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def seed_demo_endpoint(current_user, encuesta_id: int):
        data = request.get_json(silent=True) or {}
        return _run_seed_demo(current_user, encuesta_id, data)

    @bp.route("/<int:encuesta_id>/seed-demo/bulk", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def seed_demo_bulk_endpoint(current_user, encuesta_id: int):
        """Dedicated admin bulk seeding endpoint (avoids public dedupe/conflict flows)."""

        data = request.get_json(silent=True) or {}
        if "cantidad" not in data and "count" in data:
            data["cantidad"] = data.get("count")
        data.setdefault("reset", False)
        return _run_seed_demo(current_user, encuesta_id, data)

    @bp.route("/<int:encuesta_id>/respuestas", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def listar_respuestas_endpoint(current_user, encuesta_id: int):
        limit = request.args.get("limit", default=None, type=int)
        offset = request.args.get("offset", default=None, type=int)
        try:
            encuesta, respuestas, total, limit_value, offset_value = list_respuestas(
                encuesta_id,
                current_user,
                limit=limit,
                offset=offset,
            )
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        payload = {
            "encuesta_id": encuesta.id,
            "total": total,
            "limit": limit_value,
            "offset": offset_value,
            "respuestas": [serialize_respuesta(resp) for resp in respuestas],
        }
        return jsonify(payload), 200

    @bp.route("/<int:encuesta_id>/comentarios", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def ver_comentarios(current_user, encuesta_id: int):
        limit = request.args.get("limit", default=100, type=int)
        offset = request.args.get("offset", default=0, type=int)

        try:
            comentarios = list_all_comentarios_admin(
                encuesta_id, current_user, limit=limit, offset=offset
            )
            return jsonify(comentarios), 200
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

    @bp.route("/comentarios/<int:comentario_id>", methods=["PATCH"])
    @token_requerido
    @require_role("admin", "super_admin")
    def moderar_comentario_endpoint(current_user, comentario_id: int):
        data = request.get_json(silent=True) or {}
        accion = data.get("accion") # aprobar, ocultar, eliminar

        try:
            comentario = administrar_comentario(comentario_id, accion, current_user)
            return jsonify({
                "id": comentario.id,
                "estado": comentario.estado,
                "report_count": comentario.report_count
            }), 200
        except EncuestaError as err:
            db.session.rollback()
            return jsonify(err.to_dict()), err.status_code

    return bp


encuestas_admin_bp = _create_admin_blueprint("encuestas_admin_bp", "/api/encuestas")
encuestas_admin_legacy_bp = _create_admin_blueprint(
    "encuestas_admin_legacy_bp", "/admin/encuestas"
)
# Alias para clientes que consultan el API admin bajo /api/admin/encuestas
encuestas_admin_api_bp = _create_admin_blueprint(
    "encuestas_admin_api_bp", "/api/admin/encuestas"
)
# Alias para la integración municipal que consume /api/municipal/encuestas
encuestas_municipal_api_bp = _create_admin_blueprint(
    "encuestas_municipal_api_bp", "/api/municipal/encuestas"
)

# Alias legacy en inglés para clientes frontend que consultan /surveys
encuestas_admin_surveys_api_bp = _create_admin_blueprint(
    "encuestas_admin_surveys_api_bp", "/api/admin/surveys"
)
encuestas_admin_surveys_legacy_bp = _create_admin_blueprint(
    "encuestas_admin_surveys_legacy_bp", "/admin/surveys"
)
encuestas_municipal_surveys_api_bp = _create_admin_blueprint(
    "encuestas_municipal_surveys_api_bp", "/api/municipal/surveys"
)
