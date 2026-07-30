from __future__ import annotations

from datetime import datetime, timezone

import pytest

from database import db
from models import EncEncuesta, User
from services.encuestas_anchor_service import (
    build_snapshot,
    compute_content_hash,
    generate_merkle_proof,
    list_snapshots,
    publish_snapshot,
    serialize_anchor_snapshot,
    simulate_snapshot_anchor,
)
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    publicar_encuesta,
    save_respuesta,
)
import config.feature_flags as feature_flags
import routes.encuestas_anchor as encuestas_anchor_routes


def _admin(email: str, tenant_id: int) -> User:
    user = User(
        email=email,
        name=email.split("@", 1)[0],
        rol="admin",
        municipio_id=tenant_id,
        tipo_chat="municipio",
    )
    user.set_password("anchor-test")
    db.session.add(user)
    db.session.commit()
    return user


def _headers(client, user: User) -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"email": user.email, "password": "anchor-test"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.get_json()['token']}"}


def _survey_with_response(user: User, suffix: str):
    survey = create_encuesta(
        {
            "titulo": f"Anchor {suffix}",
            "slug": f"anchor-{suffix}",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "Opcion",
                    "obligatoria": True,
                    "opciones": [
                        {"orden": 1, "texto": "A"},
                        {"orden": 2, "texto": "B"},
                    ],
                }
            ],
        },
        user,
    )
    survey, link = publicar_encuesta(survey.id, user)
    survey = db.session.get(EncEncuesta, survey.id)
    survey.inicio_at = None
    survey.fin_at = None
    db.session.commit()
    response = save_respuesta(
        link.slug_publico,
        {
            "respuestas": [
                {
                    "pregunta_id": survey.preguntas[0].id,
                    "opcion_ids": [survey.preguntas[0].opciones[0].id],
                }
            ]
        },
        {
            "ip": f"10.0.0.{survey.id}",
            "user_agent": "pytest",
            "anon_id": f"anchor-{suffix}",
            "canal": "web",
        },
    )
    snapshot = build_snapshot(
        survey.id,
        "2020-01-01T00:00:00Z",
        "2035-01-01T00:00:00Z",
        user,
    )
    return survey, response, snapshot


def test_service_requires_survey_and_tenant_scope_for_snapshot_operations(client):
    with client.application.app_context():
        owner_a = _admin("anchor-a@example.com", 701)
        owner_b = _admin("anchor-b@example.com", 702)
        survey_a, response_a, snapshot_a = _survey_with_response(owner_a, "tenant-a")
        other_survey_a, _, _ = _survey_with_response(owner_a, "same-tenant-other-survey")
        survey_b, _, _ = _survey_with_response(owner_b, "tenant-b")

        with pytest.raises(EncuestaError) as wrong_survey_error:
            simulate_snapshot_anchor(
                encuesta_id=other_survey_a.id,
                snapshot_id=snapshot_a.id,
                user=owner_a,
            )
        assert wrong_survey_error.value.status_code == 404

        with pytest.raises(EncuestaError) as response_scope_error:
            compute_content_hash(survey_b.id, response_a.id, owner_b)
        assert response_scope_error.value.status_code == 404

        with pytest.raises(EncuestaError) as publish_error:
            simulate_snapshot_anchor(
                encuesta_id=survey_b.id,
                snapshot_id=snapshot_a.id,
                user=owner_b,
            )
        assert publish_error.value.status_code == 404

        with pytest.raises(EncuestaError) as proof_error:
            generate_merkle_proof(
                encuesta_id=survey_b.id,
                snapshot_id=snapshot_a.id,
                respuesta_id=response_a.id,
                user=owner_b,
            )
        assert proof_error.value.status_code == 404


def test_simulation_is_idempotent_and_never_claims_publication_or_verification(client):
    with client.application.app_context():
        owner = _admin("anchor-truth@example.com", 703)
        survey, response, snapshot = _survey_with_response(owner, "truth")

        first = simulate_snapshot_anchor(
            encuesta_id=survey.id,
            snapshot_id=snapshot.id,
            user=owner,
            requested_chain="polygon",
        )
        first_tx_id = first.tx_id
        second = publish_snapshot(survey.id, snapshot.id, owner, chain="polygon")
        assert second.tx_id == first_tx_id

        serialized = serialize_anchor_snapshot(second)
        assert serialized["anchor_status"] == "simulated"
        assert serialized["stored_anchor_status"] == "simulated"
        assert serialized["tx_id"].startswith("SIM-")
        assert serialized["chain"] == "simulation:polygon"
        assert serialized["published"] is False
        assert serialized["externally_anchored"] is False
        assert serialized["externally_verified"] is False
        assert serialized["verification_status"] == "unverified"

        proof = generate_merkle_proof(
            encuesta_id=survey.id,
            snapshot_id=snapshot.id,
            respuesta_id=response.id,
            user=owner,
        )
        assert proof["included"] is True
        assert proof["local_proof_valid"] is True
        assert proof["valido"] is False
        assert proof["verified"] is False
        assert proof["externally_verified"] is False
        assert proof["verification_status"] == "local_only"


