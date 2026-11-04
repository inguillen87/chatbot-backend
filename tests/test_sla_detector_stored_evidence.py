"""Stored SLA evidence through the real Flask/ORM/auth runtime, synthetic only."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from models import AuditEvent, TenantProfile, TenantTicket, db
from services.v2 import sla_service
from tests import test_v2_sla_truth as fixture_module


class SlaDetectorStoredEvidenceTest(unittest.TestCase):
    """Compose the existing disposable fixture without collecting its tests."""

    def setUp(self):
        self.fixture = fixture_module.SlaTruthPersistenceTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.assertEqual(str(db.engine.url), "sqlite:///:memory:")
        self.now = datetime.now(timezone.utc)

    def _case(self, extra, *, category="luminarias", status="nuevo", tenant=None):
        ticket = TenantTicket(
            tenant_id=(tenant or self.fixture.tenant).id,
            user_id=self.fixture.owner.id,
            categoria=category,
            descripcion="Synthetic stored SLA evidence case",
            estado=status,
            origen="web",
            created_at=self.now - timedelta(days=60),
            datos_extra=copy.deepcopy(extra),
        )
        db.session.add(ticket)
        db.session.commit()
        return ticket

    def _read(self, actor=None):
        response = self.fixture.client.get(
            "/api/v2/sla/breaches",
            headers=self.fixture._auth_header(actor or self.fixture.owner),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual(body["contract_version"], "sla.v2.breaches")
        self.assertIs(body["read_only"], True)
        self.assertEqual(body["total"], len(body["items"]))
        return body["items"]

    def _reload_extra(self, ticket_id):
        db.session.expire_all()
        return copy.deepcopy(db.session.get(TenantTicket, ticket_id).datos_extra)

    def _events(self):
        return AuditEvent.query.filter_by(
            tenant_id=self.fixture.tenant.id, event_type="sla.breach_detected"
        ).order_by(AuditEvent.id).all()

    def test_http_old_case_without_deadlines_stays_unknown_and_unchanged(self):
        extra = {"priority": "urgent", "unrelated": {"preserve": [True, "value"]}}
        ticket = self._case(extra)
        ticket_id = ticket.id
        evaluation = sla_service.evaluate_ticket_sla(ticket)
        self.assertEqual(evaluation["state"], "unknown")
        self.assertIsNone(evaluation["overdue"])
        self.assertEqual(self._read(), [])
        self.assertEqual(self._reload_extra(ticket_id), extra)
        self.assertEqual(self._events(), [])

    def test_materialization_never_creates_deadlines_for_missing_or_nonobject_sla(self):
        cases = [None, {}, {"sla": None}, {"sla": []}, {"sla": "unknown"}]
        stored = []
        for extra in cases:
            ticket = self._case(extra)
            stored.append((ticket.id, copy.deepcopy(extra)))
        breaches = sla_service.detect_sla_breaches_for_tenant(
            self.fixture.tenant, materialize=True
        )
        db.session.commit()
        self.assertEqual(breaches, [])
        self.assertEqual(self._events(), [])
        for ticket_id, before in stored:
            with self.subTest(extra=before):
                self.assertEqual(self._reload_extra(ticket_id), before)

    def test_partial_persisted_breach_never_completes_missing_clocks(self):
        sla = {
            "resolution_due_at": (self.now - timedelta(hours=1)).isoformat(),
            "stored_evidence": {"keep": [1, 2]},
        }
        ticket = self._case({"priority": "urgent", "sla": sla, "keep": "outside"})
        ticket_id = ticket.id
        items = self._read()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["breached_clocks"], ["resolution"])
        materialized = sla_service.detect_sla_breaches_for_tenant(
            self.fixture.tenant, materialize=True
        )
        db.session.commit()
        self.assertEqual(materialized[0]["newly_breached_clocks"], ["resolution"])
        extra = self._reload_extra(ticket_id)
        self.assertEqual(extra["sla"], sla)
        self.assertEqual(extra["keep"], "outside")
        self.assertNotIn("first_response_due_at", extra["sla"])
        self.assertNotIn("next_update_due_at", extra["sla"])
        self.assertEqual(len(extra["sla_breach_receipts"]), 1)
        self.assertEqual(len(self._events()), 1)

    def test_http_known_breach_does_not_write_receipts_events_or_json(self):
        sla = {
            field: (self.now - timedelta(hours=1)).isoformat()
            for field in (
                "first_response_due_at", "resolution_due_at", "next_update_due_at"
            )
        }
        before = {"sla": sla, "unrelated": {"kept": True}}
        ticket = self._case(before)
        ticket_id = ticket.id
        self.assertEqual(len(self._read()), 1)
        self.assertEqual(len(self._read()), 1)
        self.assertEqual(self._reload_extra(ticket_id), before)
        self.assertEqual(self._events(), [])

    def test_malformed_deadline_or_satisfaction_is_not_a_confirmed_breach(self):
        invalid_due = {"first_response_due_at": "invalid", "resolution_due_at": 42}
        invalid_satisfied = {
            "resolution_due_at": (self.now - timedelta(hours=1)).isoformat(),
            "resolution_satisfied_at": "invalid",
        }
        stored = []
        for sla in (invalid_due, invalid_satisfied):
            extra = {"sla": sla, "keep": ["untouched"]}
            ticket = self._case(extra)
            stored.append((ticket.id, extra))
            evaluation = sla_service.evaluate_ticket_sla(ticket)
            self.assertEqual(evaluation["state"], "unknown")
            self.assertIsNone(evaluation["overdue"])
        self.assertEqual(self._read(), [])
        self.assertEqual(sla_service.detect_sla_breaches_for_tenant(
            self.fixture.tenant, materialize=True
        ), [])
        db.session.commit()
        for ticket_id, extra in stored:
            self.assertEqual(self._reload_extra(ticket_id), extra)
        self.assertEqual(self._events(), [])

    def test_policy_change_cannot_retrofit_missing_or_persisted_deadlines(self):
        sla = {"resolution_due_at": (self.now + timedelta(days=5)).isoformat()}
        extra = {"priority": "urgent", "sla": sla, "keep": "old evidence"}
        ticket = self._case(extra)
        ticket_id = ticket.id
        sla_service.save_policies_for_tenant(self.fixture.tenant, {
            "urgent": {
                "first_response_minutes": 1,
                "resolution_minutes": 1,
                "next_update_minutes": 1,
            }
        })
        db.session.commit()
        self.assertEqual(self._read(), [])
        self.assertEqual(sla_service.detect_sla_breaches_for_tenant(
            self.fixture.tenant, materialize=True
        ), [])
        db.session.commit()
        self.assertEqual(self._reload_extra(ticket_id), extra)
        self.assertEqual(self._events(), [])

    def test_http_employee_category_and_foreign_tenant_scope_are_preserved(self):
        extra = {"sla": {
            "resolution_due_at": (self.now - timedelta(hours=1)).isoformat()
        }}
        allowed = self._case(extra, category="luminarias")
        denied = self._case(extra, category="arbolado")
        other = TenantProfile(
            slug="stored-sla-other", nombre="Synthetic other institution",
            tipo="municipio", municipio_id=self.fixture.owner.id, configuracion={},
        )
        db.session.add(other)
        db.session.commit()
        foreign = self._case(extra, tenant=other)
        expected_admin_ids = {allowed.id, denied.id}
        allowed_id, foreign_id = allowed.id, foreign.id
        self.fixture.employee.ticket_categorias = "luminaria"
        db.session.commit()
        items = self._read(self.fixture.employee)
        self.assertEqual({row["ticket_id"] for row in items}, {allowed_id})
        self.assertEqual({row["ticket_id"] for row in self._read()}, expected_admin_ids)
        self.fixture.employee.ticket_categorias = ""
        db.session.commit()
        self.assertEqual(self._read(self.fixture.employee), [])
        self.assertEqual(self._reload_extra(foreign_id), extra)
        self.assertEqual(self._events(), [])

    def test_inactive_cases_keep_persisted_deadlines_without_detected_breach(self):
        past = (self.now - timedelta(days=1)).isoformat()
        stored = []
        for state, paused in (("cerrado", False), ("waiting_customer", False), ("nuevo", True)):
            extra = {"sla": {
                "first_response_due_at": past, "resolution_due_at": past,
                "next_update_due_at": past, "paused": paused,
            }, "keep": {"unchanged": True}}
            ticket = self._case(extra, status=state)
            stored.append((ticket.id, extra))
        self.assertEqual(self._read(), [])
        self.assertEqual(sla_service.detect_sla_breaches_for_tenant(
            self.fixture.tenant, materialize=True
        ), [])
        db.session.commit()
        for ticket_id, extra in stored:
            self.assertEqual(self._reload_extra(ticket_id), extra)
        self.assertEqual(self._events(), [])

    def test_worker_receipt_and_event_rollback_then_dedupe_without_sla_rewrite(self):
        sla = {"first_response_due_at": (self.now - timedelta(hours=1)).isoformat()}
        before = {"sla": sla, "keep": {"nested": ["preserved"]}}
        ticket = self._case(before)
        ticket_id = ticket.id
        observed = sla_service.detect_sla_breaches_for_tenant(
            self.fixture.tenant, materialize=True
        )
        self.assertEqual(observed[0]["newly_breached_clocks"], ["first_response"])
        db.session.flush()
        self.assertEqual(len(self._events()), 1)
        db.session.rollback()
        self.assertEqual(self._reload_extra(ticket_id), before)
        self.assertEqual(self._events(), [])
        for _ in range(2):
            sla_service.detect_sla_breaches_for_tenant(
                self.fixture.tenant, materialize=True
            )
            db.session.commit()
        after = self._reload_extra(ticket_id)
        self.assertEqual(after["sla"], sla)
        self.assertEqual(after["keep"], before["keep"])
        self.assertEqual(len(after["sla_breach_receipts"]), 1)
        self.assertEqual(len(self._events()), 1)

    def test_explicit_writer_still_allocates_deadlines_before_detection(self):
        ticket = self._case({"priority": "urgent", "keep": "legitimate explicit writer"})
        ticket.created_at = self.now
        sla = sla_service.apply_sla_to_ticket(
            ticket, sla_service.get_policies_for_tenant(self.fixture.tenant)
        )
        db.session.commit()
        ticket_id = ticket.id
        before = copy.deepcopy(ticket.datos_extra)
        self.assertTrue(all(sla.get(field) for field in (
            "first_response_due_at", "resolution_due_at", "next_update_due_at"
        )))
        self.assertIs(sla_service.evaluate_ticket_sla(ticket)["known"], True)
        self.assertEqual(self._read(), [])
        self.assertEqual(self._reload_extra(ticket_id), before)


if __name__ == "__main__":
    unittest.main()
