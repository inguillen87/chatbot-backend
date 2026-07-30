from celery import Celery
from flask import Flask # Necesario para el tipado en init_celery

# Crear la instancia de Celery aquí
celery_app = Celery(__name__)

def init_celery(app: Flask):
    """
    Inicializa la aplicación Celery con la configuración de la aplicación Flask.
    También configura las tareas para que se ejecuten dentro del contexto de la aplicación Flask.
    """
    # Actualizar la configuración de Celery desde la configuración de Flask
    # Celery usará prefijo 'CELERY_' para sus variables de configuración en Flask.
    # Ej: app.config['CELERY_BROKER_URL'], app.config['CELERY_RESULT_BACKEND']
    celery_config = app.config.get('CELERY_CONFIG', {})
    required_imports = (
        'services.tasks',
        'services.whatsapp_inbound_worker',
        'services.domain_effect_worker',
        'services.survey_response_effect_worker',
    )
    configured_imports = tuple(celery_config.get('imports') or ())
    celery_config['imports'] = configured_imports + tuple(
        module for module in required_imports if module not in configured_imports
    )

    celery_app.conf.update(celery_config)
    
    # Si CELERY_BROKER_URL y CELERY_RESULT_BACKEND están directamente en app.config
    if 'CELERY_BROKER_URL' in app.config:
        celery_app.conf.broker_url = app.config['CELERY_BROKER_URL']
    if 'CELERY_RESULT_BACKEND' in app.config:
        celery_app.conf.result_backend = app.config['CELERY_RESULT_BACKEND']

    # Subclase Task para asegurar que las tareas se ejecuten con el contexto de la app Flask
    class ContextTask(celery_app.Task):
        abstract = True
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return super().__call__(*args, **kwargs)

    celery_app.Task = ContextTask
    
    # Autodiscover tasks: Celery buscará tareas en los módulos listados en CELERY_IMPORTS
    # o en un patrón definido (e.g., tasks.py en cada app registrada en INSTALLED_APPS si fuera Django).
    # Para Flask, es común especificar los módulos que contienen tareas.
    # Esto se puede hacer en la configuración de Flask (app.config['CELERY_IMPORTS'])
    # o directamente aquí si se prefiere.
    # Ejemplo: celery_app.autodiscover_tasks(['services'], related_name='tasks', force=True)
    # O, si las tareas están en un módulo específico como `tasks.py` dentro de cada servicio:
    # celery_app.autodiscover_tasks(lambda: [n.name for n in app.blueprints.values()] + ['services'])
    # Por ahora, lo dejamos más simple; las tareas se importarán donde se definan.
    # Si las tareas se definen con @celery_app.task, ya estarán registradas.

    return celery_app
