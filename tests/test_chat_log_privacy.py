from contextlib import contextmanager
import logging
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

from routes import chat as chat_routes


SENSITIVE_CANARIES = (
    "entity-token-super-secret",
    "+5492613168608",
    "vecino@example.test",
    "32877851",
    "Don Bosco 56 esquina Sarmiento",
    "-32.889458",
    "-68.845839",
    "https://media.example.test/private.ogg?token=download-secret",
    "transcripcion privada del vecino",
    "session-safe\r\nFORGED_LOG_ENTRY=1",
)


def _assert_canaries_absent(rendered: str) -> None:
    for canary in SENSITIVE_CANARIES:
        assert canary not in rendered
    assert "FORGED_LOG_ENTRY" not in rendered


class _RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capture_app_logs(app, level: int):
    collector = _RecordCollector()
    previous_level = app.logger.level
    app.logger.addHandler(collector)
    app.logger.setLevel(level)
    try:
        yield collector
    finally:
        app.logger.removeHandler(collector)
        app.logger.setLevel(previous_level)


def _render_records(collector: _RecordCollector) -> str:
    return "\n".join(record.getMessage() for record in collector.records)


def test_safe_chat_log_metadata_contains_only_allowlisted_diagnostics():
    metadata = chat_routes._safe_chat_log_metadata(
        question=SENSITIVE_CANARIES[8],
        attachment_info={
            "id": 17,
            "url": SENSITIVE_CANARIES[7],
            "name": "dni-32877851.ogg",
            "transcribed_text": SENSITIVE_CANARIES[8],
        },
        location={
            "lat": SENSITIVE_CANARIES[5],
            "lon": SENSITIVE_CANARIES[6],
            "address": SENSITIVE_CANARIES[4],
        },
        payload={
            "message_body": SENSITIVE_CANARIES[8],
            "tracking_url": SENSITIVE_CANARIES[7],
            "pin": SENSITIVE_CANARIES[3],
        },
        session_id=SENSITIVE_CANARIES[9],
        anon_id=f"anon-{SENSITIVE_CANARIES[1]}-{SENSITIVE_CANARIES[2]}",
        error=RuntimeError(" | ".join(SENSITIVE_CANARIES)),
        internal_ids={"ticket_id": 41, "unsafe": SENSITIVE_CANARIES[3]},
    )

    rendered = repr(metadata)
    _assert_canaries_absent(rendered)
    assert metadata["question"]["length"] == len(SENSITIVE_CANARIES[8])
    assert metadata["attachment"]["attachment_id"] == 17
    assert metadata["location"]["has_coordinates"] is True
    assert metadata["payload"]["has_tracking"] is True
    assert metadata["session"]["present"] is True
    assert metadata["error_type"] == "RuntimeError"
    assert metadata["internal_ids"] == {"ticket_id": 41}


def test_widget_request_success_log_never_contains_entity_token(app):
    owner = SimpleNamespace(
        id=23,
        tenant_slug="municipio-seguro",
        entity_token=SENSITIVE_CANARIES[0],
    )

    with app.test_request_context("/api/ask/municipio"):
        with _capture_app_logs(app, logging.INFO) as collector:
            response = ("ok", 200)
            assert chat_routes._log_widget_request(response, owner) is response

    rendered = _render_records(collector)
    _assert_canaries_absent(rendered)
    assert "has_entity_token=True" in rendered
    assert "user_id=23" in rendered


def test_request_parse_warning_logs_metadata_without_content(app):
    payload = {
        "pregunta": SENSITIVE_CANARIES[8],
        "tipo_chat": "municipio",
        "attachmentInfo": {
            "url": SENSITIVE_CANARIES[7],
            "name": f"audio-{SENSITIVE_CANARIES[3]}.ogg",
            "transcribed_text": SENSITIVE_CANARIES[8],
        },
        "location": {
            "lat": SENSITIVE_CANARIES[5],
            "lon": SENSITIVE_CANARIES[6],
            "address": SENSITIVE_CANARIES[4],
        },
    }

    with app.test_request_context("/api/ask/municipio", method="POST", json=payload):
        with _capture_app_logs(app, logging.WARNING) as collector:
            parsed = chat_routes._parse_request("municipio")

    rendered = _render_records(collector)
    _assert_canaries_absent(rendered)
    assert "attachment_metadata=" in rendered
    assert parsed[0] == SENSITIVE_CANARIES[8]
    assert parsed[5]["url"] == payload["attachmentInfo"]["url"]
    assert parsed[5]["transcribed_text"] == payload["attachmentInfo"]["transcribed_text"]
    assert parsed[5]["source"] == "web_upload"
    assert parsed[6]["lat"] == float(SENSITIVE_CANARIES[5])


def test_error_log_never_renders_exception_or_user_content(app):
    owner = SimpleNamespace(id=29, rubro_id=7)
    tenant = SimpleNamespace(id=11)
    raw_error = SQLAlchemyError(" | ".join(SENSITIVE_CANARIES))

    with app.test_request_context("/api/ask/pyme"):
        with _capture_app_logs(app, logging.WARNING) as collector:
            with patch(
                "routes.chat._next_pyme_ticket_number",
                return_value=7788,
            ), patch("routes.chat.db.session.add"), patch(
                "routes.chat.db.session.flush",
                side_effect=raw_error,
            ), patch("routes.chat.db.session.rollback"):
                ticket = chat_routes._persist_demo_pyme_ticket(
                    tenant=tenant,
                    owner_user=owner,
                    anon_id=f"anon-{SENSITIVE_CANARIES[1]}",
                    asunto="Pedido privado",
                    categoria="consulta",
                    pregunta=SENSITIVE_CANARIES[8],
                )

    assert ticket is None
    rendered = _render_records(collector)
    _assert_canaries_absent(rendered)
    assert "error_type=SQLAlchemyError" in rendered
