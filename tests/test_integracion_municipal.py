from types import SimpleNamespace

from services.integracion_municipal import enviar_ticket_a_sigem


def test_sigem_placeholder_never_reports_a_fake_success(app):
    with app.app_context():
        app.config["SIGEM_LIVE_ENABLED"] = False
        assert enviar_ticket_a_sigem(SimpleNamespace(id=17)) is False

        app.config["SIGEM_LIVE_ENABLED"] = True
        assert enviar_ticket_a_sigem(SimpleNamespace(id=17)) is False
