# src/jobs/orchestrator.py
import sys
import os
import logging
import json
from datetime import datetime

# --- Configuración de Path para importar la app de Flask ---
project_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_path not in sys.path:
    sys.path.append(project_path)
# --- Fin Configuración ---

# Importa tu app de Flask y la base de datos `db`
from app import create_app, db
# Importa los modelos que vas a usar
from models import User, Rubro, SitioWebInfo
# Importa las herramientas del scraper
from services.scraper_avanzado import (
    descubrir_links_relevantes,
    extraer_contenido_general,
    extraer_productos_de_url
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Lista de rubros públicos (puedes moverla a un archivo de configuración si prefieres)
RUBROS_PUBLICOS = {"municipio", "municipios", "ong", "gobierno", "hospital_publico"}

def run_scraping_orchestrator():
    """
    Función principal que recorre todos los clientes y actualiza su contenido.
    """
    logger.info("[ORQUESTADOR] Iniciando ciclo de scraping para todos los clientes.")
    
    # 1. Obtener todos los clientes con una página web definida
    clientes = User.query.filter(User.link_web.isnot(None), User.link_web != '').all()
    logger.info(f"[ORQUESTADOR] Se encontraron {len(clientes)} clientes con página web.")

    for cliente in clientes:
        logger.info(f"--- Procesando cliente ID: {cliente.id}, Web: {cliente.link_web} ---")
        
        # Primero, borramos la información vieja para este cliente para evitar duplicados
        SitioWebInfo.query.filter_by(user_id=cliente.id).delete()
        
        # 2. Descubrir todas las URLs relevantes para este cliente
        urls_a_scrapear = descubrir_links_relevantes(cliente.link_web)
        
        tipo_rubro = cliente.rubro.clave if cliente.rubro else ""

        for url in urls_a_scrapear:
            datos = {}
            if tipo_rubro in RUBROS_PUBLICOS:
                datos = extraer_contenido_general(url)
            else:
                datos = extraer_productos_de_url(url)
            
            if not datos.get("error"):
                # 3. Guardar los nuevos datos en la base de datos
                nuevo_contenido = SitioWebInfo(
                    user_id=cliente.id,
                    rubro_id=cliente.rubro_id,
                    url=url,
                    # Usamos json.dumps para convertir el dict a un string para la columna Text
                    datos_json=json.dumps(datos, ensure_ascii=False),
                    fecha_scraping=datetime.utcnow()
                )
                db.session.add(nuevo_contenido)
        
        # Hacemos commit por cada cliente para guardar su progreso
        try:
            db.session.commit()
            logger.info(f"--- Datos del cliente ID: {cliente.id} guardados exitosamente. ---")
        except Exception as e:
            logger.error(f"--- Error al guardar datos para cliente ID: {cliente.id}: {e} ---")
            db.session.rollback()

    logger.info("[ORQUESTADOR] Ciclo de scraping finalizado.")

if __name__ == "__main__":
    # Es VITAL crear un contexto de aplicación para que el script pueda usar la db
    app = create_app()
    with app.app_context():
        run_scraping_orchestrator()