from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import patch

import jwt
from flask import g
from sqlalchemy.exc import IntegrityError, OperationalError

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app
from config import Config
from database import db
from models import (
    EncEncuesta,
    SurveyDraft,
    SurveyDraftMaterialization,
    SurveyDraftMaterializationAlias,
    TenantProfile,
    User,
)
from services.encuestas_service import (
    EncuestaError,
    _normalize_logical_ref,
    delete_encuesta,
)
from services import survey_draft_materialization as materialization_service


class SurveyMaterializationTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class SurveyDraftMaterializationApiTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SurveyMaterializationTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._save_counter = 0

        self.admin_1 = self._create_user(
            "materialize-a@test.com", "admin", "tenant-materialize-a"
        )
        self.tenant_1 = TenantProfile(
            slug="tenant-materialize-a",
            nombre="Tenant Materialize A",
            tipo="municipio",
            pyme_id=self.admin_1.id,
            plan="full",
        )
        db.session.add(self.tenant_1)
        db.session.commit()
        self.admin_1.tenant_id = self.tenant_1.id

        self.admin_2 = self._create_user(
            "materialize-b@test.com", "admin", "tenant-materialize-b"
        )
        self.tenant_2 = TenantProfile(
            slug="tenant-materialize-b",
            nombre="Tenant Materialize B",
            tipo="municipio",
            pyme_id=self.admin_2.id,
            plan="full",
        )
        db.session.add(self.tenant_2)
        db.session.commit()
        self.admin_2.tenant_id = self.tenant_2.id
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _create_user(self, email: str, role: str, tenant_slug: str) -> User:
        user = User(
            name=email.split("@")[0],
            email=email,
            rol=role,
            tenant_slug=tenant_slug,
        )
        user.set_password("secret123")
        db.session.add(user)
        db.session.flush()
        return user

    def _auth(self, user: User, tenant: TenantProfile) -> dict[str, str]:
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
            "X-Tenant-Slug": tenant.slug,
        }

    def _document(
        self,
        *,
        document_ref: str = "survey-doc-neighborhood-2026",
        title: str = "Prioridades del barrio",
        question_type: str = "single_choice",
    ) -> dict:
        first_options = (
            []
            if question_type == "free_text"
            else [
                {
                    "option_ref": "option-lighting",
                    "order": 1,
                    "label": "Iluminacion",
                    "value": "lighting",
                    "extensions": {},
                },
                {
                    "option_ref": "option-streets",
                    "order": 2,
                    "label": "Calles",
                    "value": "streets",
                    "extensions": {},
                },
            ]
        )
        questions = [
            {
                "question_ref": "question-priority",
                "order": 1,
                "type": question_type,
                "prompt": "¿Que deberiamos mejorar primero?",
                "required": True,
                "selection": {"min": None, "max": None},
                "visibility": None,
                "options": first_options,
                "extensions": {},
            }
        ]
        if question_type in {"single_choice", "multiple_choice"}:
            questions.append(
                {
                    "question_ref": "question-lighting-detail",
                    "order": 2,
                    "type": "free_text",
                    "prompt": "¿En que cuadra falta iluminacion?",
                    "required": False,
                    "selection": {"min": None, "max": None},
                    "visibility": {
                        "kind": "option_selected",
                        "question_ref": "question-priority",
                        "option_ref": "option-lighting",
                    },
                    "options": [],
                    "extensions": {},
                }
            )
        return {
            "schema_version": "survey-document.v1",
            "document_ref": document_ref,
            "title": title,
            "slug": None,
            "description": "Instrumento canonico sin coerciones",
            "survey_type": "votacion",
            "schedule": {"starts_at": None, "ends_at": None},
            "policies": {
                "uniqueness": "por_cookie",
                "anonymous": True,
                "requires_contact_data": False,
            },
            "experience": {
                "live_voting": True,
                "show_live_results": True,
                "allow_comments": False,
                "reward_points": 0,
            },
            "questions": questions,
            "extensions": {},
        }

    def _v2_document(self) -> dict:
        document = self._document(question_type="multiple_choice")
        document["schema_version"] = "survey-document.v2"
        detail_question = document["questions"].pop()
        detail_question["order"] = 3
        detail_question["visibility"] = {
            "version": 2,
            "root": {
                "kind": "group",
                "operator": "and",
                "children": [
                    {
                        "kind": "option_selected",
                        "question_ref": "question-priority",
                        "option_ref": "option-lighting",
                    },
                    {
                        "kind": "group",
                        "operator": "or",
                        "children": [
                            {
                                "kind": "option_selected",
                                "question_ref": "question-priority",
                                "option_ref": "option-streets",
                            },
                            {
                                "kind": "option_selected",
                                "question_ref": "question-channel",
                                "option_ref": "option-whatsapp",
                            },
                        ],
                    },
                ],
            },
        }
        document["questions"].extend(
            [
                {
                    "question_ref": "question-channel",
                    "order": 2,
                    "type": "single_choice",
                    "prompt": "Â¿Por que canal queres recibir novedades?",
                    "required": False,
                    "selection": {"min": None, "max": None},
                    "visibility": None,
                    "options": [
                        {
                            "option_ref": "option-email",
                            "order": 1,
                            "label": "Email",
                            "value": "email",
                            "extensions": {},
                        },
                        {
                            "option_ref": "option-whatsapp",
                            "order": 2,
                            "label": "WhatsApp",
                            "value": "whatsapp",
                            "extensions": {},
                        },
                    ],
                    "extensions": {},
                },
                detail_question,
            ]
        )
        return document

    def _save_draft(
        self,
        draft_id: str,
        *,
        document: dict | None = None,
        tenant: TenantProfile | None = None,
        user: User | None = None,
        revision: int | None = None,
        align_document_ref: bool = True,
    ):
        tenant = tenant or self.tenant_1
        user = user or self.admin_1
        document_payload = dict(document or self._document())
        if align_document_ref:
            document_payload["document_ref"] = draft_id
        body = {"draft_id": draft_id, **document_payload}
        if revision is not None:
            body["revision"] = revision
        self._save_counter += 1
        return self.client.post(
            "/api/v2/surveys/draft",
            json=body,
            headers={
                **self._auth(user, tenant),
                "Idempotency-Key": f"save-{draft_id}-{self._save_counter}",
            },
        )

    def _materialize(
        self,
        draft_id: str,
        *,
        revision: int = 1,
        key: str = "materialize-neighborhood-001",
        tenant: TenantProfile | None = None,
        user: User | None = None,
    ):
        tenant = tenant or self.tenant_1
        user = user or self.admin_1
        return self.client.post(
            f"/api/v2/surveys/drafts/{draft_id}/materialize",
            json={"expected_revision": revision},
            headers={**self._auth(user, tenant), "Idempotency-Key": key},
        )

    def test_materializes_canonical_document_and_preserves_stable_refs(self):
        document = self._document()
        document["extensions"] = {"durable_builder": {"source": "survey_builder"}}
        document["questions"][0]["extensions"] = {"admin": {"color": "blue"}}
        document["questions"][0]["options"][0]["extensions"] = {
            "admin": {"icon": "lightbulb"}
        }
        saved = self._save_draft("draft-neighborhood", document=document)
        self.assertEqual(saved.status_code, 200, saved.get_json())

        response = self._materialize("draft-neighborhood")

        self.assertEqual(response.status_code, 201, response.get_json())
        body = response.get_json()
        self.assertEqual(body["contract_version"], "surveys.materialization.v1")
        self.assertFalse(body["replayed"])
        self.assertEqual(body["draft"]["schema_version"], "survey-document.v1")
        self.assertEqual(body["draft"]["document_ref"], "draft-neighborhood")
        self.assertEqual(body["survey"]["document_ref"], "draft-neighborhood")
        self.assertEqual(body["survey"]["preguntas"][0]["question_ref"], "question-priority")
        self.assertEqual(
            body["survey"]["preguntas"][0]["opciones"][0]["option_ref"],
            "option-lighting",
        )
        self.assertEqual(
            body["survey"]["preguntas"][1]["conditional_logic"],
            {
                "version": 1,
                "show_if": {"question_order": 1, "option_order": 1},
            },
        )
        self.assertEqual(EncEncuesta.query.count(), 1)
        self.assertEqual(SurveyDraft.query.count(), 1)
        self.assertEqual(SurveyDraftMaterialization.query.count(), 1)
        self.assertEqual(SurveyDraftMaterializationAlias.query.count(), 1)
        stored_draft = SurveyDraft.query.one()
        self.assertEqual(
            stored_draft.payload["extensions"]["durable_builder"]["source"],
            "survey_builder",
        )

        detail = self.client.get(
            f"/api/v2/surveys/{body['survey_id']}",
            headers=self._auth(self.admin_1, self.tenant_1),
        )
        self.assertEqual(detail.status_code, 200, detail.get_json())
        self.assertEqual(detail.get_json()["preguntas"][0]["question_ref"], "question-priority")

        published = self.client.post(
            f"/api/v2/surveys/{body['survey_id']}/publish",
            headers=self._auth(self.admin_1, self.tenant_1),
        )
        self.assertEqual(published.status_code, 200, published.get_json())
        public = self.client.get(
            f"/api/v2/public/surveys/{published.get_json()['public_token']}"
        )
        self.assertEqual(public.status_code, 200, public.get_json())
        self.assertEqual(public.get_json()["preguntas"][0]["question_ref"], "question-priority")
        self.assertEqual(
            public.get_json()["preguntas"][0]["opciones"][0]["option_ref"],
            "option-lighting",
        )

    def test_materializes_nested_v2_visibility_with_refs_and_replays_idempotently(self):
        document = self._v2_document()
        expected_root = document["questions"][2]["visibility"]["root"]
        saved = self._save_draft("draft-visibility-v2", document=document)
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual(saved.get_json()["schema_version"], "survey-document.v2")

        first = self._materialize(
            "draft-visibility-v2",
            key="materialize-visibility-v2-001",
        )

        self.assertEqual(first.status_code, 201, first.get_json())
        body = first.get_json()
        self.assertEqual(body["draft"]["schema_version"], "survey-document.v2")
        self.assertEqual(
            body["survey"]["preguntas"][2]["conditional_logic"],
            {"version": 2, "show_if": expected_root},
        )
        serialized_logic = body["survey"]["preguntas"][2]["conditional_logic"]
        self.assertNotIn("question_order", str(serialized_logic))
        self.assertNotIn("option_order", str(serialized_logic))

        replay = self._materialize(
            "draft-visibility-v2",
            key="materialize-visibility-v2-001",
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["replayed"])
        self.assertEqual(replay.get_json()["survey_id"], body["survey_id"])
        self.assertEqual(replay.get_json()["receipt_id"], body["receipt_id"])
        self.assertEqual(EncEncuesta.query.count(), 1)
        self.assertEqual(SurveyDraftMaterialization.query.count(), 1)
        self.assertEqual(SurveyDraftMaterializationAlias.query.count(), 1)

    def test_v2_visibility_rejects_invalid_ast_dangling_and_wrong_owner_refs(self):
        def visibility(document: dict) -> dict:
            return document["questions"][2]["visibility"]

        cases = []

        extra_key = self._v2_document()
        visibility(extra_key)["unexpected"] = True
        cases.append(("closed-visibility", extra_key))

        boolean_version = self._v2_document()
        visibility(boolean_version)["version"] = True
        cases.append(("exact-integer-version", boolean_version))

        root_leaf = self._v2_document()
        visibility(root_leaf)["root"] = {
            "kind": "option_selected",
            "question_ref": "question-priority",
            "option_ref": "option-lighting",
        }
        cases.append(("root-must-be-group", root_leaf))

        dangling_question = self._v2_document()
        visibility(dangling_question)["root"]["children"][0][
            "question_ref"
        ] = "question-missing"
        cases.append(("dangling-question", dangling_question))

        wrong_owner = self._v2_document()
        visibility(wrong_owner)["root"]["children"][0][
            "option_ref"
        ] = "option-whatsapp"
        cases.append(("wrong-option-owner", wrong_owner))

        contradictory_single_choice = self._v2_document()
        contradictory_single_choice["questions"][0]["type"] = "single_choice"
        visibility(contradictory_single_choice)["root"]["children"] = [
            {
                "kind": "option_selected",
                "question_ref": "question-priority",
                "option_ref": "option-lighting",
            },
            {
                "kind": "option_selected",
                "question_ref": "question-priority",
                "option_ref": "option-streets",
            },
        ]
        cases.append(("contradictory-single-choice-and", contradictory_single_choice))

        for index, (case_name, document) in enumerate(cases, start=1):
            with self.subTest(case=case_name):
                draft_id = f"draft-visibility-v2-invalid-{index}"
                saved = self._save_draft(draft_id, document=document)
                self.assertEqual(saved.status_code, 200, saved.get_json())
                rejected = self._materialize(
                    draft_id,
                    key=f"materialize-visibility-v2-invalid-{index}",
                )
                self.assertEqual(rejected.status_code, 422, rejected.get_json())
                self.assertEqual(
                    rejected.get_json()["reason_code"],
                    "survey_document_invalid",
                )

        self.assertEqual(EncEncuesta.query.count(), 0)
        self.assertEqual(SurveyDraftMaterialization.query.count(), 0)
        self.assertEqual(SurveyDraftMaterializationAlias.query.count(), 0)

    def test_supported_document_versions_preserve_v1_order_materialization(self):
        self.assertEqual(
            materialization_service.SUPPORTED_SCHEMA_VERSIONS,
            frozenset({"survey-document.v1", "survey-document.v2"}),
        )
        payload, document_ref = (
            materialization_service.canonical_document_to_encuesta_payload(
                self._document(),
                schema_version="survey-document.v1",
                draft_revision=1,
            )
        )
        self.assertEqual(document_ref, "survey-doc-neighborhood-2026")
        self.assertEqual(
            payload["preguntas"][1]["conditional_logic"],
            {
                "version": 1,
                "show_if": {"question_order": 1, "option_order": 1},
            },
        )

    def test_replay_survives_draft_advance_and_new_keys_bind_to_same_receipt(self):
        self.assertEqual(self._save_draft("draft-replay").status_code, 200)
        first = self._materialize(
            "draft-replay",
            key="materialize-replay-key-001",
        )
        self.assertEqual(first.status_code, 201, first.get_json())
        survey_id = first.get_json()["survey_id"]

        advanced_document = self._document(title="Version posterior del borrador")
        advanced = self._save_draft(
            "draft-replay",
            document=advanced_document,
            revision=1,
        )
        self.assertEqual(advanced.status_code, 200, advanced.get_json())
        self.assertEqual(advanced.get_json()["revision"], 2)

        same_key = self._materialize(
            "draft-replay",
            revision=1,
            key="materialize-replay-key-001",
        )
        alias_key = self._materialize(
            "draft-replay",
            revision=1,
            key="materialize-replay-key-002",
        )
        self.assertEqual(same_key.status_code, 200, same_key.get_json())
        self.assertEqual(alias_key.status_code, 200, alias_key.get_json())
        self.assertTrue(same_key.get_json()["replayed"])
        self.assertTrue(alias_key.get_json()["replayed"])
        self.assertEqual(same_key.get_json()["survey_id"], survey_id)
        self.assertEqual(alias_key.get_json()["survey_id"], survey_id)
        self.assertEqual(EncEncuesta.query.count(), 1)
        self.assertEqual(SurveyDraftMaterialization.query.count(), 1)
        self.assertEqual(SurveyDraftMaterializationAlias.query.count(), 2)

        self.assertEqual(self._save_draft("draft-other").status_code, 200)
        conflict = self._materialize(
            "draft-other",
            key="materialize-replay-key-002",
        )
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(
            conflict.get_json()["reason_code"],
            "survey_materialization_idempotency_conflict",
        )
        self.assertEqual(EncEncuesta.query.count(), 1)

    def test_replay_rejects_corrupt_cross_tenant_associations(self):
        cases = (
            ("alias", self.tenant_2, self.admin_2),
            ("survey", self.tenant_1, self.admin_1),
            ("draft", self.tenant_1, self.admin_1),
        )
        for index, (corrupt_target, request_tenant, request_user) in enumerate(cases, 1):
            with self.subTest(corrupt_target=corrupt_target):
                draft_id = f"draft-corrupt-scope-{index}"
                key = f"materialize-corrupt-scope-{index}"
                self.assertEqual(self._save_draft(draft_id).status_code, 200)
                created = self._materialize(draft_id, key=key)
                self.assertEqual(created.status_code, 201, created.get_json())

                receipt = SurveyDraftMaterialization.query.filter_by(
                    tenant_id=self.tenant_1.id,
                    draft_id=draft_id,
                    draft_revision=1,
                ).one()
                if corrupt_target == "alias":
                    SurveyDraftMaterializationAlias.query.filter_by(
                        materialization_id=receipt.id,
                        idempotency_key=key,
                    ).one().tenant_id = self.tenant_2.id
                elif corrupt_target == "survey":
                    receipt.survey.tenant_id = self.tenant_2.id
                else:
                    receipt.survey_draft.tenant_id = self.tenant_2.id
                db.session.commit()
                db.session.expire_all()

                replay = self._materialize(
                    draft_id,
                    key=key,
                    tenant=request_tenant,
                    user=request_user,
                )
                self.assertEqual(replay.status_code, 409, replay.get_json())
                self.assertEqual(
                    replay.get_json()["reason_code"],
                    "survey_materialization_receipt_corrupt",
                )
                self.assertNotIn("survey", replay.get_json())
                self.assertNotIn("survey_id", replay.get_json())

    def test_alias_replay_classifies_only_real_lock_errors_as_retryable(self):
        draft_id = "draft-alias-lock-classification"
        self.assertEqual(self._save_draft(draft_id).status_code, 200)
        created = self._materialize(
            draft_id,
            key="materialize-alias-lock-original",
        )
        self.assertEqual(created.status_code, 201, created.get_json())

        lock_error = OperationalError(
            "INSERT survey_draft_materialization_alias",
            {},
            Exception("database is locked"),
        )
        with patch(
            "services.survey_draft_materialization._persist_alias",
            side_effect=lock_error,
        ):
            locked = self._materialize(
                draft_id,
                key="materialize-alias-lock-retry",
            )
        self.assertEqual(locked.status_code, 409, locked.get_json())
        self.assertEqual(
            locked.get_json()["reason_code"],
            "survey_materialization_concurrent_conflict",
        )
        self.assertTrue(locked.get_json()["retryable"])

        schema_error = OperationalError(
            "INSERT survey_draft_materialization_alias",
            {},
            Exception("no such table: survey_draft_materialization_alias"),
        )
        with patch(
            "services.survey_draft_materialization._persist_alias",
            side_effect=schema_error,
        ):
            storage_failure = self._materialize(
                draft_id,
                key="materialize-alias-schema-error",
            )
        self.assertEqual(storage_failure.status_code, 500, storage_failure.get_json())
        self.assertEqual(
            storage_failure.get_json()["reason_code"],
            "survey_materialization_storage_error",
        )
        self.assertFalse(storage_failure.get_json()["retryable"])
        self.assertEqual(SurveyDraftMaterialization.query.count(), 1)
        self.assertEqual(SurveyDraftMaterializationAlias.query.count(), 1)

    def test_creation_tenant_mismatch_rolls_back_before_receipt(self):
        draft_id = "draft-create-tenant-mismatch"
        self.assertEqual(self._save_draft(draft_id).status_code, 200)
        real_create = materialization_service.create_encuesta

        def create_for_wrong_tenant(payload, user, *, commit=True, **server_metadata):
            survey = real_create(
                payload,
                user,
                commit=commit,
                **server_metadata,
            )
            survey.tenant_id = self.tenant_2.id
            db.session.flush()
            return survey

        with patch.object(
            materialization_service,
            "create_encuesta",
            side_effect=create_for_wrong_tenant,
        ):
            response = self._materialize(
                draft_id,
                key="materialize-create-tenant-mismatch",
            )

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "survey_materialization_tenant_invariant_failed",
        )
        self.assertEqual(EncEncuesta.query.filter_by(document_ref=draft_id).count(), 0)
        self.assertEqual(
            SurveyDraftMaterialization.query.filter_by(draft_id=draft_id).count(),
            0,
        )
        self.assertEqual(
            SurveyDraftMaterializationAlias.query.filter_by(
                idempotency_key="materialize-create-tenant-mismatch"
            ).count(),
            0,
        )

    def test_stale_unmaterialized_revision_fails_without_creating_survey(self):
        self.assertEqual(self._save_draft("draft-stale").status_code, 200)
        self.assertEqual(
            self._save_draft(
                "draft-stale",
                document=self._document(title="Revision dos"),
                revision=1,
            ).status_code,
            200,
        )

        response = self._materialize(
            "draft-stale",
            revision=1,
            key="materialize-stale-001",
        )

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "draft_revision_conflict")
        self.assertEqual(EncEncuesta.query.count(), 0)
        self.assertEqual(SurveyDraftMaterialization.query.count(), 0)

    def test_unsupported_schema_and_lossy_question_types_fail_closed(self):
        unsupported_schema = self._document()
        unsupported_schema["schema_version"] = "survey-builder.v2"
        self.assertEqual(
            self._save_draft(
                "draft-schema-v2",
                document=unsupported_schema,
            ).status_code,
            200,
        )
        schema_response = self._materialize(
            "draft-schema-v2",
            key="materialize-schema-v2",
        )
        self.assertEqual(schema_response.status_code, 422, schema_response.get_json())
        self.assertEqual(
            schema_response.get_json()["reason_code"],
            "unsupported_survey_document_schema",
        )

        for question_type in ("nps", "ranking", "location"):
            with self.subTest(question_type=question_type):
                document = self._document(question_type="single_choice")
                document["questions"] = [
                    {
                        **document["questions"][0],
                        "type": question_type,
                    }
                ]
                draft_id = f"draft-lossy-{question_type}"
                self.assertEqual(
                    self._save_draft(draft_id, document=document).status_code,
                    200,
                )
                rejected = self._materialize(
                    draft_id,
                    key=f"materialize-lossy-{question_type}",
                )
                self.assertEqual(rejected.status_code, 422, rejected.get_json())
                self.assertEqual(
                    rejected.get_json()["reason_code"],
                    "unsupported_survey_question_type",
                )

        quarantined_document = self._document()
        quarantined_document["questions"] = [
            {
                **quarantined_document["questions"][0],
                "type": "quarantined",
                "quarantine": {
                    "reason": "unsupported_question_type",
                    "source": "durable_builder",
                    "source_type": "nps",
                    "materializable": False,
                },
            }
        ]
        self.assertEqual(
            self._save_draft(
                "draft-quarantined-nps",
                document=quarantined_document,
            ).status_code,
            200,
        )
        quarantined = self._materialize(
            "draft-quarantined-nps",
            key="materialize-quarantined-nps",
        )
        self.assertEqual(quarantined.status_code, 422, quarantined.get_json())
        self.assertEqual(
            quarantined.get_json()["reason_code"],
            "unsupported_survey_question_type",
        )
        self.assertEqual(quarantined.get_json()["question_type"], "nps")

        self.assertEqual(EncEncuesta.query.count(), 0)
        self.assertEqual(SurveyDraftMaterialization.query.count(), 0)

    def test_empty_questions_fail_closed_and_string_option_values_are_lossless(self):
        empty_document = self._document()
        empty_document["questions"] = []
        self.assertEqual(
            self._save_draft("draft-empty-questions", document=empty_document).status_code,
            200,
        )
        empty_response = self._materialize(
            "draft-empty-questions",
            key="materialize-empty-questions",
        )
        self.assertEqual(empty_response.status_code, 422, empty_response.get_json())
        self.assertEqual(empty_response.get_json()["field"], "questions")
        self.assertEqual(EncEncuesta.query.count(), 0)

        null_rewards = self._document()
        null_rewards["experience"]["reward_points"] = None
        self.assertEqual(
            self._save_draft("draft-null-rewards", document=null_rewards).status_code,
            200,
        )
        null_rewards_response = self._materialize(
            "draft-null-rewards",
            key="materialize-null-rewards",
        )
        self.assertEqual(
            null_rewards_response.status_code,
            422,
            null_rewards_response.get_json(),
        )
        self.assertEqual(
            null_rewards_response.get_json()["field"],
            "experience.reward_points",
        )

        padded_title = self._document()
        padded_title["title"] = " Prioridades del barrio "
        self.assertEqual(
            self._save_draft("draft-padded-title", document=padded_title).status_code,
            200,
        )
        padded_response = self._materialize(
            "draft-padded-title",
            key="materialize-padded-title",
        )
        self.assertEqual(padded_response.status_code, 422, padded_response.get_json())
        self.assertEqual(padded_response.get_json()["field"], "title")

        exact_values = self._document()
        exact_values["questions"][0]["options"][0]["value"] = ""
        exact_values["questions"][0]["options"][1]["value"] = "  "
        self.assertEqual(
            self._save_draft("draft-exact-values", document=exact_values).status_code,
            200,
        )
        exact_response = self._materialize(
            "draft-exact-values",
            key="materialize-exact-values",
        )
        self.assertEqual(exact_response.status_code, 201, exact_response.get_json())
        stored_options = exact_response.get_json()["survey"]["preguntas"][0]["opciones"]
        self.assertEqual([option["valor"] for option in stored_options], ["", "  "])

        numeric_value = self._document()
        numeric_value["questions"][0]["options"][0]["value"] = 7
        self.assertEqual(
            self._save_draft("draft-numeric-value", document=numeric_value).status_code,
            200,
        )
        numeric_response = self._materialize(
            "draft-numeric-value",
            key="materialize-numeric-value",
        )
        self.assertEqual(numeric_response.status_code, 422, numeric_response.get_json())
        self.assertEqual(
            numeric_response.get_json()["field"],
            "questions[0].options[0].value",
        )
        self.assertEqual(EncEncuesta.query.count(), 1)

    def test_v2_create_and_update_reject_lossy_type_without_coercion(self):
        headers = self._auth(self.admin_1, self.tenant_1)
        for question_type in ("nps", "ranking", "location"):
            with self.subTest(question_type=question_type):
                payload = {
                    "title": f"No coercion {question_type}",
                    "questions": [
                        {
                            "type": question_type,
                            "label": "Pregunta incompatible",
                            "options": ["Uno", "Dos"],
                        }
                    ],
                }
                rejected = self.client.post(
                    "/api/v2/surveys",
                    json=payload,
                    headers=headers,
                )
                self.assertEqual(rejected.status_code, 422, rejected.get_json())
                self.assertEqual(
                    rejected.get_json()["reason_code"],
                    "unsupported_survey_question_type",
                )

        created = self.client.post(
            "/api/v2/surveys",
            json={
                "title": "Encuesta compatible",
                "questions": [
                    {
                        "type": "single_choice",
                        "label": "Pregunta",
                        "options": ["Uno", "Dos"],
                    }
                ],
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        rejected_update = self.client.patch(
            f"/api/v2/surveys/{created.get_json()['id']}",
            json={
                "questions": [
                    {
                        "type": "location",
                        "label": "Ubicacion",
                        "options": [],
                    }
                ]
            },
            headers=headers,
        )
        self.assertEqual(rejected_update.status_code, 422, rejected_update.get_json())
        self.assertEqual(
            rejected_update.get_json()["reason_code"],
            "unsupported_survey_question_type",
        )
        self.assertEqual(EncEncuesta.query.count(), 1)

    def test_receipt_failure_rolls_back_survey_receipt_and_alias_atomically(self):
        self.assertEqual(self._save_draft("draft-atomic").status_code, 200)
        forced_error = IntegrityError("forced receipt failure", {}, Exception("forced"))

        with patch(
            "services.survey_draft_materialization._flush_materialization_receipt",
            side_effect=forced_error,
        ):
            response = self._materialize(
                "draft-atomic",
                key="materialize-atomic-001",
            )

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "survey_materialization_conflict",
        )
        self.assertEqual(EncEncuesta.query.count(), 0)
        self.assertEqual(SurveyDraftMaterialization.query.count(), 0)
        self.assertEqual(SurveyDraftMaterializationAlias.query.count(), 0)
        self.assertEqual(SurveyDraft.query.count(), 1)

    def test_materialized_survey_cannot_be_deleted_and_refs_are_immutable(self):
        self.assertEqual(self._save_draft("draft-immutable").status_code, 200)
        materialized = self._materialize(
            "draft-immutable",
            key="materialize-immutable-001",
        )
        self.assertEqual(materialized.status_code, 201, materialized.get_json())
        survey = materialized.get_json()["survey"]
        survey_id = materialized.get_json()["survey_id"]
        headers = self._auth(self.admin_1, self.tenant_1)

        changed_document_ref = self.client.patch(
            f"/api/v2/surveys/{survey_id}",
            json={"document_ref": "replacement-document-ref"},
            headers=headers,
        )
        deleted_document_ref = self.client.patch(
            f"/api/v2/surveys/{survey_id}",
            json={"document_ref": None},
            headers=headers,
        )
        self.assertEqual(changed_document_ref.status_code, 409, changed_document_ref.get_json())
        self.assertEqual(deleted_document_ref.status_code, 409, deleted_document_ref.get_json())

        first_question = survey["preguntas"][0]
        first_option = first_question["opciones"][0]
        changed_question_ref = self.client.patch(
            f"/api/v2/surveys/{survey_id}",
            json={
                "questions": [
                    {
                        "id": first_question["id"],
                        "question_ref": "replacement-question-ref",
                        "type": "single_choice",
                        "label": first_question["texto"],
                        "required": True,
                        "order_index": 1,
                        "options": [
                            {
                                "id": option["id"],
                                "option_ref": option["option_ref"],
                                "order": option["orden"],
                                "label": option["texto"],
                                "value": option["valor"],
                            }
                            for option in first_question["opciones"]
                        ],
                    },
                    {
                        "id": survey["preguntas"][1]["id"],
                        "question_ref": survey["preguntas"][1]["question_ref"],
                        "type": "free_text",
                        "label": survey["preguntas"][1]["texto"],
                        "required": False,
                        "order_index": 2,
                        "conditional_logic": survey["preguntas"][1]["conditional_logic"],
                        "options": [],
                    },
                ]
            },
            headers=headers,
        )
        self.assertEqual(changed_question_ref.status_code, 409, changed_question_ref.get_json())
        self.assertEqual(
            changed_question_ref.get_json()["reason_code"],
            "survey_logical_ref_immutable",
        )

        changed_option_ref = self.client.patch(
            f"/api/v2/surveys/{survey_id}",
            json={
                "questions": [
                    {
                        "id": first_question["id"],
                        "question_ref": first_question["question_ref"],
                        "type": "single_choice",
                        "label": first_question["texto"],
                        "required": True,
                        "order_index": 1,
                        "options": [
                            {
                                "id": option["id"],
                                "option_ref": (
                                    "replacement-option-ref"
                                    if option["id"] == first_option["id"]
                                    else option["option_ref"]
                                ),
                                "order": option["orden"],
                                "label": option["texto"],
                                "value": option["valor"],
                            }
                            for option in first_question["opciones"]
                        ],
                    },
                    {
                        "id": survey["preguntas"][1]["id"],
                        "question_ref": survey["preguntas"][1]["question_ref"],
                        "type": "free_text",
                        "label": survey["preguntas"][1]["texto"],
                        "required": False,
                        "order_index": 2,
                        "conditional_logic": survey["preguntas"][1]["conditional_logic"],
                        "options": [],
                    },
                ]
            },
            headers=headers,
        )
        self.assertEqual(changed_option_ref.status_code, 409, changed_option_ref.get_json())
        self.assertEqual(
            changed_option_ref.get_json()["reason_code"],
            "survey_logical_ref_immutable",
        )

        g.tenant_profile = self.tenant_1
        with self.assertRaises(EncuestaError) as caught:
            delete_encuesta(survey_id, self.admin_1)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.payload["reason_code"],
            "survey_materialization_delete_blocked",
        )
        db.session.rollback()
        self.assertIsNotNone(db.session.get(EncEncuesta, survey_id))
        self.assertEqual(SurveyDraftMaterialization.query.count(), 1)

    def test_materialization_requires_revision_key_and_tenant_access(self):
        self.assertEqual(self._save_draft("draft-contract").status_code, 200)
        headers = self._auth(self.admin_1, self.tenant_1)

        missing_revision = self.client.post(
            "/api/v2/surveys/drafts/draft-contract/materialize",
            json={},
            headers={**headers, "Idempotency-Key": "materialize-contract-001"},
        )
        missing_key = self.client.post(
            "/api/v2/surveys/drafts/draft-contract/materialize",
            json={"expected_revision": 1},
            headers=headers,
        )
        cross_tenant = self.client.post(
            "/api/v2/surveys/drafts/draft-contract/materialize",
            json={"expected_revision": 1},
            headers={
                **self._auth(self.admin_2, self.tenant_1),
                "Idempotency-Key": "materialize-cross-tenant",
            },
        )

        self.assertEqual(missing_revision.status_code, 400, missing_revision.get_json())
        self.assertEqual(
            missing_revision.get_json()["reason_code"],
            "invalid_materialization_revision",
        )
        self.assertEqual(missing_key.status_code, 400, missing_key.get_json())
        self.assertEqual(
            missing_key.get_json()["reason_code"],
            "missing_materialization_idempotency_key",
        )
        self.assertIn(cross_tenant.status_code, {403, 404}, cross_tenant.get_json())
        self.assertEqual(EncEncuesta.query.count(), 0)

    def test_document_identity_hash_and_url_safe_refs_are_fail_closed(self):
        mismatched = self._document(document_ref="another-draft-identity")
        self.assertEqual(
            self._save_draft(
                "draft-identity",
                document=mismatched,
                align_document_ref=False,
            ).status_code,
            200,
        )
        mismatch_response = self._materialize(
            "draft-identity",
            key="materialize-identity-mismatch",
        )
        self.assertEqual(mismatch_response.status_code, 422, mismatch_response.get_json())
        self.assertEqual(
            mismatch_response.get_json()["reason_code"],
            "survey_document_identity_mismatch",
        )

        url_refs = self._document()
        url_refs["questions"][0]["question_ref"] = "question/priority%20stable@v1;draft"
        url_refs["questions"][0]["options"][0]["option_ref"] = (
            "option/light%2Fstable,choice@v1"
        )
        url_refs["questions"][1]["visibility"]["question_ref"] = (
            "question/priority%20stable@v1;draft"
        )
        url_refs["questions"][1]["visibility"]["option_ref"] = (
            "option/light%2Fstable,choice@v1"
        )
        self.assertEqual(
            self._save_draft("draft-url-refs", document=url_refs).status_code,
            200,
        )
        accepted = self._materialize(
            "draft-url-refs",
            key="materialize-url-refs-001",
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_json())
        self.assertEqual(
            accepted.get_json()["survey"]["preguntas"][0]["question_ref"],
            "question/priority%20stable@v1;draft",
        )
        self.assertEqual(
            accepted.get_json()["survey"]["preguntas"][0]["opciones"][0]["option_ref"],
            "option/light%2Fstable,choice@v1",
        )

        whitespace_refs = self._document()
        whitespace_refs["questions"][0]["question_ref"] = " question-priority "
        self.assertEqual(
            self._save_draft("draft-whitespace-ref", document=whitespace_refs).status_code,
            200,
        )
        whitespace_response = self._materialize(
            "draft-whitespace-ref",
            key="materialize-whitespace-ref",
        )
        self.assertEqual(
            whitespace_response.status_code,
            422,
            whitespace_response.get_json(),
        )
        self.assertEqual(
            whitespace_response.get_json()["field"],
            "questions[0].question_ref",
        )

        for non_canonical_ref in (" question-priority", "question-priority "):
            with self.subTest(non_canonical_ref=non_canonical_ref):
                with self.assertRaises(EncuestaError) as caught:
                    _normalize_logical_ref(non_canonical_ref, field="question_ref")
                self.assertEqual(
                    caught.exception.payload["reason_code"],
                    "survey_logical_ref_invalid",
                )
        self.assertEqual(
            _normalize_logical_ref("question@v1;stable", field="question_ref"),
            "question@v1;stable",
        )

        self.assertEqual(self._save_draft("draft-corrupt-hash").status_code, 200)
        corrupt = SurveyDraft.query.filter_by(
            tenant_id=self.tenant_1.id,
            draft_id="draft-corrupt-hash",
        ).one()
        corrupt.payload_hash = "0" * 64
        db.session.commit()
        hash_response = self._materialize(
            "draft-corrupt-hash",
            key="materialize-corrupt-hash",
        )
        self.assertEqual(hash_response.status_code, 409, hash_response.get_json())
        self.assertEqual(
            hash_response.get_json()["reason_code"],
            "survey_draft_integrity_failed",
        )
        self.assertEqual(EncEncuesta.query.count(), 1)

    def test_guard_preserves_updated_at_and_schema_drift_is_not_contention(self):
        self.assertEqual(self._save_draft("draft-guard-time").status_code, 200)
        draft = SurveyDraft.query.filter_by(
            tenant_id=self.tenant_1.id,
            draft_id="draft-guard-time",
        ).one()
        updated_at_before = draft.updated_at

        materialized = self._materialize(
            "draft-guard-time",
            key="materialize-guard-time",
        )
        self.assertEqual(materialized.status_code, 201, materialized.get_json())
        db.session.expire_all()
        updated_at_after = SurveyDraft.query.filter_by(
            tenant_id=self.tenant_1.id,
            draft_id="draft-guard-time",
        ).one().updated_at
        self.assertEqual(updated_at_after, updated_at_before)

        self.assertEqual(self._save_draft("draft-schema-drift").status_code, 200)
        schema_error = OperationalError(
            "UPDATE survey_draft",
            {},
            Exception("no such column: survey_draft.revision"),
        )
        with patch(
            "services.survey_draft_materialization._acquire_draft_write_guard",
            side_effect=schema_error,
        ):
            response = self._materialize(
                "draft-schema-drift",
                key="materialize-schema-drift",
            )
        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "survey_materialization_storage_error",
        )
        self.assertFalse(response.get_json()["retryable"])

    def test_plan_role_and_header_body_idempotency_contract(self):
        self.assertEqual(self._save_draft("draft-policy-gates").status_code, 200)
        headers = self._auth(self.admin_1, self.tenant_1)

        mismatch = self.client.post(
            "/api/v2/surveys/drafts/draft-policy-gates/materialize",
            json={
                "expected_revision": 1,
                "idempotency_key": "materialize-body-key",
            },
            headers={**headers, "Idempotency-Key": "materialize-header-key"},
        )
        self.assertEqual(mismatch.status_code, 400, mismatch.get_json())
        self.assertEqual(
            mismatch.get_json()["reason_code"],
            "materialization_idempotency_key_mismatch",
        )

        with patch("routes.v2.surveys._survey_writes_allowed", return_value=False):
            denied_plan = self._materialize(
                "draft-policy-gates",
                key="materialize-plan-denied",
            )
        self.assertEqual(denied_plan.status_code, 403, denied_plan.get_json())

        citizen = self._create_user(
            "materialize-citizen@test.com",
            "ciudadano",
            self.tenant_1.slug,
        )
        citizen.tenant_id = self.tenant_1.id
        db.session.commit()
        denied_role = self._materialize(
            "draft-policy-gates",
            key="materialize-role-denied",
            user=citizen,
            tenant=self.tenant_1,
        )
        self.assertEqual(denied_role.status_code, 403, denied_role.get_json())
        self.assertEqual(EncEncuesta.query.count(), 0)


