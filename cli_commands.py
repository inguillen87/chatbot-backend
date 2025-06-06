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