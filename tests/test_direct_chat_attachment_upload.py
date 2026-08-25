from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError

from app import create_app, db
from config import Config
from models import ArchivoAdjunto, TenantProfile, User
from routes import archivos as archivos_route
from services import direct_attachment_upload
from services.r2_service import R2ObjectStorageUnavailableError, R2Service


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "direct-upload-test-signing-key-at-least-32-bytes"
    RATELIMIT_STORAGE_URI = "memory://"
    DIRECT_UPLOAD_PREPARE_ACTOR_RATE_LIMIT = "100 per 60 seconds"
    DIRECT_UPLOAD_PREPARE_GLOBAL_RATE_LIMIT = "1000 per 60 seconds"


class _InMemoryR2Client:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.presigned_calls: list[dict] = []
        self.copy_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.force_etag_race = False
        self.final_metadata_override: dict | None = None

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        params = dict(Params or {})
        self.presigned_calls.append(
            {
                "operation": operation,
                "params": params,
                "expires_in": ExpiresIn,
            }
        )
        return f"https://r2-upload.test/{params['Key']}?operation={operation}"

    def head_object(self, *, Bucket, Key):
        del Bucket
        self.objects[Key].setdefault("ETag", f'"etag-{Key}"')
        return dict(self.objects[Key])

    def copy_object(self, **kwargs):
        self.copy_calls.append(dict(kwargs))
        source_key = kwargs["CopySource"]["Key"]
        if self.force_etag_race:
            self.objects[source_key]["ETag"] = '"replaced-after-head"'
        if kwargs.get("CopySourceIfMatch") != self.objects[source_key].get("ETag"):
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "CopyObject",
            )
        self.objects[kwargs["Key"]] = {
            **self.objects[source_key],
            "ContentType": kwargs["ContentType"],
            "CacheControl": kwargs["CacheControl"],
            "ETag": '"copy-etag"',
        }
        if self.final_metadata_override:
            self.objects[kwargs["Key"]].update(self.final_metadata_override)
        return {"CopyObjectResult": {"ETag": "copy-etag"}}

    def delete_object(self, *, Bucket, Key):
        del Bucket
        self.delete_calls.append({"Key": Key})
        self.objects.pop(Key, None)
        return {}


def _configured_r2() -> tuple[R2Service, _InMemoryR2Client]:
    service = R2Service()
    client = _InMemoryR2Client()
    service.client = client
    service.bucket_name = "chatboc-test"
    service.public_base_url = "https://cdn.test"
    return service, client


