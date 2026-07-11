# cli_commands.py
import importlib
import logging
import os

import click
from flask import current_app
from flask.cli import with_appcontext

from extensions import db

# Logger para este módulo
cli_logger = logging.getLogger(__name__)


def register_commands(app):
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
