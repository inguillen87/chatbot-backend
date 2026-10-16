import io
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import pytest
from werkzeug.datastructures import FileStorage

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "https://example.com")

from app import create_app
from config import TestConfig
from models import CatalogoItem, CatalogUpload, TenantProfile, User, db
from services.catalog.pipeline import CatalogPipeline as LegacyCatalogPipeline
from services.catalog_ingestion_assurance import (
    build_ingestion_assurance,
    extracted_stage,
    index_catalog_item_with_ack,
    persist_catalog_source,
    publish_tenant_catalog_snapshot,
)
from services.catalog_quality import _has_verified_catalog_retrieval
from services.qdrant_service import _catalog_qdrant_point_id, verify_catalog_item_index


def _verified_storage() -> dict:
    return {
        "status": "verified",
        "provider": "cloudflare_r2",
        "acknowledged": True,
        "receipt_ref": "r2:test-receipt",
        "size_bytes": 12,
    }


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _tenant(slug: str) -> tuple[User, TenantProfile]:
    owner = User(
        email=f"{slug}@test.local",
        name=f"Owner {slug}",
        rol="admin",
        tipo_chat="pyme",
        tenant_slug=slug,
    )
    owner.set_password("test-pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="pyme",
        pyme_id=owner.id,
        plan="full",
        is_active=True,
        configuracion={},
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.commit()
    return owner, tenant


def _headers(app, user: User, tenant: TenantProfile | None = None) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"user_id": user.id, "exp": now + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        headers["X-Tenant"] = tenant.slug
    return headers


def _ready_upload(tenant: TenantProfile, *, sku: str = "SKU-NEW") -> CatalogUpload:
    assurance = build_ingestion_assurance(
        stored=_verified_storage(),
        extracted=extracted_stage(1),
    )
    upload = CatalogUpload(
        tenant_id=tenant.id,
        filename="catalogo.csv",
        mime_type="text/csv",
        processor_slug="generic_v2",
        status="ready_to_commit",
        stats={"ingestion_assurance": assurance},
        preview_data={
            "items": [
                {
                    "sku": sku,
                    "title": "Producto nuevo",
                    "price": "1200",
                    "stock": 5,
                    "category": "General",
                }
            ]
        },
        warnings=[],
    )
    db.session.add(upload)
    db.session.commit()
    return upload


def test_r2_provider_without_ack_is_pending_and_stream_is_rewound(monkeypatch):
    storage = FileStorage(stream=io.BytesIO(b"catalog"), filename="catalogo.csv", content_type="text/csv")
    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.upload_to_gcs",
        lambda *args, **kwargs: None,
    )

    stage = persist_catalog_source(storage)

    assert stage == {
        "status": "pending",
        "acknowledged": False,
        "provider": "cloudflare_r2",
        "reason_code": "r2_provider_unavailable",
    }
    assert storage.stream.tell() == 0


def test_r2_positive_ack_returns_non_sensitive_receipt(monkeypatch):
    storage = FileStorage(stream=io.BytesIO(b"catalog"), filename="persona-123.csv", content_type="text/csv")
    observed_filename = None

    def upload_stub(provider_file, *args, **kwargs):
        nonlocal observed_filename
        observed_filename = provider_file.filename
        return {
            "unique_name": "opaque-provider-object",
            "public_url": "https://assets.invalid/private-object",
            "size": 7,
            "original_name": "catalog-source.csv",
        }

    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.upload_to_gcs",
        upload_stub,
    )

    stage = persist_catalog_source(storage)

    assert stage["status"] == "verified"
    assert stage["acknowledged"] is True
    assert stage["provider"] == "cloudflare_r2"
    assert stage["receipt_ref"].startswith("r2:")
    assert "public_url" not in stage
    assert "original_name" not in stage
    assert "persona-123" not in json.dumps(stage)
    assert observed_filename == "catalog-source.csv"


def test_qdrant_ack_requires_index_and_retrieval_receipts(monkeypatch):
    monkeypatch.setattr("services.qdrant_service.index_catalog_item", lambda *args, **kwargs: True)
    monkeypatch.setattr("services.qdrant_service.verify_catalog_item_index", lambda *args, **kwargs: True)

    result = index_catalog_item_with_ack(
        7,
        {"id": 10, "tenant_id": 7, "nombre": "Producto"},
        [0.1, 0.2],
    )

    assert result == {"indexed": True, "retrieval_verified": True, "reason_code": None}


