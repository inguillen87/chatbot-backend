from flask import Flask

from celery_utils import init_celery


def test_celery_registers_authoritative_database_workers():
    app = Flask(__name__)
    app.config["CELERY_CONFIG"] = {"imports": ("custom.tasks",)}

    celery = init_celery(app)

    assert celery.conf.imports == (
        "custom.tasks",
        "services.tasks",
        "services.whatsapp_inbound_worker",
        "services.domain_effect_worker",
        "services.survey_response_effect_worker",
    )
