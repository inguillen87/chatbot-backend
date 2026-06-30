from datetime import datetime, timezone

from models import MunicipioTicket, TenantProfile, User, db
from services.ticket_ai_enrichment import build_ticket_ai_enrichment


class _Comment:
    comentario = "El vecino pregunta si pueden resolverlo hoy."


class _MunicipioTicket:
    id = 10
    tenant_id = 20
    asunto = "Reclamo por luminaria"
    categoria = "Luminaria"
    pregunta = "Hay una luz quemada y la esquina queda oscura."
    detalles = "Poste apagado hace dos dias."
    direccion = "San Martin 100"
    distrito = "Junin"
    foto_url_directa = None


def test_build_ticket_ai_enrichment_for_municipio(monkeypatch):
    monkeypatch.setattr(
        "services.ticket_ai_enrichment.build_reclamo_ai_enrichment",
        lambda text, categories=None: {
            "contract_version": "municipio.reclamo_ai_enrichment.v1",
            "crm_hints": {
                "risk_level": "medio",
                "requires_photo": True,
                "requires_exact_location": False,
                "requires_human_attention": False,
                "tags": ["Luminaria", "signal:servicio_interrumpido"],
            },
        },
    )

    result = build_ticket_ai_enrichment(
        _MunicipioTicket(),
        scope="municipio",
        comments=[_Comment()],
    )

    assert result["contract_version"] == "ticket.ai_enrichment.v1"
    assert result["ticket_id"] == 10
    assert result["tenant_id"] == 20
    assert result["source"]["comments_count"] == 1
    assert result["crm_hints"]["risk_level"] == "medio"
    assert result["advisory_policy"]["state_mutation_allowed"] is False
    assert result["state_mutation"]["applied"] is False
    assert result["persisted"] is False
    assert result["secret_values_exposed"] is False


def test_build_ticket_ai_enrichment_for_municipio_uses_local_fallback_without_hf(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", "0.60")
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE", "0.60")
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE", "0.60")
    monkeypatch.setenv("HUGGINGFACE_SENTIMENT_MIN_SCORE", "0.55")
    monkeypatch.setattr(
        "services.huggingface_inference_service.classify_zero_shot",
        lambda text, labels, multi_label=False: None,
    )

    class RiskTicket(_MunicipioTicket):
        pregunta = "Poste de luz por caer con cable suelto en la esquina de una escuela. Estoy preocupado."
        detalles = "Mando foto para que lo revisen urgente."
        direccion = "Don Bosco 55"

    result = build_ticket_ai_enrichment(RiskTicket(), scope="municipio")

    assert result["huggingface"]["category"]["provider"] == "deterministic_local_fallback"
    assert result["huggingface"]["priority"]["prioridad"] == "urgente"
    assert result["crm_hints"]["requires_human_attention"] is True
    assert result["crm_hints"]["requires_photo"] is True
    assert "signal:riesgo_personas" in result["crm_hints"]["tags"]
    assert result["state_mutation"]["applied"] is False
    assert result["persisted"] is False


def test_build_ticket_ai_enrichment_for_pyme_intent(monkeypatch):
    class PymeTicket:
        id = 11
        tenant_id = 21
        asunto = "Pedido"
        categoria = "Ventas"
        pregunta = "Quiero comprar dos cajas y consultar envio."
        detalles = ""
        direccion = ""
        foto_url_directa = None

    monkeypatch.setenv("HUGGINGFACE_PYME_INTENT_MIN_SCORE", "0.60")
    monkeypatch.setattr(
        "services.huggingface_inference_service.classify_zero_shot",
        lambda text, labels, multi_label=False: [
            {"label": "crear pedido", "score": 0.87},
            {"label": "consulta de pago", "score": 0.2},
        ],
    )

    result = build_ticket_ai_enrichment(PymeTicket(), scope="pyme")

    assert result["huggingface"]["intent"]["intent"] == "crear_pedido"
    assert result["huggingface"]["intent"]["threshold"] == 0.6
    assert result["huggingface"]["advisory_policy"]["mutates_operational_state"] is False
    assert result["crm_hints"]["suggested_queue"] == "crear_pedido"


def test_admin_ticket_ai_enrichment_endpoint(client, monkeypatch):
    owner = User(
        name="Municipio Junin",
        email="municipio-junin@example.com",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("admin")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="junin", nombre="Junin", tipo="municipio", municipio_id=owner.id)
    db.session.add(tenant)
    db.session.flush()
    ticket = MunicipioTicket(
        nro_ticket="123456",
        tenant_id=tenant.id,
        municipio_id=owner.id,
        pregunta="Hay una luminaria rota",
        asunto="Luminaria",
        categoria="Luminaria",
        consulta_pin="123456",
        fecha=datetime.now(timezone.utc),
    )
    db.session.add(ticket)
    db.session.commit()

    monkeypatch.setattr(
        "services.ticket_ai_enrichment.build_ticket_ai_enrichment",
        lambda ticket, scope, comments=None, tenant=None: {
            "contract_version": "ticket.ai_enrichment.v1",
            "ticket_id": ticket.id,
            "ticket_type": scope,
            "tenant_id": tenant.id if tenant else ticket.tenant_id,
            "crm_hints": {"risk_level": "medio"},
            "secret_values_exposed": False,
        },
    )

    response = client.post(
        f"/admin/tickets/{ticket.id}/ai-enrichment",
        json={"scope": "municipio"},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant.id)},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "ticket.ai_enrichment.v1"
    assert payload["ticket_id"] == ticket.id
    assert payload["crm_hints"]["risk_level"] == "medio"

    db.session.expire_all()
    assert MunicipioTicket.query.get(ticket.id).estado == "nuevo"

    fallback_response = client.post(
        f"/admin/tickets/{ticket.id + 9999}/ai-enrichment",
        json={"scope": "municipio", "nro_ticket": f"M-{ticket.nro_ticket}"},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant.id)},
    )

    assert fallback_response.status_code == 200
    fallback_payload = fallback_response.get_json()
    assert fallback_payload["ticket_id"] == ticket.id
    assert fallback_payload["ticket_type"] == "municipio"

    rejected = client.post(
        f"/admin/tickets/{ticket.id}/ai-enrichment",
        json={"scope": "municipio", "apply": True},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant.id)},
    )

    assert rejected.status_code == 400
    db.session.expire_all()
    assert MunicipioTicket.query.get(ticket.id).estado == "nuevo"