def test_legacy_sim_reference_is_downgraded_when_serialized(client):
    with client.application.app_context():
        owner = _admin("anchor-legacy@example.com", 704)
        survey, _, snapshot = _survey_with_response(owner, "legacy")
        snapshot.anchor_status = "published"
        snapshot.chain = "polygon"
        snapshot.tx_id = "SIM-legacy-reference"
        snapshot.anchor_at = datetime.now(timezone.utc)
        db.session.commit()

        serialized = serialize_anchor_snapshot(snapshot)
        listed = list_snapshots(survey.id, owner)["snapshots"][0]
        assert serialized["stored_anchor_status"] == "published"
        assert serialized["anchor_status"] == "simulated"
        assert serialized["published"] is False
        assert serialized["externally_verified"] is False
        assert listed["anchor_status"] == "simulated"

        snapshot.tx_id = "0xlegacy-unverified-reference"
        db.session.commit()
        unverified = serialize_anchor_snapshot(snapshot)
        assert unverified["is_simulated"] is False
        assert unverified["anchor_status"] == "unverified"
        assert unverified["published"] is False
        assert unverified["externally_anchored"] is False
        assert unverified["externally_verified"] is False
        with pytest.raises(EncuestaError) as unsafe_overwrite:
            simulate_snapshot_anchor(
                encuesta_id=survey.id,
                snapshot_id=snapshot.id,
                user=owner,
            )
        assert unsafe_overwrite.value.status_code == 409
        assert unsafe_overwrite.value.payload["reason_code"] == "anchor_external_state_requires_review"


def test_routes_scope_snapshot_and_keep_publish_alias_truthful(client, monkeypatch):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_anchor_routes, "FEATURE_ENCUESTAS", True)

    with client.application.app_context():
        owner_a = _admin("anchor-route-a@example.com", 705)
        owner_b = _admin("anchor-route-b@example.com", 706)
        survey_a, response_a, snapshot_a = _survey_with_response(owner_a, "route-a")
        survey_b, _, _ = _survey_with_response(owner_b, "route-b")
        owner_a_id = owner_a.id
        owner_b_id = owner_b.id
        survey_a_id = survey_a.id
        survey_b_id = survey_b.id
        snapshot_a_id = snapshot_a.id
        response_a_id = response_a.id

    owner_b = db.session.get(User, owner_b_id)
    headers_b = _headers(client, owner_b)

    cross_publish = client.post(
        f"/admin/encuestas/{survey_b_id}/publish/{snapshot_a_id}",
        json={"chain": "polygon"},
        headers=headers_b,
    )
    assert cross_publish.status_code == 404

    cross_simulate = client.post(
        f"/admin/encuestas/{survey_b_id}/simulate/{snapshot_a_id}",
        json={"chain": "polygon"},
        headers=headers_b,
    )
    assert cross_simulate.status_code == 404

    cross_verify = client.get(
        f"/admin/encuestas/{survey_b_id}/{snapshot_a_id}/verify",
        query_string={"respuesta_id": response_a_id},
        headers=headers_b,
    )
    assert cross_verify.status_code == 404

    with client.application.app_context():
        owner_a = db.session.get(User, owner_a_id)
    headers_a = _headers(client, owner_a)
    compatibility = client.post(
        f"/admin/encuestas/{survey_a_id}/publish/{snapshot_a_id}",
        json={"chain": "polygon"},
        headers=headers_a,
    )
    assert compatibility.status_code == 200
    payload = compatibility.get_json()
    assert payload["ok"] is True
    assert payload["operation"] == "local_simulation"
    assert payload["anchor_status"] == "simulated"
    assert payload["published"] is False
    assert payload["externally_verified"] is False

    verify = client.get(
        f"/admin/encuestas/{survey_a_id}/{snapshot_a_id}/verify",
        query_string={"respuesta_id": response_a_id},
        headers=headers_a,
    )
    assert verify.status_code == 200
    verification = verify.get_json()
    assert verification["included"] is True
    assert verification["local_proof_valid"] is True
    assert verification["valido"] is False
    assert verification["verified"] is False
    assert verification["externally_verified"] is False
