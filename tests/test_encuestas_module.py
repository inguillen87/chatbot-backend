import hashlib
import json
import sys
import types
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

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
from models import EncEncuesta, EncLink, EncRespuesta, EncSegmento
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    publicar_encuesta,
    save_respuesta,
    list_respuestas,
    serialize_respuesta,
    serialize_public_encuesta,
    get_public_encuesta,
    delete_encuesta,
    list_encuestas,
    _build_mendoza_bootstrap_payload,
    _build_godoy_cruz_bootstrap_payload,
    build_template_draft_from_slug,
    seed_encuesta_respuestas_demo,
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


def test_bootstrap_templates_match_frontend_config():
    inicio = datetime(2025, 1, 1, tzinfo=timezone.utc)
    fin = inicio + timedelta(days=45)

    payloads = encuestas_service_module._build_junin_bootstrap_payload(inicio, fin)
    assert isinstance(payloads, list)
    assert len(payloads) >= 9

    servicios = next(
        template for template in payloads if template["slug"].startswith("servicios-publicos-junin")
    )
    assert servicios["titulo"] == "Encuesta sobre servicios públicos en Junín"
    assert servicios["anonimo_permitido"] is False
    assert servicios["requiere_identidad"] is True
    assert servicios["politica_unicidad"] == "por_dni_o_phone"
    assert servicios["inicio_at"] == inicio.isoformat()
    assert servicios["fin_at"] == fin.isoformat()
    assert "Servicios públicos" in servicios.get("tags", [])

    pregunta_multiple = next(
        pregunta for pregunta in servicios["preguntas"] if pregunta["tipo"] == "opcion_multiple"
    )
    assert pregunta_multiple.get("min_selecciones") == 1
    assert pregunta_multiple.get("max_selecciones") == 3
    assert any(opt["texto"] == "Recolección de residuos" for opt in pregunta_multiple["opciones"])

    for pregunta in servicios["preguntas"]:
        assert "{{municipality}}" not in pregunta["texto"]
        for opcion in pregunta.get("opciones", []):
            assert "{{municipality}}" not in opcion["texto"]

    sustentable = next(
        template for template in payloads if template["slug"].startswith("ciudad-sustentable-2025-junin")
    )
    assert sustentable["tipo"] == "planificacion"
    assert sustentable["politica_unicidad"] == "por_dni"
    assert any("reciclados" in pregunta["texto"].lower() for pregunta in sustentable["preguntas"])

    obras = next(
        template for template in payloads if template["slug"].startswith("obras-publicas-2025-junin")
    )
    assert obras["politica_unicidad"] == "por_phone"
    assert obras["requiere_identidad"] is True

    intencion = next(
        template
        for template in payloads
        if template["slug"].startswith("sondeo-intencion-voto-2025-junin")
    )
    assert intencion["tipo"] == "sondeo"
    assert len(intencion["preguntas"]) == 4
    location_question = intencion["preguntas"][0]
    assert any(
        opcion.get("valor") == "geo_autocomplete"
        for opcion in location_question.get("opciones", [])
    )
    contenido_intencion = intencion["preguntas"][1:]
    assert len(contenido_intencion) == 3
    opciones_voto = contenido_intencion[0]["opciones"]
    assert any(opt["texto"] == "Frente oficialista local" for opt in opciones_voto)

    pulso = next(
        template
        for template in payloads
        if template["slug"].startswith("pulso-economico-2025-junin")
    )
    assert pulso["anonimo_permitido"] is True
    assert pulso["requiere_identidad"] is False
    contenido_pulso = pulso["preguntas"][1:]
    assert any(pregunta["tipo"] == "abierta" for pregunta in contenido_pulso)

    agenda = next(
        template
        for template in payloads
        if template["slug"].startswith("agenda-gobierno-2026-junin")
    )
    assert agenda["tipo"] == "planificacion"
    assert agenda["politica_unicidad"] == "por_dni_o_phone"
    assert any("agenda" in tag.lower() or "planificación" in tag.lower() for tag in agenda.get("tags", []))

    san_martin_payloads = encuestas_service_module._build_san_martin_bootstrap_payload(inicio, fin)
    san_martin_servicios = next(template for template in san_martin_payloads if template["slug"].startswith("servicios-publicos-san-martin"))
    assert "San Martín" in san_martin_servicios["titulo"]

    rivadavia_payloads = encuestas_service_module._build_rivadavia_bootstrap_payload(inicio, fin)
    rivadavia_servicios = next(template for template in rivadavia_payloads if template["slug"].startswith("servicios-publicos-rivadavia"))
    assert "Rivadavia" in rivadavia_servicios["titulo"]

    mendoza_payloads = _build_mendoza_bootstrap_payload(inicio, fin)
    mendoza_servicios = next(template for template in mendoza_payloads if template["slug"].startswith("servicios-publicos-mendoza"))
    assert "Mendoza" in mendoza_servicios["titulo"]


    mendoza_tracking = next(
        template
        for template in mendoza_payloads
        if template["slug"].startswith("luis-petri-tracking-campana-mendoza")
    )
    assert mendoza_tracking["mostrar_resultados_envivo"] is True
    assert mendoza_tracking["permitir_comentarios"] is True
    assert mendoza_tracking.get("auto_seed_demo", {}).get("cantidad") == 260

    mendoza_votacion = next(
        template
        for template in mendoza_payloads
        if template["slug"].startswith("luis-petri-votacion-prioridades-mendoza")
    )
    assert mendoza_votacion["es_votacion_envivo"] is True

    godoy_cruz_payloads = _build_godoy_cruz_bootstrap_payload(inicio, fin)
    godoy_servicios = next(template for template in godoy_cruz_payloads if template["slug"].startswith("servicios-publicos-godoy-cruz"))
    assert "Godoy Cruz" in godoy_servicios["titulo"]

    profile_keys = {profile["key"] for profile in encuestas_service_module._BOOTSTRAP_PROFILES}
    assert {"junin", "san_martin", "rivadavia", "mendoza", "godoy_cruz", "lavalle"}.issubset(profile_keys)
    mendoza_profile = next(
        profile for profile in encuestas_service_module._BOOTSTRAP_PROFILES if profile["key"] == "mendoza"
    )
    assert "luis-petri-tracking-campana" in (mendoza_profile.get("template_slugs") or [])

    draft_payload = build_template_draft_from_slug("servicios-publicos", "Junín")
    auto_seed = draft_payload.get("auto_seed_demo")
    assert auto_seed is not None
    assert auto_seed["enabled"] is True
    assert auto_seed["cantidad"] == 100
    assert auto_seed["geo_profile_key"] == "junin"
    assert any(action["key"] == "demo_seed" for action in draft_payload.get("quick_actions", []))




