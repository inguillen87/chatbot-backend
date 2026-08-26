"""Idempotently materialize the versioned Junin demo instruments.

The command is dry-run by default.  It accepts the database URL only through
an environment variable, verifies the connected Neon branch before reading or
writing, never authenticates with a password, and never publishes a survey.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import func, text
from sqlalchemy.engine import make_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("ENABLE_RUNTIME_SCHEMA_SYNC", "0")
os.environ.setdefault("ENABLE_RUNTIME_TENANT_INIT", "0")

from app import create_app  # noqa: E402
from config import Config  # noqa: E402
from database import db  # noqa: E402
from models import (  # noqa: E402
    EncEncuesta,
    EncRespuesta,
    TenantProfile,
    User,
)
from services.encuestas_service import create_encuesta  # noqa: E402
from services.institutional_demo_surveys import (  # noqa: E402
    CONTRACT_VERSION,
    TARGET_TENANT_SLUG,
    TARGET_USER_EMAIL,
    build_persisted_survey_payload,
    load_institutional_demo_survey_manifest,
)


SEED_CONTRACT_VERSION = "chatboc.qa_preview_institutional_survey_seed.v1"
DEFAULT_DATABASE_ENVIRONMENT_VARIABLE = "MIGRATIONS_DATABASE_URL"
DEFAULT_BRANCH_ENVIRONMENT_VARIABLE = "EXPECTED_NEON_BRANCH_ID"
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_NEON_BRANCH_PATTERN = re.compile(r"^br-[a-z0-9-]{3,63}$")
_ALLOWED_ENVIRONMENTS = {"qa", "preview"}
_PRODUCTION_LABELS = {"prod", "production"}
_TRUTHY = {"1", "true", "yes", "on"}


class InstitutionalSurveySeedError(RuntimeError):
    """Stable, redacted blocker for the controlled seed operation."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise InstitutionalSurveySeedError(reason_code)


def _validate_environment_variable_name(value: str) -> str:
    normalized = str(value or "").strip()
    _require(
        bool(_ENVIRONMENT_VARIABLE_PATTERN.fullmatch(normalized)),
        "environment_variable_name_invalid",
    )
    return normalized


def validate_seed_runtime(
    *,
    environment: str,
    database_url: str,
    expected_branch_id: str,
    environ: Mapping[str, str] | None = None,
    allow_sqlite_for_tests: bool = False,
) -> dict[str, Any]:
    """Fail closed before the application creates a database connection."""

    environment_name = str(environment or "").strip().lower()
    _require(environment_name in _ALLOWED_ENVIRONMENTS, "runtime_environment_forbidden")
    source = dict(environ or {})
    runtime_labels = {
        str(source.get(key) or "").strip().lower()
        for key in ("VERCEL_ENV", "APP_ENV", "ENVIRONMENT", "FLASK_ENV")
        if str(source.get(key) or "").strip()
    }
    _require(not runtime_labels.intersection(_PRODUCTION_LABELS), "production_runtime_forbidden")
    scoped_labels = runtime_labels.intersection(_ALLOWED_ENVIRONMENTS)
    _require(
        not scoped_labels or scoped_labels == {environment_name},
        "runtime_environment_mismatch",
    )
    render_markers = (
        str(source.get("RENDER") or "").strip().lower() in _TRUTHY
        or bool(str(source.get("RENDER_EXTERNAL_URL") or "").strip())
        or bool(str(source.get("RENDER_SERVICE_ID") or "").strip())
    )
    _require(not render_markers, "render_runtime_forbidden")

    branch_id = str(expected_branch_id or "").strip().lower()
    _require(bool(_NEON_BRANCH_PATTERN.fullmatch(branch_id)), "expected_neon_branch_id_invalid")

    try:
        parsed = make_url(str(database_url or "").strip())
    except Exception as exc:
        raise InstitutionalSurveySeedError("database_url_invalid") from exc
    backend = parsed.get_backend_name()
    if backend == "sqlite" and allow_sqlite_for_tests:
        return {
            "environment": environment_name,
            "database_provider": "sqlite_test",
            "expected_branch_id": branch_id,
        }
    _require(backend == "postgresql", "database_backend_not_postgresql")
    host = str(parsed.host or "").strip().lower().rstrip(".")
    _require(host.endswith(".neon.tech"), "database_provider_not_neon")
    _require(bool(parsed.username), "database_username_missing")
    _require(bool(parsed.database), "database_name_missing")
    ssl_mode = str(parsed.query.get("sslmode") or "").strip().lower()
    _require(ssl_mode in {"require", "verify-ca", "verify-full"}, "database_tls_not_required")
    return {
        "environment": environment_name,
        "database_provider": "neon_postgres",
        "expected_branch_id": branch_id,
    }