def test_qdrant_readback_rejects_cross_tenant_payload(monkeypatch):
    class Record:
        payload = {
            "tenant_id": "8",
            "db_id": 10,
            "catalog_version": "catalog-v1",
        }

    class Client:
        def retrieve(self, **_kwargs):
            return [Record()]

    monkeypatch.setattr(
        "services.qdrant_service.get_qdrant_utils_client",
        lambda: Client(),
    )

    assert verify_catalog_item_index(
        "7",
        {
            "id": 10,
            "tenant_id": 7,
            "rubro": "general",
            "catalog_version": "catalog-v1",
        },
    ) is False


def test_qdrant_readback_accepts_only_matching_versioned_receipt(monkeypatch):
    requested_ids = []

    class Record:
        payload = {
            "tenant_id": 7,
            "db_id": 10,
            "catalog_version": "catalog-v1",
        }

    class Client:
        def retrieve(self, **kwargs):
            requested_ids.append(kwargs["ids"])
            return [Record()]

    monkeypatch.setattr(
        "services.qdrant_service.get_qdrant_utils_client",
        lambda: Client(),
    )

    assert verify_catalog_item_index(
        "7",
        {
            "id": 10,
            "tenant_id": 7,
            "rubro": "general",
            "catalog_version": "catalog-v1",
        },
    ) is True
    assert verify_catalog_item_index(
        "7",
        {
            "id": 10,
            "tenant_id": 7,
            "rubro": "general",
            "catalog_version": "catalog-other",
        },
    ) is False
    assert requested_ids[0] == [
        _catalog_qdrant_point_id(10, "7", "catalog-v1")
    ]
    assert requested_ids[1] == [
        _catalog_qdrant_point_id(10, "7", "catalog-other")
    ]


def test_qdrant_candidate_point_ids_are_isolated_by_catalog_version():
    first = _catalog_qdrant_point_id(10, "7", "catalog-v1")
    second = _catalog_qdrant_point_id(10, "7", "catalog-v2")

    assert first != second
    assert first == _catalog_qdrant_point_id(10, "7", "catalog-v1")


def test_quality_reports_retrieval_only_from_verified_provider_receipts():
    base_import = {
        "status": "committed",
        "stats": {
            "ingestion_assurance": {
                "stages": {
                    "indexed": {"status": "verified", "acknowledged": True},
                    "retrieval_verified": {"status": "pending", "acknowledged": False},
                }
            }
        },
    }

    assert _has_verified_catalog_retrieval([base_import]) is False

    base_import["stats"]["ingestion_assurance"]["stages"]["retrieval_verified"] = {
        "status": "verified",
        "acknowledged": True,
    }
    assert _has_verified_catalog_retrieval([base_import]) is True


