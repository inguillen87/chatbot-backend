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

from services.interpretacion_imagen_service import interpretar_imagen_para_chat
from models import ChatSessionContext
from twilio.rest import Client
import os
from sqlalchemy.orm.attributes import flag_modified
from services.llm_provider_network_policy import (
    ProviderNetworkDisabledError,
    require_provider_network,
)
from services.notification_orchestrator import NotificationOrchestrator


def _send_image_analysis_twilio_message(
    *,
    account_sid,
    auth_token,
    from_number,
    to_number,
    body,
):
    try:
        require_provider_network("twilio")
    except ProviderNetworkDisabledError:
        logger.info(
            "Image analysis reply blocked provider=twilio reason=test_network_disabled"
        )
        raise
    twilio_client = Client(account_sid, auth_token)
    require_provider_network("twilio")
    return twilio_client.messages.create(
        from_=from_number,
        body=body,
        to=to_number,
    )

@celery_app.task
def process_image_for_chat_task(user_phone_number, client_user_id, uploaded_file_info_whatsapp, chat_session_id):
    from services.municipio_responder import CONTEXTO_MUNICIPIO, ConversationState
    """
    Celery task to process an image for a chat session.
    """
    from app import app
    with app.app_context():
        from_number = f"whatsapp:{os.environ.get('TWILIO_WHATSAPP_NUMBER_JUNIN')}"
        to_number = f"whatsapp:{user_phone_number}"

        # Call the image interpretation service
        analisis_resultado = interpretar_imagen_para_chat(
            archivo_adjunto=uploaded_file_info_whatsapp,
            tipo_interpretacion="reclamo_auto_descripcion_categoria"
        )

        # Get the chat session
        session_context_db_entry = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
        if not session_context_db_entry:
            return

        contexto_municipio_actual = session_context_db_entry.context_data.get(CONTEXTO_MUNICIPIO, {})

        if analisis_resultado and not analisis_resultado.get("error"):
            contexto_municipio_actual["analisis_imagen_reclamo_auto_raw"] = analisis_resultado
            cat_sug_wp = analisis_resultado.get("categoria_sugerida")
            desc_sug_wp = analisis_resultado.get("descripcion_sugerida")

            if cat_sug_wp:
                contexto_municipio_actual["categoria_reclamo"] = cat_sug_wp
            if desc_sug_wp:
                contexto_municipio_actual["descripcion_reclamo"] = desc_sug_wp

            if analisis_resultado.get('es_reclamo'):
                contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
                message_body = f"He analizado la imagen y parece que es un reclamo sobre *{cat_sug_wp}*. Para continuar, por favor, decime la dirección del problema."
            else:
                message_body = "He recibido tu foto. Para continuar con el reclamo, por favor, decime la dirección del problema."
        else:
            message_body = "No pude analizar la imagen correctamente. Por favor, ¿podrías describir el problema y la dirección?"

        session_context_db_entry.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
        flag_modified(session_context_db_entry, "context_data")
        db.session.commit()

        # Send the message
        _send_image_analysis_twilio_message(
            account_sid=os.environ.get("TWILIO_ACCOUNT_SID"),
            auth_token=os.environ.get("TWILIO_AUTH_TOKEN"),
            from_number=from_number,
            body=message_body,
            to_number=to_number,
        )


@celery_app.task(name="tasks.dispatch_notifications", bind=True, max_retries=2, default_retry_delay=120)
def dispatch_notifications_task(self, tenant_id: int, limit: int = 50):
    """Worker task to dispatch due notifications for a tenant."""
    try:
        orchestrator = NotificationOrchestrator(int(tenant_id))
        result = orchestrator.dispatch_due_notifications(limit=int(limit))
        db.session.commit()
        return result
    except Exception as exc:
        db.session.rollback()
        raise self.retry(exc=exc)


@celery_app.task(name="tasks.v2_detect_sla_breaches", bind=True, max_retries=1, default_retry_delay=60)
def v2_detect_sla_breaches_task(self, tenant_id: int):
    """Detect SLA breaches for TenantTicket v2 and emit audit events."""
    try:
        from models import TenantProfile
        from services.v2.sla_service import detect_sla_breaches_for_tenant

        tenant = TenantProfile.query.get(int(tenant_id))
        if not tenant:
            return {"status": "tenant_not_found", "tenant_id": tenant_id}

        breaches = detect_sla_breaches_for_tenant(tenant)
        db.session.commit()
        return {"status": "ok", "tenant_id": tenant_id, "breaches": len(breaches)}
    except Exception as exc:
        db.session.rollback()
        raise self.retry(exc=exc)


