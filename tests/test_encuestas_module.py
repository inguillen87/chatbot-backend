import hashlib
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any

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
    list_respuestas,
    serialize_respuesta,
    delete_encuesta,
    list_encuestas,
)
from services.encuestas_analytics_service import get_summary
from services.encuestas_anchor_service import compute_content_hash, build_snapshot

# Make sure the feature flag is enabled during tests so helper utilities remain active.
import config.feature_flags as feature_flags
import services.encuestas_service as encuestas_service_module

feature_flags.FEATURE_ENCUESTAS = True
app_module = sys.modules.get("app")
if app_module is not None:
    app_module.FEATURE_ENCUESTAS = True


class DummyUser:
    def __init__(self, tenant_id: int = 1):
        self.id = None
        self.municipio_id = tenant_id


def _create_active_encuesta(politica_unicidad: str = "libre", tenant_id: int = 1):
    user = DummyUser(tenant_id)
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


def _respuesta_payload(
    encuesta: EncEncuesta,
    texto: str = "Todo bien",
    opcion_index: int = 0,
    **extra: Any,
):
    pregunta_opcion = next(p for p in encuesta.preguntas if p.tipo == "opcion_unica")
    pregunta_abierta = next(p for p in encuesta.preguntas if p.tipo == "abierta")
    opcion = pregunta_opcion.opciones[opcion_index]
    payload = {
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
    payload.update(extra)
    return payload


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


def test_respuesta_por_ip_sin_datos_no_bloquea(client):
    with client.application.app_context():
        encuesta, slug, _ = _create_active_encuesta(politica_unicidad="por_ip")
        payload = _respuesta_payload(encuesta)

        ctx_sin_ip = {"ip": None, "user_agent": "pytest", "anon_id": "anon-a", "canal": "web"}
        primera = save_respuesta(slug, payload, ctx_sin_ip)
        assert primera.id is not None

        # Si no tenemos IP disponible, no debemos generar la misma huella y bloquear respuestas posteriores.
        ctx_sin_ip_otro = {"ip": None, "user_agent": "pytest", "anon_id": "anon-b", "canal": "web"}
        segunda = save_respuesta(slug, payload, ctx_sin_ip_otro)
        assert segunda.id is not None


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


def test_list_respuestas_paginadas_y_serializadas(client):
    with client.application.app_context():
        encuesta, slug, user = _create_active_encuesta()
        base_time = datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc)

        generos = ["femenino", "masculino", "no_binario"]
        for idx in range(3):
            payload = _respuesta_payload(
                encuesta,
                texto=f"Comentario {idx}",
                genero=generos[idx % len(generos)],
                edad=25 + idx,
                barrio="Centro" if idx % 2 == 0 else "Sur",
                ciudad="Junín",
                provincia="Buenos Aires",
                pais="Argentina",
            )
            respuesta = save_respuesta(slug, payload, _request_ctx(f"anon-{idx}"))
            respuesta.submitted_at = base_time + timedelta(minutes=idx)
        db.session.commit()

        encuesta_obj, primeras, total, limit_value, offset_value = list_respuestas(
            encuesta.id,
            user,
            limit=2,
            offset=0,
        )

        assert encuesta_obj.id == encuesta.id
        assert total == 3
        assert limit_value == 2
        assert offset_value == 0
        assert len(primeras) == 2
        assert primeras[0].submitted_at >= primeras[1].submitted_at

        serializadas = [serialize_respuesta(r) for r in primeras]
        assert all("detalles" in item for item in serializadas)
        assert any(detalle.get("texto_libre") for detalle in serializadas[0]["detalles"])
        assert all(item.get("genero") for item in serializadas)
        assert all(item.get("barrio") for item in serializadas)

        _, restantes, _, _, offset_dos = list_respuestas(
            encuesta.id,
            user,
            limit=2,
            offset=2,
        )

        assert offset_dos == 2
        assert len(restantes) == 1


def test_get_summary_returns_metrics(client):
    with client.application.app_context():
        encuesta, slug, user = _create_active_encuesta()

        payload_a = _respuesta_payload(
            encuesta,
            texto="Comentario A",
            opcion_index=0,
            genero="femenino",
            edad=29,
            barrio="Centro",
            ciudad="Junín",
            provincia="Buenos Aires",
            pais="Argentina",
        )
        payload_a["phone"] = "+541111"
        save_respuesta(slug, payload_a, _request_ctx("anon-a"))

        payload_b = _respuesta_payload(
            encuesta,
            texto="Comentario B",
            opcion_index=1,
            genero="masculino",
            anio_nacimiento=1988,
            barrio="Sur",
            ciudad="Junín",
            provincia="Buenos Aires",
            pais="Argentina",
        )
        payload_b["phone"] = "+542222"
        save_respuesta(slug, payload_b, _request_ctx("anon-b"))

        incompleta = EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
            submitted_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
            canal="web",
            ip="10.0.0.5",
        )
        db.session.add(incompleta)
        db.session.commit()

        preguntas_ids = [preg.id for preg in encuesta.preguntas]
        resumen = get_summary(encuesta.id)

    assert resumen["total_respuestas"] == 3
    assert resumen["participantes_unicos"] == 3
    assert resumen["respuestas_completas"] == 2
    assert resumen["respuestas_incompletas"] == 1
    assert resumen["tasa_completitud"] == pytest.approx(66.67, rel=1e-2)
    assert len(resumen["preguntas"]) == len(preguntas_ids)
    assert set(item["pregunta_id"] for item in resumen["preguntas"]) == set(preguntas_ids)
    assert all(item["total_respuestas"] == 3 for item in resumen["preguntas"])
    demografia = resumen["demografia"]
    assert demografia["genero"]["femenino"] == 1
    assert demografia["genero"]["masculino"] == 1
    assert demografia["edad"]["muestra"] == 2
    assert demografia["edad"]["promedio"] is not None
    assert demografia["edad"]["promedio"] >= 29
    assert any(entry["label"] == "Centro" for entry in demografia["territorio"]["barrios"])


def test_delete_encuesta_elimina_respuestas_y_salta_bootstrap(client):
    with client.application.app_context():
        tenant_id = encuestas_service_module._BOOTSTRAP_TENANT_ID or 4
        encuesta, slug, user = _create_active_encuesta(tenant_id=tenant_id)
        save_respuesta(slug, _respuesta_payload(encuesta), _request_ctx("anon-del"))

        delete_encuesta(encuesta.id, user)

        assert db.session.get(EncEncuesta, encuesta.id) is None
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0

        restantes = list_encuestas(tenant_id)

    assert all(enc.tenant_id == tenant_id for enc in restantes)
    assert not restantes


def test_create_encuesta_generates_unique_slug(client):
    with client.application.app_context():
        user = DummyUser(tenant_id=7)
        base_payload = {
            "slug": "junin-participa",
            "titulo": "Participación Ciudadana Junín 2025",
            "descripcion": "Encuesta para validar manejo de slugs",
            "tipo": "opinion",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "¿Te interesa participar?",
                    "opciones": [
                        {"orden": 1, "texto": "Sí"},
                        {"orden": 2, "texto": "No"},
                    ],
                }
            ],
        }

        primera = create_encuesta(dict(base_payload), user)
        segunda = create_encuesta(dict(base_payload), user)

        assert primera.slug == "junin-participa"
        assert segunda.slug.startswith("junin-participa-")
        assert segunda.slug != primera.slug