def test_product_search_requires_exact_tenant_and_active_version(app, monkeypatch):
    from services import qdrant_search

    _owner, tenant = _tenant("catalog-search-scope")
    tenant.configuracion = {"catalog_vector_version": "catalog-active-v1"}
    db.session.commit()
    observed_filters = []

    class MatchValue:
        def __init__(self, *, value):
            self.value = value

    class FieldCondition:
        def __init__(self, *, key, match=None, range=None):
            self.key = key
            self.match = match
            self.range = range

    class Filter:
        def __init__(self, *, must):
            self.must = must

    fake_models = SimpleNamespace(
        MatchValue=MatchValue,
        FieldCondition=FieldCondition,
        Filter=Filter,
    )

    class Client:
        def search(self, **kwargs):
            observed_filters.append(kwargs["query_filter"])
            return []

    monkeypatch.setattr(qdrant_search, "qdrant_models", fake_models)
    monkeypatch.setattr(qdrant_search, "get_qdrant_client", lambda: Client())
    monkeypatch.setattr(
        qdrant_search,
        "verificar_y_crear_coleccion_qdrant",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(qdrant_search, "embed_textos", lambda *args, **kwargs: [[0.1, 0.2]])

    assert qdrant_search.buscar_catalogo_qdrant(
        user_id=None,
        tenant_id=tenant.id,
        pregunta="consulta",
    ) == []
    conditions = {
        condition.key: condition.match.value
        for condition in observed_filters[0].must
    }
    assert conditions["tenant_id"] == tenant.id
    assert conditions["catalog_version"] == "catalog-active-v1"

    observed_filters.clear()
    assert qdrant_search.buscar_catalogo_qdrant(
        user_id=None,
        tenant_id=None,
        pregunta="consulta",
    ) == []
    assert observed_filters == []


def test_database_fallback_is_tenant_scoped_and_has_no_fake_score(app):
    from services.qdrant_search import buscar_catalogo_db_fallback

    owner_a, tenant_a = _tenant("catalog-db-a")
    owner_b, tenant_b = _tenant("catalog-db-b")
    db.session.add_all(
        [
            CatalogoItem(
                user_id=owner_a.id,
                tenant_id=tenant_a.id,
                sku="A-1",
                nombre="Lampara accesible",
                modalidad="venta",
            ),
            CatalogoItem(
                user_id=owner_b.id,
                tenant_id=tenant_b.id,
                sku="B-1",
                nombre="Lampara privada",
                modalidad="venta",
            ),
        ]
    )
    db.session.commit()

    results = buscar_catalogo_db_fallback(
        user_id=None,
        tenant_id=tenant_a.id,
        pregunta="Lampara",
    )

    assert [result.payload["sku"] for result in results] == ["A-1"]
    assert results[0].score == 0.0
    assert results[0].payload["retrieval_source"] == "tenant_database_fallback"
    assert buscar_catalogo_db_fallback(
        user_id=None,
        tenant_id=None,
        pregunta="Lampara",
    ) == []


def test_active_import_requires_explicit_tenant_and_never_falls_back(app, client):
    owner, _tenant_a = _tenant("catalog-explicit")
    response = client.post(
        "/api/admin/catalog/import",
        data={"file": (io.BytesIO(b"sku,nombre\n1,A"), "catalogo.csv")},
        headers=_headers(app, owner),
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["codigo"] == "catalog_tenant_required"
    assert CatalogUpload.query.count() == 0


def test_active_import_isolation_denies_another_tenant(app, client):
    owner_a, _tenant_a = _tenant("catalog-a")
    _owner_b, tenant_b = _tenant("catalog-b")

    response = client.post(
        "/api/admin/catalog/import",
        data={
            "tenant_slug": tenant_b.slug,
            "file": (io.BytesIO(b"sku,nombre\n1,A"), "catalogo.csv"),
        },
        headers=_headers(app, owner_a, tenant_b),
        content_type="multipart/form-data",
    )

    assert response.status_code == 403
    assert response.get_json()["codigo"] == "catalog_tenant_scope_forbidden"
    assert CatalogUpload.query.count() == 0


def test_provider_unavailable_keeps_preview_pending_without_mock_job_or_citation(
    app,
    client,
):
    owner, tenant = _tenant("catalog-provider-pending")
    extraction = {
        "items": [{"sku": "REAL-1", "title": "Producto real", "price": 100}],
        "warnings": [],
        "confidence": 0.0,
        "engine": "xlsx_deterministic",
    }
    with patch("routes.catalog_import.persist_catalog_source", return_value={
        "status": "pending",
        "provider": "cloudflare_r2",
        "acknowledged": False,
        "reason_code": "r2_provider_unavailable",
    }), patch(
        "services.catalog_pipeline.CatalogPipeline.process_upload_preview",
        return_value=extraction,
    ):
        response = client.post(
            "/api/admin/catalog/import",
            data={
                "tenant_slug": tenant.slug,
                "file": (io.BytesIO(b"sku,nombre\nREAL-1,Producto real"), "catalogo.csv"),
            },
            headers=_headers(app, owner, tenant),
            content_type="multipart/form-data",
        )

    body = response.get_json()
    assert response.status_code == 202
    assert body["status"] == "storage_pending"
    assert body["data_status"] == "degraded"
    assert "job_id" not in body
    assert body["ingestion_assurance"]["ready"] is False
    assert body["ingestion_assurance"]["stages"]["stored"]["acknowledged"] is False
    serialized = json.dumps(body).lower()
    assert "citation" not in serialized
    assert "pdf-001" not in serialized


def test_commit_preserves_previous_catalog_when_qdrant_has_no_ack(app, client):
    owner, tenant = _tenant("catalog-preserve")
    tenant.configuracion = {"catalog_vector_version": "catalog-previous-v1"}
    previous = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        sku="OLD-1",
        nombre="Producto anterior",
        precio="900",
        modalidad="venta",
    )
    db.session.add(previous)
    db.session.commit()
    upload = _ready_upload(tenant)

    with patch("services.embedding_service.embed_textos_llm", return_value=[[0.1, 0.2]]), patch(
        "routes.catalog_import.index_catalog_item_with_ack",
        return_value={
            "indexed": False,
            "retrieval_verified": False,
            "reason_code": "qdrant_index_ack_missing",
        },
    ):
        response = client.post(
            f"/api/admin/catalog/import/{upload.id}/commit",
            json={"tenant_slug": tenant.slug, "mode": "replace"},
            headers=_headers(app, owner, tenant),
        )

    body = response.get_json()
    assert response.status_code == 503
    assert body["codigo"] == "qdrant_index_ack_missing"
    assert body["previous_catalog_preserved"] is True
    current = CatalogoItem.query.filter_by(tenant_id=tenant.id).all()
    assert [(item.sku, item.nombre) for item in current] == [("OLD-1", "Producto anterior")]
    db.session.refresh(upload)
    db.session.refresh(tenant)
    assert upload.status == "index_degraded"
    assert upload.stats["ingestion_assurance"]["ready"] is False
    assert tenant.configuracion["catalog_vector_version"] == "catalog-previous-v1"


def test_commit_never_starts_indexing_before_r2_ack(app, client):
    owner, tenant = _tenant("catalog-storage-gate")
    upload = _ready_upload(tenant)
    upload.stats = {
        "ingestion_assurance": build_ingestion_assurance(
            stored={
                "status": "pending",
                "provider": "cloudflare_r2",
                "acknowledged": False,
                "reason_code": "r2_ack_missing",
            },
            extracted=extracted_stage(1),
        )
    }
    db.session.commit()

    with patch("routes.catalog_import.index_catalog_item_with_ack") as provider:
        response = client.post(
            f"/api/admin/catalog/import/{upload.id}/commit",
            json={"tenant_slug": tenant.slug, "mode": "replace"},
            headers=_headers(app, owner, tenant),
        )

    assert response.status_code == 409
    assert response.get_json()["codigo"] == "catalog_storage_ack_required"
    provider.assert_not_called()
    assert CatalogoItem.query.filter_by(tenant_id=tenant.id).count() == 0


def test_commit_replaces_only_exact_tenant_after_positive_provider_ack(app, client):
    owner_a, tenant_a = _tenant("catalog-positive-a")
    owner_b, tenant_b = _tenant("catalog-positive-b")
    db.session.add_all(
        [
            CatalogoItem(
                user_id=owner_a.id,
                tenant_id=tenant_a.id,
                sku="OLD-A",
                nombre="Anterior A",
                modalidad="venta",
            ),
            CatalogoItem(
                user_id=owner_b.id,
                tenant_id=tenant_b.id,
                sku="KEEP-B",
                nombre="Conservar B",
                modalidad="venta",
            ),
        ]
    )
    upload = _ready_upload(tenant_a, sku="NEW-A")
    indexed_payloads = []

    def provider_ack(_tenant_id, item_data, _embedding):
        indexed_payloads.append(dict(item_data))
        return {"indexed": True, "retrieval_verified": True, "reason_code": None}

    with patch("services.embedding_service.embed_textos_llm", return_value=[[0.1, 0.2]]), patch(
        "routes.catalog_import.index_catalog_item_with_ack",
        side_effect=provider_ack,
    ):
        response = client.post(
            f"/api/admin/catalog/import/{upload.id}/commit",
            json={"tenant_slug": tenant_a.slug, "mode": "replace"},
            headers=_headers(app, owner_a, tenant_a),
        )
        replay = client.post(
            f"/api/admin/catalog/import/{upload.id}/commit",
            json={"tenant_slug": tenant_a.slug, "mode": "replace"},
            headers=_headers(app, owner_a, tenant_a),
        )

    body = response.get_json()
    assert response.status_code == 200
    assert body["ingestion_assurance"]["ready"] is True
    assert body["ingestion_assurance"]["stages"]["indexed"]["status"] == "verified"
    assert body["ingestion_assurance"]["stages"]["retrieval_verified"]["status"] == "verified"
    assert body["catalog_version"]
    assert len(indexed_payloads) == 1
    assert indexed_payloads[0]["tenant_id"] == tenant_a.id
    assert indexed_payloads[0]["catalog_version"] == body["catalog_version"]
    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replay"] is True
    assert replay.get_json()["catalog_version"] == body["catalog_version"]
    db.session.refresh(tenant_a)
    assert tenant_a.configuracion["catalog_vector_version"] == body["catalog_version"]
    assert [item.sku for item in CatalogoItem.query.filter_by(tenant_id=tenant_a.id).all()] == ["NEW-A"]
    assert [item.sku for item in CatalogoItem.query.filter_by(tenant_id=tenant_b.id).all()] == ["KEEP-B"]


def test_upsert_publishes_a_complete_versioned_snapshot(app, client):
    owner, tenant = _tenant("catalog-upsert-snapshot")
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            sku="KEEP-1",
            nombre="Producto existente",
            modalidad="venta",
        )
    )
    db.session.commit()
    upload = _ready_upload(tenant, sku="ADD-1")
    indexed_payloads = []

    def provider_ack(_tenant_id, item_data, _embedding):
        indexed_payloads.append(dict(item_data))
        return {"indexed": True, "retrieval_verified": True, "reason_code": None}

    with patch(
        "services.embedding_service.embed_textos_llm",
        return_value=[[0.1, 0.2]],
    ), patch(
        "routes.catalog_import.index_catalog_item_with_ack",
        side_effect=provider_ack,
    ):
        response = client.post(
            f"/api/admin/catalog/import/{upload.id}/commit",
            json={"tenant_slug": tenant.slug, "mode": "upsert"},
            headers=_headers(app, owner, tenant),
        )

    body = response.get_json()
    assert response.status_code == 200
    assert body["ingestion_assurance"]["stages"]["indexed"]["expected_count"] == 2
    assert {item["sku"] for item in indexed_payloads} == {"KEEP-1", "ADD-1"}
    assert {item["catalog_version"] for item in indexed_payloads} == {
        body["catalog_version"]
    }