def verify_connected_branch(
    *,
    expected_branch_id: str,
    allow_sqlite_for_tests: bool = False,
) -> str:
    backend = str(db.session.get_bind().dialect.name or "").strip().lower()
    if backend == "sqlite" and allow_sqlite_for_tests:
        return str(expected_branch_id).strip().lower()
    _require(backend == "postgresql", "connected_database_not_postgresql")
    db.session.execute(text("SET LOCAL statement_timeout = '30s'"))
    db.session.execute(text("SET LOCAL lock_timeout = '2s'"))
    actual = str(
        db.session.execute(
            text("SELECT current_setting('neon.branch_id', true)")
        ).scalar_one()
        or ""
    ).strip().lower()
    _require(bool(actual), "connected_neon_branch_id_missing")
    _require(actual == str(expected_branch_id).strip().lower(), "connected_neon_branch_mismatch")
    return actual


def _survey_tags(survey: EncEncuesta) -> list[str]:
    return sorted(
        {
            str(segment.valor or "").strip()
            for segment in survey.segmentos
            if segment.clave == "tag" and str(segment.valor or "").strip()
        }
    )


def _survey_instrument(survey: EncEncuesta) -> list[dict[str, Any]]:
    return [
        {
            "orden": int(question.orden),
            "question_ref": question.logical_ref,
            "tipo": question.tipo,
            "texto": question.texto,
            "obligatoria": bool(question.obligatoria),
            "opciones": [
                {
                    "orden": int(option.orden),
                    "option_ref": option.logical_ref,
                    "texto": option.texto,
                    "valor": option.valor,
                }
                for option in sorted(question.opciones, key=lambda item: item.orden)
            ],
        }
        for question in sorted(survey.preguntas, key=lambda item: item.orden)
    ]


def _expected_instrument(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "orden": int(question["orden"]),
            "question_ref": question["question_ref"],
            "tipo": question["tipo"],
            "texto": question["texto"],
            "obligatoria": bool(question.get("obligatoria", False)),
            "opciones": [
                {
                    "orden": int(option["orden"]),
                    "option_ref": option["option_ref"],
                    "texto": option["texto"],
                    "valor": option["valor"],
                }
                for option in sorted(question["opciones"], key=lambda item: item["orden"])
            ],
        }
        for question in sorted(payload["preguntas"], key=lambda item: item["orden"])
    ]


def _assert_existing_survey_is_exact(
    survey: EncEncuesta,
    *,
    payload: dict[str, Any],
    survey_spec: dict[str, Any],
    tenant_id: int,
    actor_user_id: int,
) -> None:
    scalar_fields = {
        "tenant_id": (int(survey.tenant_id), int(tenant_id)),
        "created_by": (int(survey.created_by or 0), int(actor_user_id)),
        "document_ref": (survey.document_ref, payload["document_ref"]),
        "content_origin": (survey.content_origin, "seed_demo"),
        "content_origin_ref": (survey.content_origin_ref, survey_spec["content_origin_ref"]),
        "slug": (survey.slug, payload["slug"]),
        "titulo": (survey.titulo, payload["titulo"]),
        "descripcion": (survey.descripcion, payload["descripcion"]),
        "tipo": (survey.tipo, payload["tipo"]),
        "estado": (survey.estado, "borrador"),
        "requiere_identidad": (bool(survey.requiere_identidad), False),
        "anonimo_permitido": (bool(survey.anonimo_permitido), True),
        "privacy_mode": (survey.privacy_mode, payload["privacy_mode"]),
        "privacy_policy_version": (
            survey.privacy_policy_version,
            payload["privacy_policy_version"],
        ),
        "privacy_policy_url": (survey.privacy_policy_url, payload["privacy_policy_url"]),
        "privacy_consent_required": (
            bool(survey.privacy_consent_required),
            bool(payload["privacy_consent_required"]),
        ),
        "response_retention_days": (
            int(survey.response_retention_days or 0),
            int(payload["response_retention_days"]),
        ),
        "es_votacion_envivo": (
            bool(survey.es_votacion_envivo),
            bool(payload["es_votacion_envivo"]),
        ),
        "mostrar_resultados_envivo": (
            bool(survey.mostrar_resultados_envivo),
            bool(payload["mostrar_resultados_envivo"]),
        ),
        "permitir_comentarios": (bool(survey.permitir_comentarios), False),
        "puntos_recompensa": (int(survey.puntos_recompensa or 0), 0),
    }
    drifted = sorted(key for key, (actual, expected) in scalar_fields.items() if actual != expected)
    _require(not drifted, "existing_seed_survey_contract_drift")
    _require(_survey_tags(survey) == sorted(payload["tags"]), "existing_seed_survey_tags_drift")
    _require(
        _survey_instrument(survey) == _expected_instrument(payload),
        "existing_seed_survey_instrument_drift",
    )
    response_count = int(
        db.session.query(func.count(EncRespuesta.id))
        .filter(EncRespuesta.encuesta_id == survey.id)
        .scalar()
        or 0
    )
    _require(response_count == 0, "existing_seed_survey_has_responses")


