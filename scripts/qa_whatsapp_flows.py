import json
import os
import sys
import uuid
import base64
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("ENABLE_RUNTIME_SCHEMA_SYNC", "0")
os.environ.setdefault("ENABLE_RUNTIME_TENANT_INIT", "0")
os.environ.setdefault("STARTUP_RUNTIME_BOOTSTRAP", "0")
os.environ.setdefault("WHATSAPP_AUDIO_ENABLED", "0")

from twilio.request_validator import RequestValidator

from app import create_app
from models import ArchivoAdjunto, ChatSessionContext, MunicipioTicket, PymePedido, PymeTicket, WhatsappNumero


@dataclass
class WhatsappCase:
    label: str
    to_number: str
    from_number: str
    body: str
    extra: dict


class FakeTwilioMessages:
    def __init__(self):
        self.sent = []

    def create(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(sid=f"SM_FAKE_{len(self.sent)}")


class FakeTwilioClient:
    def __init__(self):
        self.messages = FakeTwilioMessages()


class FakeHttpResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


def _safe_print(label: str, payload=None) -> None:
    if payload is None:
        text = label
    elif isinstance(payload, str):
        text = f"{label} {payload}"
    else:
        text = f"{label} {json.dumps(payload, ensure_ascii=False, default=str)}"
    print(text.encode("ascii", "backslashreplace").decode("ascii"))


def _normalize(number: str) -> str:
    return "+" + "".join(ch for ch in number if ch.isdigit())


def _signed_headers(url: str, data: dict) -> dict:
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        raise RuntimeError("TWILIO_AUTH_TOKEN no esta configurado")
    signature = RequestValidator(token).compute_signature(url, data)
    return {"X-Twilio-Signature": signature}


def _twilio_form(case: WhatsappCase) -> dict:
    profile_name = case.extra.get("_ProfileName") or f"QA {case.label}"
    form = {
        "SmsMessageSid": f"SM{uuid.uuid4().hex[:30]}",
        "MessageSid": f"SM{uuid.uuid4().hex[:30]}",
        "AccountSid": os.environ.get("TWILIO_ACCOUNT_SID", "AC_LOCAL_TEST"),
        "From": f"whatsapp:{_normalize(case.from_number)}",
        "To": f"whatsapp:{_normalize(case.to_number)}",
        "Body": case.body,
        "ProfileName": profile_name,
        "NumMedia": "0",
    }
    form.update({k: v for k, v in case.extra.items() if not k.startswith("_")})
    return form


def _row_counts(session_id_prefix: str):
    return {
        "contexts": ChatSessionContext.query.filter(ChatSessionContext.chat_session_id.like(f"{session_id_prefix}%")).count(),
        "municipio_tickets": MunicipioTicket.query.count(),
        "pyme_tickets": PymeTicket.query.count(),
        "pyme_pedidos": PymePedido.query.count(),
        "adjuntos": ArchivoAdjunto.query.count(),
    }


def main():
    app = create_app()
    base_url = os.environ.get("QA_WEBHOOK_BASE_URL", "http://localhost")
    endpoint = f"{base_url.rstrip('/')}/webhook/whatsapp"
    fake_twilio = FakeTwilioClient()
    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    run_seed = f"{uuid.uuid4().int % 10000:04d}"
    junin_from = f"+54926155{run_seed}"
    junin_audio_from = f"+54926156{run_seed}"
    bodega_from = f"+54926157{run_seed}"
    junin_location_from = f"+54926158{run_seed}"

    cases = [
        WhatsappCase(
            label="junin_texto_reclamo",
            to_number="+17432643718",
            from_number=junin_from,
            body=f"Hola, soy QA Texto, email qa.texto@example.com, telefono {junin_from}. Quiero registrar un reclamo por luminaria apagada en Av San Martin 123, distrito Centro, Junin.",
            extra={"_ProfileName": "QA Junin Texto"},
        ),
        WhatsappCase(
            label="junin_imagen",
            to_number="+17432643718",
            from_number=junin_from,
            body="Adjunto foto del problema.",
            extra={
                "_ProfileName": "QA Junin Texto",
                "NumMedia": "1",
                "MediaUrl0": "https://media.local/qa-junin-luz.png",
                "MediaContentType0": "image/png",
                "MediaSid0": f"ME{uuid.uuid4().hex[:30]}",
            },
        ),
        WhatsappCase(
            label="junin_dni_reclamo",
            to_number="+17432643718",
            from_number=junin_from,
            body="Mi DNI es 30111222.",
            extra={"_ProfileName": "QA Junin Texto"},
        ),
        WhatsappCase(
            label="junin_confirmacion_reclamo",
            to_number="+17432643718",
            from_number=junin_from,
            body="1",
            extra={"_ProfileName": "QA Junin Texto"},
        ),
        WhatsappCase(
            label="junin_ubicacion_inicio",
            to_number="+17432643718",
            from_number=junin_location_from,
            body="Hola, quiero registrar un reclamo por luminaria apagada.",
            extra={"_ProfileName": "QA Junin Ubicacion"},
        ),
        WhatsappCase(
            label="junin_ubicacion_compartida",
            to_number="+17432643718",
            from_number=junin_location_from,
            body="Te comparto la ubicacion del reclamo.",
            extra={
                "_ProfileName": "QA Junin Ubicacion",
                "Latitude": "-34.5889",
                "Longitude": "-60.9462",
                "Address": "Av San Martin 123, Junin, Buenos Aires",
                "Label": "Alumbrado apagado",
            },
        ),
        WhatsappCase(
            label="junin_ubicacion_sin_foto",
            to_number="+17432643718",
            from_number=junin_location_from,
            body="No, omitir foto.",
            extra={"_ProfileName": "QA Junin Ubicacion"},
        ),
        WhatsappCase(
            label="junin_ubicacion_datos",
            to_number="+17432643718",
            from_number=junin_location_from,
            body=f"Soy QA Ubicacion, DNI 30222333, email qa.ubicacion@example.com, telefono {junin_location_from}.",
            extra={"_ProfileName": "QA Junin Ubicacion"},
        ),
        WhatsappCase(
            label="junin_ubicacion_confirmar",
            to_number="+17432643718",
            from_number=junin_location_from,
            body="1",
            extra={"_ProfileName": "QA Junin Ubicacion"},
        ),
        WhatsappCase(
            label="junin_audio",
            to_number="+17432643718",
            from_number=junin_audio_from,
            body="",
            extra={
                "_ProfileName": "QA Junin Audio",
                "NumMedia": "1",
                "MediaUrl0": "https://media.local/qa-junin-audio.ogg",
                "MediaContentType0": "audio/ogg",
                "MediaSid0": f"ME{uuid.uuid4().hex[:30]}",
            },
        ),
        WhatsappCase(
            label="junin_audio_sin_foto",
            to_number="+17432643718",
            from_number=junin_audio_from,
            body="No, omitir foto.",
            extra={"_ProfileName": "QA Junin Audio"},
        ),
        WhatsappCase(
            label="junin_audio_datos",
            to_number="+17432643718",
            from_number=junin_audio_from,
            body=f"Soy QA Audio, DNI 30333444, email qa.audio@example.com, telefono {junin_audio_from}.",
            extra={"_ProfileName": "QA Junin Audio"},
        ),
        WhatsappCase(
            label="junin_audio_confirmar",
            to_number="+17432643718",
            from_number=junin_audio_from,
            body="1",
            extra={"_ProfileName": "QA Junin Audio"},
        ),
        WhatsappCase(
            label="cuatro_fincas_pedido",
            to_number="+18564858589",
            from_number=bodega_from,
            body=f"Hola, quiero comprar 2 botellas de Malbec y 1 Cabernet. Soy QA Bodega, telefono {bodega_from}. Enviar a Godoy Cruz 456.",
            extra={"_ProfileName": "QA Bodega"},
        ),
        WhatsappCase(
            label="cuatro_fincas_confirmar",
            to_number="+18564858589",
            from_number=bodega_from,
            body="Confirmar pedido.",
            extra={"_ProfileName": "QA Bodega"},
        ),
    ]

    with app.app_context():
        for number in sorted({_normalize(case.to_number) for case in cases}):
            mapping = WhatsappNumero.query.filter_by(numero_whatsapp=number, is_active=True).first()
            if not mapping:
                raise RuntimeError(f"No hay mapping activo para {number}")
            _safe_print(
                "mapping",
                {
                    "numero": number,
                    "user_id": mapping.user_id,
                    "email": getattr(mapping.user, "email", None),
                    "tipo": getattr(mapping.user, "tipo_chat", None),
                },
            )

        before = _row_counts("whatsapp_")

        with app.test_client() as client, patch("routes.whatsapp_webhook.twilio_client", fake_twilio), patch(
            "routes.whatsapp_webhook.requests.get",
            return_value=FakeHttpResponse(png_1x1),
        ), patch(
            "services.audio_transcription_service.transcribe_audio_from_url",
            return_value=f"Audio transcripto: hay una luminaria apagada en Av San Martin 123, distrito Centro, Junin. Soy QA Audio, email qa.audio@example.com, telefono {junin_audio_from}.",
        ):
            for case in cases:
                data = _twilio_form(case)
                response = client.post(
                    "/webhook/whatsapp",
                    base_url=base_url,
                    data=data,
                    headers=_signed_headers(endpoint, data),
                )
                _safe_print(
                    "case",
                    {
                        "label": case.label,
                        "status": response.status_code,
                        "body": response.get_data(as_text=True)[:200],
                    },
                )

        after = _row_counts("whatsapp_")
        _safe_print("delta", {key: after[key] - before[key] for key in before})
        _safe_print("twilio_messages", json.dumps(fake_twilio.messages.sent, ensure_ascii=False, default=str)[:4000])


if __name__ == "__main__":
    main()