def test_get_public_encuesta_refreshes_expired_bootstrap_window(client):
    with client.application.app_context():
        user = DummyUser(tenant_id=4)
        draft_payload = build_template_draft_from_slug("movilidad-y-transporte", "Junín")
        encuesta = create_encuesta(draft_payload, user)

        encuesta.estado = "publicada"
        encuesta.inicio_at = datetime.now(timezone.utc) - timedelta(days=30)
        encuesta.fin_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.session.add(encuesta)
        db.session.commit()

        fetched = get_public_encuesta(encuesta.slug)
        assert fetched.id == encuesta.id
        assert fetched.esta_activa() is True
        assert fetched.fin_at is not None
        fin_at = fetched.fin_at
        if fin_at.tzinfo is None:
            fin_at = fin_at.replace(tzinfo=timezone.utc)
        assert fin_at > datetime.now(timezone.utc)

def test_list_template_payloads_scope_all_returns_catalog(client):
    with client.application.app_context():
        payload = encuestas_service_module.list_template_payloads(tenant_id=4, scope="all")

    assert payload["templates"], "La respuesta estándar debe incluir plantillas"
    catalogo = payload.get("all_templates")
    assert catalogo, "Debe exponer un catálogo extendido"
    claves = {entrada.get("key") for entrada in catalogo}
    assert "junin" in claves
    assert "san_martin" in claves
    assert "lavalle" in claves
    assert any(entrada.get("templates") for entrada in catalogo)

    render = encuestas_service_module.list_template_catalog(municipality="Lavalle")
    assert render, "Debe renderizar plantillas para Lavalle"
    for template in render:
        demo_seed = template.get("demo_seed")
        assert demo_seed, "Cada plantilla debe exponer demo_seed"
        assert demo_seed["cantidad"] == 100
        assert demo_seed.get("geo_profile_key") == "lavalle"


