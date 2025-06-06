# tasks.py
# Este archivo contiene tareas que se ejecutan en segundo plano.

import logging
from models import User, db # Asumo que tu modelo de usuario se llama User y tienes db
from services.scraper import extraer_info_contacto_web
from services.webinfo import guardar_info_web # --- INTEGRADO: Importamos tu función ---

logger = logging.getLogger(__name__)

def actualizar_info_pyme_desde_web(user_id: int):
    """
    Toma el ID de un usuario (Pyme), busca su web en la DB, la scrapea,
    guarda la info completa en SitioWebInfo y actualiza los campos principales en User.
    """
    pyme_user = User.query.get(user_id)

    if not pyme_user:
        logger.error(f"[TASK] No se encontró el usuario con ID: {user_id}")
        return

    link_web = getattr(pyme_user, 'link_web', None)
    if not link_web:
        logger.warning(f"[TASK] El usuario {pyme_user.nombre_empresa} (ID: {user_id}) no tiene un link_web configurado.")
        return

    logger.info(f"[TASK] Actualizando datos para {pyme_user.nombre_empresa} desde {link_web}")
    
    # 1. Llamamos a nuestro scraper mejorado
    info_scraped = extraer_info_contacto_web(link_web)

    if "error" in info_scraped:
        logger.error(f"[TASK] Falló el scrape para {link_web}: {info_scraped['error']}")
        return

    # 2. --- ¡USAMOS TU FUNCIÓN! ---
    # Guardamos el diccionario completo en la tabla SitioWebInfo.
    rubro_id = getattr(pyme_user, 'rubro_id', None) # Necesitamos el rubro_id
    try:
        guardar_info_web(user_id=user_id, rubro_id=rubro_id, url=link_web, data_dict=info_scraped)
        logger.info(f"[TASK] Datos crudos del scrape guardados en SitioWebInfo para user {user_id}.")
    except Exception as e:
        logger.error(f"[TASK] Error al usar guardar_info_web para user {user_id}: {e}")
        # Continuamos de todas formas para intentar actualizar los campos principales.

    # 3. --- ACTUALIZACIÓN PARA ACCESO RÁPIDO ---
    # Ahora actualizamos los campos principales en el modelo User para un acceso veloz.
    # Damos prioridad a los datos que el usuario cargó manualmente, si los hay.
    if not pyme_user.telefono and info_scraped.get("telefonos"):
        pyme_user.telefono = info_scraped["telefonos"][0]

    if not pyme_user.email and info_scraped.get("emails"):
        email_valido = next((email for email in info_scraped["emails"] if "no-reply" not in email), None)
        if email_valido:
            pyme_user.email = email_valido
            
    if not pyme_user.direccion and info_scraped.get("direcciones"):
        pyme_user.direccion = info_scraped["direcciones"][0]

    # 4. Guardamos los cambios en la base de datos
    try:
        db.session.commit()
        logger.info(f"[TASK] ¡Campos principales en User {pyme_user.nombre_empresa} actualizados con éxito!")
    except Exception as e:
        db.session.rollback()
        logger.error(f"[TASK] Error al guardar cambios en la DB para user {user_id}: {e}")