class TestDirectChatAttachmentUpload:
    def setup_method(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        direct_attachment_upload.limiter.limiter.storage.reset()
        db.create_all()

        self.owner = User(
            rol="admin",
            name="Municipio Direct",
            email="direct-upload@example.test",
            password_hash="hash",
            tipo_chat="municipio",
            tenant_slug="municipio-direct",
            token="entity-token-direct-upload",
        )
        db.session.add(self.owner)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="municipio-direct",
            nombre="Municipio Direct",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.commit()
        self.storage, self.storage_client = _configured_r2()

    def teardown_method(self):
        direct_attachment_upload.limiter.limiter.storage.reset()
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _post(
        self,
        payload,
        *,
        session_id="chat-session-1",
        storage=None,
        anon_id="anonymous-direct-actor",
    ):
        headers = {
            "X-Chat-Session-Id": session_id,
            "X-Tenant-Slug": self.tenant.slug,
            "X-Request-Id": "direct-upload-request",
        }
        with self.app.test_request_context(
            "/archivos/upload/chat_attachment",
            method="POST",
            json=payload,
            headers=headers,
        ):
            with ExitStack() as stack:
                active_storage = storage or self.storage
                stack.enter_context(
                    patch(
                        "services.direct_attachment_upload.r2_service",
                        active_storage,
                    )
                )
                stack.enter_context(
                    patch(
                        "services.attachment_delivery.r2_service",
                        active_storage,
                    )
                )
                return archivos_route.upload_chat_attachment.__wrapped__(
                    current_user=None,
                    owner_user=self.owner,
                    anon_id=anon_id,
                )

    def _prepare(self, **overrides):
        payload = {
            "operation": "prepare_direct_upload",
            "filename": "evidencia.png",
            "mime_type": "image/png",
            "size_bytes": 4,
            **overrides,
        }
        response = self._post(payload)
        return response, response.get_json()

    def test_prepare_and_complete_are_scoped_and_completion_is_idempotent(self):
        prepare_response, prepared = self._prepare()

        assert prepare_response.status_code == 200
        assert prepared["contract_version"] == "chat.attachment.direct.v1"
        assert prepared["upload"]["method"] == "PUT"
        assert prepared["upload"]["headers"] == {"Content-Type": "image/png"}
        assert prepared["constraints"]["exact_size_required"] is True
        assert prepare_response.headers["Cache-Control"] == "no-store"

        put_call = self.storage_client.presigned_calls[-1]
        assert put_call["params"]["ContentLength"] == 4
        temporary_key = put_call["params"]["Key"]
        self.storage_client.objects[temporary_key] = {
            "ContentLength": 4,
            "ContentType": "image/png",
        }
        complete_payload = {
            "operation": "complete_direct_upload",
            "intent_token": prepared["intent_token"],
        }

        completed_response = self._post(complete_payload)
        completed = completed_response.get_json()

        assert completed_response.status_code == 200
        assert completed["operation"] == "complete_direct_upload"
        assert completed["idempotent"] is False
        assert completed["attachmentInfo"]["storage_provider"] == "cloudflare_r2"
        assert completed["attachmentInfo"]["storage_access"] == "signed"
        assert completed["attachmentInfo"]["name"] == "evidencia.png"
        assert ArchivoAdjunto.query.count() == 1
        attachment = ArchivoAdjunto.query.one()
        assert attachment.session_id == "chat-session-1"
        assert attachment.user_id == self.owner.id
        assert attachment.tamano == 4
        assert temporary_key not in self.storage_client.objects
        assert len(self.storage_client.copy_calls) == 1
        assert self.storage_client.copy_calls[0]["CopySourceIfMatch"].startswith(
            '"etag-'
        )

        replay_response = self._post(complete_payload)
        replay = replay_response.get_json()

        assert replay_response.status_code == 200
        assert replay["idempotent"] is True
        assert replay["attachmentInfo"]["id"] == completed["attachmentInfo"]["id"]
        assert ArchivoAdjunto.query.count() == 1
        assert len(self.storage_client.copy_calls) == 1

    def test_prepare_works_through_the_real_anonymous_entity_auth_decorator(self):
        headers = {
            "X-Entity-Token": self.owner.token,
            "X-Anon-Id": "anonymous-direct-http",
            "X-Chat-Session-Id": "chat-session-http",
            "X-Tenant-Slug": self.tenant.slug,
        }
        with patch(
            "services.direct_attachment_upload.r2_service",
            self.storage,
        ), patch(
            "services.attachment_delivery.r2_service",
            self.storage,
        ):
            response = self.app.test_client().post(
                "/archivos/upload/chat_attachment",
                json={
                    "operation": "prepare_direct_upload",
                    "filename": "evidencia.png",
                    "mime_type": "image/png",
                    "size_bytes": 4,
                },
                headers=headers,
            )

        payload = response.get_json()
        assert response.status_code == 200
        assert payload["operation"] == "prepare_direct_upload"
        assert payload["upload"]["headers"] == {"Content-Type": "image/png"}
        assert response.headers["X-Anon-Id"] == "anonymous-direct-http"

    def test_complete_rejects_a_different_chat_session_before_r2_calls(self):
        _, prepared = self._prepare()

        response = self._post(
            {
                "operation": "complete_direct_upload",
                "intent_token": prepared["intent_token"],
            },
            session_id="another-chat-session",
        )

        payload = response.get_json()
        assert response.status_code == 409
        assert payload["code"] == "upload_scope_mismatch"
        assert self.storage_client.copy_calls == []

    def test_complete_rejects_size_mismatch_and_deletes_temporary_object(self):
        _, prepared = self._prepare(size_bytes=4)
        temporary_key = self.storage_client.presigned_calls[-1]["params"]["Key"]
        self.storage_client.objects[temporary_key] = {
            "ContentLength": 5,
            "ContentType": "image/png",
        }

        response = self._post(
            {
                "operation": "complete_direct_upload",
                "intent_token": prepared["intent_token"],
            }
        )

        payload = response.get_json()
        assert response.status_code == 409
        assert payload["code"] == "uploaded_object_mismatch"
        assert temporary_key not in self.storage_client.objects
        assert ArchivoAdjunto.query.count() == 0

    def test_prepare_normalizes_codec_mime_for_the_signed_put(self):
        response, payload = self._prepare(
            filename="nota.webm",
            mime_type="Audio/WebM; codecs=opus",
        )

        assert response.status_code == 200
        assert payload["upload"]["headers"] == {"Content-Type": "audio/webm"}
        assert self.storage_client.presigned_calls[-1]["params"]["ContentType"] == "audio/webm"

    def test_prepare_rejects_oversize_before_presigning(self):
        response, payload = self._prepare(size_bytes=15 * 1024 * 1024 + 1)

        assert response.status_code == 413
        assert payload["code"] == "file_too_large"
        assert payload["max_file_bytes"] == 15 * 1024 * 1024
        assert self.storage_client.presigned_calls == []

    def test_prepare_rate_limits_tenant_actor_before_creating_more_presigned_urls(self):
        self.app.config["DIRECT_UPLOAD_PREPARE_ACTOR_RATE_LIMIT"] = "2 per 60 seconds"

        first_response, _ = self._prepare(filename="uno.png")
        second_response, _ = self._prepare(filename="dos.png")
        rejected_response, rejected = self._prepare(filename="tres.png")

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert rejected_response.status_code == 429
        assert rejected["code"] == "direct_upload_rate_limited"
        assert rejected["retryable"] is True
        assert rejected["rate_limit"] == {
            "scope": "tenant_actor",
            "available": True,
            "limit": 2,
            "remaining": 0,
            "window_seconds": 60,
            "retry_after_seconds": int(rejected_response.headers["Retry-After"]),
        }
        assert int(rejected_response.headers["Retry-After"]) >= 1
        assert len(self.storage_client.presigned_calls) == 2

    def test_prepare_global_bucket_cannot_be_evaded_by_rotating_anonymous_actor(self):
        self.app.config["DIRECT_UPLOAD_PREPARE_GLOBAL_RATE_LIMIT"] = "2 per 60 seconds"

        responses = [
            self._post(
                {
                    "operation": "prepare_direct_upload",
                    "filename": f"evidencia-{index}.png",
                    "mime_type": "image/png",
                    "size_bytes": 4,
                },
                anon_id=f"rotating-anonymous-{index}",
            )
            for index in range(3)
        ]

        assert [response.status_code for response in responses] == [200, 200, 429]
        rejected = responses[-1].get_json()
        assert rejected["rate_limit"]["scope"] == "global"
        assert rejected["rate_limit"]["limit"] == 2
        assert "tenant" not in rejected["rate_limit"]
        assert "actor" not in rejected["rate_limit"]
        assert len(self.storage_client.presigned_calls) == 2

    def test_prepare_fails_closed_when_shared_rate_limit_storage_is_unavailable(self):
        with patch.object(
            direct_attachment_upload.limiter.limiter,
            "hit",
            side_effect=RuntimeError("shared storage unavailable"),
        ):
            response, payload = self._prepare()

        assert response.status_code == 503
        assert payload["code"] == "direct_upload_rate_limit_unavailable"
        assert payload["retryable"] is True
        assert payload["rate_limit"]["available"] is False
        assert payload["rate_limit"]["scope"] == "tenant_actor"
        assert response.headers["Retry-After"] == "60"
        assert self.storage_client.presigned_calls == []

    def test_prepare_rejects_process_local_rate_limit_storage_on_vercel(self):
        with patch.dict(
            "os.environ",
            {
                "VERCEL": "1",
                "SECRET_KEY": TestConfig.SECRET_KEY,
            },
        ):
            response, payload = self._prepare()

        assert response.status_code == 503
        assert payload["code"] == "direct_upload_rate_limit_unavailable"
        assert payload["rate_limit"] == {
            "scope": "shared_storage",
            "available": False,
            "retry_after_seconds": 60,
        }
        assert response.headers["Retry-After"] == "60"
        assert self.storage_client.presigned_calls == []

    def test_prepare_fails_closed_when_r2_is_not_configured(self):
        unavailable = R2Service()
        unavailable.client = None
        unavailable.bucket_name = None

        response = self._post(
            {
                "operation": "prepare_direct_upload",
                "filename": "evidencia.png",
                "mime_type": "image/png",
                "size_bytes": 4,
            },
            storage=unavailable,
        )

        payload = response.get_json()
        assert response.status_code == 503
        assert payload["code"] == "object_storage_unavailable"
        assert payload["retryable"] is True

    def test_prepare_rejects_unknown_mime_and_missing_session(self):
        mime_response, mime_payload = self._prepare(mime_type="application/x-msdownload")
        assert mime_response.status_code == 400
        assert mime_payload["code"] == "unsupported_mime_type"

        session_response = self._post(
            {
                "operation": "prepare_direct_upload",
                "filename": "evidencia.png",
                "mime_type": "image/png",
                "size_bytes": 4,
            },
            session_id="",
        )
        assert session_response.status_code == 400
        assert session_response.get_json()["code"] == "chat_session_required"

    def test_complete_rejects_a_tampered_intent(self):
        _, prepared = self._prepare()
        token = prepared["intent_token"]

        response = self._post(
            {
                "operation": "complete_direct_upload",
                "intent_token": f"x{token[1:]}",
            }
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_upload_intent"

    def test_complete_preserves_temporary_object_when_initial_head_is_unavailable(self):
        _, prepared = self._prepare()
        temporary_key = self.storage_client.presigned_calls[-1]["params"]["Key"]
        self.storage_client.objects[temporary_key] = {
            "ContentLength": 4,
            "ContentType": "image/png",
            "ETag": '"original-etag"',
        }

        with patch.object(
            self.storage,
            "head_object",
            side_effect=R2ObjectStorageUnavailableError("HEAD timeout"),
        ):
            response = self._post(
                {
                    "operation": "complete_direct_upload",
                    "intent_token": prepared["intent_token"],
                }
            )

        payload = response.get_json()
        assert response.status_code == 503
        assert payload["code"] == "object_storage_temporarily_unavailable"
        assert payload["retryable"] is True
        assert response.headers["Retry-After"] == "5"
        assert temporary_key in self.storage_client.objects
        assert self.storage_client.delete_calls == []
        assert ArchivoAdjunto.query.count() == 0

    def test_complete_preserves_objects_when_copy_result_is_unavailable(self):
        _, prepared = self._prepare()
        temporary_key = self.storage_client.presigned_calls[-1]["params"]["Key"]
        self.storage_client.objects[temporary_key] = {
            "ContentLength": 4,
            "ContentType": "image/png",
            "ETag": '"original-etag"',
        }

        with patch.object(
            self.storage,
            "copy_object",
            side_effect=R2ObjectStorageUnavailableError("COPY 503"),
        ):
            response = self._post(
                {
                    "operation": "complete_direct_upload",
                    "intent_token": prepared["intent_token"],
                }
            )

        assert response.status_code == 503
        assert response.get_json()["code"] == "object_storage_temporarily_unavailable"
        assert response.headers["Retry-After"] == "5"
        assert temporary_key in self.storage_client.objects
        assert self.storage_client.delete_calls == []
        assert ArchivoAdjunto.query.count() == 0

    def test_complete_preserves_final_and_temporary_objects_when_final_head_is_unavailable(self):
        _, prepared = self._prepare()
        temporary_key = self.storage_client.presigned_calls[-1]["params"]["Key"]
        self.storage_client.objects[temporary_key] = {
            "ContentLength": 4,
            "ContentType": "image/png",
            "ETag": '"original-etag"',
        }
        real_head_object = self.storage.head_object

        def head_with_final_outage(key):
            if key != temporary_key:
                raise R2ObjectStorageUnavailableError("final HEAD 503")
            return real_head_object(key)

        with patch.object(
            self.storage,
            "head_object",
            side_effect=head_with_final_outage,
        ):
            response = self._post(
                {
                    "operation": "complete_direct_upload",
                    "intent_token": prepared["intent_token"],
                }
            )

        final_key = self.storage_client.copy_calls[-1]["Key"]
        assert response.status_code == 503
        assert response.get_json()["code"] == "object_storage_temporarily_unavailable"
        assert response.headers["Retry-After"] == "5"
        assert temporary_key in self.storage_client.objects
        assert final_key in self.storage_client.objects
        assert self.storage_client.delete_calls == []
        assert ArchivoAdjunto.query.count() == 0

    def test_complete_rejects_etag_race_and_cleans_temporary_and_final_objects(self):
        _, prepared = self._prepare()
        temporary_key = self.storage_client.presigned_calls[-1]["params"]["Key"]
        self.storage_client.objects[temporary_key] = {
            "ContentLength": 4,
            "ContentType": "image/png",
            "ETag": '"original-etag"',
        }
        self.storage_client.force_etag_race = True

        response = self._post(
            {
                "operation": "complete_direct_upload",
                "intent_token": prepared["intent_token"],
            }
        )

        payload = response.get_json()
        final_key = self.storage_client.copy_calls[-1]["Key"]
        assert response.status_code == 409
        assert payload["code"] == "uploaded_object_changed"
        assert self.storage_client.copy_calls[-1]["CopySourceIfMatch"] == '"original-etag"'
        assert temporary_key not in self.storage_client.objects
        assert final_key not in self.storage_client.objects
        assert ArchivoAdjunto.query.count() == 0

    def test_complete_rejects_final_head_mismatch_and_cleans_both_objects(self):
        _, prepared = self._prepare()
        temporary_key = self.storage_client.presigned_calls[-1]["params"]["Key"]
        self.storage_client.objects[temporary_key] = {
            "ContentLength": 4,
            "ContentType": "image/png",
            "ETag": '"original-etag"',
        }
        self.storage_client.final_metadata_override = {"ContentLength": 5}

        response = self._post(
            {
                "operation": "complete_direct_upload",
                "intent_token": prepared["intent_token"],
            }
        )

        payload = response.get_json()
        final_key = self.storage_client.copy_calls[-1]["Key"]
        assert response.status_code == 409
        assert payload["code"] == "promoted_object_mismatch"
        assert temporary_key not in self.storage_client.objects
        assert final_key not in self.storage_client.objects
        assert ArchivoAdjunto.query.count() == 0

    def test_complete_rejects_final_mime_mismatch_before_database_persistence(self):
        _, prepared = self._prepare()
        temporary_key = self.storage_client.presigned_calls[-1]["params"]["Key"]
        self.storage_client.objects[temporary_key] = {
            "ContentLength": 4,
            "ContentType": "image/png",
            "ETag": '"original-etag"',
        }
        self.storage_client.final_metadata_override = {
            "ContentType": "application/pdf"
        }

        response = self._post(
            {
                "operation": "complete_direct_upload",
                "intent_token": prepared["intent_token"],
            }
        )

        final_key = self.storage_client.copy_calls[-1]["Key"]
        assert response.status_code == 409
        assert response.get_json()["code"] == "promoted_object_mismatch"
        assert temporary_key not in self.storage_client.objects
        assert final_key not in self.storage_client.objects
        assert ArchivoAdjunto.query.count() == 0

    def test_postgres_completion_uses_a_transaction_scoped_advisory_lock(self):
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        with patch.object(
            db.session,
            "get_bind",
            return_value=bind,
        ), patch.object(db.session, "execute") as execute_mock:
            direct_attachment_upload._acquire_completion_lock("upload-intent-1")

        execute_mock.assert_called_once()
        statement, parameters = execute_mock.call_args.args
        assert "pg_advisory_xact_lock" in str(statement)
        assert isinstance(parameters["lock_key"], int)