def test_create_encuesta_auto_seed_demo_creates_responses(client):
    with client.application.app_context():
        user = DummyUser(tenant_id=4)
        draft_payload = build_template_draft_from_slug("servicios-publicos", "Junín")

        encuesta = create_encuesta(draft_payload, user)

        respuestas_count = EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count()
        assert respuestas_count == draft_payload["auto_seed_demo"]["cantidad"]

        barrios = {
            respuesta.barrio for respuesta in EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all()
        }
        assert barrios, "Las respuestas demo deben incluir barrios/distritos"

        segmento = EncSegmento.query.filter_by(encuesta_id=encuesta.id, clave="auto_seed_demo").first()
        assert segmento is not None
        seed_cfg = json.loads(segmento.valor)
        assert seed_cfg["cantidad"] == draft_payload["auto_seed_demo"]["cantidad"]
        assert seed_cfg["geo_profile_key"] == "junin"
        assert seed_cfg["municipality_label"] == "Junín"


def test_publicar_encuesta_auto_seed_when_no_responses(monkeypatch, client):
    with client.application.app_context():
        user = DummyUser(tenant_id=4)
        draft_payload = build_template_draft_from_slug("servicios-publicos", "Junín")

        calls = []

        def _fake_seed(encuesta_id, user_arg, cantidad, *, geo_profile_key=None, municipality_label=None):
            calls.append(
                {
                    "encuesta_id": encuesta_id,
                    "cantidad": cantidad,
                    "geo_profile_key": geo_profile_key,
                    "municipality_label": municipality_label,
                }
            )
            return {
                "encuesta_id": encuesta_id,
                "creadas": cantidad,
                "omitidas": 0,
                "objetivo": cantidad,
            }

        monkeypatch.setattr(encuestas_service_module, "seed_encuesta_respuestas_demo", _fake_seed)

        encuesta = create_encuesta(draft_payload, user)
        assert len(calls) == 1, "Debe invocar seed al crear la encuesta"
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0

        encuesta, _ = publicar_encuesta(encuesta.id, user)
        assert len(calls) == 2, "Debe invocar seed nuevamente al publicar"
        publish_call = calls[-1]
        assert publish_call["cantidad"] == draft_payload["auto_seed_demo"]["cantidad"]
        assert publish_call["geo_profile_key"] == "junin"
        assert publish_call["municipality_label"] == "Junín"


class DummyUser:
    def __init__(self, tenant_id: int = 1):
        self.id = None
        self.municipio_id = tenant_id


def _create_active_encuesta(
    politica_unicidad: str = "libre",
    tenant_id: int = 1,
    tags: Optional[Sequence[str]] = None,
):
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
    if tags:
        payload["tags"] = list(tags)

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


def test_save_respuesta_prefers_requested_tenant_for_shared_slug(client):
    with client.application.app_context():
        encuesta_tenant_4, slug_tenant_4, _ = _create_active_encuesta(tenant_id=4)
        encuesta_tenant_7, slug_tenant_7, _ = _create_active_encuesta(tenant_id=7)

        link_tenant_4 = EncLink.query.filter_by(encuesta_id=encuesta_tenant_4.id, slug_publico=slug_tenant_4).first()
        link_tenant_7 = EncLink.query.filter_by(encuesta_id=encuesta_tenant_7.id, slug_publico=slug_tenant_7).first()
        assert link_tenant_4 is not None
        assert link_tenant_7 is not None

        shared_slug = "votacion-en-vivo-rio-grande"
        link_tenant_4.slug_publico = shared_slug
        link_tenant_7.slug_publico = shared_slug
        db.session.commit()

        payload = _respuesta_payload(encuesta_tenant_4, texto="Respuesta tenant 4")
        respuesta = save_respuesta(
            shared_slug,
            payload,
            _request_ctx("tenant-aware-submit"),
            preferred_tenant_id=4,
        )

        assert respuesta.id is not None
        assert respuesta.encuesta_id == encuesta_tenant_4.id
        assert respuesta.tenant_id == encuesta_tenant_4.tenant_id


