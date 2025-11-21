import io
from datetime import datetime, timedelta

import jwt
import pytest

from app import db
from models import User


class TestDocumentIntelligencePreview:
    @pytest.fixture(autouse=True)
    def setup(self, client):
        self.client = client
        self.pyme_user = User(
            id=1,
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