def test_retired_fallback_endpoint_is_authenticated_tenant_scoped_and_fail_closed(app, client):
    owner, tenant = _tenant("catalog-legacy-disabled")
    response = client.post(
        "/api/catalog/upload",
        data={"tenant_slug": tenant.slug},
        headers=_headers(app, owner, tenant),
    )

    assert response.status_code == 410
    assert response.get_json()["codigo"] == "catalog_legacy_endpoint_disabled"
    assert CatalogUpload.query.count() == 0


def test_legacy_pdf_pipeline_never_fabricates_catalog_rows(monkeypatch, tmp_path):
    source = tmp_path / "catalogo.pdf"
    source.write_bytes(b"not-a-real-pdf")
    pipeline = LegacyCatalogPipeline()
    monkeypatch.setattr(pipeline, "_preview_from_processor", lambda *args, **kwargs: ([], []))

    result = pipeline.process_upload_preview(1, str(source), "application/pdf", "generic")

    assert result["items"] == []
    assert "PDF-001" not in json.dumps(result)
    assert "mock" not in json.dumps(result).lower()


def test_snapshot_publisher_requires_r2_then_indexes_full_tenant_version(app, monkeypatch):
    owner, tenant = _tenant("catalog-inline-snapshot")
    tenant.configuracion = {
        "catalog_version": "catalog-previous-v1",
        "catalog_vector_version": "catalog-previous-v1",
    }
    db.session.add_all(
        [
            CatalogoItem(
                user_id=owner.id,
                tenant_id=tenant.id,
                sku="ONE-1",
                nombre="Producto uno",
                categoria="General",
                precio="100",
                modalidad="venta",
                extra_metadata={
                    "gallery_urls": ["https://assets.invalid/one.jpg"],
                    "api_token": "must-not-leave-sql",
                },
            ),
            CatalogoItem(
                user_id=owner.id,
                tenant_id=tenant.id,
                sku="TWO-2",
                nombre="Producto dos",
                categoria="General",
                precio="200",
                modalidad="venta",
            ),
        ]
    )
    db.session.commit()
    order = []
    stored_snapshot = {}
    indexed_payloads = []

    def storage_ack(source):
        order.append("r2")
        stored_snapshot.update(json.loads(source.stream.read().decode("utf-8")))
        return _verified_storage()

    def provider_ack(scoped_tenant_id, item_data, embedding):
        order.append("qdrant")
        assert scoped_tenant_id == tenant.id
        assert embedding == [0.1, 0.2]
        indexed_payloads.append(dict(item_data))
        return {"indexed": True, "retrieval_verified": True, "reason_code": None}

    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.persist_catalog_source",
        storage_ack,
    )
    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.embed_textos_llm",
        lambda texts: [[0.1, 0.2] for _ in texts],
    )
    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.index_catalog_item_with_ack",
        provider_ack,
    )

    publication = publish_tenant_catalog_snapshot(tenant.id)
    db.session.commit()

    assert order == ["r2", "qdrant", "qdrant"]
    assert publication["ingestion_assurance"]["ready"] is True
    assert publication["item_count"] == 2
    assert {payload["tenant_id"] for payload in indexed_payloads} == {tenant.id}
    assert {payload["catalog_version"] for payload in indexed_payloads} == {
        publication["catalog_version"]
    }
    assert {payload["sku"] for payload in indexed_payloads} == {"ONE-1", "TWO-2"}
    assert "must-not-leave-sql" not in json.dumps(stored_snapshot)
    assert stored_snapshot["tenant_id"] == tenant.id
    assert len(stored_snapshot["items"]) == 2
    db.session.refresh(tenant)
    assert tenant.configuracion["catalog_version"] == publication["catalog_version"]
    assert tenant.configuracion["catalog_vector_version"] == publication["catalog_version"]
    assert tenant.configuracion["catalog_snapshot_receipt_ref"] == "r2:test-receipt"


