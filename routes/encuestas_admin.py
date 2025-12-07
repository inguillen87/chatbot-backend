"""Administrative endpoints for managing surveys."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, g

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    update_encuesta,
    publicar_encuesta,
    cerrar_encuesta,
    delete_encuesta,
    list_encuestas,
    get_encuesta,
    list_respuestas,
    serialize_encuesta,
    serialize_respuesta,
    build_admin_list_payload,
    list_template_catalog,
    build_template_draft_from_slug,
    seed_encuesta_respuestas_demo,
    list_all_comentarios_admin,
    administrar_comentario,
)
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


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
    @token_requerido
    @require_role("admin", "super_admin")
    def crear_encuesta_endpoint(current_user):
        try:
            encuesta = create_encuesta(request.get_json(force=True), current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_encuesta(encuesta)), 201

    @bp.route("/<int:encuesta_id>", methods=["PUT"])
    @token_requerido
    @require_role("admin", "super_admin")
    def editar_encuesta_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = update_encuesta(encuesta_id, request.get_json(force=True), current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_encuesta(encuesta)), 200

    @bp.route("/<int:encuesta_id>", methods=["DELETE"])
    @token_requerido
    @require_role("admin", "super_admin")
    def eliminar_encuesta_endpoint(current_user, encuesta_id: int):
        try:
            delete_encuesta(encuesta_id, current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify({"ok": True, "encuesta_id": encuesta_id}), 200

    @bp.route("/<int:encuesta_id>/publicar", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def publicar_endpoint(current_user, encuesta_id: int):
        try:
            encuesta, link = publicar_encuesta(encuesta_id, current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        base_url = (
            current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
            or request.host_url.rstrip("/")
        )
        url_publica = f"{base_url}/e/{link.slug_publico}"
        return (
            jsonify({"ok": True, "slug_publico": link.slug_publico, "url_publica": url_publica}),
            200,
        )

    @bp.route("/<int:encuesta_id>/cerrar", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def cerrar_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = cerrar_encuesta(encuesta_id, current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify({"ok": True, "estado": encuesta.estado}), 200

    @bp.route("", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def listar_encuestas_endpoint(current_user):
        estado = request.args.get("estado")
        try:
            tenant_id = None
            tenant_profile = getattr(g, "tenant_profile", None)

            if tenant_profile:
                if current_user.rol == "super_admin":
                    tenant_id = tenant_profile.id
                else:
                    user_tenant_id = (
                        getattr(current_user, "municipio_id", None)
                        or getattr(current_user, "empresa_id", None)
                        or current_user.id
                    )
                    if user_tenant_id == tenant_profile.id:
                        tenant_id = user_tenant_id
                    else:
                        return jsonify({"error": "No autorizado para este tenant"}), 403

            if not tenant_id:
                tenant_id = (
                    getattr(current_user, "municipio_id", None)
                    or getattr(current_user, "empresa_id", None)
                    or current_user.id
                )
            encuestas = list_encuestas(tenant_id, estado)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        if (request.args.get("legacy") or "").lower() in {"1", "true", "yes"}:
            return jsonify([serialize_encuesta(e) for e in encuestas]), 200
        payload = build_admin_list_payload(encuestas)
        return jsonify(payload), 200

    @bp.route("/templates", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def listar_plantillas_endpoint(current_user):
        municipality = request.args.get("municipality") or request.args.get("municipio")
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
            catalog = list_template_catalog(municipality=municipality, template_slugs=template_slugs)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        if include_draft:
            if not municipality:
                return (
                    jsonify({"error": "Debés indicar una localidad para generar borradores"}),
                    400,
                )
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

    @bp.route("/<int:encuesta_id>/seed-demo", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def seed_demo_endpoint(current_user, encuesta_id: int):
        data = request.get_json(silent=True) or {}
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

        try:
            result = seed_encuesta_respuestas_demo(
                encuesta_id,
                current_user,
                cantidad=cantidad_int,
                geo_profile_key=geo_profile_key,
                municipality_label=municipality_label,
                seed=seed_value,
            )
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(result), 200

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
