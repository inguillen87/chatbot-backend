import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from sqlalchemy import update

from database import db
from models import (
    EncEncuesta,
    EncRespuesta,
    SurveyResponseEffect,
    TenantProfile,
    User,
)
from services.survey_response_effects import (
    EFFECT_ANALYTICS,
    EFFECT_REALTIME,
    EFFECT_REWARD,
    STATUS_DEAD,
    STATUS_PROCESSING,
    STATUS_RETRY_WAIT,
    STATUS_SUCCEEDED,
    _EffectOutcome,
    dispatch_survey_response_effects,
    stage_survey_response_effects,
    summarize_survey_response_effects,
)


class SurveyResponseEffectsTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="survey-effects-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.owner = User(
            name="Effects owner",
            email="effects-owner@test.com",
            rol="admin",
        )
        self.owner.set_password("secret123")
        self.voter = User(
            name="Effects voter",
            email="effects-voter@test.com",
            rol="usuario",
            saldo_puntos=0,
        )
        self.voter.set_password("secret123")
        db.session.add_all([self.owner, self.voter])
        db.session.flush()
        self.tenant = TenantProfile(
            slug="survey-effects",
            nombre="Survey Effects",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.owner.tenant_id = self.tenant.id
        self.voter.tenant_id = self.tenant.id
        self.survey = EncEncuesta(
            tenant_id=self.tenant.id,
            slug="durable-effects-survey",
            titulo="Durable effects survey",
            estado="publicada",
            puntos_recompensa=25,
            mostrar_resultados_envivo=True,
            created_by=self.owner.id,
        )
        db.session.add(self.survey)
        db.session.flush()
        self.response = self._new_response(
            dni="30111222",
            phone="+5491112345678",
            metadata_payload={
                "submission_id": "raw-submission-secret-0001",
                "authorization": "Bearer should-never-be-staged",
            },
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _new_response(self, **overrides):
        values = {
            "encuesta_id": self.survey.id,
            "tenant_id": self.tenant.id,
            "user_id": self.voter.id,
            "canal": "portal",
            "huella_unica": "fingerprint-that-must-not-be-staged",
        }
        values.update(overrides)
        response = EncRespuesta(**values)
        db.session.add(response)
        db.session.flush()
        return response

    def _stage(self, response=None, **overrides):
        options = {
            "slug_publico": "public-effects-token",
            "respuestas_payload": [{"pregunta_id": 10, "texto_libre": "private"}],
            "authenticated_user": self.voter,
            "grant_reward": True,
            "emit_realtime_update": True,
        }
        options.update(overrides)
        return stage_survey_response_effects(
            self.survey,
            response or self.response,
            **options,
        )

    @staticmethod
    def _confirmed_analytics_event(*_args, **kwargs):
        return SimpleNamespace(
            id=kwargs["event_id"],
            tenant_id=kwargs["tenant_id"],
            event_name=kwargs["event_name"],
            entity_ref=kwargs["entity_ref"],
            metadata_payload=kwargs["payload"],
        )

    def test_stage_is_idempotent_and_payload_excludes_submission_pii(self):
        first = self._stage()
        second = self._stage()
        db.session.commit()

        self.assertEqual(len(first), 3)
        self.assertEqual([effect.id for effect in first], [effect.id for effect in second])
        self.assertEqual(SurveyResponseEffect.query.count(), 3)
        self.assertEqual(
            {effect.effect_type for effect in first},
            {EFFECT_ANALYTICS, EFFECT_REWARD, EFFECT_REALTIME},
        )

        serialized = json.dumps(
            [effect.payload_json for effect in first],
            sort_keys=True,
        )
        for forbidden in (
            "30111222",
            "+5491112345678",
            "raw-submission-secret-0001",
            "Bearer should-never-be-staged",
            "fingerprint-that-must-not-be-staged",
            "private",
        ):
            self.assertNotIn(forbidden, serialized)
        reward = next(effect for effect in first if effect.effect_type == EFFECT_REWARD)
        self.assertEqual(reward.payload_json["points"], 25)
        self.assertEqual(
            reward.payload_json["idempotency_key"],
            f"survey_reward:{self.survey.id}:user:{self.voter.id}",
        )

    def test_reward_scope_is_unique_across_free_responses(self):
        first = self._stage()
        second_response = self._new_response(
            huella_unica="another-fingerprint",
            dni=None,
            phone=None,
        )
        second = self._stage(second_response)
        db.session.commit()

        rewards = SurveyResponseEffect.query.filter_by(effect_type=EFFECT_REWARD).all()
        self.assertEqual(len(rewards), 1)
        self.assertEqual(rewards[0].response_id, self.response.id)
        self.assertEqual(
            next(effect for effect in second if effect.effect_type == EFFECT_REWARD).id,
            next(effect for effect in first if effect.effect_type == EFFECT_REWARD).id,
        )
        self.assertEqual(
            SurveyResponseEffect.query.filter_by(response_id=second_response.id).count(),
            2,
        )

    def test_reward_scope_collision_fails_closed_when_policy_changes(self):
        self._stage(stage_analytics=False, emit_realtime_update=False)
        db.session.commit()
        self.survey.puntos_recompensa = 99
        second_response = self._new_response(
            huella_unica="policy-collision-fingerprint",
            dni=None,
            phone=None,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "survey_effect_idempotency_collision_mismatch",
        ):
            self._stage(
                second_response,
                stage_analytics=False,
                emit_realtime_update=False,
            )

        db.session.rollback()
        reward = SurveyResponseEffect.query.one()
        self.assertEqual(reward.payload_json["points"], 25)

    def test_meta_style_stage_can_disable_reward_and_realtime_without_commit(self):
        staged = self._stage(grant_reward=False, emit_realtime_update=False)
        self.assertEqual([effect.effect_type for effect in staged], [EFFECT_ANALYTICS])
        db.session.rollback()
        self.assertEqual(SurveyResponseEffect.query.count(), 0)

    def test_dispatch_calls_handlers_and_finishes_each_effect_once(self):
        now = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
        self._stage(now=now)
        db.session.commit()

        with (
            patch(
                "services.analytics.ingestor.analytics_ingestor.track",
                side_effect=self._confirmed_analytics_event,
            ) as track,
            patch(
                "services.encuestas_service._grant_survey_reward_effect",
                return_value="credited",
            ) as reward,
            patch(
                "services.encuestas_service.emit_survey_response_update",
                return_value=True,
            ) as realtime,
        ):
            result = dispatch_survey_response_effects(
                tenant_id=self.tenant.id,
                response_id=self.response.id,
                now=now,
            )
            repeated = dispatch_survey_response_effects(
                tenant_id=self.tenant.id,
                response_id=self.response.id,
                now=now,
            )

        self.assertEqual(result["claimed"], 3)
        self.assertEqual(result["succeeded"], 3)
        self.assertEqual(repeated["claimed"], 0)
        effects = SurveyResponseEffect.query.order_by(SurveyResponseEffect.id).all()
        self.assertEqual({effect.status for effect in effects}, {STATUS_SUCCEEDED})
        self.assertEqual({effect.attempt_count for effect in effects}, {1})
        self.assertTrue(all(effect.lease_token is None for effect in effects))
        track.assert_called_once()
        self.assertFalse(track.call_args.kwargs["commit"])
        self.assertTrue(track.call_args.kwargs["raise_on_error"])
        self.assertEqual(
            track.call_args.kwargs["event_id"],
            next(
                effect.payload_json["event_id"]
                for effect in effects
                if effect.effect_type == EFFECT_ANALYTICS
            ),
        )
        reward.assert_called_once_with(
            self.survey,
            self.response,
            self.voter,
            reward_points=25,
            idempotency_key=f"survey_reward:{self.survey.id}:user:{self.voter.id}",
            commit=False,
        )
        realtime.assert_called_once()
        realtime_args = realtime.call_args
        self.assertEqual(realtime_args.args, (self.survey, "public-effects-token"))
        self.assertEqual(
            realtime_args.kwargs["event_envelope"]["event_id"],
            next(
                effect.payload_json["event_id"]
                for effect in effects
                if effect.effect_type == EFFECT_REALTIME
            ),
        )

    def test_worker_realtime_effect_cannot_succeed_without_shared_transport(self):
        now = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)
        self.app.config.update(
            CHATBOC_PROCESS_ROLE="survey-effect-worker",
            SOCKETIO_MESSAGE_QUEUE_URL="",
        )
        self._stage(
            stage_analytics=False,
            grant_reward=False,
            emit_realtime_update=True,
            now=now,
        )
        db.session.commit()

        with patch(
            "services.encuestas_service.emit_survey_response_update",
            return_value=True,
        ) as realtime:
            result = dispatch_survey_response_effects(now=now)

        effect = SurveyResponseEffect.query.one()
        self.assertEqual(result["claimed"], 1)
        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["retry_wait"], 1)
        self.assertEqual(effect.status, STATUS_RETRY_WAIT)
        self.assertEqual(
            effect.last_error,
            "survey_realtime_shared_transport_not_configured",
        )
        self.assertIsNone(effect.result_json)
        realtime.assert_not_called()

    def test_failure_is_sanitized_retried_and_then_succeeds(self):
        first_now = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)
        self._stage(
            grant_reward=False,
            emit_realtime_update=False,
            now=first_now,
        )
        db.session.commit()
        secret = "+549118887777 raw-submission-key"

        with patch(
            "services.analytics.ingestor.analytics_ingestor.track",
            side_effect=RuntimeError(secret),
        ):
            first = dispatch_survey_response_effects(now=first_now)

        effect = SurveyResponseEffect.query.one()
        self.assertEqual(first["retry_wait"], 1)
        self.assertEqual(effect.status, STATUS_RETRY_WAIT)
        self.assertEqual(effect.attempt_count, 1)
        self.assertEqual(effect.last_error, "RuntimeError")
        self.assertNotIn(secret, effect.last_error)
        retry_at = effect.available_at

        with patch(
            "services.analytics.ingestor.analytics_ingestor.track",
            side_effect=self._confirmed_analytics_event,
        ):
            second = dispatch_survey_response_effects(
                now=first_now + timedelta(seconds=31)
            )

        db.session.refresh(effect)
        self.assertEqual(retry_at.replace(tzinfo=timezone.utc), first_now + timedelta(seconds=30))
        self.assertEqual(second["succeeded"], 1)
        self.assertEqual(effect.status, STATUS_SUCCEEDED)
        self.assertEqual(effect.attempt_count, 2)

    def test_analytics_collision_with_wrong_tenant_is_dead(self):
        now = datetime(2026, 7, 28, 16, 30, tzinfo=timezone.utc)
        self._stage(
            grant_reward=False,
            emit_realtime_update=False,
            now=now,
        )
        db.session.commit()

        def adulterated_event(*_args, **kwargs):
            return SimpleNamespace(
                id=kwargs["event_id"],
                tenant_id=kwargs["tenant_id"] + 1,
                event_name=kwargs["event_name"],
                entity_ref=kwargs["entity_ref"],
                metadata_payload=kwargs["payload"],
            )

        with patch(
            "services.analytics.ingestor.analytics_ingestor.track",
            side_effect=adulterated_event,
        ):
            result = dispatch_survey_response_effects(now=now)

        effect = SurveyResponseEffect.query.one()
        self.assertEqual(result["dead"], 1)
        self.assertEqual(effect.status, STATUS_DEAD)
        self.assertEqual(
            effect.last_error,
            "analytics_event_collision_mismatch",
        )

    def test_max_attempt_failure_becomes_dead(self):
        now = datetime(2026, 7, 28, 17, 0, tzinfo=timezone.utc)
        self._stage(
            grant_reward=False,
            emit_realtime_update=False,
            max_attempts=1,
            now=now,
        )
        db.session.commit()
        with patch(
            "services.analytics.ingestor.analytics_ingestor.track",
            side_effect=ValueError("private payload must not leak"),
        ):
            result = dispatch_survey_response_effects(
                now=now
            )

        effect = SurveyResponseEffect.query.one()
        self.assertEqual(result["dead"], 1)
        self.assertEqual(effect.status, STATUS_DEAD)
        self.assertEqual(effect.last_error, "ValueError")
        self.assertIsNotNone(effect.processed_at)

    def test_expired_processing_lease_is_recovered_with_same_attempt_number(self):
        now = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
        self._stage(
            grant_reward=False,
            emit_realtime_update=False,
            now=now - timedelta(minutes=5),
        )
        db.session.commit()
        effect = SurveyResponseEffect.query.one()
        effect.status = STATUS_PROCESSING
        effect.attempt_count = 1
        effect.lease_token = "abandoned-worker"
        effect.leased_until = now - timedelta(seconds=1)
        db.session.commit()

        with patch(
            "services.analytics.ingestor.analytics_ingestor.track",
            side_effect=self._confirmed_analytics_event,
        ):
            result = dispatch_survey_response_effects(now=now)

        db.session.refresh(effect)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(effect.status, STATUS_SUCCEEDED)
        self.assertEqual(effect.attempt_count, 1)
        self.assertIsNone(effect.lease_token)

    def test_stale_worker_cannot_finalize_after_lease_token_changes(self):
        now = datetime(2026, 7, 28, 19, 0, tzinfo=timezone.utc)
        self._stage(
            grant_reward=False,
            emit_realtime_update=False,
            now=now,
        )
        db.session.commit()

        def steal_lease(effect):
            db.session.execute(
                update(SurveyResponseEffect)
                .where(SurveyResponseEffect.id == effect.id)
                .values(
                    lease_token="replacement-worker",
                    leased_until=now + timedelta(minutes=5),
                )
            )
            db.session.commit()
            return _EffectOutcome(
                status=STATUS_SUCCEEDED,
                result={"should_not_persist": True},
            )

        with patch(
            "services.survey_response_effects._execute_effect",
            side_effect=steal_lease,
        ):
            result = dispatch_survey_response_effects(now=now)

        effect = SurveyResponseEffect.query.one()
        self.assertEqual(result["fenced"], 1)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(effect.status, STATUS_PROCESSING)
        self.assertEqual(effect.lease_token, "replacement-worker")
        self.assertIsNone(effect.result_json)

    def test_exhausted_pending_and_retry_effects_are_not_claimed(self):
        now = datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc)
        self._stage(
            grant_reward=False,
            emit_realtime_update=True,
            now=now - timedelta(minutes=1),
        )
        db.session.commit()
        effects = SurveyResponseEffect.query.order_by(
            SurveyResponseEffect.id
        ).all()
        self.assertEqual(len(effects), 2)
        effects[0].status = "pending"
        effects[1].status = STATUS_RETRY_WAIT
        for effect in effects:
            effect.attempt_count = effect.max_attempts
            effect.available_at = now - timedelta(seconds=1)
        db.session.commit()

        with patch(
            "services.survey_response_effects._execute_effect"
        ) as execute_effect:
            result = dispatch_survey_response_effects(now=now)

        self.assertEqual(result["claimed"], 0)
        execute_effect.assert_not_called()
        self.assertEqual(
            {effect.status for effect in SurveyResponseEffect.query.all()},
            {"pending", STATUS_RETRY_WAIT},
        )

    def test_summary_reports_status_and_effect_breakdown(self):
        self._stage()
        db.session.commit()
        reward = SurveyResponseEffect.query.filter_by(effect_type=EFFECT_REWARD).one()
        reward.status = STATUS_DEAD
        reward.attempt_count = reward.max_attempts
        reward.processed_at = datetime.now(timezone.utc)
        db.session.commit()

        summary = summarize_survey_response_effects(self.tenant.id)

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["dead"], 1)
        self.assertEqual(summary["by_status"][STATUS_DEAD], 1)
        self.assertEqual(
            summary["by_effect_type"][EFFECT_REWARD][STATUS_DEAD],
            1,
        )

    def test_stage_rejects_synthetic_and_unverified_origins_without_effects(self):
        for origin in ("synthetic_demo", "legacy_unverified"):
            response = self._new_response(
                huella_unica=f"non-real-{origin}",
                response_origin=origin,
            )
            with self.assertRaisesRegex(
                ValueError,
                "verified real response",
            ):
                self._stage(response=response)
        self.assertEqual(SurveyResponseEffect.query.count(), 0)

    def test_dispatch_rechecks_origin_and_blocks_legacy_effects_without_side_effects(self):
        now = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
        self._stage(now=now)
        db.session.commit()
        self.response.response_origin = "legacy_unverified"
        db.session.commit()

        with (
            patch(
                "services.analytics.ingestor.analytics_ingestor.track"
            ) as track,
            patch(
                "services.encuestas_service._grant_survey_reward_effect"
            ) as reward,
            patch(
                "services.encuestas_service.emit_survey_response_update"
            ) as realtime,
        ):
            result = dispatch_survey_response_effects(
                tenant_id=self.tenant.id,
                response_id=self.response.id,
                now=now,
            )

        self.assertEqual(result["claimed"], 3)
        self.assertEqual(result["dead"], 3)
        self.assertEqual(
            {effect.status for effect in SurveyResponseEffect.query.all()},
            {STATUS_DEAD},
        )
        track.assert_not_called()
        reward.assert_not_called()
        realtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