def test_inline_edit_rolls_back_sql_and_pointer_when_qdrant_readback_fails(
    app,
    client,
    monkeypatch,
):
    owner, tenant = _tenant("catalog-inline-rollback")
    tenant.configuracion = {
        "catalog_version": "catalog-previous-v1",
        "catalog_vector_version": "catalog-previous-v1",
    }
    item = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        sku="KEEP-1",
        nombre="Producto anterior",
        precio="900",
        cantidad="4",
        modalidad="venta",
    )
    db.session.add(item)
    db.session.commit()
    item_id = item.id

    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.persist_catalog_source",
        lambda source: _verified_storage(),
    )
    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.embed_textos_llm",
        lambda texts: [[0.1, 0.2] for _ in texts],
    )
    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.index_catalog_item_with_ack",
        lambda *args, **kwargs: {
            "indexed": True,
            "retrieval_verified": False,
            "reason_code": "qdrant_retrieval_ack_missing",
        },
    )

    response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/catalog/items/{item_id}",
        json={"stock_quantity": 18},
        headers=_headers(app, owner, tenant),
    )

    assert response.status_code == 503
    body = response.get_json()
    assert body["codigo"] == "qdrant_retrieval_ack_missing"
    assert body["previous_catalog_preserved"] is True
    assert body["ingestion_assurance"]["ready"] is False
    db.session.expire_all()
    preserved = CatalogoItem.query.filter_by(id=item_id, tenant_id=tenant.id).one()
    refreshed_tenant = db.session.get(TenantProfile, tenant.id)
    assert preserved.cantidad == "4"
    assert refreshed_tenant.configuracion["catalog_version"] == "catalog-previous-v1"
    assert refreshed_tenant.configuracion["catalog_vector_version"] == "catalog-previous-v1"