def _resolve_target(
    *,
    target_user_email: str,
    expected_tenant_slug: str,
) -> tuple[User, TenantProfile]:
    email = str(target_user_email or "").strip().lower()
    users = User.query.filter(func.lower(User.email) == email).all()
    _require(len(users) == 1, "target_user_not_unique")
    user = users[0]
    _require(str(user.rol or "").strip().lower() in {"admin", "super_admin"}, "target_user_not_admin")
    _require(user.tenant_id is not None, "target_user_canonical_tenant_missing")
    tenant = TenantProfile.query.filter_by(id=int(user.tenant_id)).one_or_none()
    _require(tenant is not None, "target_tenant_missing")
    _require(bool(tenant.is_active), "target_tenant_inactive")
    expected_slug = str(expected_tenant_slug or "").strip().lower()
    _require(str(tenant.slug or "").strip().lower() == expected_slug, "target_tenant_slug_mismatch")
    user_slug = str(user.tenant_slug or "").strip().lower()
    _require(not user_slug or user_slug == expected_slug, "target_user_tenant_slug_mismatch")
    return user, tenant


def seed_institutional_surveys(
    *,
    environment: str,
    expected_branch_id: str,
    expected_tenant_slug: str = TARGET_TENANT_SLUG,
    target_user_email: str = TARGET_USER_EMAIL,
    apply: bool = False,
    allow_sqlite_for_tests: bool = False,
) -> dict[str, Any]:
    """Plan or atomically create only the three exact versioned drafts."""

    manifest = load_institutional_demo_survey_manifest()
    _require(
        str(environment or "").strip().lower()
        in set(manifest["allowed_runtime_environments"]),
        "runtime_environment_forbidden",
    )
    actual_branch_id = verify_connected_branch(
        expected_branch_id=expected_branch_id,
        allow_sqlite_for_tests=allow_sqlite_for_tests,
    )
    user, tenant = _resolve_target(
        target_user_email=target_user_email,
        expected_tenant_slug=expected_tenant_slug,
    )

    from flask import g

    g.tenant_profile = tenant
    results: list[dict[str, Any]] = []
    try:
        for survey_spec in manifest["surveys"]:
            payload = build_persisted_survey_payload(survey_spec, manifest=manifest)
            existing = EncEncuesta.query.filter_by(slug=payload["slug"]).one_or_none()
            if existing is not None:
                _assert_existing_survey_is_exact(
                    existing,
                    payload=payload,
                    survey_spec=survey_spec,
                    tenant_id=int(tenant.id),
                    actor_user_id=int(user.id),
                )
                results.append(
                    {
                        "action": "reused",
                        "id": int(existing.id),
                        "slug": existing.slug,
                        "titulo": existing.titulo,
                        "tipo": existing.tipo,
                        "estado": existing.estado,
                        "response_count": 0,
                    }
                )
                continue

            if not apply:
                results.append(
                    {
                        "action": "would_create",
                        "id": None,
                        "slug": payload["slug"],
                        "titulo": payload["titulo"],
                        "tipo": payload["tipo"],
                        "estado": "borrador",
                        "response_count": 0,
                    }
                )
                continue

            created = create_encuesta(
                payload,
                user,
                commit=False,
                content_origin="seed_demo",
                content_origin_ref=survey_spec["content_origin_ref"],
            )
            # Re-read the flushed graph before validating.  The legacy model
            # builders populate both relationship sides in memory, which can
            # transiently expose duplicate collection entries until reload.
            db.session.flush()
            db.session.expire_all()
            created = EncEncuesta.query.filter_by(slug=payload["slug"]).one()
            _assert_existing_survey_is_exact(
                created,
                payload=payload,
                survey_spec=survey_spec,
                tenant_id=int(tenant.id),
                actor_user_id=int(user.id),
            )
            results.append(
                {
                    "action": "created",
                    "id": int(created.id),
                    "slug": created.slug,
                    "titulo": created.titulo,
                    "tipo": created.tipo,
                    "estado": created.estado,
                    "response_count": 0,
                }
            )

        if apply:
            db.session.commit()
        else:
            db.session.rollback()
    except Exception:
        db.session.rollback()
        raise

    return {
        "contract_version": SEED_CONTRACT_VERSION,
        "manifest_contract_version": CONTRACT_VERSION,
        "status": "applied" if apply else "dry_run",
        "writes_performed": bool(apply and any(item["action"] == "created" for item in results)),
        "environment": str(environment).strip().lower(),
        "verified_neon_branch_id": actual_branch_id,
        "target": {
            "user_id": int(user.id),
            "tenant_id": int(tenant.id),
            "tenant_slug": tenant.slug,
        },
        "channel_configuration_mutated": False,
        "publication": {
            "performed": False,
            "state": "draft_only",
            "governance_release_created": False,
        },
        "persisted_response_policy": {
            "initial_count": 0,
            "synthetic_seed_enabled": False,
            "municipal_truth": False,
        },
        "demo_mirror": manifest["demo_mirror_policy"],
        "surveys": results,
    }