def test_save_respuesta_accepts_answers_aliases(client):
    with client.application.app_context():
        encuesta, slug, _ = _create_active_encuesta()
        pregunta_opcion = next(p for p in encuesta.preguntas if p.tipo == "opcion_unica")
        pregunta_abierta = next(p for p in encuesta.preguntas if p.tipo == "abierta")
        opcion = pregunta_opcion.opciones[0]

        payload = {
            "answers": [
                {"questionId": pregunta_opcion.id, "selectedOptionIds": [str(opcion.id)]},
                {"questionId": pregunta_abierta.id, "value": "Excelente"},
            ],
            "metadata": {"canal": "web"},
        }

        respuesta = save_respuesta(slug, payload, _request_ctx("alias"))
        assert respuesta.id is not None
        detalles = {(det.pregunta_id, det.opcion_id, det.texto_libre) for det in respuesta.detalles}
        assert (pregunta_opcion.id, opcion.id, None) in detalles
        assert any(det[0] == pregunta_abierta.id and det[2] == "Excelente" for det in detalles)


def test_save_respuesta_accepts_option_labels(client):
    with client.application.app_context():
        encuesta, slug, _ = _create_active_encuesta()
        pregunta_opcion = next(p for p in encuesta.preguntas if p.tipo == "opcion_unica")
        opcion = pregunta_opcion.opciones[1]

        payload = {
            "answers": [
                {"questionId": pregunta_opcion.id, "value": opcion.texto},
            ]
        }

        respuesta = save_respuesta(slug, payload, _request_ctx("alias-label"))
        assert respuesta.id is not None
        opcion_ids = [det.opcion_id for det in respuesta.detalles if det.opcion_id]
        assert opcion.id in opcion_ids


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


def test_respuesta_con_metadata_completa_geolocaliza(client):
    with client.application.app_context():
        encuesta, slug, _ = _create_active_encuesta(politica_unicidad="por_dni")
        pregunta_opcion = next(p for p in encuesta.preguntas if p.tipo == "opcion_unica")
        opcion = pregunta_opcion.opciones[0]
        pregunta_abierta = next(p for p in encuesta.preguntas if p.tipo == "abierta")

        submitted_at = datetime(2025, 4, 20, 14, 30, tzinfo=timezone.utc)

        payload = {
            "dni": "32165498",
            "respuestas": [
                {"pregunta_id": pregunta_opcion.id, "opcion_ids": [opcion.id]},
                {"pregunta_id": pregunta_abierta.id, "texto_libre": "Todo muy ordenado"},
            ],
            "metadata": {
                "canal": "qr",
                "submittedAt": submitted_at.isoformat(),
                "demographics": {
                    "genero": "Femenino",
                    "rangoEtario": "25-34",
                    "ubicacion": {
                        "lat": -34.5912,
                        "lng": -58.4103,
                        "ciudad": "Junín",
                        "barrio": "Centro",
                        "provincia": "Buenos Aires",
                        "pais": "Argentina",
                        "origen": "gps",
                        "precision": "gps",
                    },
                },
            },
        }

        respuesta = save_respuesta(slug, payload, _request_ctx("meta-demo"))

        assert respuesta.canal == "qr"
        assert respuesta.genero == "femenino"
        assert respuesta.rango_etario == "25-34"
        assert respuesta.lat == pytest.approx(-34.5912)
        assert respuesta.lng == pytest.approx(-58.4103)
        assert respuesta.barrio == "Centro"
        assert respuesta.ciudad == "Junín"
        assert respuesta.provincia == "Buenos Aires"
        assert respuesta.pais == "Argentina"
        assert respuesta.metadata_payload and respuesta.metadata_payload.get("demographics")
        assert respuesta.submitted_at is not None
        almacenada = respuesta.submitted_at
        if almacenada.tzinfo is None:
            almacenada = almacenada.replace(tzinfo=timezone.utc)
        else:
            almacenada = almacenada.astimezone(timezone.utc)
        assert almacenada == submitted_at

        serializado = serialize_respuesta(respuesta)
        assert serializado.get("metadata", {}).get("demographics")