def test_inline_edit_never_indexes_before_r2_ack(app, client, monkeypatch):
    owner, tenant = _tenant("catalog-inline-r2-gate")
    tenant.configuracion = {"catalog_vector_version": "catalog-previous-v1"}
    item = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        sku="KEEP-1",
        nombre="Producto anterior",
        precio="900",
        cantidad="4",
        modalidad="venta",
    )
    db.session.add(item)
    db.session.commit()
    item_id = item.id
    provider_called = False

    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.persist_catalog_source",
        lambda source: {
            "status": "pending",
            "provider": "cloudflare_r2",
            "acknowledged": False,
            "reason_code": "r2_provider_unavailable",
        },
    )

    def unexpected_provider_call(*args, **kwargs):
        nonlocal provider_called
        provider_called = True
        return {"indexed": True, "retrieval_verified": True, "reason_code": None}

    monkeypatch.setattr(
        "services.catalog_ingestion_assurance.index_catalog_item_with_ack",
        unexpected_provider_call,
    )

    response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/catalog/items/{item_id}",
        json={"stock_quantity": 18},
        headers=_headers(app, owner, tenant),
    )

    assert response.status_code == 503
    assert response.get_json()["codigo"] == "r2_provider_unavailable"
    assert provider_called is False
    db.session.expire_all()
    assert db.session.get(CatalogoItem, item_id).cantidad == "4"
    assert db.session.get(TenantProfile, tenant.id).configuracion["catalog_vector_version"] == "catalog-previous-v1"
