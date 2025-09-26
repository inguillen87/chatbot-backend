# cli_commands.py
import click
from flask.cli import with_appcontext
from extensions import db
import logging

# Logger para este módulo
cli_logger = logging.getLogger(__name__)

def register_commands(app):
    @app.cli.command("cargar_datos_iniciales")
    def cargar_datos_command():
        from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
        cli_logger.info("Iniciando carga de datos iniciales CLI...")
        with app.app_context():
            try:
                cli_logger.info("Asegurando que todas las tablas estén creadas (db.create_all())...")
                db.create_all()
                cargar_usuarios_demo()
                cargar_faqs()
                cargar_sugerencias()
                cli_logger.info("Datos iniciales cargados correctamente CLI.")
            except Exception as e:
                cli_logger.error(f"❌ Error cargando datos iniciales CLI: {e}", exc_info=True)

    @app.cli.command("asignar-whatsapp")
    @click.option("--email", required=True, help="Email del usuario PyME/Municipio al que se asignará el número.")
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
            click.echo(f"ℹ️ No se asignaron números para {email}; verifique los valores ingresados.")
            return

        for result in results:
            number = result["number"]
            status = result["status"]
            prev_email = result.get("previous_user_email") or result.get("previous_user_id")
            reactivated = result.get("reactivated")

            if status == "created":
                click.echo(f"✅ Número WhatsApp {number} asignado a {user.email}.")
            elif status == "reassigned":
                if prev_email:
                    click.echo(
                        f"🔁 Número WhatsApp {number} reasignado de {prev_email} a {user.email}."
                    )
                else:
                    click.echo(f"🔁 Número WhatsApp {number} reasignado a {user.email}.")
                if reactivated:
                    click.echo(f"♻️ Número WhatsApp {number} reactivado para {user.email}.")
            elif status == "reactivated":
                click.echo(f"♻️ Número WhatsApp {number} reactivado para {user.email}.")
            elif status == "updated":
                click.echo(f"ℹ️ Número WhatsApp {number} ya estaba activo para {user.email}.")

    @app.cli.command("aplicar_migraciones")
    def aplicar_migraciones_command():
        from flask_migrate import upgrade
        cli_logger.info("Intentando aplicar migraciones de base de datos CLI...")
        with app.app_context():
            try:
                upgrade()
                cli_logger.info("Migraciones de base de datos aplicadas correctamente CLI.")
            except Exception as e:
                cli_logger.error(f"❌ Error durante la aplicación de migraciones CLI (upgrade): {e}", exc_info=True)

    @app.cli.command("generate-template-embeddings")
    @with_appcontext
    def generate_template_embeddings_command():
        """Genera y almacena embeddings para las Plantillas de Respuesta."""
        from models import PlantillasRespuesta
        from services.embedding_service import embed_textos_llm
        from extensions import db

        cli_logger.info("Iniciando generación de embeddings para Plantillas de Respuesta...")

        try:
            plantillas = PlantillasRespuesta.query.all()
            if not plantillas:
                cli_logger.info("No se encontraron plantillas de respuesta para procesar.")
                return

            count_processed = 0
            count_errors = 0
            for plantilla in plantillas:
                if not plantilla.text or not plantilla.text.strip():
                    cli_logger.warning(f"Plantilla ID {plantilla.id} ('{plantilla.name}') no tiene texto, saltando.")
                    continue

                cli_logger.info(f"Procesando plantilla ID {plantilla.id} ('{plantilla.name}')...")
                try:
                    # embed_textos_llm espera una lista de textos y devuelve una lista de embeddings
                    embedding_list = embed_textos_llm(textos=[plantilla.text], input_type="search_document") # Usar search_document para plantillas almacenadas

                    if embedding_list and embedding_list[0]:
                        plantilla.embedding = embedding_list[0]
                        db.session.add(plantilla)
                        count_processed += 1
                        cli_logger.info(f"Embedding generado para plantilla ID {plantilla.id}.")
                    else:
                        cli_logger.error(f"No se pudo generar embedding para plantilla ID {plantilla.id} ('{plantilla.name}'). Respuesta de Cohere vacía.")
                        count_errors += 1
                except Exception as e:
                    cli_logger.error(f"Error generando embedding para plantilla ID {plantilla.id} ('{plantilla.name}'): {e}", exc_info=False) # exc_info=False para no ser tan verboso por cada error
                    count_errors += 1

            if count_processed > 0 or count_errors > 0: # Solo commitear si hubo algo que procesar o errores que podrían necesitar atención
                db.session.commit()
                cli_logger.info(f"Proceso completado. Embeddings generados/actualizados para {count_processed} plantillas.")
                if count_errors > 0:
                    cli_logger.warning(f"Hubo errores al procesar {count_errors} plantillas.")
            else:
                cli_logger.info("No se procesaron nuevas plantillas (o ninguna tenía texto).")

        except Exception as e:
            cli_logger.error(f"❌ Error general durante la generación de embeddings para plantillas: {e}", exc_info=True)
            db.session.rollback()