SURVEY_RESPONSE_EFFECT_TASK_NAME = "tasks.dispatch_survey_response_effects"
SURVEY_RESPONSE_EFFECT_TASK_CONTRACT_VERSION = (
    "tasks.dispatch_survey_response_effects.v1"
)
SURVEY_RESPONSE_EFFECT_TASK_DEFAULT_LIMIT = 50
SURVEY_RESPONSE_EFFECT_TASK_MAX_LIMIT = 100


def _positive_tenant_id(value):
    """Return a positive integer tenant id, rejecting lossy coercions."""
    if isinstance(value, bool):
        return None

    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if normalized <= 0:
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return normalized


def _bounded_survey_response_effect_limit(value) -> int:
    """Keep every worker invocation inside a small, predictable batch."""
    if isinstance(value, bool):
        return SURVEY_RESPONSE_EFFECT_TASK_DEFAULT_LIMIT

    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return SURVEY_RESPONSE_EFFECT_TASK_DEFAULT_LIMIT

    return max(1, min(normalized, SURVEY_RESPONSE_EFFECT_TASK_MAX_LIMIT))


def _survey_response_effect_task_context(task) -> dict:
    request = getattr(task, "request", None)
    retries = int(getattr(request, "retries", 0) or 0)
    return {
        "task_id": getattr(request, "id", None),
        "attempt": retries + 1,
    }


def _rollback_survey_response_effect_task_session() -> None:
    """Best-effort cleanup that never masks the error that triggers retry."""
    try:
        db.session.rollback()
    except Exception:
        logger.exception(
            "[SURVEY_RESPONSE_EFFECT_TASK] Failed to roll back the DB session"
        )


@celery_app.task(
    name=SURVEY_RESPONSE_EFFECT_TASK_NAME,
    bind=True,
    max_retries=4,
    default_retry_delay=60,
    acks_late=True,
)
def dispatch_survey_response_effects_task(
    self,
    tenant_id: int,
    limit: int = SURVEY_RESPONSE_EFFECT_TASK_DEFAULT_LIMIT,
):
    """Dispatch a bounded batch of durable survey effects for one tenant."""
    normalized_tenant_id = _positive_tenant_id(tenant_id)
    bounded_limit = _bounded_survey_response_effect_limit(limit)
    task_context = _survey_response_effect_task_context(self)

    if normalized_tenant_id is None:
        logger.warning(
            "[SURVEY_RESPONSE_EFFECT_TASK] Rejected invalid tenant scope task_id=%s",
            task_context["task_id"],
        )
        return {
            "contract_version": SURVEY_RESPONSE_EFFECT_TASK_CONTRACT_VERSION,
            "status": "rejected",
            "reason_code": "tenant_id_invalid",
            "retryable": False,
            "tenant_id": None,
            "limit": bounded_limit,
            **task_context,
        }

    try:
        # Lazy import avoids coupling every Celery worker import to the outbox
        # dispatcher while still registering this task through services.tasks.
        from services.survey_response_effects import (
            dispatch_survey_response_effects,
        )

        dispatcher_result = dispatch_survey_response_effects(
            tenant_id=normalized_tenant_id,
            limit=bounded_limit,
        )
        result = {
            "contract_version": SURVEY_RESPONSE_EFFECT_TASK_CONTRACT_VERSION,
            "status": "completed",
            "tenant_id": normalized_tenant_id,
            "limit": bounded_limit,
            "dispatcher": dispatcher_result,
            **task_context,
        }
        logger.info(
            "[SURVEY_RESPONSE_EFFECT_TASK] Completed task_id=%s tenant_id=%s "
            "limit=%s claimed=%s processed=%s succeeded=%s dead=%s",
            task_context["task_id"],
            normalized_tenant_id,
            bounded_limit,
            dispatcher_result.get("claimed", 0),
            dispatcher_result.get("processed", 0),
            dispatcher_result.get("succeeded", 0),
            dispatcher_result.get("dead", 0),
        )
        return result
    except Exception as exc:
        _rollback_survey_response_effect_task_session()
        logger.warning(
            "[SURVEY_RESPONSE_EFFECT_TASK] Retrying task_id=%s tenant_id=%s "
            "attempt=%s error_type=%s",
            task_context["task_id"],
            normalized_tenant_id,
            task_context["attempt"],
            type(exc).__name__,
        )
        raise self.retry(exc=exc)
