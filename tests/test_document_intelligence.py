import io
from datetime import datetime, timedelta

import jwt
import pytest

from app import db
from models import CatalogoItem, CatalogUpload, TenantProfile, User


class TestDocumentIntelligencePreview:
    @pytest.fixture(autouse=True)
    def setup(self, client):
        self.client = client
        self.pyme_user = User(
            name="Test PYME",
            email="pyme@test.com",
            rol="admin",
        )
        self.pyme_user.set_password("password")
        db.session.add(self.pyme_user)
        db.session.commit()

        payload = {
            "user_id": self.pyme_user.id,
            "exp": datetime.utcnow() + timedelta(days=1),
        }
        token = jwt.encode(
            payload, self.client.application.config["SECRET_KEY"], algorithm="HS256"
        )
        self.auth_headers = {"Authorization": f"Bearer {token}"}

    def test_preview_csv_file(self):
        csv_content = "nombre,precio\nProducto A,10\nProducto B,20\n"

        response = self.client.post(
            f"/api/pymes/{self.pyme_user.id}/document-intelligence/preview",
            headers=self.auth_headers,
            data={"file": (io.BytesIO(csv_content.encode("utf-8")), "items.csv")},
            content_type="multipart/form-data",
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["pymeId"] == self.pyme_user.id
        assert payload["totalRows"] == 2
        assert [col["name"] for col in payload["columns"]] == ["nombre", "precio"]
        assert payload["rows"][0]["nombre"] == "Producto A"

    def test_legacy_commit_is_retired_without_catalog_or_upload_writes(self):
        tenant = TenantProfile(
            slug="document-intelligence-retired",
            nombre="Document Intelligence Retired",
            tipo="pyme",
            pyme_id=self.pyme_user.id,
            plan="full",
            is_active=True,
            configuracion={"catalog_vector_version": "catalog-previous-v1"},
        )
        db.session.add(tenant)
        db.session.flush()
        self.pyme_user.tenant_id = tenant.id
        existing = CatalogoItem(
            user_id=self.pyme_user.id,
            tenant_id=tenant.id,
            nombre="Producto anterior",
            sku="OLD-1",
            precio="900",
            modalidad="venta",
        )
        upload = CatalogUpload(
            tenant_id=tenant.id,
            filename="catalogo.csv",
            processor_slug="generic_v2",
            status="preview_ready",
            preview_data={"rows": [{"nombre": "Producto nuevo"}]},
        )
        db.session.add_all([existing, upload])
        db.session.commit()

        response = self.client.post(
            f"/api/pymes/{self.pyme_user.id}/document-intelligence/commit",
            headers=self.auth_headers,
            json={
                "catalogUploadId": upload.id,
                "replaceCatalog": True,
                "columns": ["nombre", "precio"],
                "rows": [{"nombre": "Producto nuevo", "precio": "1200"}],
            },
        )

        assert response.status_code == 410
        body = response.get_json()
        assert body["codigo"] == "document_intelligence_catalog_commit_retired"
        assert body["writes_performed"] is False
        assert body["replacement"]["create"] == "/api/admin/catalog/import"
        assert CatalogoItem.query.filter_by(tenant_id=tenant.id).count() == 1
        assert CatalogoItem.query.filter_by(tenant_id=tenant.id).one().sku == "OLD-1"
        db.session.refresh(upload)
        db.session.refresh(tenant)
        assert upload.status == "preview_ready"
        assert tenant.configuracion["catalog_vector_version"] == "catalog-previous-v1"
