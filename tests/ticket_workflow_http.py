"""Normal native login and original HTTP guards in a disposable offline app."""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == "__main__":
    prepare_process()

import copy
import tempfile
import unittest


class TicketWorkflowHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        from models import User, TenantProfile
        cls.temp = tempfile.TemporaryDirectory(prefix="chatboc-workflow-")
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)
        with cls.app.app_context():
            cls.users = {u.id: {k: copy.deepcopy(getattr(u, k)) for k in
                ("rol", "tenant_id", "tenant_slug", "municipio_id", "pyme_id", "empresa_id", "accesibilidad", "es_empleado", "ticket_categorias", "tipo_chat")}
                for u in User.query.all()}
            cls.tenant_ids = [t.id for t in TenantProfile.query.all()]

    @classmethod
    def tearDownClass(cls):
        from models import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.temp.cleanup()

    def setUp(self):
        from models import db, MunicipioTicket, PymeTicket, TenantProfile, TicketComentario, TicketSatisfaccion, User
        self.context = self.app.app_context()
        self.context.push()
        db.session.rollback()
        TicketComentario.query.delete()
        TicketSatisfaccion.query.delete()
        MunicipioTicket.query.delete()
        PymeTicket.query.delete()
        TenantProfile.query.filter(TenantProfile.id.notin_(self.tenant_ids)).delete()
        for ident, values in self.users.items():
            user = db.session.get(User, ident)
            for key, value in values.items():
                setattr(user, key, copy.deepcopy(value))
        self.owner = db.session.get(User, self.accounts["acceptance-a"]["id"])
        self.employee = db.session.get(User, self.accounts["viewer"]["id"])
        self.employee.es_empleado = True
        self.employee.ticket_categorias = "Sugerencia"
        self.tenant = db.session.get(TenantProfile, self.accounts["acceptance-a"]["tenant_id"])
        self.ticket = MunicipioTicket(tenant_id=self.tenant.id, municipio_id=self.owner.id,
            pregunta="Synthetic case", categoria="Sugerencia", estado="nuevo", nro_ticket="700001",
            asignado_a_id=self.employee.id)
        self.foreign = MunicipioTicket(tenant_id=self.accounts["acceptance-b"]["tenant_id"],
            municipio_id=self.accounts["acceptance-b"]["id"], pregunta="Synthetic foreign case",
            categoria="Sugerencia", estado="nuevo", nro_ticket="700002")
        db.session.add_all([self.ticket, self.foreign])
        db.session.commit()
        self.ticket_id = self.ticket.id
        self.endpoint = f"/api/tickets/municipio/{self.ticket_id}/estado"
        self.client = self.login()

    def tearDown(self):
        from models import db
        db.session.rollback()
        db.session.remove()
        self.context.pop()

    def login(self, account="acceptance-a"):
        client = self.app.test_client()
        result = client.post("/api/auth/admin/login", json={
            "email": self.accounts[account]["email"], "password": self.password})
        self.assertEqual(result.status_code, 200, result.get_json())
        token = result.get_json().get("token") or result.get_json().get("access_token")
        self.assertIsInstance(token, str)
        client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + token
        return client

    def put(self, *, client=None, **fields):
        return (client or self.client).put(self.endpoint, headers={"X-Tenant-Slug": self.tenant.slug},
            json={"estado": "en_proceso", "expected_estado": "nuevo", **fields})

    def assert_unchanged(self):
        from models import db, MunicipioTicket, TicketComentario, TicketSatisfaccion
        db.session.expire_all()
        self.assertEqual(db.session.get(MunicipioTicket, self.ticket_id).estado, "nuevo")
        self.assertEqual(TicketComentario.query.count(), 0)
        self.assertEqual(TicketSatisfaccion.query.count(), 0)

    def test_list_detail_and_legacy_inbox_publish_same_exact_private_policy(self):
        responses = [
            self.client.get("/api/tickets", query_string={"tenant_slug": self.tenant.slug, "include": "compact"}),
            self.client.get(f"/api/tickets/municipio/{self.ticket_id}", headers={"X-Tenant-Slug": self.tenant.slug}),
            self.client.get(f"/api/v2/inbox/omnichannel/{self.ticket_id}",
                query_string={"source_model": "MunicipioTicket", "tenant_slug": self.tenant.slug}),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 200, response.get_json())
        listed = responses[0].get_json()
        rows = listed.get("tickets", listed.get("items", []))
        detail = responses[1].get_json()
        inbox = responses[2].get_json()["item"]
        workflows = [next(t for t in rows if t["id"] == self.ticket_id)["workflow"], detail["workflow"], inbox["workflow"]]
        for workflow in workflows:
            self.assertEqual(workflow, workflows[0])
            self.assertEqual(workflow["contract_version"], "ticket.workflow.instance.v2")
            self.assertEqual(workflow["identity"], {"tenant_id": self.tenant.id, "tenant_slug": self.tenant.slug,
                "ticket_id": self.ticket_id, "source_model": "MunicipioTicket"})
            self.assertEqual(workflow["next_states"], ["en_proceso", "cerrado"])
            self.assertTrue(workflow["can_transition"])
            self.assertFalse(workflow["final_state"])
            self.assertEqual(workflow["mutation"]["endpoint"], self.endpoint)

    def test_success_is_once_and_response_republishes_current_policy(self):
        first = self.put()
        self.assertEqual(first.status_code, 200, first.get_json())
        workflow = first.get_json()["workflow"]
        self.assertEqual(workflow["current_state"], "en_proceso")
        self.assertEqual(workflow["mutation"]["expected_estado"], "en_proceso")
        second = self.put()
        self.assertEqual(second.status_code, 409, second.get_json())
        from models import TicketComentario, TicketSatisfaccion
        self.assertEqual(TicketComentario.query.count(), 1)
        self.assertEqual(TicketSatisfaccion.query.count(), 0)

    def test_missing_invalid_expected_state_has_no_effect(self):
        for expected in (None, "", "unsupported", True, {"estado": "nuevo"}, ["nuevo"]):
            with self.subTest(expected_type=type(expected).__name__):
                response = self.put(expected_estado=expected)
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(response.get_json()["reason_code"], "workflow_expected_state_invalid")
                self.assert_unchanged()
        response = self.client.put(self.endpoint, json={"estado": "en_proceso"})
        self.assertEqual(response.status_code, 400)
        self.assert_unchanged()

    def test_stale_expected_state_and_illegal_edge_have_no_effect(self):
        response = self.put(expected_estado="en_proceso", estado="cerrado")
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assert_unchanged()
        for state in ("nuevo", "en_vivo", "esperando_agente_en_vivo"):
            response = self.put(estado=state)
            self.assertEqual(response.status_code, 422, response.get_json())
            self.assert_unchanged()

    def test_closed_alias_is_final_and_cannot_be_reopened_by_state_put(self):
        response = self.put(estado="resuelto")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["estado"], "cerrado")
        self.assertTrue(response.get_json()["workflow"]["final_state"])
        self.assertEqual(response.get_json()["workflow"]["next_states"], [])
        response = self.put(expected_estado="resuelto", estado="en_proceso")
        self.assertEqual(response.status_code, 422, response.get_json())
        from models import TicketComentario, TicketSatisfaccion
        self.assertEqual(TicketComentario.query.count(), 1)
        self.assertEqual(TicketSatisfaccion.query.count(), 0)

    def test_employee_requires_both_category_and_current_assignment(self):
        from models import db
        employee_client = self.login("viewer")
        own = employee_client.get(f"/api/tickets/municipio/{self.ticket_id}")
        self.assertEqual(own.status_code, 200, own.get_json())
        self.assertTrue(own.get_json()["workflow"]["can_transition"])
        self.ticket.asignado_a_id = self.owner.id
        db.session.commit()
        detail = employee_client.get(f"/api/tickets/municipio/{self.ticket_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["workflow"]["next_states"], [])
        self.assertEqual(self.put(client=employee_client).status_code, 403)
        self.assert_unchanged()
        self.ticket.asignado_a_id = self.employee.id
        self.employee.ticket_categorias = "Otra categoría"
        db.session.commit()
        self.assertIn(self.put(client=employee_client).status_code, (403, 404))
        self.assert_unchanged()

    def test_read_role_or_scoped_read_grant_never_implies_write(self):
        from models import db
        self.owner.rol = "supervisor"
        db.session.commit()
        detail = self.client.get(f"/api/tickets/municipio/{self.ticket_id}")
        self.assertEqual(detail.status_code, 200, detail.get_json())
        self.assertFalse(detail.get_json()["workflow"]["can_transition"])
        self.assertEqual(detail.get_json()["workflow"]["blocked_reason"], "workflow_role_forbidden")
        self.assertEqual(self.put().status_code, 403)
        self.assert_unchanged()

    def test_headers_query_and_body_unknown_foreign_or_conflicting_never_fallback(self):
        combinations = [
            ({"X-Tenant-Slug": "missing"}, {}, {}),
            ({"X-Tenant-Slug": "acceptance-b"}, {}, {}),
            ({"X-Tenant-Slug": self.tenant.slug}, {"tenant": "acceptance-b"}, {}),
            ({"X-Tenant-Slug": self.tenant.slug}, {}, {"tenant_slug": "acceptance-b"}),
            ({}, {}, {"tenant_id": self.accounts["acceptance-b"]["tenant_id"]}),
            ({}, {"tenant_slug": "missing"}, {}),
            ({"X-Tenant-Slug": ""}, {}, {}),
            ({}, {}, {"tenant_slug": None}),
            ({}, {"tenant_id": str(self.accounts["acceptance-b"]["tenant_id"])}, {"tenant_slug": self.tenant.slug}),
        ]
        for headers, query, body in combinations:
            with self.subTest(headers=list(headers), query=list(query), body=list(body)):
                response = self.client.put(self.endpoint, headers=headers, query_string=query,
                    json={"estado": "en_proceso", "expected_estado": "nuevo", **body})
                self.assertIn(response.status_code, (400, 403))
                self.assert_unchanged()
                denied = self.client.get(f"/api/tickets/municipio/{self.ticket_id}", headers=headers,
                    query_string={**query, **body}) if not body or body.get("tenant_slug") is not None else None
                if denied is not None:
                    self.assertIn(denied.status_code, (400, 403, 404))

    def test_foreign_ticket_and_contradictory_actor_membership_are_denied(self):
        from models import db
        response = self.client.put(f"/api/tickets/municipio/{self.foreign.id}/estado",
            json={"estado": "en_proceso", "expected_estado": "nuevo"})
        self.assertEqual(response.status_code, 404)
        self.owner.municipio_id = self.accounts["acceptance-b"]["id"]
        db.session.commit()
        self.assertIn(self.put().status_code, (403, 404))
        self.assertEqual(self.client.get(f"/api/tickets/municipio/{self.ticket_id}").status_code, 404)
        self.assert_unchanged()

    def test_unique_municipal_legacy_ticket_supported_ambiguous_owner_quarantined(self):
        from models import db, TenantProfile
        self.ticket.tenant_id = None
        db.session.commit()
        own = self.client.get(f"/api/tickets/municipio/{self.ticket_id}")
        self.assertEqual(own.status_code, 200, own.get_json())
        self.assertTrue(own.get_json()["workflow"]["can_transition"])
        db.session.add(TenantProfile(slug="ambiguous-owner", nombre="Ambiguous", tipo="municipio", municipio_id=self.owner.id))
        db.session.commit()
        self.assertEqual(self.client.get(f"/api/tickets/municipio/{self.ticket_id}").status_code, 404)
        self.assertIn(self.put().status_code, (403, 404))
        self.assert_unchanged()

    def test_anonymous_disabled_and_demo_do_not_gain_workflow_write(self):
        from models import db
        anon = self.app.test_client().put(self.endpoint, json={"estado": "cerrado", "expected_estado": "nuevo"})
        self.assertEqual(anon.status_code, 401)
        for metadata in ({"auth": {"disabled": True}}, {"auth": {"demo": {"restricted": True}}}):
            self.owner.accesibilidad = metadata
            db.session.commit()
            self.assertIn(self.put().status_code, (401, 403))
            self.assert_unchanged()

    def test_static_widget_credential_does_not_gain_workflow_write(self):
        from models import db, User
        owner = db.session.get(User, self.owner.id)
        import secrets
        credential = secrets.token_urlsafe(32)
        owner.entity_token = credential
        owner.token = credential
        db.session.commit()
        self.assertIsInstance(credential, str)
        result = self.app.test_client().put(self.endpoint,
            headers={"Authorization": "Bearer " + credential},
            json={"estado": "cerrado", "expected_estado": "nuevo"})
        self.assertIn(result.status_code, (401, 403))
        self.assert_unchanged()

    def test_public_tracking_and_tenant_ticket_do_not_inherit_legacy_workflow(self):
        from flask import g
        from models import db, TenantTicket
        from routes.ticket import _serialize_ticket_details
        # The actual public serializer intentionally excludes internal policy.
        with self.app.test_request_context():
            g.current_user = self.owner
            public = _serialize_ticket_details(self.ticket, "municipio", include_internal=False)
            self.assertNotIn("workflow", public)
            self.assertNotIn("next_states", public)
        ticket = TenantTicket(tenant_id=self.tenant.id, descripcion="Synthetic V2 case", estado="nuevo", origen="crm")
        db.session.add(ticket)
        db.session.commit()
        response = self.client.get(f"/api/v2/tickets/{ticket.id}", headers={"X-Tenant-Slug": self.tenant.slug})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertNotIn("workflow", payload)
        self.assertNotIn("next_states", payload)

    def test_shared_socket_snapshot_never_transfers_an_actors_workflow(self):
        from flask import g
        from routes.ticket import serialize_ticket_to_json
        # The same real serializer serves the shared SocketIO events. The
        # default must omit policy even when the requesting actor can write.
        with self.app.test_request_context():
            for actor in (self.owner, self.employee):
                g.current_user = actor
                private = serialize_ticket_to_json(self.ticket, "municipio", compact=True, publish_workflow=True)
                self.assertTrue(private["workflow"]["can_transition"])
                shared = serialize_ticket_to_json(self.ticket, "municipio", compact=True)
                self.assertNotIn("workflow", shared)
                self.assertNotIn("next_states", shared)

    def test_superadmin_json_selection_remains_verified_in_response_policy(self):
        from flask import g
        from models import db
        from services.ticket_workflow_policy import build_workflow_instance, resolve_private_ticket_tenant, TicketWorkflowError
        import os
        old_allowlist = os.environ.get("CLERK_SUPERADMIN_EMAILS")
        self.owner.rol = "super_admin"
        db.session.commit()
        os.environ["CLERK_SUPERADMIN_EMAILS"] = self.owner.email
        try:
            # Policy regression only: no fabricated Clerk session or claim of
            # actual SuperAdmin HTTP authentication is made by this test.
            with self.app.test_request_context(self.endpoint, method="PUT", json={
                    "tenant_slug": self.tenant.slug, "estado": "en_proceso", "expected_estado": "nuevo"}):
                g.current_user = self.owner
                selected = resolve_private_ticket_tenant(self.owner, body={"tenant_slug": self.tenant.slug})
                workflow = build_workflow_instance(self.ticket, "municipio", actor=self.owner, tenant=selected)
                self.assertTrue(workflow["can_transition"])
                self.assertEqual(workflow["identity"]["tenant_id"], self.tenant.id)
                self.assertIsNone(workflow["blocked_reason"])
            os.environ["CLERK_SUPERADMIN_EMAILS"] = "other-platform@example.invalid"
            with self.app.test_request_context(self.endpoint, method="PUT", json={"tenant_slug": self.tenant.slug}):
                denied = build_workflow_instance(self.ticket, "municipio", actor=self.owner, tenant=self.tenant)
                self.assertEqual(denied["blocked_reason"], "workflow_role_forbidden")
                self.assertEqual(denied["next_states"], [])
                self.assertFalse(denied["can_transition"])
        finally:
            if old_allowlist is None:
                os.environ.pop("CLERK_SUPERADMIN_EMAILS", None)
            else:
                os.environ["CLERK_SUPERADMIN_EMAILS"] = old_allowlist

    def test_explicit_list_verifies_tenant_context_once_not_per_ticket(self):
        from models import db, MunicipioTicket
        from sqlalchemy import event
        def read_count():
            statements = []
            def capture(connection, cursor, statement, parameters, context, executemany):
                if "from tenant_profile" in statement.lower():
                    statements.append(statement)
            db.session.expire_all()
            event.listen(db.engine, "before_cursor_execute", capture)
            try:
                response = self.client.get("/api/tickets", query_string={
                    "tenant_slug": self.tenant.slug, "include": "compact", "per_page": 100})
                self.assertEqual(response.status_code, 200, response.get_json())
            finally:
                event.remove(db.engine, "before_cursor_execute", capture)
            return len(statements)
        one = read_count()
        db.session.add_all([MunicipioTicket(tenant_id=self.tenant.id, municipio_id=self.owner.id,
            nro_ticket=str(710000 + i), pregunta="Synthetic page case", categoria="Sugerencia", estado="nuevo")
            for i in range(25)])
        db.session.commit()
        many = read_count()
        self.assertLessEqual(many, one + 2, "Explicit tenant context queries must not grow per row")

    def test_real_database_comment_failure_rolls_back_state_before_notification(self):
        from models import db
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        db.session.execute(text("CREATE TRIGGER reject_workflow_comment BEFORE INSERT ON ticket_comentario "
            "BEGIN SELECT RAISE(ABORT, 'Synthetic comment failure'); END"))
        db.session.commit()
        try:
            with self.assertRaises(IntegrityError):
                self.put(estado="cerrado")
            self.assert_unchanged()
        finally:
            db.session.execute(text("DROP TRIGGER reject_workflow_comment"))
            db.session.commit()

    def test_pyme_explicit_scope_is_supported_without_inventing_legacy_owner_column(self):
        from models import db, PymeTicket, TenantProfile, User
        actor = db.session.get(User, self.accounts["second"]["id"])
        actor.tipo_chat = "pyme"
        actor.municipio_id = None
        tenant = TenantProfile(slug="workflow-pyme", nombre="Pyme", tipo="pyme", pyme_id=actor.id)
        db.session.add(tenant)
        db.session.flush()
        actor.tenant_id = tenant.id
        actor.tenant_slug = tenant.slug
        ticket = PymeTicket(tenant_id=tenant.id, nro_ticket=700003, pregunta="Synthetic business", estado="nuevo")
        db.session.add(ticket)
        db.session.commit()
        client = self.login("second")
        endpoint = f"/api/tickets/pyme/{ticket.id}/estado"
        response = client.put(endpoint, headers={"X-Tenant-Slug": tenant.slug},
            json={"estado": "en_proceso", "expected_estado": "nuevo"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["workflow"]["identity"]["source_model"], "PymeTicket")
        ticket.tenant_id = None
        db.session.commit()
        response = client.put(endpoint, headers={"X-Tenant-Slug": tenant.slug},
            json={"estado": "cerrado", "expected_estado": "en_proceso"})
        self.assertEqual(response.status_code, 404, response.get_json())


if __name__ == "__main__":
    unittest.main()
