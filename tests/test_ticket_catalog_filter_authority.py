"""Disposable local fixtures for CRM catalog filters and category authority."""

import unittest
from datetime import datetime, timedelta, timezone

from app import create_app, db
from config import Config
from models import CategoriaTicket, MunicipioTicket, TenantProfile, User
from services.auth_session_lifecycle import issue_token


class CatalogFilterConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class TicketCatalogFilterAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(CatalogFilterConfig)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()
        self.owner = User(
            name="Titular de prueba", email="catalog-filter@test.com",
            password_hash="local-fixture", rol="admin", tipo_chat="municipio",
            tenant_slug="catalog-filter",
        )
        db.session.add(self.owner)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="catalog-filter", nombre="Catálogo local", tipo="municipio",
            municipio_id=self.owner.id, plan="full",
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.owner.tenant_id = self.tenant.id
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _ticket(self, category, *, category_id=None):
        ticket = MunicipioTicket(
            tenant_id=self.tenant.id, municipio_id=self.owner.id,
            pregunta="Caso de prueba de lectura", categoria=category,
            categoria_id=category_id, estado="nuevo",
        )
        db.session.add(ticket)
        db.session.flush()
        return ticket

    def _get(self, filters=None, *, actor=None):
        actor = actor or self.owner
        token = issue_token({
            "user_id": actor.id, "rol": actor.rol,
            "tenant_id": self.tenant.id, "tenant_slug": self.tenant.slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        })
        return self.client.get(
            "/tickets", query_string={"include": "compact", "per_page": "100", **(filters or {})},
            headers={"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug},
        )

    def test_renamed_catalog_label_filters_the_same_records_as_list_and_facets(self):
        category = CategoriaTicket(tenant_id=self.tenant.id, nombre="Obras Públicas")
        db.session.add(category)
        db.session.flush()
        first = self._ticket("Nombre anterior", category_id=category.id)
        second = self._ticket("Otro nombre anterior", category_id=category.id)
        db.session.commit()

        response = self._get({"categoria": "obras públicas"})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual({ticket["id"] for ticket in payload["tickets"]}, {first.id, second.id})
        self.assertTrue(all(ticket["categoria"] == "Obras Públicas" for ticket in payload["tickets"]))
        self.assertEqual(payload["facets"]["categories"], [
            {"value": "Obras Públicas", "label": "Obras Públicas", "category_id": category.id, "count": 2},
        ])
        self.assertEqual(payload["facets"]["areas"], payload["facets"]["categories"])
        self.assertEqual(self._get({"categoria": "Nombre anterior"}).get_json()["tickets"], [])
        self.assertEqual(len(self._get({"categoria_id": category.id}).get_json()["tickets"]), 2)
        self.assertEqual(db.session.get(MunicipioTicket, first.id).categoria, "Nombre anterior")

    def test_foreign_catalog_id_is_not_a_filter_or_a_facet_authority(self):
        foreign_owner = User(name="Otro titular", email="catalog-foreign@test.com", password_hash="local-fixture", rol="admin")
        db.session.add(foreign_owner)
        db.session.flush()
        foreign_tenant = TenantProfile(slug="catalog-foreign", nombre="Otro tenant", tipo="pyme", pyme_id=foreign_owner.id)
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign_category = CategoriaTicket(tenant_id=foreign_tenant.id, nombre="Reservada del otro tenant")
        db.session.add(foreign_category)
        db.session.flush()
        local = self._ticket("Etiqueta local", category_id=foreign_category.id)
        db.session.commit()

        response = self._get({"categoria": "Etiqueta local"})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual([ticket["id"] for ticket in payload["tickets"]], [local.id])
        self.assertFalse(payload["tickets"][0]["category_authority"]["verified"])
        self.assertEqual(payload["facets"]["categories"], [
            {"value": "Etiqueta local", "label": "Etiqueta local", "count": 1},
        ])
        self.assertEqual(self._get({"categoria": foreign_category.nombre}).get_json()["tickets"], [])
        self.assertEqual(self._get({"categoria_id": foreign_category.id}).get_json()["tickets"], [])

    def test_luminarias_filter_uses_exact_legacy_aliases_without_substring_guessing(self):
        first = self._ticket("Alumbrado Publico")
        second = self._ticket("luminaria")
        other = self._ticket("Luminarias especiales")
        db.session.commit()

        response = self._get({"categoria": "Luminarias"})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual({ticket["id"] for ticket in payload["tickets"]}, {first.id, second.id})
        self.assertTrue(all(ticket["categoria"] == "Luminarias" for ticket in payload["tickets"]))
        facets = {item["value"]: item["count"] for item in payload["facets"]["categories"]}
        self.assertEqual(facets["Luminarias"], 2)
        self.assertEqual(facets["Luminarias especiales"], 1)
        self.assertEqual(self._get({"categoria": "Luminarias especiales"}).get_json()["tickets"][0]["id"], other.id)

    def test_catalog_filter_does_not_expand_employee_persisted_category_scope(self):
        category = CategoriaTicket(tenant_id=self.tenant.id, nombre="Categoría actual")
        db.session.add(category)
        db.session.flush()
        allowed = self._ticket("Categoría permitida original", category_id=category.id)
        self._ticket("Categoría restringida original", category_id=category.id)
        employee = User(
            name="Operador local", email="catalog-employee@test.com", password_hash="local-fixture",
            rol="empleado", es_empleado=True, tipo_chat="municipio",
            tenant_id=self.tenant.id, tenant_slug=self.tenant.slug,
            accesibilidad={"employee_scope": {"categorias": ["Categoría permitida original"]}},
        )
        db.session.add(employee)
        db.session.commit()

        response = self._get({"categoria": "Categoría actual"}, actor=employee)
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual([ticket["id"] for ticket in payload["tickets"]], [allowed.id])
        self.assertEqual(payload["facets"]["categories"][0]["count"], 1)
        self.assertEqual(payload["tickets"][0]["categoria"], "Categoría actual")
