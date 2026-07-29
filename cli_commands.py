# cli_commands.py
import importlib
import json
import logging
import os
from collections.abc import Mapping

import click
from flask import current_app
from flask.cli import with_appcontext

from extensions import db

# Logger para este módulo
cli_logger = logging.getLogger(__name__)


SURVEY_EFFECT_CLI_CONTRACT_VERSION = "cli.survey_response_effect_dispatch.v1"
SURVEY_EFFECT_CLI_COMMAND = "dispatch-survey-response-effects"
SURVEY_EFFECT_CLI_DEFAULT_BATCH_SIZE = 50
SURVEY_EFFECT_CLI_MAX_BATCH_SIZE = 100
SURVEY_EFFECT_CLI_DEFAULT_MAX_BATCHES = 10
SURVEY_EFFECT_CLI_MAX_BATCHES = 100
SURVEY_EFFECT_CLI_DEFAULT_MAX_EFFECTS = 500
SURVEY_EFFECT_CLI_MAX_EFFECTS = 10_000
SURVEY_EFFECT_CLI_EXIT_OK = 0
SURVEY_EFFECT_CLI_EXIT_FAILURE = 1
SURVEY_EFFECT_CLI_EXIT_INVALID_ARGUMENTS = 2
SURVEY_EFFECT_CLI_EXIT_DEAD_EFFECTS = 3

_SURVEY_EFFECT_COUNTER_FIELDS = (
    "claimed",
    "processed",
    "succeeded",
    "skipped",
    "retry_wait",
    "dead",
    "fenced",
)


def _empty_survey_effect_totals() -> dict[str, int]:
    return {field: 0 for field in _SURVEY_EFFECT_COUNTER_FIELDS}


def _survey_effect_cli_payload(
    *,
    tenant_id: int | None,
    batch_size: int,
    max_batches: int,
    max_effects: int,
    fail_on_dead: bool,
) -> dict:
    return {
        "contract_version": SURVEY_EFFECT_CLI_CONTRACT_VERSION,
        "command": SURVEY_EFFECT_CLI_COMMAND,
        "scope": {
            "mode": "tenant" if tenant_id is not None else "global",
            "tenant_id": tenant_id,
        },
        "limits": {
            "batch_size": batch_size,
            "max_batches": max_batches,
            "max_effects": max_effects,
        },
        "exit_policy": {
            "fail_on_dead": fail_on_dead,
            "dead_effects_exit_code": SURVEY_EFFECT_CLI_EXIT_DEAD_EFFECTS,
        },
        "status": "running",
        "termination_reason": None,
        "batches": 0,
        "totals": _empty_survey_effect_totals(),
        "outbox_after": None,
    }


