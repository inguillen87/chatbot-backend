import pytest
from models import TenantProfile, User, MunicipioPost, OrderEvent, EncEncuesta, EncRespuesta, PointsTransaction
from app import db
from datetime import datetime, timezone

def test_full_flow(client):
    # 1. Create Owner User
    owner = User(name="Owner", email="owner@demo.com", rol="admin", tipo_chat="municipio")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    # 2. Create Tenant linked to Owner
    tenant = TenantProfile(
        slug="demo-flow",
        nombre="Demo Flow",
        tipo="municipio",
        municipio_id=owner.id
    )
    db.session.add(tenant)
    db.session.commit()

    # 3. Public API Check
    resp = client.get("/api/public/tenant?tenant_slug=demo-flow")
    print(f"DEBUG RESPONSE: {resp.data}")
    assert resp.status_code == 200
    assert resp.json['slug'] == "demo-flow"
    print("Step 3 Done")

    # 4. End User Registration
    reg_payload = {
        "email": "user@demo.com",
        "password": "password",
        "nombre": "User Demo",
        "tenant_slug": "demo-flow"
    }
    print("Step 4 Start")
    resp = client.post("/auth/register", json=reg_payload)
    print(f"Step 4 Resp: {resp.status_code} {resp.data}")
    assert resp.status_code == 201
    token = resp.json['token']

    # 5. Add Content (News) by Owner
    post = MunicipioPost(
        municipio_id=owner.id,
        titulo="Test News",
        descripcion="Body content",
        tipo_post="noticia",
        fecha_publicacion=datetime.now(timezone.utc)
    )
    db.session.add(post)
    db.session.commit()

    # 6. Portal API Check (Content)
    headers = {'Authorization': f'Bearer {token}'}
    resp = client.get(f"/api/v1/portal/demo-flow/content", headers=headers)
    assert resp.status_code == 200
    data = resp.json
    assert len(data['news']) > 0
    assert data['news'][0]['title'] == "Test News"
    assert "cover_url" in data['news'][0]

    # 7. Portal API Check (Orders - Empty)
    resp = client.get(f"/api/v1/portal/demo-flow/orders", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json, list)

    # 8. Create Order
    order_payload = {"items": [{"product_id": 1, "price": 100}], "total": 100}
    resp = client.post(f"/api/v1/portal/demo-flow/orders", headers=headers, json=order_payload)
    assert resp.status_code == 201
    order_id = resp.json["id"]
    assert resp.json["status_label"] == "Pendiente"

    # 9. Portal API Check (Orders - populated)
    resp = client.get(f"/api/v1/portal/demo-flow/orders", headers=headers)
    assert len(resp.json) == 1
    assert resp.json[0]["tracking"]["latest_event"]["type"] == "created"

    # 10. Portal API Check (Order detail + timeline)
    detail = client.get(f"/api/v1/portal/demo-flow/orders/{order_id}", headers=headers)
    assert detail.status_code == 200
    payload = detail.json
    assert payload["id"] == order_id
    assert payload["tracking"]["has_timeline"] is True
    assert payload["tracking"]["timeline"][0]["type"] == "created"

    created_event = OrderEvent.query.filter_by(market_order_id=order_id, type="created").first()
    assert created_event is not None

    user_id = User.query.filter_by(email="user@demo.com").first().id


    # 11. Seed points + survey response for unified history
    encuesta = EncEncuesta(tenant_id=tenant.id, slug="encuesta-demo-flow", titulo="Encuesta de Satisfacción", tipo="opinion", estado="publicada")
    db.session.add(encuesta)
    db.session.flush()
    db.session.add(EncRespuesta(encuesta_id=encuesta.id, tenant_id=tenant.id, user_id=user_id, canal="portal"))
    db.session.add(PointsTransaction(user_id=user_id, tenant_id=tenant.id, tipo="compra", delta=50, saldo_final=50, metadata_payload={"detalle": "Puntos por compra"}))
    db.session.commit()

    # 12. Benefits list
    benefits_resp = client.get(f"/api/v1/portal/demo-flow/benefits", headers=headers)
    assert benefits_resp.status_code == 200
    assert len(benefits_resp.json["benefits"]) == 3

    # 13. Redeem should fail with insufficient points for 10% OFF
    redeem_fail = client.post(
        f"/api/v1/portal/demo-flow/redeem",
        headers=headers,
        json={"benefit_id": "discount_10"},
    )
    assert redeem_fail.status_code == 400

    # Add enough points and redeem
    user_obj = User.query.get(user_id)
    user_obj.saldo_puntos = 850
    db.session.add(user_obj)
    db.session.add(PointsTransaction(user_id=user_id, tenant_id=tenant.id, tipo="bonus", delta=800, saldo_final=850))
    db.session.commit()

    redeem_ok = client.post(
        f"/api/v1/portal/demo-flow/redeem",
        headers=headers,
        json={"benefit_id": "discount_10"},
    )
    assert redeem_ok.status_code == 200
    assert redeem_ok.json["benefit"]["id"] == "discount_10"

    # 14. Redeem history
    redeems_resp = client.get(f"/api/v1/portal/demo-flow/redeems", headers=headers)
    assert redeems_resp.status_code == 200
    assert any(item.get("benefit_id") == "discount_10" for item in redeems_resp.json)

    # 15. Unified history
    history_resp = client.get(f"/api/v1/portal/demo-flow/history", headers=headers)
    assert history_resp.status_code == 200
    history = history_resp.json
    assert len(history["orders"]) == 1
    assert len(history["claims"]) == 0
    assert len(history["surveys"]) == 1
    timeline_types = {item["type"] for item in history["timeline"]}
    assert {"order", "points", "survey"}.issubset(timeline_types)