def _failure_payload(reason_code: str, *, error_type: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": SEED_CONTRACT_VERSION,
        "status": "blocked",
        "writes_performed": False,
        "reason_code": reason_code,
    }
    if error_type:
        payload["error_type"] = error_type
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=sorted(_ALLOWED_ENVIRONMENTS))
    parser.add_argument("--expected-tenant-slug", default=TARGET_TENANT_SLUG)
    parser.add_argument(
        "--database-environment-variable",
        default=DEFAULT_DATABASE_ENVIRONMENT_VARIABLE,
        help="Environment variable containing the Neon URL; the value is never printed.",
    )
    parser.add_argument(
        "--branch-id-environment-variable",
        default=DEFAULT_BRANCH_ENVIRONMENT_VARIABLE,
        help="Environment variable containing the exact expected Neon branch ID.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create missing drafts atomically. Without this flag the command is read-only.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database_environment_variable = _validate_environment_variable_name(
            args.database_environment_variable
        )
        branch_environment_variable = _validate_environment_variable_name(
            args.branch_id_environment_variable
        )
        database_url = os.environ.get(database_environment_variable, "")
        _require(bool(database_url), "database_environment_variable_missing")
        expected_branch_id = os.environ.get(branch_environment_variable, "")
        _require(bool(expected_branch_id), "expected_neon_branch_id_missing")
        validate_seed_runtime(
            environment=args.environment,
            database_url=database_url,
            expected_branch_id=expected_branch_id,
            environ=os.environ,
        )
        seed_config = type(
            "InstitutionalSurveySeedConfig",
            (Config,),
            {
                "SQLALCHEMY_DATABASE_URI": database_url,
                "ENABLE_RUNTIME_SCHEMA_SYNC": False,
                "ENABLE_RUNTIME_TENANT_INIT": False,
                "SKIP_INIT_TENANTS": True,
            },
        )
        app = create_app(seed_config)
        with app.app_context():
            payload = seed_institutional_surveys(
                environment=args.environment,
                expected_branch_id=expected_branch_id,
                expected_tenant_slug=args.expected_tenant_slug,
                apply=args.apply,
            )
        exit_code = 0
    except InstitutionalSurveySeedError as exc:
        payload = _failure_payload(exc.reason_code)
        exit_code = 2
    except Exception as exc:  # Never serialize credentials or provider messages.
        payload = _failure_payload("unexpected_seed_failure", error_type=type(exc).__name__)
        exit_code = 3
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
