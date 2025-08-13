import pytest
import json
from app import db
from models import User, CatalogMapping

class TestCatalogMappingsAPI:

    @pytest.fixture(autouse=True)
    def setup(self, client):
        """Setup for each test method."""
        self.client = client
        # Create a test pyme user with a token
        self.pyme_user = User(
            id=1,
            name="Test PYME",
            email="pyme@test.com",
            rol="admin",
            token="test-token-123"
        )
        self.pyme_user.set_password("password")
        db.session.add(self.pyme_user)
        db.session.commit()

        self.auth_headers = {
            'Authorization': f'Bearer {self.pyme_user.token}'
        }


    def test_get_all_mappings_empty(self):
        """Test getting all mappings when there are none."""
        response = self.client.get(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings',
            headers=self.auth_headers
        )
        assert response.status_code == 200
        assert response.json == []

    def test_create_and_get_mapping(self):
        """Test creating a new mapping and then fetching it."""
        # 1. Create a new mapping
        new_mapping_data = {
            "name": "Mi Mapeo de Excel",
            "mapping": {
                "sheetName": "Hoja1",
                "headerRow": 1,
                "columns": {
                    "nombre": "A",
                    "precio": "B",
                    "sku": "C"
                }
            }
        }
        response = self.client.post(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings',
            data=json.dumps(new_mapping_data),
            content_type='application/json',
            headers=self.auth_headers
        )
        assert response.status_code == 201
        created_mapping = response.json
        assert created_mapping['name'] == new_mapping_data['name']
        assert 'id' in created_mapping

        # 2. Get all mappings and check if the new one is there
        response = self.client.get(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings',
            headers=self.auth_headers
        )
        assert response.status_code == 200
        assert len(response.json) == 1
        assert response.json[0]['name'] == "Mi Mapeo de Excel"

        # 3. Get the single mapping by its ID
        mapping_id = created_mapping['id']
        response = self.client.get(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings/{mapping_id}',
            headers=self.auth_headers
        )
        assert response.status_code == 200
        assert response.json['id'] == mapping_id
        assert response.json['mapping']['columns']['nombre'] == 'A'

    def test_update_mapping(self):
        """Test updating an existing mapping."""
        # 1. Create a mapping first
        initial_data = {"name": "Mapeo Original", "mapping": {"columns": {"nombre": "Name"}}}
        create_response = self.client.post(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings',
            data=json.dumps(initial_data),
            content_type='application/json',
            headers=self.auth_headers
        )
        mapping_id = create_response.json['id']

        # 2. Update the mapping
        updated_data = {
            "name": "Mapeo Actualizado",
            "mapping": {"columns": {"nombre": "Product Name"}}
        }
        update_response = self.client.put(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings/{mapping_id}',
            data=json.dumps(updated_data),
            content_type='application/json',
            headers=self.auth_headers
        )
        assert update_response.status_code == 200
        assert update_response.json['name'] == "Mapeo Actualizado"
        assert update_response.json['mapping']['columns']['nombre'] == "Product Name"

    def test_delete_mapping(self):
        """Test deleting a mapping."""
        # 1. Create a mapping
        initial_data = {"name": "Mapeo para Borrar", "mapping": {}}
        create_response = self.client.post(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings',
            data=json.dumps(initial_data),
            content_type='application/json',
            headers=self.auth_headers
        )
        mapping_id = create_response.json['id']

        # 2. Delete the mapping
        delete_response = self.client.delete(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings/{mapping_id}',
            headers=self.auth_headers
        )
        assert delete_response.status_code == 204

        # 3. Verify it's gone
        get_response = self.client.get(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings/{mapping_id}',
            headers=self.auth_headers
        )
        assert get_response.status_code == 404

    def test_access_other_pyme_mapping_fails(self):
        """Test that a user cannot access mappings of another PYME."""
        # 1. Create a mapping for pyme_user (id=1)
        initial_data = {"name": "Mapeo de Pyme 1", "mapping": {}}
        create_response = self.client.post(
            f'/api/pymes/{self.pyme_user.id}/catalog-mappings',
            data=json.dumps(initial_data),
            content_type='application/json',
            headers=self.auth_headers
        )
        mapping_id = create_response.json['id']

        # 2. Try to access it as another pyme (id=2)
        other_pyme_id = 2
        get_response = self.client.get(
            f'/api/pymes/{other_pyme_id}/catalog-mappings/{mapping_id}',
            headers=self.auth_headers
        )
        # This test is not perfect because the decorator might fail first if the user is not pyme 2.
        # But the route logic itself should also prevent this.
        # The get_single_mapping checks if mapping['pymeId'] matches the URL pyme_id
        assert get_response.status_code == 404