def test_respuesta_metadata_invalida_no_rompe_guardado(client):
    with client.application.app_context():
        encuesta, slug, _ = _create_active_encuesta()
        payload = _respuesta_payload(encuesta, metadata=object())

        respuesta = save_respuesta(slug, payload, _request_ctx("meta-invalid"))

        assert respuesta.metadata_payload is None
        assert respuesta.lat is None
        assert respuesta.ciudad is None


def test_respuesta_metadata_ubicacion_incompleta(client):
    with client.application.app_context():
        encuesta, slug, _ = _create_active_encuesta()
        metadata = {
            "demographics": {
                "ubicacion": {
                    "lat": "",
                    "lng": None,
                    "ciudad": "Junín",
                }
            }
        }
        payload = _respuesta_payload(encuesta, metadata=metadata)

        respuesta = save_respuesta(slug, payload, _request_ctx("meta-ubicacion"))

        assert respuesta.lat is None
        assert respuesta.lng is None
        assert respuesta.ciudad == "Junín"
        assert respuesta.metadata_payload and respuesta.metadata_payload.get("demographics")


def test_serialize_public_encuesta_incluye_tags(client):
    with client.application.app_context():
        tags = ["Servicios públicos", "Infraestructura"]
        encuesta, slug, _ = _create_active_encuesta(tags=tags)
        data = serialize_public_encuesta(encuesta, slug)

        assert data.get("tags") == sorted(tags, key=lambda value: value.lower())


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

        encuesta = db.session.get(EncEncuesta, encuesta.id)
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
    tipos = {item["tipo"] for item in resumen["preguntas"]}
    assert "single_choice" in tipos
    assert "text" in tipos
    unica = next(item for item in resumen["preguntas"] if item["tipo"] == "single_choice")
    assert unica["series"], "Las preguntas de opción única deben exponer series para gráficos"
    opciones_map = {opt["texto"]: opt["conteo"] for opt in unica["opciones"]}
    series_map = {serie["label"]: serie["value"] for serie in unica["series"]}
    assert series_map == opciones_map
    assert all(opt["value"] == opt["conteo"] for opt in unica["opciones"])
    abierta = next(item for item in resumen["preguntas"] if item["tipo"] == "text")
    assert abierta["series"] == []
    assert any(item.get("tipo_interno") == "opcion_unica" for item in resumen["preguntas"])
    assert resumen["canales"]
    assert resumen["canales_map"]["qr"] == 2
    canales_labels = {entry["label"] for entry in resumen["canales"]}
    assert "qr" in canales_labels

    demografia = resumen["demografia"]
    assert demografia["genero_map"]["femenino"] == 1
    assert demografia["genero_map"]["masculino"] == 1
    generos_labels = {entry["label"] for entry in demografia["genero"]}
    assert {"femenino", "masculino"}.issubset(generos_labels)
    assert all(
        demografia["genero_map"][entry["label"]] == entry["value"]
        for entry in demografia["genero"]
    )
    assert demografia["edad"]["muestra"] == 2
    assert demografia["edad"]["promedio"] is not None
    assert demografia["edad"]["promedio"] >= 29
    territorio_map = demografia["territorio_map"]
    assert any(entry["label"] == "Centro" for entry in territorio_map["barrios"])
    territorio_sections = {section["key"]: section for section in demografia["territorio"]}
    assert territorio_sections["barrios"]["series"] == territorio_map["barrios"]
    assert {
        entry["label"] for entry in demografia["rango_etario"]
    } == set(demografia["rango_etario_map"].keys())
    assert all(
        demografia["rango_etario_map"][entry["label"]] == entry["value"]
        for entry in demografia["rango_etario"]
    )
    assert all(item.keys() >= {"label", "value"} for item in demografia["rango_etario"])


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



