# tasks.py
# Este archivo contiene tareas que se ejecutan en segundo plano.

import logging
from models import User, db # Asumo que tu modelo de usuario se llama User y tienes db
from services.scraper_avanzado import extraer_info_contacto_web
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

# --- Tarea para Envío de Campañas por Email ---
from celery_utils import celery_app # Importar la instancia de Celery
from services.email_service import enviar_email # Importar el servicio de email
from typing import List

@celery_app.task(name='tasks.enviar_campana_email', bind=True, max_retries=3, default_retry_delay=300) # 5 min delay
def tarea_enviar_campana_email(
    self, # Es 'self' por bind=True
    empresa_id_solicitante: int,
    lista_ids_clientes_destinatarios: List[int],
    asunto: str,
    cuerpo_html: str,
    cuerpo_texto: str = ""
):
    logger.info(f"[CELERY_CAMPAIGN_TASK] Iniciando tarea de envío de campaña para empresa ID {empresa_id_solicitante} a {len(lista_ids_clientes_destinatarios)} clientes. Asunto: '{asunto}'")

    # Nota: Dentro de una tarea Celery, no tenemos acceso directo al 'current_app' de Flask de la misma forma.
    # Si email_service.py fue refactorizado para usar current_app.config, necesitaremos
    # asegurarnos de que la app Flask esté configurada para Celery o pasar la config necesaria.
    # Por ahora, asumimos que email_service.py (si usa os.getenv o si Celery está bien integrado con Flask app context)
    # puede acceder a su configuración. Si usa current_app.config, la tarea debe correr en un contexto de app.
    # Celery-Flask (o una configuración manual en init_celery) usualmente maneja esto.

    # Alternativamente, podríamos pasar los datos de config SMTP a la tarea, pero es menos ideal.
    # O el email_service.py podría tener un inicializador que tome la app config.

    # Asumiendo que la app Flask está disponible para la tarea Celery (común con Flask-Celery-Helper o configuración adecuada)
    from flask import current_app as flask_current_app # Renombrar para evitar confusión con self
    if not flask_current_app:
        logger.error("[CELERY_CAMPAIGN_TASK] Contexto de aplicación Flask no disponible en tarea Celery. No se puede enviar email.")
        # Podríamos reintentar si es un problema temporal de contexto, pero usualmente es configuración.
        return {"status": "error", "message": "Flask app context not available."}


    emails_enviados_ok = 0
    emails_con_error = 0

    for user_id in lista_ids_clientes_destinatarios:
        cliente = User.query.get(user_id) # Usar User.query ya que db está importado
        if cliente and cliente.email:
            # El servicio de email ya fue modificado para tomar `es_campana=True` y usar config de campaña.
            # La función enviar_email en email_service.py ahora usa current_app.config
            try:
                # Crear un contexto de aplicación para la tarea si es necesario
                # Esto depende de cómo esté configurado Celery con Flask.
                # Si init_celery en celery_utils.py configura la app para las tareas, esto es implícito.
                # with flask_current_app.app_context(): # Descomentar si es necesario y la config lo requiere

                exito = enviar_email(
                    destinatario_email=cliente.email,
                    asunto=asunto,
                    cuerpo_html=cuerpo_html,
                    cuerpo_texto=cuerpo_texto,
                    es_campana=True # Indicar que es un email de campaña
                )
                if exito:
                    logger.info(f"[CELERY_CAMPAIGN_TASK] Email de campaña enviado a {cliente.email} (User ID: {user_id})")
                    emails_enviados_ok += 1
                else:
                    logger.warning(f"[CELERY_CAMPAIGN_TASK] Fallo al enviar email de campaña a {cliente.email} (User ID: {user_id})")
                    emails_con_error += 1
            except Exception as e_task_send:
                logger.error(f"[CELERY_CAMPAIGN_TASK] Excepción enviando email a {cliente.email} (User ID: {user_id}): {e_task_send}", exc_info=True)
                emails_con_error += 1
        else:
            logger.warning(f"[CELERY_CAMPAIGN_TASK] Cliente ID {user_id} no encontrado o sin email. Saltando.")
            emails_con_error += 1

        # Pequeña pausa para no saturar el servidor SMTP, opcional y configurable
        # import time
        # time.sleep(0.1)

    logger.info(f"[CELERY_CAMPAIGN_TASK] Tarea de envío de campaña para empresa ID {empresa_id_solicitante} completada. Enviados: {emails_enviados_ok}, Errores: {emails_con_error}.")
    return {"status": "completado", "enviados_ok": emails_enviados_ok, "errores": emails_con_error}