def _echo_survey_effect_json(payload: Mapping) -> None:
    """Emit one machine-readable line and never serialize arbitrary objects."""
    click.echo(
        json.dumps(
            dict(payload),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _survey_effect_tenant_exists(tenant_id: int) -> bool:
    from models import TenantProfile

    return (
        db.session.query(TenantProfile.id)
        .filter(TenantProfile.id == tenant_id)
        .first()
        is not None
    )


def _count_dead_survey_response_effects() -> int:
    from models import SurveyResponseEffect

    return int(
        db.session.query(SurveyResponseEffect.id)
        .filter(SurveyResponseEffect.status == "dead")
        .count()
    )


def _accumulate_survey_effect_batch(
    totals: dict[str, int], result: Mapping, *, requested_limit: int
) -> int:
    """Validate dispatcher counters before using them as loop progress."""
    if not isinstance(result, Mapping):
        raise RuntimeError("survey_effect_dispatch_result_invalid")

    parsed: dict[str, int] = {}
    for field in _SURVEY_EFFECT_COUNTER_FIELDS:
        raw_value = result.get(field, 0)
        if isinstance(raw_value, bool):
            raise RuntimeError("survey_effect_dispatch_counter_invalid")
        try:
            value = int(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("survey_effect_dispatch_counter_invalid") from exc
        if value < 0:
            raise RuntimeError("survey_effect_dispatch_counter_invalid")
        parsed[field] = value

    if parsed["claimed"] > requested_limit:
        raise RuntimeError("survey_effect_dispatch_claim_limit_exceeded")
    if parsed["processed"] > parsed["claimed"]:
        raise RuntimeError("survey_effect_dispatch_counter_inconsistent")
    if (
        parsed["succeeded"]
        + parsed["skipped"]
        + parsed["retry_wait"]
        + parsed["dead"]
        != parsed["processed"]
    ):
        raise RuntimeError("survey_effect_dispatch_counter_inconsistent")

    for field, value in parsed.items():
        totals[field] += value
    return parsed["claimed"]


def _rollback_survey_effect_cli_session() -> None:
    try:
        db.session.rollback()
    except Exception:
        cli_logger.warning(
            "Survey response effect CLI session rollback failed",
            exc_info=False,
        )


def register_commands(app):
    @app.cli.command(SURVEY_EFFECT_CLI_COMMAND)
    @click.option(
        "--tenant-id",
        type=click.IntRange(min=1),
        default=None,
        help="Process only this existing tenant.",
    )
    @click.option(
        "--all-tenants",
        is_flag=True,
        help="Process the global outbox. Required when --tenant-id is omitted.",
    )
    @click.option(
        "--batch-size",
        type=click.IntRange(min=1, max=SURVEY_EFFECT_CLI_MAX_BATCH_SIZE),
        default=SURVEY_EFFECT_CLI_DEFAULT_BATCH_SIZE,
        show_default=True,
        help="Maximum effects claimed by one dispatcher call.",
    )
    @click.option(
        "--max-batches",
        type=click.IntRange(min=1, max=SURVEY_EFFECT_CLI_MAX_BATCHES),
        default=SURVEY_EFFECT_CLI_DEFAULT_MAX_BATCHES,
        show_default=True,
        help="Hard cap on dispatcher calls for this command invocation.",
    )
    @click.option(
        "--max-effects",
        type=click.IntRange(min=1, max=SURVEY_EFFECT_CLI_MAX_EFFECTS),
        default=SURVEY_EFFECT_CLI_DEFAULT_MAX_EFFECTS,
        show_default=True,
        help="Hard cap on effects claimed across every batch.",
    )
    @click.option(
        "--fail-on-dead/--allow-dead",
        default=True,
        show_default=True,
        help="Exit 3 when dead effects exist after processing, or allow exit 0.",
    )
    @with_appcontext
    def dispatch_survey_response_effects_command(
        tenant_id: int | None,
        all_tenants: bool,
        batch_size: int,
        max_batches: int,
        max_effects: int,
        fail_on_dead: bool,
    ):
        """Drain bounded survey-effect batches and emit one JSON summary.

        Exactly one scope is mandatory: --tenant-id or --all-tenants. Exit
        codes are stable for automation: 0 completed, 1 operational failure,
        2 invalid arguments/scope, and 3 dead effects with --fail-on-dead.
        """

        payload = _survey_effect_cli_payload(
            tenant_id=tenant_id,
            batch_size=batch_size,
            max_batches=max_batches,
            max_effects=max_effects,
            fail_on_dead=fail_on_dead,
        )

        if all_tenants == (tenant_id is not None):
            payload["scope"] = {
                "mode": "invalid",
                "tenant_id": tenant_id,
            }
            payload.update(
                status="rejected",
                termination_reason="invalid_arguments",
                reason_code="exactly_one_scope_required",
            )
            _echo_survey_effect_json(payload)
            raise click.exceptions.Exit(SURVEY_EFFECT_CLI_EXIT_INVALID_ARGUMENTS)

        try:
            from services.survey_response_effects import (
                dispatch_survey_response_effects,
                summarize_survey_response_effects,
            )

            if tenant_id is not None and not _survey_effect_tenant_exists(tenant_id):
                payload.update(
                    status="rejected",
                    termination_reason="invalid_arguments",
                    reason_code="tenant_not_found",
                )
                _echo_survey_effect_json(payload)
                raise click.exceptions.Exit(
                    SURVEY_EFFECT_CLI_EXIT_INVALID_ARGUMENTS
                )

            totals = payload["totals"]
            for _batch_index in range(max_batches):
                remaining_effects = max_effects - totals["claimed"]
                if remaining_effects <= 0:
                    payload["termination_reason"] = "max_effects"
                    break

                requested_limit = min(batch_size, remaining_effects)
                batch_result = dispatch_survey_response_effects(
                    tenant_id=tenant_id,
                    limit=requested_limit,
                )
                payload["batches"] += 1
                claimed = _accumulate_survey_effect_batch(
                    totals,
                    batch_result,
                    requested_limit=requested_limit,
                )
                if claimed == 0:
                    payload["termination_reason"] = "drained"
                    break
                if totals["claimed"] >= max_effects:
                    payload["termination_reason"] = "max_effects"
                    break
            else:
                payload["termination_reason"] = "max_batches"

            if payload["termination_reason"] is None:
                payload["termination_reason"] = "max_effects"

            if tenant_id is not None:
                outbox_after = summarize_survey_response_effects(tenant_id)
                if not isinstance(outbox_after, Mapping):
                    raise RuntimeError("survey_effect_summary_invalid")
                dead_effects = outbox_after.get("dead", 0)
                payload["outbox_after"] = dict(outbox_after)
            else:
                dead_effects = _count_dead_survey_response_effects()
                payload["outbox_after"] = {
                    "scope": "global",
                    "dead": dead_effects,
                }

            if isinstance(dead_effects, bool):
                raise RuntimeError("survey_effect_dead_count_invalid")
            dead_effects = int(dead_effects)
            if dead_effects < 0:
                raise RuntimeError("survey_effect_dead_count_invalid")
            payload["outbox_after"]["dead"] = dead_effects

            payload["status"] = (
                "completed_with_dead" if dead_effects else "completed"
            )
            _echo_survey_effect_json(payload)
            if dead_effects and fail_on_dead:
                raise click.exceptions.Exit(SURVEY_EFFECT_CLI_EXIT_DEAD_EFFECTS)
        except click.exceptions.Exit:
            raise
        except Exception as exc:
            _rollback_survey_effect_cli_session()
            payload.update(
                status="failed",
                termination_reason="operational_failure",
                reason_code="dispatch_failed",
                error_type=type(exc).__name__,
            )
            _echo_survey_effect_json(payload)
            cli_logger.warning(
                "Survey response effect CLI failed error_type=%s",
                type(exc).__name__,
                exc_info=False,
            )
            raise click.exceptions.Exit(SURVEY_EFFECT_CLI_EXIT_FAILURE)

    @app.cli.command("cargar_datos_iniciales")
    def cargar_datos_command():
        from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo

        cli_logger.info("Iniciando carga de datos iniciales CLI...")
        with app.app_context():
            try:
                cli_logger.info(
                    "Asegurando que todas las tablas estén creadas (db.create_all())..."
                )
                db.create_all()
                cargar_usuarios_demo()
                cargar_faqs()
                cargar_sugerencias()
                cli_logger.info("Datos iniciales cargados correctamente CLI.")
            except Exception as e:
                cli_logger.error(
                    f"❌ Error cargando datos iniciales CLI: {e}", exc_info=True
                )

    @app.cli.command("asignar-whatsapp")
    @click.option(
        "--email", required=True, help="Email del usuario PyME/Municipio al que se asignará el número."
    )
    @click.option(
        "--numero",
        "numeros",
        multiple=True,
        required=True,
        help="Número(s) de WhatsApp en formato E.164 (puede especificarse varias veces).",
    )
    @with_appcontext
    def asignar_whatsapp_command(email: str, numeros: tuple[str, ...]):
        """Asigna uno o más números de WhatsApp a un usuario existente."""

        from models import User
        from services.user_service import assign_whatsapp_numbers

        user = User.query.filter_by(email=email).first()
        if not user:
            click.echo(f"❌ Usuario no encontrado: {email}")
            return

        results = assign_whatsapp_numbers(user, numeros, activate=True, commit=True)

        if not results:
            click.echo(
                f"ℹ️ No se asignaron números para {email}; verifique los valores ingresados."
            )
            return

        for result in results:
            number = result["number"]
            status = result["status"]
            prev_email = result.get("previous_user_email") or result.get(
                "previous_user_id"
            )
            reactivated = result.get("reactivated")

            if status == "created":
                click.echo(f"✅ Número WhatsApp {number} asignado a {user.email}.")
            elif status == "reassigned":
                if prev_email:
                    click.echo(
                        f"🔁 Número WhatsApp {number} reasignado de {prev_email} a {user.email}."
                    )
                else:
                    click.echo(
                        f"🔁 Número WhatsApp {number} reasignado a {user.email}."
                    )
                if reactivated:
                    click.echo(f"♻️ Número WhatsApp {number} reactivado para {user.email}.")
            elif status == "reactivated":
                click.echo(f"♻️ Número WhatsApp {number} reactivado para {user.email}.")
            elif status == "updated":
                click.echo(
                    f"ℹ️ Número WhatsApp {number} ya estaba activo para {user.email}."
                )

    @app.cli.command("aplicar_migraciones")
    def aplicar_migraciones_command():
        from flask_migrate import upgrade

        cli_logger.info("Intentando aplicar migraciones de base de datos CLI...")
        with app.app_context():
            try:
                upgrade()
                cli_logger.info("Migraciones de base de datos aplicadas correctamente CLI.")
            except Exception as e:
                cli_logger.error(
                    f"❌ Error durante la aplicación de migraciones CLI (upgrade): {e}",
                    exc_info=True,
                )

    @app.cli.command("generate-template-embeddings")
    @with_appcontext
    def generate_template_embeddings_command():
        """Genera y almacena embeddings para las Plantillas de Respuesta."""
        from models import PlantillasRespuesta
        from services.embedding_service import embed_textos_llm

        cli_logger.info(
            "Iniciando generación de embeddings para Plantillas de Respuesta..."
        )

        try:
            plantillas = PlantillasRespuesta.query.all()
            if not plantillas:
                cli_logger.info(
                    "No se encontraron plantillas de respuesta para procesar."
                )
                return

            count_processed = 0
            count_errors = 0
            for plantilla in plantillas:
                if not plantilla.text or not plantilla.text.strip():
                    cli_logger.warning(
                        f"Plantilla ID {plantilla.id} ('{plantilla.name}') no tiene texto, saltando."
                    )
                    continue

                cli_logger.info(
                    f"Procesando plantilla ID {plantilla.id} ('{plantilla.name}')..."
                )
                try:
                    # embed_textos_llm espera una lista de textos y devuelve una lista de embeddings
                    embedding_list = embed_textos_llm(
                        textos=[plantilla.text], input_type="search_document"
                    )

                    if embedding_list and embedding_list[0]:
                        plantilla.embedding = embedding_list[0]
                        db.session.add(plantilla)
                        count_processed += 1
                        cli_logger.info(
                            f"Embedding generado para plantilla ID {plantilla.id}."
                        )
                    else:
                        cli_logger.error(
                            f"No se pudo generar embedding para plantilla ID {plantilla.id} ('{plantilla.name}'). Respuesta de Cohere vacía."
                        )
                        count_errors += 1
                except Exception as e:
                    cli_logger.error(
                        f"Error generando embedding para plantilla ID {plantilla.id} ('{plantilla.name}'): {e}",
                        exc_info=False,
                    )
                    count_errors += 1

            if count_processed > 0 or count_errors > 0:
                db.session.commit()
                cli_logger.info(
                    f"Proceso completado. Embeddings generados/actualizados para {count_processed} plantillas."
                )
                if count_errors > 0:
                    cli_logger.warning(
                        f"Hubo errores al procesar {count_errors} plantillas."
                    )
            else:
                cli_logger.info(
                    "No se procesaron nuevas plantillas (o ninguna tenía texto)."
                )

        except Exception as e:
            cli_logger.error(
                f"❌ Error general durante la generación de embeddings para plantillas: {e}",
                exc_info=True,
            )
            db.session.rollback()

    @app.cli.command("seed-demo-junin")
    @with_appcontext
    def seed_demo_junin():
        """Inicializa el demo municipal de Junín con datos, token y seeds."""

        from werkzeug.security import generate_password_hash

        from models import Rubro, TenantProfile, User
        from services.user_service import assign_whatsapp_numbers

        ensure_seed_catalog = None
        catalog_seed_spec = importlib.util.find_spec("services.catalog_seed")
        if catalog_seed_spec:
            from services.catalog_seed import ensure_seed_catalog  # type: ignore

        ensure_seed_encuestas_for_tenant = None
        encuestas_seed_spec = importlib.util.find_spec("services.encuestas_seed")
        if encuestas_seed_spec:
            from services.encuestas_seed import (  # type: ignore
                ensure_seed_encuestas_for_tenant,
            )

        email = "mauricio@junin.com"
        raw_password = os.getenv("JUNIN_ADMIN_BOOTSTRAP_PASSWORD")

        widget_token = os.getenv("DEMO_WIDGET_TOKEN_JUNIN")
        official_whatsapp = "+17432643718"

        user = User.query.filter_by(email=email).first()
        if not user:
            if not raw_password:
                raise click.ClickException(
                    "JUNIN_ADMIN_BOOTSTRAP_PASSWORD is required to create the Junin admin"
                )
            current_app.logger.info("Creando usuario municipal demo Junín...")
            user = User(
                email=email,
                name="Mauricio",
                rol="admin",
                tipo_chat="municipio",
                tenant_slug="municipio",
                nombre_empresa="Municipio de Junin",
                is_active=True,
            )
            if hasattr(user, "is_admin"):
                user.is_admin = True
            if hasattr(user, "role") and not getattr(user, "role", None):
                user.role = "admin"

            if hasattr(user, "set_password"):
                user.set_password(raw_password)
            else:
                user.password_hash = generate_password_hash(raw_password)

            db.session.add(user)
        else:
            current_app.logger.info(
                "Usuario municipal ya existe; se conserva su credencial actual."
            )
            user.rol = "admin"
            user.tipo_chat = "municipio"
            user.tenant_slug = "municipio"
            user.nombre_empresa = user.nombre_empresa or "Municipio de Junin"

        if hasattr(user, "is_admin"):
            user.is_admin = True
        if hasattr(user, "role"):
            user.role = "admin"
        user.name = user.name or "Mauricio"
        user.nombre_empresa = "Municipio de Junin"

        rubro = None
        try:
            rubro = Rubro.query.filter_by(slug="municipio").first()
        except Exception:
            current_app.logger.warning(
                "No existe modelo Rubro o campo slug, saltando creación de rubro."
            )

        if rubro is None:
            try:
                rubro = Rubro(slug="municipio", nombre="Municipio / Gobierno local")
                db.session.add(rubro)
                current_app.logger.info("Rubro 'municipio' creado.")
            except Exception as exc:
                current_app.logger.warning(
                    "No se pudo crear rubro 'municipio' (ajusta a tu modelo): %s", exc
                )
                rubro = None

        tenant = TenantProfile.query.filter_by(slug="municipio").first()
        if not tenant:
            current_app.logger.info("Creando TenantProfile slug='municipio'...")
            tenant = TenantProfile(
                slug="municipio",
                nombre="Municipio de Junín",
                tipo="municipio",
                municipio_id=user.id,
            )
            db.session.add(tenant)
        else:
            tenant.nombre = "Municipio de Junin"
            tenant.tipo = "municipio"
            tenant.pyme_id = None
            tenant.municipio_id = user.id
            tenant.is_active = True

        db.session.flush()
        tenant.nombre = "Municipio de Junin"
        user.tenant_id = tenant.id
        user.tenant_slug = tenant.slug
        user.municipio_id = user.id

        config = tenant.configuracion or {}
        widget_tokens = set(config.get("widget_tokens", []))
        if widget_token:
            widget_tokens.add(widget_token)
        config["widget_tokens"] = list(widget_tokens)

        whatsapp_numbers = set()
        if isinstance(config.get("whatsapp_numbers"), list):
            whatsapp_numbers.update(filter(None, config.get("whatsapp_numbers")))
        elif isinstance(config.get("whatsapp_numbers"), str):
            whatsapp_numbers.add(config["whatsapp_numbers"])
        if official_whatsapp:
            whatsapp_numbers.add(official_whatsapp)
        if whatsapp_numbers:
            config["whatsapp_numbers"] = list(whatsapp_numbers)

        config["whatsapp_oficial"] = official_whatsapp
        config.setdefault(
            "descripcion_corta", "Atención ciudadana 24/7 - Municipio de Junín"
        )
        config.setdefault("tema", "municipio_junin")

        assign_whatsapp_numbers(user, [official_whatsapp], activate=True, commit=False)

        tenant.configuracion = config

        if rubro is not None:
            if hasattr(tenant, "rubro_id") and getattr(tenant, "rubro_id", None) is None:
                tenant.rubro_id = rubro.id

        conflicting_tenants = []
        if widget_token:
            conflicting_tenants = (
                TenantProfile.query.filter(
                    TenantProfile.slug != tenant.slug,
                    TenantProfile.configuracion["widget_tokens"].astext.contains(widget_token),
                )
                .order_by(TenantProfile.id.asc())
                .all()
            )

        for other in conflicting_tenants:
            cfg = other.configuracion or {}
            tokens = cfg.get("widget_tokens")
            updated_tokens: list[str] = []

            if isinstance(tokens, str):
                updated_tokens = [t for t in [tokens] if t != widget_token]
            elif isinstance(tokens, list):
                updated_tokens = [t for t in tokens if t != widget_token]

            cfg["widget_tokens"] = updated_tokens
            other.configuracion = cfg
            db.session.add(other)
            current_app.logger.info(
                "Removiendo widget_token %s del tenant %s para evitar colisiones.",
                widget_token,
                other.slug,
            )

        db.session.commit()
        current_app.logger.info("Usuario + tenant municipal guardados.")

        if ensure_seed_catalog is not None:
            try:
                current_app.logger.info("Ejecutando ensure_seed_catalog para Junín...")
                ensure_seed_catalog(user=user, tenant=tenant, rubro=rubro)
            except Exception as exc:
                current_app.logger.warning(
                    "ensure_seed_catalog falló, revisa los parámetros esperados: %s",
                    exc,
                )
        else:
            current_app.logger.info(
                "services.catalog_seed no encontrado, saltando catálogo demo."
            )

        if ensure_seed_encuestas_for_tenant is not None:
            try:
                current_app.logger.info(
                    "Ejecutando ensure_seed_encuestas_for_tenant('municipio')..."
                )
                ensure_seed_encuestas_for_tenant(tenant_slug=tenant.slug)
            except Exception as exc:
                current_app.logger.warning(
                    "ensure_seed_encuestas_for_tenant falló: %s", exc
                )
        else:
            current_app.logger.info(
                "services.encuestas_seed no encontrado, saltando encuestas demo."
            )

        click.echo("✅ Demo municipal Junín inicializada/actualizada.")

    @app.cli.command("generate-weekly-reports")
    def generate_weekly_reports():
        """Generates cached AI reports for all active tenants."""
        from models import TenantProfile
        from services.analytics_service import analytics_service
        from services.openai_bridge import generate_analytics_report
        from datetime import datetime, timedelta

        cli_logger.info("Starting weekly report generation...")

        with app.app_context():
            tenants = TenantProfile.query.filter_by(is_active=True).all()
            now = datetime.utcnow()
            start_date = now - timedelta(days=7)

            for tenant in tenants:
                try:
                    cli_logger.info(f"Processing tenant {tenant.slug}...")

                    # 1. Check if recently generated
                    cached = analytics_service.get_cached_report(tenant.id, f"consultant_{tenant.tipo}", max_age_hours=24)
                    if cached:
                        cli_logger.info(f"Report already fresh for {tenant.slug}.")
                        continue

                    # 2. Aggregate stats
                    summary = analytics_service.get_summary(
                        tenant_id=tenant.id,
                        start_date=start_date,
                        end_date=now,
                        context=tenant.tipo
                    )

                    if tenant.tipo == 'pyme':
                        commerce = analytics_service.get_commerce_analytics(
                            tenant_id=tenant.id,
                            start_date=start_date,
                            end_date=now
                        )
                        summary.update(commerce)

                    # 3. Generate & Cache
                    report = generate_analytics_report(summary, tenant_type=tenant.tipo)
                    analytics_service.cache_report(tenant.id, f"consultant_{tenant.tipo}", report)

                    cli_logger.info(f"Generated report for {tenant.slug}.")

                except Exception as e:
                    cli_logger.error(f"Error processing {tenant.slug}: {e}")

        cli_logger.info("Weekly report generation complete.")
