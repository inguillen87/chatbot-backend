import copy
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import AuditEvent, MunicipioTicket, TenantProfile, TenantTicket, User
from services.ticket_service import ServicioTickets
from services.v2.sla_service import (
    SlaPolicyValidationError,
    apply_operator_response_sla,
    apply_priority_change_sla,
    apply_reopen_sla,
    apply_sla_to_ticket,
    detect_sla_breaches_for_tenant,
    evaluate_sla_clocks,
    get_policies_for_tenant,
    is_ticket_overdue,
    save_policies_for_tenant,
)
from services.v2.ticket_event_service import record_ticket_event
from services.v2.ticket_service import _sla_status, add_comment, patch_ticket, serialize_ticket
from routes.v2.saas import _ticket_sla_payload, _ticket_transition_status


_DEFAULT_TEST_POLICIES = {
    "urgent": {
        "first_response_minutes": 15,
        "resolution_minutes": 240,
        "next_update_minutes": 60,
    }
}


class SlaTruthTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class SlaTruthPureTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def _due(self, **hours):
        return {
            f"{name}_due_at": (self.now + timedelta(hours=value)).isoformat()
            for name, value in hours.items()
        }

    def test_policies_are_deep_copied_and_never_leak_between_tenants(self):
        tenant_a = SimpleNamespace(
            configuracion={
                "v2_sla_policies": {
                    "urgent": {"first_response_minutes": 7},
                }
            }
        )
        tenant_b = SimpleNamespace(configuracion={})

        policies_a = get_policies_for_tenant(tenant_a)
        self.assertEqual(policies_a["urgent"]["first_response_minutes"], 7)
        policies_a["urgent"]["first_response_minutes"] = 999
        policies_a["medium"]["resolution_minutes"] = 1

        self.assertEqual(
            get_policies_for_tenant(tenant_a)["urgent"]["first_response_minutes"],
            7,
        )
        policies_b = get_policies_for_tenant(tenant_b)
        self.assertEqual(policies_b["urgent"]["first_response_minutes"], 15)
        self.assertEqual(policies_b["medium"]["resolution_minutes"], 1440)

        malformed = SimpleNamespace(configuracion=None)
        self.assertEqual(get_policies_for_tenant(malformed)["low"], {
            "first_response_minutes": 240,
            "resolution_minutes": 2880,
            "next_update_minutes": 1440,
        })
        self.assertIsNone(malformed.configuracion)

    def test_policies_only_accept_positive_bounded_integer_minutes(self):
        tenant = SimpleNamespace(
            configuracion={
                "v2_sla_policies": {
                    "urgent": {
                        "first_response_minutes": 0,
                        "resolution_minutes": -1,
                        "next_update_minutes": 525_601,
                        "unknown_minutes": 1,
                    },
                    "medium": {
                        "first_response_minutes": "45",
                        "resolution_minutes": True,
                        "next_update_minutes": 30.5,
                    },
                    "invented": {"first_response_minutes": 1},
                }
            }
        )

        policies = get_policies_for_tenant(tenant)

        self.assertEqual(policies["urgent"], {"first_response_minutes": 15, "resolution_minutes": 240, "next_update_minutes": 60})
        self.assertEqual(policies["medium"]["first_response_minutes"], 45)
        self.assertEqual(policies["medium"]["resolution_minutes"], 1440)
        self.assertEqual(policies["medium"]["next_update_minutes"], 720)
        self.assertNotIn("invented", policies)
        self.assertNotIn("unknown_minutes", policies["urgent"])

    def test_all_three_known_healthy_clocks_are_ok(self):
        evaluation = evaluate_sla_clocks(
            self._due(first_response=24, resolution=48, next_update=36),
            ticket_status="nuevo",
            now=self.now,
        )

        self.assertEqual(evaluation["contract_version"], "ticket.sla.v1")
        self.assertEqual(evaluation["state"], "ok")
        self.assertTrue(evaluation["known"])
        self.assertFalse(evaluation["unknown"])
        self.assertFalse(evaluation["overdue"])
        self.assertTrue(all(clock["status"] == "due" for clock in evaluation["clocks"].values()))

    def test_first_response_breach_is_not_hidden_by_later_resolution(self):
        evaluation = evaluate_sla_clocks(
            self._due(first_response=-1, resolution=48, next_update=36),
            ticket_status="nuevo",
            now=self.now,
        )

        self.assertEqual(evaluation["state"], "breached")
        self.assertTrue(evaluation["overdue"])
        self.assertEqual(evaluation["breached_clocks"], ["first_response"])
        self.assertEqual(evaluation["clocks"]["first_response"]["status"], "overdue")

    def test_next_update_breach_is_not_hidden_by_later_resolution(self):
        evaluation = evaluate_sla_clocks(
            self._due(first_response=24, resolution=48, next_update=-1),
            ticket_status="in_progress",
            now=self.now,
        )

        self.assertEqual(evaluation["state"], "breached")
        self.assertEqual(evaluation["breached_clocks"], ["next_update"])

    def test_missing_or_invalid_evidence_is_unknown_not_ok(self):
        missing = evaluate_sla_clocks(
            {"resolution_due_at": (self.now + timedelta(days=2)).isoformat()},
            ticket_status="nuevo",
            now=self.now,
        )
        invalid = evaluate_sla_clocks(
            {
                **self._due(first_response=24, resolution=48),
                "next_update_due_at": "not-a-date",
            },
            ticket_status="nuevo",
            now=self.now,
        )

        self.assertEqual(missing["state"], "unknown")
        self.assertFalse(missing["known"])
        self.assertIsNone(missing["overdue"])
        self.assertEqual(set(missing["unknown_clocks"]), {"first_response", "next_update"})
        self.assertEqual(invalid["state"], "unknown")
        self.assertEqual(
            invalid["clocks"]["next_update"]["unknown_reason"],
            "invalid_due_at",
        )

    def test_satisfied_first_response_is_not_left_active(self):
        satisfied_at = (self.now - timedelta(hours=2)).isoformat()
        evaluation = evaluate_sla_clocks(
            {
                **self._due(first_response=-1, resolution=48, next_update=36),
                "first_response_satisfied_at": satisfied_at,
            },
            ticket_status="in_progress",
            now=self.now,
        )

        clock = evaluation["clocks"]["first_response"]
        self.assertEqual(clock["status"], "satisfied")
        self.assertEqual(clock["fulfilled_at"], satisfied_at)
        self.assertNotIn("first_response", evaluation["breached_clocks"])
        self.assertEqual(evaluation["state"], "ok")

    def test_late_satisfaction_remains_a_breach_with_fulfillment_evidence(self):
        satisfied_at = (self.now - timedelta(minutes=30)).isoformat()
        evaluation = evaluate_sla_clocks(
            {
                **self._due(first_response=-1, resolution=48, next_update=36),
                "first_response_satisfied_at": satisfied_at,
            },
            ticket_status="in_progress",
            now=self.now,
        )

        clock = evaluation["clocks"]["first_response"]
        self.assertEqual(clock["status"], "overdue")
        self.assertEqual(clock["state"], "satisfied_late")
        self.assertEqual(clock["fulfilled_at"], satisfied_at)
        self.assertGreater(clock["satisfaction_lag_seconds"], 0)
        self.assertIn("first_response", evaluation["breached_clocks"])
        self.assertTrue(evaluation["overdue"])

    def test_satisfaction_without_deadline_is_unknown_not_healthy(self):
        satisfied_at = (self.now - timedelta(minutes=30)).isoformat()
        evaluation = evaluate_sla_clocks(
            {
                "first_response_satisfied_at": satisfied_at,
                **self._due(resolution=48, next_update=36),
            },
            ticket_status="in_progress",
            now=self.now,
        )

        clock = evaluation["clocks"]["first_response"]
        self.assertEqual(clock["status"], "unknown")
        self.assertFalse(clock["known"])
        self.assertEqual(clock["fulfilled_at"], satisfied_at)
        self.assertEqual(clock["unknown_reason"], "missing_due_at")
        self.assertEqual(evaluation["state"], "unknown")

    def test_invalid_satisfaction_timestamp_is_unknown_with_valid_deadline(self):
        evaluation = evaluate_sla_clocks(
            {
                **self._due(first_response=24, resolution=48, next_update=36),
                "first_response_satisfied_at": "not-a-timestamp",
            },
            ticket_status="in_progress",
            now=self.now,
        )

        clock = evaluation["clocks"]["first_response"]
        self.assertEqual(clock["status"], "unknown")
        self.assertFalse(clock["known"])
        self.assertEqual(clock["unknown_reason"], "invalid_satisfied_at")
        self.assertEqual(evaluation["state"], "unknown")
        self.assertIsNone(evaluation["overdue"])

    def test_first_response_at_is_supported_as_compatibility_evidence(self):
        evaluation = evaluate_sla_clocks(
            {
                **self._due(first_response=-1, resolution=48, next_update=36),
                "first_response_at": (self.now - timedelta(hours=2)).isoformat(),
            },
            ticket_status="in_progress",
            now=self.now,
        )

        self.assertEqual(evaluation["clocks"]["first_response"]["status"], "satisfied")
        self.assertEqual(
            evaluation["clocks"]["first_response"]["satisfied_field"],
            "first_response_at",
        )

    def test_warning_uses_the_earliest_active_clock(self):
        evaluation = evaluate_sla_clocks(
            self._due(first_response=6, resolution=48, next_update=36),
            ticket_status="nuevo",
            now=self.now,
        )

        self.assertEqual(evaluation["state"], "warning")
        self.assertEqual(evaluation["warning_clocks"], ["first_response"])
        self.assertEqual(evaluation["clocks"]["first_response"]["status"], "due")
        self.assertTrue(evaluation["clocks"]["first_response"]["at_risk"])

    def test_closed_and_paused_tickets_are_not_currently_overdue(self):
        due = self._due(first_response=-1, resolution=-1, next_update=-1)
        closed = evaluate_sla_clocks(due, ticket_status="cerrado", now=self.now)
        paused = evaluate_sla_clocks(due, ticket_status="waiting_customer", now=self.now)

        self.assertEqual(closed["state"], "closed")
        self.assertFalse(closed["active"])
        self.assertFalse(closed["overdue"])
        self.assertEqual(paused["state"], "paused")
        self.assertFalse(paused["overdue"])

    def test_priority_change_recalculates_only_active_obligations_from_change_time(self):
        first_due = (self.now - timedelta(hours=4)).isoformat()
        first_satisfied = (self.now - timedelta(hours=5)).isoformat()
        ticket = SimpleNamespace(
            estado="in_progress",
            datos_extra={
                "priority": "urgent",
                "sla": {
                    "first_response_due_at": first_due,
                    "first_response_satisfied_at": first_satisfied,
                    "resolution_due_at": (self.now + timedelta(days=1)).isoformat(),
                    "next_update_due_at": (self.now + timedelta(hours=2)).isoformat(),
                },
            },
        )

        sla = apply_priority_change_sla(ticket, _DEFAULT_TEST_POLICIES, occurred_at=self.now)

        self.assertEqual(sla["first_response_due_at"], first_due)
        self.assertEqual(sla["first_response_satisfied_at"], first_satisfied)
        self.assertEqual(
            datetime.fromisoformat(sla["resolution_due_at"]),
            self.now + timedelta(minutes=240),
        )
        self.assertEqual(
            datetime.fromisoformat(sla["next_update_due_at"]),
            self.now + timedelta(minutes=60),
        )
        self.assertEqual(sla["history"][-1]["event"], "priority_changed")

    def test_public_response_archives_on_time_next_update_cycle_before_opening_next(self):
        prior_due_at = self.now + timedelta(minutes=10)
        ticket = SimpleNamespace(
            estado="in_progress",
            datos_extra={
                "priority": "urgent",
                "sla": {"next_update_due_at": prior_due_at.isoformat()},
            },
        )

        sla = apply_operator_response_sla(
            ticket,
            _DEFAULT_TEST_POLICIES,
            occurred_at=self.now,
        )

        self.assertEqual(sla["next_update_cycle"], 2)
        self.assertEqual(len(sla["next_update_history"]), 1)
        archived = sla["next_update_history"][0]
        self.assertEqual(archived["contract_version"], "ticket.sla.next_update_cycle.v1")
        self.assertEqual(archived["cycle"], 1)
        self.assertEqual(archived["due_at"], prior_due_at.isoformat())
        self.assertEqual(archived["satisfied_at"], self.now.isoformat())
        self.assertEqual(archived["result"], "on_time")
        self.assertTrue(archived["known"])
        self.assertIsNone(archived["unknown_reason"])
        self.assertEqual(archived["completion_delta_seconds"], -600)
        self.assertEqual(
            datetime.fromisoformat(sla["next_update_due_at"]),
            self.now + timedelta(minutes=60),
        )
        self.assertEqual(sla["next_update_last_satisfied_at"], self.now.isoformat())

    def test_public_response_archives_late_next_update_cycle(self):
        prior_due_at = self.now - timedelta(minutes=7)
        ticket = SimpleNamespace(
            estado="in_progress",
            datos_extra={
                "priority": "urgent",
                "sla": {
                    "next_update_cycle": 4,
                    "next_update_due_at": prior_due_at.isoformat(),
                },
            },
        )

        sla = apply_operator_response_sla(
            ticket,
            _DEFAULT_TEST_POLICIES,
            occurred_at=self.now,
        )

        archived = sla["next_update_history"][-1]
        self.assertEqual(archived["cycle"], 4)
        self.assertEqual(archived["result"], "late")
        self.assertTrue(archived["known"])
        self.assertEqual(archived["completion_delta_seconds"], 420)
        self.assertEqual(sla["next_update_cycle"], 5)

    def test_subsecond_lateness_is_not_truncated_into_an_on_time_result(self):
        ticket = SimpleNamespace(
            estado="in_progress",
            datos_extra={
                "priority": "urgent",
                "sla": {
                    "next_update_due_at": (
                        self.now - timedelta(milliseconds=500)
                    ).isoformat(),
                },
            },
        )

        sla = apply_operator_response_sla(
            ticket,
            _DEFAULT_TEST_POLICIES,
            occurred_at=self.now,
        )

        archived = sla["next_update_history"][-1]
        self.assertEqual(archived["result"], "late")
        self.assertEqual(archived["completion_delta_seconds"], 0.5)

    def test_successive_public_responses_close_distinct_cycles_without_rewriting_prior_entry(self):
        ticket = SimpleNamespace(
            estado="in_progress",
            datos_extra={
                "priority": "urgent",
                "sla": {
                    "next_update_due_at": (self.now + timedelta(minutes=5)).isoformat(),
                },
            },
        )
        first_response_at = self.now
        second_response_at = self.now + timedelta(minutes=61)

        first_sla = apply_operator_response_sla(
            ticket,
            _DEFAULT_TEST_POLICIES,
            occurred_at=first_response_at,
        )
        first_entry = copy.deepcopy(first_sla["next_update_history"][0])
        second_sla = apply_operator_response_sla(
            ticket,
            _DEFAULT_TEST_POLICIES,
            occurred_at=second_response_at,
        )

        self.assertEqual(len(second_sla["next_update_history"]), 2)
        self.assertEqual(second_sla["next_update_history"][0], first_entry)
        self.assertEqual(second_sla["next_update_history"][0]["cycle"], 1)
        self.assertEqual(second_sla["next_update_history"][0]["result"], "on_time")
        self.assertEqual(second_sla["next_update_history"][1]["cycle"], 2)
        self.assertEqual(second_sla["next_update_history"][1]["result"], "late")
        self.assertEqual(second_sla["next_update_cycle"], 3)

    def test_public_response_fails_closed_for_missing_or_invalid_prior_deadline(self):
        for prior_due_at, reason in (
            (None, "missing_due_at"),
            ("not-a-timestamp", "invalid_due_at"),
        ):
            with self.subTest(prior_due_at=prior_due_at):
                ticket = SimpleNamespace(
                    estado="in_progress",
                    datos_extra={
                        "priority": "urgent",
                        "sla": {"next_update_due_at": prior_due_at},
                    },
                )

                sla = apply_operator_response_sla(
                    ticket,
                    _DEFAULT_TEST_POLICIES,
                    occurred_at=self.now,
                )

                archived = sla["next_update_history"][-1]
                self.assertEqual(archived["result"], "unknown")
                self.assertFalse(archived["known"])
                self.assertEqual(archived["unknown_reason"], reason)
                self.assertIsNone(archived["completion_delta_seconds"])
                self.assertNotIn(archived["result"], {"on_time", "late"})
                self.assertEqual(sla["next_update_cycle"], 2)

    def test_next_update_history_is_append_only_bounded_and_resists_malformed_cycle(self):
        existing_history = [
            {
                "contract_version": "ticket.sla.next_update_cycle.v1",
                "cycle": cycle,
                "due_at": (self.now - timedelta(minutes=cycle)).isoformat(),
                "satisfied_at": (self.now - timedelta(minutes=cycle - 1)).isoformat(),
                "result": "late",
                "known": True,
            }
            for cycle in range(1, 51)
        ]
        preserved_tail = copy.deepcopy(existing_history[1:])
        ticket = SimpleNamespace(
            estado="in_progress",
            datos_extra={
                "priority": "urgent",
                "sla": {
                    "next_update_cycle": "malformed",
                    "next_update_due_at": (self.now + timedelta(minutes=1)).isoformat(),
                    "next_update_history": existing_history,
                },
            },
        )

        sla = apply_operator_response_sla(
            ticket,
            _DEFAULT_TEST_POLICIES,
            occurred_at=self.now,
        )

        self.assertEqual(len(sla["next_update_history"]), 50)
        self.assertEqual(sla["next_update_history"][:-1], preserved_tail)
        self.assertEqual(sla["next_update_history"][-1]["cycle"], 51)
        self.assertEqual(sla["next_update_cycle"], 52)
        self.assertEqual(existing_history[0]["cycle"], 1)
        self.assertEqual(existing_history[-1]["cycle"], 50)

    def test_priority_change_after_breach_is_an_amendment_and_preserves_fulfilled_fact(self):
        first_due = (self.now - timedelta(hours=2)).isoformat()
        first_satisfied_late = (self.now - timedelta(hours=1)).isoformat()
        resolution_due_before_amendment = (self.now - timedelta(minutes=30)).isoformat()
        ticket = SimpleNamespace(
            estado="in_progress",
            datos_extra={
                "priority": "urgent",
                "sla": {
                    "first_response_due_at": first_due,
                    "first_response_satisfied_at": first_satisfied_late,
                    "resolution_due_at": resolution_due_before_amendment,
                    "next_update_due_at": (self.now - timedelta(minutes=15)).isoformat(),
                },
            },
        )

        before = evaluate_sla_clocks(ticket.datos_extra["sla"], ticket_status=ticket.estado, now=self.now)
        self.assertEqual(before["clocks"]["first_response"]["state"], "satisfied_late")

        sla = apply_priority_change_sla(ticket, _DEFAULT_TEST_POLICIES, occurred_at=self.now)

        self.assertEqual(sla["first_response_due_at"], first_due)
        self.assertEqual(sla["first_response_satisfied_at"], first_satisfied_late)
        self.assertEqual(
            sla["history"][-1]["snapshot"]["resolution_due_at"],
            resolution_due_before_amendment,
        )
        self.assertEqual(
            datetime.fromisoformat(sla["resolution_due_at"]),
            self.now + timedelta(minutes=240),
        )
        after = evaluate_sla_clocks(sla, ticket_status=ticket.estado, now=self.now)
        self.assertEqual(after["clocks"]["first_response"]["state"], "satisfied_late")

    def test_close_and_reopen_statuses_fail_closed_when_payload_contradicts_action(self):
        self.assertEqual(_ticket_transition_status("close", None, default="cerrado"), "cerrado")
        self.assertEqual(_ticket_transition_status("reopen", None, default="nuevo"), "nuevo")
        self.assertIsNone(_ticket_transition_status("close", "nuevo", default="cerrado"))
        self.assertIsNone(_ticket_transition_status("reopen", "cerrado", default="nuevo"))
        self.assertIsNone(_ticket_transition_status("reopen", "inventado", default="nuevo"))
        self.assertIsNone(_ticket_transition_status("close", False, default="cerrado"))

    def test_reopen_starts_new_resolution_cycle_and_archives_completed_fact(self):
        resolution_satisfied = (self.now - timedelta(hours=1)).isoformat()
        first_satisfied = (self.now - timedelta(hours=3)).isoformat()
        ticket = SimpleNamespace(
            estado="nuevo",
            datos_extra={
                "priority": "urgent",
                "sla": {
                    "first_response_due_at": (self.now - timedelta(hours=2)).isoformat(),
                    "first_response_satisfied_at": first_satisfied,
                    "resolution_due_at": (self.now - timedelta(hours=1)).isoformat(),
                    "resolution_satisfied_at": resolution_satisfied,
                    "resolved_at": resolution_satisfied,
                    "next_update_due_at": (self.now - timedelta(hours=1)).isoformat(),
                },
            },
        )

        sla = apply_reopen_sla(ticket, _DEFAULT_TEST_POLICIES, occurred_at=self.now)

        self.assertEqual(sla["first_response_satisfied_at"], first_satisfied)
        self.assertNotIn("resolution_satisfied_at", sla)
        self.assertNotIn("resolved_at", sla)
        self.assertEqual(
            sla["history"][-1]["snapshot"]["resolution_satisfied_at"],
            resolution_satisfied,
        )
        self.assertEqual(
            datetime.fromisoformat(sla["resolution_due_at"]),
            self.now + timedelta(minutes=240),
        )
        self.assertEqual(
            datetime.fromisoformat(sla["next_update_due_at"]),
            self.now + timedelta(minutes=60),
        )
        self.assertEqual(sla["resolution_cycle"], 1)

    def test_legacy_helpers_keep_boolean_overdue_but_surface_unknown_status(self):
        missing = SimpleNamespace(estado="nuevo", datos_extra={})
        first_response_breached = SimpleNamespace(
            estado="nuevo",
            datos_extra={
                "sla": {
                    **self._due(first_response=-1, resolution=48, next_update=36),
                }
            },
        )

        self.assertFalse(is_ticket_overdue(missing, now=self.now))
        self.assertEqual(_sla_status(missing), "unknown")
        self.assertTrue(is_ticket_overdue(first_response_breached, now=self.now))

    def test_legacy_municipio_ticket_without_sla_fails_closed(self):
        legacy_ticket = MunicipioTicket(
            pregunta="Reclamo historico",
            estado="nuevo",
            datos_extra=None,
        )

        self.assertFalse(is_ticket_overdue(legacy_ticket, now=self.now))
        self.assertEqual(_sla_status(legacy_ticket), "unknown")
        payload = _ticket_sla_payload(legacy_ticket, {})
        self.assertEqual(payload["state"], "unknown")
        self.assertTrue(payload["unknown"])
        self.assertIsNone(payload["overdue"])
        self.assertEqual(
            set(payload["unknown_clocks"]),
            {"first_response", "resolution", "next_update"},
        )


class SlaTruthPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SlaTruthTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(name="Owner", email="owner@sla.test", rol="admin")
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="sla-tenant",
            nombre="SLA Tenant",
            tipo="municipio",
            pyme_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.owner.tenant_id = self.tenant.id
        self.owner.tenant_slug = self.tenant.slug
        db.session.add(self.owner)
        self.customer = User(
            name="Customer",
            email="customer@sla.test",
            rol="usuario",
            tenant_id=self.tenant.id,
            tenant_slug=self.tenant.slug,
        )
        self.customer.set_password("secret123")
        db.session.add(self.customer)
        self.employee = User(
            name="Employee",
            email="employee@sla.test",
            rol="empleado",
            es_empleado=True,
            tenant_id=self.tenant.id,
            tenant_slug=self.tenant.slug,
        )
        self.employee.set_password("secret123")
        db.session.add(self.employee)
        db.session.commit()

    def _auth_header(self, user: User) -> dict[str, str]:
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": user.tenant_slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": self.tenant.slug,
        }

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _ticket(self, *, priority="urgent"):
        ticket = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            categoria="luminarias",
            descripcion="SLA truth",
            estado="nuevo",
            origen="web",
            datos_extra={"priority": priority, "comments": []},
        )
        db.session.add(ticket)
        db.session.flush()
        apply_sla_to_ticket(ticket, get_policies_for_tenant(self.tenant), force_recalculate=True)
        db.session.add(ticket)
        db.session.commit()
        return ticket

    def test_saved_policy_rejects_invalid_values_without_mutating_storage(self):
        before = copy.deepcopy(self.tenant.configuracion)

        with self.assertRaises(SlaPolicyValidationError) as raised:
            save_policies_for_tenant(
                self.tenant,
                {
                    "urgent": {
                        "first_response_minutes": 8,
                        "resolution_minutes": 0,
                        "next_update_minutes": 525_601,
                    }
                },
            )

        self.assertEqual(
            {error["field"] for error in raised.exception.errors},
            {
                "policies.urgent.resolution_minutes",
                "policies.urgent.next_update_minutes",
            },
        )
        self.assertEqual(self.tenant.configuracion, before)

    def test_saved_partial_policy_is_merged_and_return_value_cannot_mutate_storage(self):
        normalized = save_policies_for_tenant(
            self.tenant,
            {
                "urgent": {
                    "first_response_minutes": 8,
                }
            },
        )
        db.session.flush()
        normalized["urgent"]["first_response_minutes"] = 999

        stored = self.tenant.configuracion["v2_sla_policies"]
        self.assertEqual(stored["urgent"]["first_response_minutes"], 8)
        self.assertEqual(stored["urgent"]["resolution_minutes"], 240)
        self.assertEqual(stored["urgent"]["next_update_minutes"], 60)

    def test_policy_endpoint_rejects_every_invalid_field_with_422(self):
        headers = self._auth_header(self.owner)
        cases = (
            (
                {"urgent": {"first_response_minutes": True}},
                "policies.urgent.first_response_minutes",
            ),
            (
                {"urgent": {"first_response_minutes": "15"}},
                "policies.urgent.first_response_minutes",
            ),
            (
                {"urgent": {"first_response_minutes": 0}},
                "policies.urgent.first_response_minutes",
            ),
            (
                {"urgent": {"first_response_minutes": 525_601}},
                "policies.urgent.first_response_minutes",
            ),
            (
                {"critical": {"first_response_minutes": 5}},
                "policies.critical",
            ),
            (
                {"urgent": {"breach_grace_minutes": 5}},
                "policies.urgent.breach_grace_minutes",
            ),
        )

        for policies, expected_field in cases:
            with self.subTest(expected_field=expected_field):
                before = copy.deepcopy(self.tenant.configuracion)
                response = self.client.post(
                    "/api/v2/sla/policies",
                    json={"policies": policies},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 422, response.get_json())
                payload = response.get_json() or {}
                self.assertEqual(payload.get("reason_code"), "invalid_sla_policy")
                self.assertIn(
                    expected_field,
                    {error.get("field") for error in payload.get("field_errors") or []},
                )
                self.assertEqual(self.tenant.configuracion, before)

    def test_policy_endpoint_accepts_partial_patch_without_resetting_other_fields(self):
        headers = self._auth_header(self.owner)
        save_policies_for_tenant(
            self.tenant,
            {"urgent": {"resolution_minutes": 17}},
        )
        db.session.commit()

        response = self.client.post(
            "/api/v2/sla/policies",
            json={"policies": {"urgent": {"first_response_minutes": 8}}},
            headers=headers,
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        policies = (response.get_json() or {}).get("policies") or {}
        self.assertEqual(policies["urgent"]["first_response_minutes"], 8)
        self.assertEqual(policies["urgent"]["resolution_minutes"], 17)

    def test_employee_can_read_but_cannot_modify_tenant_wide_sla_policy(self):
        headers = self._auth_header(self.employee)

        readable = self.client.get("/api/v2/sla/policies", headers=headers)
        forbidden = self.client.post(
            "/api/v2/sla/policies",
            json={"policies": {"urgent": {"first_response_minutes": 8}}},
            headers=headers,
        )

        self.assertEqual(readable.status_code, 200, readable.get_json())
        self.assertEqual(forbidden.status_code, 403, forbidden.get_json())
        self.assertEqual(
            (forbidden.get_json() or {}).get("reason_code"),
            "sla_policy_admin_required",
        )

    def test_breach_event_dedupes_per_clock_deadline_and_allows_new_cycle(self):
        ticket = self._ticket(priority="urgent")
        now = datetime.now(timezone.utc)
        extra = copy.deepcopy(ticket.datos_extra)
        for field in (
            "first_response_due_at",
            "resolution_due_at",
            "next_update_due_at",
        ):
            extra["sla"][field] = (now - timedelta(minutes=5)).isoformat()
        ticket.datos_extra = extra
        db.session.add(ticket)
        db.session.commit()

        detect_sla_breaches_for_tenant(self.tenant, materialize=True)
        db.session.commit()
        first_count = AuditEvent.query.filter_by(
            tenant_id=self.tenant.id,
            event_type="sla.breach_detected",
            resource_id=str(ticket.id),
        ).count()
        self.assertEqual(first_count, 1)

        detect_sla_breaches_for_tenant(self.tenant, materialize=True)
        db.session.commit()
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant.id,
                event_type="sla.breach_detected",
                resource_id=str(ticket.id),
            ).count(),
            1,
        )

        refreshed = db.session.get(TenantTicket, ticket.id)
        updated = copy.deepcopy(refreshed.datos_extra)
        updated["sla"]["next_update_due_at"] = (now - timedelta(minutes=1)).isoformat()
        refreshed.datos_extra = updated
        db.session.add(refreshed)
        db.session.commit()

        detect_sla_breaches_for_tenant(self.tenant, materialize=True)
        db.session.commit()
        events = AuditEvent.query.filter_by(
            tenant_id=self.tenant.id,
            event_type="sla.breach_detected",
            resource_id=str(ticket.id),
        ).order_by(AuditEvent.id.asc()).all()
        self.assertEqual(len(events), 2)
        self.assertEqual(
            (events[-1].details or {}).get("newly_breached_clocks"),
            ["next_update"],
        )

    def test_legacy_breach_marker_only_adopts_the_clock_the_old_detector_could_see(self):
        ticket = self._ticket(priority="urgent")
        now = datetime.now(timezone.utc)
        extra = copy.deepcopy(ticket.datos_extra)
        for field in (
            "first_response_due_at",
            "resolution_due_at",
            "next_update_due_at",
        ):
            extra["sla"][field] = (now - timedelta(minutes=5)).isoformat()
        extra["sla_breach_event_emitted_at"] = (now - timedelta(minutes=1)).isoformat()
        ticket.datos_extra = extra
        db.session.add(ticket)
        db.session.commit()

        breaches = detect_sla_breaches_for_tenant(self.tenant, materialize=True)
        db.session.commit()

        self.assertEqual(len(breaches), 1)
        # The old detector represented resolution only. The newly visible
        # first-response and next-update clocks must still be audited.
        self.assertEqual(
            breaches[0]["newly_breached_clocks"],
            ["first_response", "next_update"],
        )
        event = AuditEvent.query.filter_by(
            tenant_id=self.tenant.id,
            event_type="sla.breach_detected",
            resource_id=str(ticket.id),
        ).one()
        self.assertEqual(
            (event.details or {}).get("newly_breached_clocks"),
            ["first_response", "next_update"],
        )

    def test_record_ticket_event_works_in_worker_without_request_context(self):
        ticket = self._ticket()

        event = record_ticket_event(
            tenant_id=self.tenant.id,
            event_type="sla.worker_probe",
            ticket=ticket,
            details={"source": "worker"},
        )
        db.session.flush()

        self.assertIsNone(event.ip_address)
        persisted = db.session.get(AuditEvent, event.id)
        self.assertEqual(persisted.event_type, "sla.worker_probe")

    def test_public_operator_comment_satisfies_first_response_and_resets_update_clock(self):
        ticket = self._ticket(priority="urgent")
        before = datetime.now(timezone.utc)

        add_comment(
            tenant=self.tenant,
            actor_user=self.owner,
            ticket=ticket,
            body="Respuesta operativa",
            visibility="public",
        )

        sla = ticket.datos_extra["sla"]
        first_response = datetime.fromisoformat(sla["first_response_satisfied_at"])
        next_update = datetime.fromisoformat(sla["next_update_due_at"])
        self.assertGreaterEqual(first_response, before)
        self.assertEqual(sla["first_response_at"], sla["first_response_satisfied_at"])
        self.assertAlmostEqual((next_update - first_response).total_seconds(), 3600, delta=1)
        self.assertEqual(
            evaluate_sla_clocks(sla, ticket_status=ticket.estado)["clocks"]["first_response"]["status"],
            "satisfied",
        )
        serialized = serialize_ticket(ticket)
        self.assertEqual(serialized["sla"]["contract_version"], "ticket.sla.v1")
        self.assertEqual(serialized["sla"]["clocks"]["first_response"]["status"], "satisfied")
        self.assertEqual(
            serialized["sla"]["next_update_history"][0]["contract_version"],
            "ticket.sla.next_update_cycle.v1",
        )
        self.assertEqual(serialized["sla_evaluation"]["contract_version"], "ticket.sla.v1")

    def test_internal_comment_does_not_satisfy_public_response_clock(self):
        ticket = self._ticket()
        add_comment(
            tenant=self.tenant,
            actor_user=self.owner,
            ticket=ticket,
            body="Nota privada",
            visibility="internal",
        )

        self.assertNotIn("first_response_satisfied_at", ticket.datos_extra["sla"])

    def test_public_customer_comment_does_not_satisfy_operator_response_clock(self):
        ticket = self._ticket()
        add_comment(
            tenant=self.tenant,
            actor_user=self.customer,
            ticket=ticket,
            body="Seguimiento ciudadano",
            visibility="public",
        )

        self.assertNotIn("first_response_satisfied_at", ticket.datos_extra["sla"])

    def test_close_satisfies_resolution_and_reopen_starts_an_active_clock(self):
        ticket = self._ticket()

        patch_ticket(
            tenant=self.tenant,
            actor_user=self.owner,
            ticket=ticket,
            payload={"status": "cerrado"},
        )
        self.assertTrue(ticket.datos_extra["sla"].get("resolution_satisfied_at"))
        self.assertEqual(
            evaluate_sla_clocks(ticket.datos_extra["sla"], ticket_status=ticket.estado)["state"],
            "closed",
        )

        patch_ticket(
            tenant=self.tenant,
            actor_user=self.owner,
            ticket=ticket,
            payload={"status": "nuevo"},
        )
        self.assertNotIn("resolution_satisfied_at", ticket.datos_extra["sla"])
        self.assertNotIn("resolved_at", ticket.datos_extra["sla"])
        self.assertTrue(ticket.datos_extra["sla"].get("resolution_due_at"))

    def test_durable_public_reply_persists_sla_facts_in_same_transaction(self):
        ticket = self._ticket(priority="urgent")

        result = ServicioTickets().crear_respuesta_tenant(
            ticket,
            {
                "body": "Respuesta durable",
                "visibility": "public",
                "actor_user_id": self.owner.id,
                "actor_name": self.owner.name,
                "actor_role": self.owner.rol,
                "requested_channels": [],
                "emit_socket": False,
            },
            idempotency_key="sla-reply-0001",
            idempotency_tenant_id=self.tenant.id,
        )

        self.assertFalse(result["replayed"])
        db.session.expire_all()
        persisted = db.session.get(TenantTicket, ticket.id)
        sla = persisted.datos_extra["sla"]
        self.assertTrue(sla.get("first_response_satisfied_at"))
        event_at = datetime.fromisoformat(result["event"]["created_at"])
        next_update = datetime.fromisoformat(sla["next_update_due_at"])
        self.assertAlmostEqual((next_update - event_at).total_seconds(), 3600, delta=1)
        self.assertEqual(len(sla["next_update_history"]), 1)
        self.assertAlmostEqual(
            (
                datetime.fromisoformat(sla["next_update_history"][0]["satisfied_at"])
                - event_at
            ).total_seconds(),
            0,
            delta=0.001,
        )
        history_before_replay = copy.deepcopy(sla["next_update_history"])

        replay = ServicioTickets().crear_respuesta_tenant(
            persisted,
            {
                "body": "Respuesta durable",
                "visibility": "public",
                "actor_user_id": self.owner.id,
                "actor_name": self.owner.name,
                "actor_role": self.owner.rol,
                "requested_channels": [],
                "emit_socket": False,
            },
            idempotency_key="sla-reply-0001",
            idempotency_tenant_id=self.tenant.id,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            replay["ticket"].datos_extra["sla"]["next_update_due_at"],
            sla["next_update_due_at"],
        )
        self.assertEqual(
            replay["ticket"].datos_extra["sla"]["next_update_history"],
            history_before_replay,
        )

    def test_cross_tenant_comment_cannot_append_next_update_history(self):
        ticket = self._ticket(priority="urgent")
        other_tenant = TenantProfile(
            slug="sla-other-tenant",
            nombre="SLA Other Tenant",
            tipo="municipio",
            municipio_id=self.owner.id,
            configuracion={},
        )
        db.session.add(other_tenant)
        db.session.flush()
        before = copy.deepcopy(ticket.datos_extra["sla"])

        with self.assertRaisesRegex(LookupError, "ticket_not_found"):
            add_comment(
                tenant=other_tenant,
                actor_user=self.owner,
                ticket=ticket,
                body="Intento cross-tenant",
                visibility="public",
            )

        self.assertEqual(ticket.datos_extra["sla"], before)
        self.assertNotIn("next_update_history", ticket.datos_extra["sla"])

    def test_durable_public_non_operator_reply_does_not_satisfy_operator_clock(self):
        ticket = self._ticket(priority="urgent")

        result = ServicioTickets().crear_respuesta_tenant(
            ticket,
            {
                "body": "Mensaje ciudadano durable",
                "visibility": "public",
                "actor_user_id": self.customer.id,
                "actor_name": self.customer.name,
                "actor_role": self.customer.rol,
                "requested_channels": [],
                "emit_socket": False,
            },
            idempotency_key="sla-customer-reply-0001",
            idempotency_tenant_id=self.tenant.id,
        )

        self.assertFalse(result["replayed"])
        self.assertNotIn(
            "first_response_satisfied_at",
            result["ticket"].datos_extra["sla"],
        )


if __name__ == "__main__":
    unittest.main()
