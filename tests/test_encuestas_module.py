import hashlib
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

# Provide a lightweight QRCode stub so tests don't require the optional dependency.
if "qrcode" not in sys.modules:
    class _FakeImage:
        def __init__(self):
            self._size = (1, 1)

        def resize(self, size):
            self._size = size
            return self

        def save(self, buffer, format="PNG"):
            buffer.write(b"fake-png")

    class _FakeQRCode:
        def __init__(self, border=1, box_size=10, error_correction=None):
            self.border = border
            self.box_size = box_size
            self.error_correction = error_correction

        def add_data(self, data):
            self._data = data

        def make(self, fit=True):
            return True

        def make_image(self, fill_color="black", back_color="white"):
            return _FakeImage()

    sys.modules["qrcode"] = types.SimpleNamespace(
        QRCode=_FakeQRCode,
        constants=types.SimpleNamespace(ERROR_CORRECT_M=1),
    )

import models  # noqa: F401  # ensure models are registered
from database import db
from models import EncEncuesta, EncRespuesta
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    publicar_encuesta,
    save_respuesta,
)
from services.encuestas_anchor_service import compute_content_hash, build_snapshot

# Make sure the feature flag is enabled during tests so helper utilities remain active.
import config.feature_flags as feature_flags

feature_flags.FEATURE_ENCUESTAS = True
app_module = sys.modules.get("app")
if app_module is not None:
    app_module.FEATURE_ENCUESTAS = True


class DummyUser:
    def __init__(self, tenant_id: int = 1):
        self.id = None
        self.municipio_id = tenant_id


def _create_active_encuesta(politica_unicidad: str = "libre"):
    user = DummyUser()
    payload = {
        "titulo": "Encuesta Test",
        "descripcion": "Prueba de módulo de encuestas",
        "tipo": "opinion",
        "requiere_identidad": False,
        "politica_unicidad": politica_unicidad,
        "anonimo_permitido": True,
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "¿Participaste de la reunión barrial?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Sí"},
                    {"orden": 2, "texto": "No"},
                ],
            },
            {
                "orden": 2,
                "tipo": "abierta",
                "texto": "Comentarios",
                "obligatoria": False,
            },
        ],
    }

    encuesta = create_encuesta(payload, user)
    encuesta, link = publicar_encuesta(encuesta.id, user)
    db.session.refresh(link)
    encuesta = db.session.get(EncEncuesta, encuesta.id)
    encuesta.inicio_at = None
    encuesta.fin_at = None
    db.session.commit()
    return encuesta, link.slug_publico, user


def _respuesta_payload(encuesta: EncEncuesta, texto: str = "Todo bien", opcion_index: int = 0):
    pregunta_opcion = next(p for p in encuesta.preguntas if p.tipo == "opcion_unica")
    pregunta_abierta = next(p for p in encuesta.preguntas if p.tipo == "abierta")
    opcion = pregunta_opcion.opciones[opcion_index]
    return {
        "dni": None,
        "phone": None,
        "respuestas": [
            {"pregunta_id": pregunta_opcion.id, "opcion_ids": [opcion.id]},
            {"pregunta_id": pregunta_abierta.id, "texto_libre": texto},
        ],
        "utm_source": "qr",
        "utm_campaign": "test",
        "canal": "qr",
    }


def _request_ctx(anon: str) -> dict:
    return {
        "ip": "1.1.1.1",
        "user_agent": "pytest",
        "anon_id": anon,
        "canal": "qr",
    }


def test_respuesta_unica_por_cookie(client):
    with client.application.app_context():
        encuesta, slug, _ = _create_active_encuesta(politica_unicidad="por_cookie")
        payload = _respuesta_payload(encuesta)

        primera = save_respuesta(slug, payload, _request_ctx("anon-1"))
        assert primera.id is not None

        with pytest.raises(EncuestaError) as error:
            save_respuesta(slug, payload, _request_ctx("anon-1"))
        assert error.value.status_code == 409
        assert "Ya registramos" in error.value.message or "Respuesta duplicada" in error.value.message


def test_compute_content_hash_es_deterministico(client):
    with client.application.app_context():
        encuesta, slug, _ = _create_active_encuesta()
        payload = _respuesta_payload(encuesta, texto="Respuesta inicial")
        respuesta = save_respuesta(slug, payload, _request_ctx("anon-uniq"))

        primer_hash = compute_content_hash(respuesta.id)
        assert primer_hash

        db.session.expire_all()
        segundo_hash = compute_content_hash(respuesta.id)
        assert segundo_hash == primer_hash

        almacenada = db.session.get(EncRespuesta, respuesta.id)
        assert almacenada.content_hash == primer_hash


def _manual_merkle(hashes):
    level = list(hashes)
    while len(level) > 1:
        siguiente = []
        for idx in range(0, len(level), 2):
            izquierdo = level[idx]
            derecho = level[idx + 1] if idx + 1 < len(level) else level[idx]
            combinado = hashlib.sha256((izquierdo + derecho).encode("utf-8")).hexdigest()
            siguiente.append(combinado)
        level = siguiente
    return level[0]


def test_merkle_snapshot_root_consistente(client):
    with client.application.app_context():
        encuesta, slug, user = _create_active_encuesta()
        base_time = datetime(2025, 2, 1, 12, 0, tzinfo=timezone.utc)

        for idx in range(3):
            payload = _respuesta_payload(encuesta, texto=f"Comentario {idx}", opcion_index=idx % 2)
            respuesta = save_respuesta(slug, payload, _request_ctx(f"anon-{idx}"))
            respuesta.submitted_at = base_time + timedelta(minutes=idx)
        db.session.commit()

        desde = (base_time - timedelta(minutes=1)).isoformat()
        hasta = (base_time + timedelta(minutes=3)).isoformat()
        snapshot = build_snapshot(encuesta.id, desde, hasta, user)

        ordenadas = (
            EncRespuesta.query.filter_by(encuesta_id=encuesta.id)
            .order_by(EncRespuesta.submitted_at.asc())
            .all()
        )
        hashes = [resp.content_hash for resp in ordenadas]
        assert all(hashes), "Cada respuesta debe tener content_hash calculado"

        esperado = _manual_merkle(hashes)
        assert snapshot.root_hash == esperado
        assert snapshot.total_respuestas == len(hashes)
