from services.contact_intake import infer_phone_from_anon_id, missing_contact_fields, resolve_contact_snapshot


def test_resolve_contact_snapshot_reuses_whatsapp_phone_and_profile_name():
    snapshot = resolve_contact_snapshot(
        datos={"email_detectado": "cliente@example.com"},
        profile_name="Marcelo",
        anon_id="whatsapp:+5492615554444",
    )

    assert snapshot["nombre"] == "Marcelo"
    assert snapshot["telefono"] == "+5492615554444"
    assert snapshot["email"] == "cliente@example.com"
    assert missing_contact_fields(snapshot) == []


def test_resolve_contact_snapshot_ignores_placeholder_email():
    snapshot = resolve_contact_snapshot(
        datos={
            "nombre_usuario_detectado": "Ana",
            "telefono_detectado": "+5492611231234",
            "email_detectado": "ana@whatsapp.chatboc.com",
        }
    )

    assert snapshot["nombre"] == "Ana"
    assert snapshot["telefono"] == "+5492611231234"
    assert snapshot["email"] is None
    assert missing_contact_fields(snapshot) == ["email"]


def test_infer_phone_from_prefixed_anon_id():
    assert infer_phone_from_anon_id("whatsapp_4_+5492617778888") == "+5492617778888"


def test_infer_phone_from_uuid_like_anon_id_returns_none():
    assert infer_phone_from_anon_id("550e8400-e29b-41d4-a716-446655440000") is None