def test_get_encuesta_allows_access_when_tenant_profile_matches(client):
    with client.application.app_context():
        user = DummyUser(tenant_id=4)
        encuesta = create_encuesta(
            {
                "titulo": "Tenant profile access",
                "preguntas": [
                    {
                        "orden": 1,
                        "tipo": "opcion_unica",
                        "texto": "¿Acceso?",
                        "obligatoria": True,
                        "opciones": [{"orden": 1, "texto": "Sí"}],
                    }
                ],
            },
            user,
        )

        alt_user = DummyUser(tenant_id=999)
        from flask import g
        g.tenant_profile = SimpleNamespace(id=encuesta.tenant_id)

        loaded = encuestas_service_module.get_encuesta(encuesta.id, user=alt_user)
        assert loaded.id == encuesta.id


def test_get_public_encuesta_does_not_refresh_non_demo_surveys(client):
    with client.application.app_context():
        user = DummyUser(tenant_id=4)
        payload = {
            "titulo": "Encuesta Operativa Interna",
            "descripcion": "Cierre de campaña",
            "tipo": "opinion",
            "politica_unicidad": "libre",
            "anonimato": True,
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "¿Cómo calificás el servicio?",
                    "obligatoria": True,
                    "opciones": [
                        {"orden": 1, "texto": "Bueno"},
                        {"orden": 2, "texto": "Regular"},
                    ],
                }
            ],
        }
        encuesta = create_encuesta(payload, user)
        encuesta.estado = "publicada"
        encuesta.inicio_at = datetime.now(timezone.utc) - timedelta(days=20)
        encuesta.fin_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.session.add(encuesta)
        db.session.commit()

        with pytest.raises(EncuestaError) as exc_info:
            get_public_encuesta(encuesta.slug)

        assert exc_info.value.status_code == 403
        db.session.refresh(encuesta)
        fin_at = encuesta.fin_at
        if fin_at.tzinfo is None:
            fin_at = fin_at.replace(tzinfo=timezone.utc)
        assert fin_at < datetime.now(timezone.utc)


def test_get_public_encuesta_returns_reason_code_for_unpublished(client):
    with client.application.app_context():
        user = DummyUser(tenant_id=4)
        encuesta = create_encuesta(
            {
                "titulo": "Encuesta no publicada",
                "preguntas": [
                    {
                        "orden": 1,
                        "tipo": "opcion_unica",
                        "texto": "¿ok?",
                        "obligatoria": True,
                        "opciones": [{"orden": 1, "texto": "Sí"}],
                    }
                ],
            },
            user,
        )
        encuesta.estado = "borrador"
        db.session.add(encuesta)
        db.session.commit()

        with pytest.raises(EncuestaError) as exc_info:
            get_public_encuesta(encuesta.slug)

        assert exc_info.value.status_code == 403
        assert exc_info.value.to_dict().get("reason_code") == "survey_not_published"


def test_get_public_encuesta_returns_reason_code_for_window(client):
    with client.application.app_context():
        user = DummyUser(tenant_id=4)
        encuesta = create_encuesta(
            {
                "titulo": "Encuesta fuera de ventana",
                "preguntas": [
                    {
                        "orden": 1,
                        "tipo": "opcion_unica",
                        "texto": "¿ok?",
                        "obligatoria": True,
                        "opciones": [{"orden": 1, "texto": "Sí"}],
                    }
                ],
            },
            user,
        )
        encuesta.estado = "publicada"
        encuesta.inicio_at = datetime.now(timezone.utc) + timedelta(days=1)
        encuesta.fin_at = datetime.now(timezone.utc) + timedelta(days=10)
        db.session.add(encuesta)
        db.session.commit()

        with pytest.raises(EncuestaError) as exc_info:
            get_public_encuesta(encuesta.slug)

        assert exc_info.value.status_code == 403
        assert exc_info.value.to_dict().get("reason_code") == "survey_outside_active_window"
