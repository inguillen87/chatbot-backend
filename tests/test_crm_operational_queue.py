import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, PymeTicket, TenantProfile, TenantTicket, User


class OperationalQueueTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    CRM_OPERATIONAL_QUEUE_CURSOR_SECRET = "operational-queue-test-secret"
    CRM_OPERATIONAL_QUEUE_CURSOR_TTL_SECONDS = 60


class CrmOperationalQueueTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(OperationalQueueTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = self._user("Admin Junin", "admin@junin.test", "admin", "junin")
        self.tenant = TenantProfile(
            slug="junin",
            nombre="Municipalidad de Junin",
            tipo="municipio",
            municipio_id=self.admin.id,
            plan="full",
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.admin.tenant_id = self.tenant.id
        db.session.commit()
        self._pyme_number = 1000

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _user(self, name, email, role, tenant_slug=None, **kwargs):
        user = User(
            name=name,
            email=email,
            password_hash="test-hash",
            rol=role,
            tenant_slug=tenant_slug,
            **kwargs,
        )
        db.session.add(user)
        db.session.flush()
        return user

    def _auth(self, actor=None, *, header_slug=None, token_slug=None):
        actor = actor or self.admin
        token = jwt.encode(
            {
                "user_id": actor.id,
                "rol": actor.rol,
                "tenant_slug": token_slug if token_slug is not None else actor.tenant_slug,
                "tenant_id": actor.tenant_id,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": header_slug or self.tenant.slug,
        }

    def _tenant_ticket(
        self,
        *,
        created_at,
        category="alumbrado",
        status="nuevo",
        assignee_id=None,
        sla_state=None,
        tenant_id=None,
    ):
        extra = {"title": f"Tenant {category}", "channel": "whatsapp"}
        if assignee_id is not None:
            extra["assignee_id"] = assignee_id
        if sla_state is not None:
            extra["sla_status"] = sla_state
        ticket = TenantTicket(
            tenant_id=tenant_id or self.tenant.id,
            user_id=self.admin.id,
            categoria=category,
            descripcion=f"Problema de {category}",
            estado=status,
            origen="whatsapp",
            datos_extra=extra,
            created_at=created_at,
            updated_at=created_at,
        )
        db.session.add(ticket)
        db.session.flush()
        return ticket

    def _municipio_ticket(
        self,
        *,
        created_at,
        category="arbolado",
        status="nuevo",
        assignee_id=None,
        tenant_id=None,
    ):
        ticket = MunicipioTicket(
            tenant_id=tenant_id or self.tenant.id,
            municipio_id=self.admin.id,
            pregunta=f"Problema de {category}",
            asunto=f"Municipio {category}",
            categoria=category,
            estado=status,
            asignado_a_id=assignee_id,
            nro_ticket=f"mun-{self.tenant.id}-{self._pyme_number}",
            canal_ingreso="whatsapp",
            fecha=created_at,
            ultima_actividad=created_at,
        )
        self._pyme_number += 1
        db.session.add(ticket)
        db.session.flush()
        return ticket

    def _pyme_ticket(
        self,
        *,
        created_at,
        category="soporte",
        status="nuevo",
        assignee_id=None,
        sla_state=None,
        tenant_id=None,
    ):
        self._pyme_number += 1
        extra = {"sla_status": sla_state} if sla_state is not None else {}
        ticket = PymeTicket(
            tenant_id=tenant_id or self.tenant.id,
            pregunta=f"Problema de {category}",
            asunto=f"Pyme {category}",
            categoria=category,
            estado=status,
            asignado_a_id=assignee_id,
            nro_ticket=self._pyme_number,
            fecha=created_at,
            datos_extra=extra,
        )
        db.session.add(ticket)
        db.session.flush()
        return ticket

    def _get(self, query="", *, actor=None, headers=None):
        suffix = f"?{query}" if query else ""
        return self.client.get(
            f"/api/v2/inbox/operational-queue{suffix}",
            headers=headers or self._auth(actor),
        )

    def test_contract_unifies_three_sources_without_id_collisions_or_pii(self):
        now = datetime(2026, 8, 2, 14, 0, tzinfo=timezone.utc)
        tenant_ticket = self._tenant_ticket(created_at=now)
        municipio_ticket = self._municipio_ticket(created_at=now)
        pyme_ticket = self._pyme_ticket(created_at=now)
        self._tenant_ticket(created_at=now, status="cerrado")
        db.session.commit()

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            response = self._get("limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "inbox.operational_queue.v1")
        self.assertEqual(payload["metric_contract"], "operations.queue_truth.v1")
        self.assertEqual(payload["grain"], "one_current_open_source_record")
        self.assertEqual(payload["state_consistency"], "created_at_anchored_live_state")
        self.assertEqual(
            payload["consistency"],
            {
                "creation_membership": "created_at_null_or_lte_as_of",
                "null_created_at": "included_ordered_last",
                "mutable_fields": "live_at_each_page_read",
                "historical_snapshot": False,
                "durable_revision": False,
            },
        )
        self.assertEqual(payload["tenant_slug"], "junin")
        self.assertEqual(
            payload["filters"],
            {
                "queue": "open",
                "sla": None,
                "age": None,
                "assignee": None,
                "source_model": None,
                "category": None,
            },
        )
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, private")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")

        expected_ids = [
            f"TenantTicket:{tenant_ticket.id}",
            f"MunicipioTicket:{municipio_ticket.id}",
            f"PymeTicket:{pyme_ticket.id}",
        ]
        self.assertEqual([item["queue_id"] for item in payload["items"]], expected_ids)
        self.assertEqual(
            [item["detail_endpoint"] for item in payload["items"]],
            [
                f"/api/v2/tickets/{tenant_ticket.id}",
                f"/api/v2/inbox/omnichannel/{municipio_ticket.id}?source_model=MunicipioTicket",
                f"/api/tickets/pyme/{pyme_ticket.id}",
            ],
        )
        required_fields = {
            "queue_id",
            "source_model",
            "source_id",
            "title",
            "status",
            "category",
            "channel",
            "priority",
            "assignee_id",
            "created_at",
            "updated_at",
            "age_bucket",
            "sla",
            "detail_endpoint",
        }
        for item in payload["items"]:
            self.assertEqual(set(item), required_fields)
            serialized = str(item).lower()
            self.assertNotIn("email", serialized)
            self.assertNotIn("telefono", serialized)
            self.assertNotIn("dni", serialized)
        self.assertNotIn("total", payload["page"])

    def test_null_created_at_is_included_ordered_last_and_future_rows_are_excluded(self):
        now = datetime(2026, 8, 2, 14, 30, tzinfo=timezone.utc)
        dated = self._tenant_ticket(created_at=now - timedelta(minutes=1))
        null_municipio = self._municipio_ticket(created_at=now - timedelta(minutes=2))
        null_pyme = self._pyme_ticket(created_at=now - timedelta(minutes=3))
        self._tenant_ticket(created_at=now + timedelta(seconds=1))
        db.session.flush()
        null_municipio.fecha = None
        null_pyme.fecha = None
        db.session.commit()

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            response = self._get("limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            payload["consistency"]["creation_membership"],
            "created_at_null_or_lte_as_of",
        )
        self.assertEqual(
            payload["consistency"]["null_created_at"],
            "included_ordered_last",
        )
        self.assertEqual(
            [item["queue_id"] for item in payload["items"]],
            [
                f"TenantTicket:{dated.id}",
                f"MunicipioTicket:{null_municipio.id}",
                f"PymeTicket:{null_pyme.id}",
            ],
        )
        for item in payload["items"][1:]:
            self.assertIsNone(item["created_at"])
            self.assertEqual(item["age_bucket"], "unknown")

    def test_legacy_title_and_assignee_are_canonicalized_before_filtering(self):
        now = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)
        legacy = self._tenant_ticket(created_at=now - timedelta(minutes=1))
        legacy.datos_extra = {
            "title": 12345,
            "channel": "whatsapp",
            "assignee_id": "legacy-not-an-id",
        }
        db.session.commit()

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            response = self._get("assignee=unassigned")

        self.assertEqual(response.status_code, 200)
        items = response.get_json()["items"]
        self.assertEqual([item["queue_id"] for item in items], [f"TenantTicket:{legacy.id}"])
        self.assertEqual(items[0]["title"], "12345")
        self.assertIsInstance(items[0]["title"], str)
        self.assertIsNone(items[0]["assignee_id"])

    def test_keyset_pagination_over_three_pages_has_no_duplicates_or_skips(self):
        tied_at = datetime(2026, 8, 2, 13, 30, tzinfo=timezone.utc)
        for _ in range(4):
            self._tenant_ticket(created_at=tied_at)
            self._municipio_ticket(created_at=tied_at)
            self._pyme_ticket(created_at=tied_at)
        db.session.commit()

        expected = [
            *(f"TenantTicket:{source_id}" for source_id in range(4, 0, -1)),
            *(f"MunicipioTicket:{source_id}" for source_id in range(4, 0, -1)),
            *(f"PymeTicket:{source_id}" for source_id in range(4, 0, -1)),
        ]
        collected = []
        cursor = None
        as_of = tied_at + timedelta(hours=1)
        page_count = 0
        while True:
            query = "limit=3"
            if cursor:
                query += f"&cursor={cursor}"
            with patch("services.crm_operational_queue._utc_now", return_value=as_of):
                response = self._get(query)
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            page_count += 1
            collected.extend(item["queue_id"] for item in payload["items"])
            cursor = payload["page"]["next_cursor"]
            if not payload["page"]["has_more"]:
                break

        self.assertGreater(page_count, 2)
        self.assertEqual(collected, expected)
        self.assertEqual(len(collected), len(set(collected)))

    def test_first_page_as_of_excludes_rows_created_later_from_continuation(self):
        now = datetime(2026, 8, 2, 16, 0, tzinfo=timezone.utc)
        for minutes in range(5):
            self._tenant_ticket(created_at=now - timedelta(minutes=minutes + 1))
        db.session.commit()

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            first = self._get("limit=2")
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json()

        late = self._tenant_ticket(created_at=now + timedelta(seconds=1))
        db.session.commit()
        with patch(
            "services.crm_operational_queue._utc_now",
            return_value=now + timedelta(seconds=10),
        ):
            second = self._get(f"limit=2&cursor={first_payload['page']['next_cursor']}")
        self.assertEqual(second.status_code, 200)
        seen = {
            *(item["queue_id"] for item in first_payload["items"]),
            *(item["queue_id"] for item in second.get_json()["items"]),
        }
        self.assertNotIn(f"TenantTicket:{late.id}", seen)
        self.assertEqual(second.get_json()["as_of"], first_payload["as_of"])

    def test_sla_age_assignee_source_and_category_filters_are_exact(self):
        now = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
        breached = self._tenant_ticket(
            created_at=now - timedelta(hours=4),
            category="alumbrado",
            assignee_id=self.admin.id,
            sla_state="breached",
        )
        self._tenant_ticket(
            created_at=now - timedelta(minutes=30),
            category="arbolado",
            sla_state="healthy",
        )
        self._pyme_ticket(
            created_at=now - timedelta(days=7),
            category="soporte",
            sla_state="at_risk",
        )
        db.session.commit()

        cases = {
            "sla=breached": [f"TenantTicket:{breached.id}"],
            "age=4h_24h": [f"TenantTicket:{breached.id}"],
            f"assignee={self.admin.id}": [f"TenantTicket:{breached.id}"],
            "assignee=unassigned&source_model=TenantTicket&category=arbolado": ["TenantTicket:2"],
            "source_model=PymeTicket&age=gte_7d": ["PymeTicket:1"],
        }
        for query, expected_ids in cases.items():
            with self.subTest(query=query), patch(
                "services.crm_operational_queue._utc_now", return_value=now
            ):
                response = self._get(query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [item["queue_id"] for item in response.get_json()["items"]],
                    expected_ids,
                )

    def test_age_filter_boundaries_are_half_open_and_deterministic(self):
        now = datetime(2026, 8, 2, 19, 0, tzinfo=timezone.utc)
        tickets = {
            "lt_1h": self._tenant_ticket(created_at=now - timedelta(seconds=3599)),
            "1h_4h": self._tenant_ticket(created_at=now - timedelta(hours=1)),
            "4h_24h": self._tenant_ticket(created_at=now - timedelta(hours=4)),
            "1d_3d": self._tenant_ticket(created_at=now - timedelta(days=1)),
            "3d_7d": self._tenant_ticket(created_at=now - timedelta(days=3)),
            "gte_7d": self._tenant_ticket(created_at=now - timedelta(days=7)),
        }
        db.session.commit()

        for bucket, ticket in tickets.items():
            with self.subTest(bucket=bucket), patch(
                "services.crm_operational_queue._utc_now", return_value=now
            ):
                response = self._get(f"age={bucket}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [item["queue_id"] for item in response.get_json()["items"]],
                    [f"TenantTicket:{ticket.id}"],
                )

    def test_mutable_status_is_re_evaluated_live_between_pages(self):
        now = datetime(2026, 8, 2, 19, 30, tzinfo=timezone.utc)
        self._tenant_ticket(created_at=now - timedelta(minutes=1))
        pending = self._tenant_ticket(created_at=now - timedelta(minutes=2))
        db.session.commit()

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            first = self._get("limit=1")
        self.assertTrue(first.get_json()["page"]["has_more"])

        pending.estado = "cerrado"
        db.session.commit()
        with patch(
            "services.crm_operational_queue._utc_now",
            return_value=now + timedelta(seconds=10),
        ):
            second = self._get(f"limit=1&cursor={first.get_json()['page']['next_cursor']}")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["items"], [])
        self.assertEqual(second.get_json()["state_consistency"], "created_at_anchored_live_state")
        self.assertEqual(
            second.get_json()["consistency"]["mutable_fields"],
            "live_at_each_page_read",
        )

    def test_invalid_filters_tampering_and_cursor_binding_fail_closed(self):
        now = datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc)
        for offset in range(3):
            self._tenant_ticket(created_at=now - timedelta(minutes=offset + 1))
        other_actor = self._user(
            "Second Admin",
            "second@junin.test",
            "admin",
            self.tenant.slug,
            tenant_id=self.tenant.id,
        )
        db.session.commit()

        invalid_queries = (
            "queue=all",
            "limit=",
            "limit=101",
            "sla=BREACHED",
            "age=recent",
            "assignee=0",
            "source_model=tenantticket",
            "category=",
            "unknown=value",
            "sla=breached&sla=healthy",
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                response = self._get(query)
                self.assertEqual(response.status_code, 400)

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            first = self._get("limit=1&source_model=TenantTicket")
        cursor = first.get_json()["page"]["next_cursor"]
        self.assertTrue(cursor)

        tampered = cursor[:-1] + ("a" if cursor[-1] != "a" else "b")
        invalid_cursor_requests = (
            (f"limit=1&source_model=TenantTicket&cursor={tampered}", self._auth()),
            (f"limit=1&cursor={cursor}", self._auth()),
            (
                f"limit=1&source_model=TenantTicket&cursor={cursor}",
                self._auth(other_actor),
            ),
        )
        for query, headers in invalid_cursor_requests:
            with self.subTest(query=query, actor=headers.get("Authorization")):
                with patch("services.crm_operational_queue._utc_now", return_value=now):
                    response = self._get(query, headers=headers)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["reason_code"], "invalid_queue_cursor")

    def test_cursor_expiry_returns_410(self):
        now = datetime(2026, 8, 2, 21, 0, tzinfo=timezone.utc)
        self._tenant_ticket(created_at=now - timedelta(minutes=1))
        self._tenant_ticket(created_at=now - timedelta(minutes=2))
        db.session.commit()

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            first = self._get("limit=1")
        cursor = first.get_json()["page"]["next_cursor"]

        with patch(
            "services.crm_operational_queue._utc_now",
            return_value=now + timedelta(seconds=61),
        ):
            expired = self._get(f"limit=1&cursor={cursor}")
        self.assertEqual(expired.status_code, 410)
        self.assertEqual(expired.get_json()["reason_code"], "queue_cursor_expired")

    def test_tenant_binding_conflicts_and_cross_tenant_cursor_are_rejected(self):
        now = datetime(2026, 8, 2, 22, 0, tzinfo=timezone.utc)
        self._tenant_ticket(created_at=now - timedelta(minutes=1))
        self._tenant_ticket(created_at=now - timedelta(minutes=2))

        other_owner = self._user("Other Owner", "owner@other.test", "admin", "other")
        other_tenant = TenantProfile(
            slug="other",
            nombre="Other Tenant",
            tipo="municipio",
            municipio_id=other_owner.id,
        )
        db.session.add(other_tenant)
        db.session.flush()
        other_owner.tenant_id = other_tenant.id
        contradictory = self._user(
            "Contradictory",
            "contradictory@test.local",
            "admin",
            other_tenant.slug,
            tenant_id=self.tenant.id,
        )
        db.session.commit()

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            first = self._get("limit=1")
        cursor = first.get_json()["page"]["next_cursor"]

        duplicate_scope = self.client.get(
            "/api/v2/inbox/operational-queue?tenant_slug=junin&tenant_slug=junin",
            headers=self._auth(),
        )
        self.assertEqual(duplicate_scope.status_code, 403)

        conflicting_scope = self.client.get(
            "/api/v2/inbox/operational-queue?tenant_id=999",
            headers=self._auth(),
        )
        self.assertEqual(conflicting_scope.status_code, 403)

        actor_conflict = self.client.get(
            "/api/v2/inbox/operational-queue",
            headers=self._auth(
                contradictory,
                header_slug=other_tenant.slug,
                token_slug=other_tenant.slug,
            ),
        )
        self.assertEqual(actor_conflict.status_code, 403)
        self.assertEqual(actor_conflict.get_json()["reason_code"], "actor_tenant_scope_conflict")

        cross_tenant = self.client.get(
            f"/api/v2/inbox/operational-queue?limit=1&cursor={cursor}",
            headers=self._auth(
                other_owner,
                header_slug=other_tenant.slug,
                token_slug=other_tenant.slug,
            ),
        )
        self.assertEqual(cross_tenant.status_code, 400)
        self.assertEqual(cross_tenant.get_json()["reason_code"], "invalid_queue_cursor")

    def test_employee_category_scope_is_exact_empty_and_cursor_bound(self):
        now = datetime(2026, 8, 2, 23, 0, tzinfo=timezone.utc)
        allowed_first = self._tenant_ticket(created_at=now - timedelta(minutes=1), category="alumbrado")
        self._municipio_ticket(created_at=now - timedelta(minutes=2), category="alumbrado")
        self._pyme_ticket(created_at=now - timedelta(minutes=3), category="soporte")
        employee = self._user(
            "Scoped Employee",
            "employee@junin.test",
            "empleado",
            self.tenant.slug,
            tenant_id=self.tenant.id,
            es_empleado=True,
            accesibilidad={"employee_scope": {"categorias": ["alumbrado"]}},
        )
        empty_employee = self._user(
            "Empty Employee",
            "empty@junin.test",
            "empleado",
            self.tenant.slug,
            tenant_id=self.tenant.id,
            es_empleado=True,
            accesibilidad={"employee_scope": {"categorias": []}},
        )
        db.session.commit()

        with patch("services.crm_operational_queue._utc_now", return_value=now):
            scoped = self._get("limit=1", actor=employee)
            empty = self._get(actor=empty_employee)
        self.assertEqual(scoped.status_code, 200)
        self.assertEqual(
            [item["queue_id"] for item in scoped.get_json()["items"]],
            [f"TenantTicket:{allowed_first.id}"],
        )
        self.assertTrue(scoped.get_json()["page"]["has_more"])
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.get_json()["items"], [])

        cursor = scoped.get_json()["page"]["next_cursor"]
        employee.accesibilidad = {"employee_scope": {"categorias": ["soporte"]}}
        db.session.commit()
        with patch("services.crm_operational_queue._utc_now", return_value=now):
            rebound = self._get(f"limit=1&cursor={cursor}", actor=employee)
        self.assertEqual(rebound.status_code, 400)
        self.assertEqual(rebound.get_json()["reason_code"], "invalid_queue_cursor")


if __name__ == "__main__":
    unittest.main()