def test_concurrent_keys_materialize_one_survey_and_bind_both_aliases(tmp_path):
    database_path = tmp_path / "survey-materialization-race.sqlite3"

    class ConcurrentMaterializationConfig(SurveyMaterializationTestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False, "timeout": 10}
        }

    app = create_app(ConcurrentMaterializationConfig)
    with app.app_context():
        db.create_all()
        admin = User(
            name="concurrent-admin",
            email="concurrent-materialize@test.com",
            rol="admin",
            tenant_slug="tenant-materialize-concurrent",
        )
        admin.set_password("secret123")
        db.session.add(admin)
        db.session.flush()
        tenant = TenantProfile(
            slug="tenant-materialize-concurrent",
            nombre="Tenant Concurrent",
            tipo="municipio",
            pyme_id=admin.id,
            plan="full",
        )
        db.session.add(tenant)
        db.session.commit()
        admin.tenant_id = tenant.id
        db.session.commit()
        token = jwt.encode(
            {
                "user_id": admin.id,
                "rol": admin.rol,
                "tenant_slug": admin.tenant_slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        tenant_slug = tenant.slug

    auth_headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant_slug,
    }
    document = {
        "draft_id": "draft-concurrent",
        "schema_version": "survey-document.v1",
        "document_ref": "draft-concurrent",
        "title": "Materializacion concurrente",
        "description": None,
        "survey_type": "opinion",
        "schedule": {"starts_at": None, "ends_at": None},
        "policies": {
            "uniqueness": "libre",
            "anonymous": True,
            "requires_contact_data": False,
        },
        "experience": {
            "live_voting": False,
            "show_live_results": False,
            "allow_comments": False,
            "reward_points": 0,
        },
        "questions": [
            {
                "question_ref": "question/concurrent%20one",
                "order": 1,
                "type": "free_text",
                "prompt": "Contanos tu prioridad",
                "required": True,
                "selection": {"min": None, "max": None},
                "visibility": None,
                "options": [],
                "extensions": {"durable_builder": {"source": "concurrency-test"}},
            }
        ],
        "extensions": {"durable_builder": {"source": "concurrency-test"}},
    }
    with app.test_client() as client:
        saved = client.post(
            "/api/v2/surveys/draft",
            json=document,
            headers={**auth_headers, "Idempotency-Key": "save-concurrent-draft"},
        )
        assert saved.status_code == 200, saved.get_json()

    barrier = Barrier(2)

    def _materialize(key: str):
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/v2/surveys/drafts/draft-concurrent/materialize",
                json={"expected_revision": 1},
                headers={**auth_headers, "Idempotency-Key": key},
            )
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                _materialize,
                ("materialize-concurrent-key-a", "materialize-concurrent-key-b"),
            )
        )

    assert sorted(status for status, _ in outcomes) == [200, 201], outcomes
    survey_ids = {body["survey_id"] for _, body in outcomes}
    assert len(survey_ids) == 1
    with app.app_context():
        assert EncEncuesta.query.count() == 1
        assert SurveyDraftMaterialization.query.count() == 1
        assert SurveyDraftMaterializationAlias.query.count() == 2
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
