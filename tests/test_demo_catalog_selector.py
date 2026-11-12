from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config, validate_runtime_security
from extensions import limiter
from limits.storage import RedisStorage
from routes.v2 import demo as demo_routes
from services.demo_catalog_admission import (
    admit_demo_catalog_full,
    demo_catalog_counter_keys,
)


class DemoCatalogSelectorTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    RATELIMIT_STORAGE_URI = "memory://"
    DEMO_CATALOG_FULL_RATE_LIMIT = "2 per second"
    DEMO_CATALOG_FULL_GLOBAL_RATE_LIMIT = "4 per second"


def _field_names(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _field_names(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _field_names(nested)


class DemoCatalogSelectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(DemoCatalogSelectorTestConfig)
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = cls.app.test_client()

    def setUp(self):
        limiter.reset()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_default_profile_preserves_full_catalog_contract(self):
        response = self.client.get(
            "/api/v2/demo/catalog",
            headers={"X-Request-Id": "catalog-full-contract"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "demo.catalog.v2")
        self.assertNotIn("response_profile", payload)
        self.assertEqual(payload.get("request_id"), "catalog-full-contract")
        self.assertEqual(len(payload.get("rubros") or []), 18)

        first_rubro = (payload.get("rubros") or [])[0]
        for field in (
            "sales_story",
            "consulting_playbook",
            "wow_flows",
            "live_modules",
            "openai_runtime",
            "survey_voting",
        ):
            self.assertIn(field, first_rubro)

        for group in payload.get("sector_groups") or []:
            self.assertIn("rubros", group)
            self.assertNotIn("rubro_slugs", group)

    def test_selector_contract_is_complete_small_and_excludes_heavy_fields(self):
        response = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector",
            headers={"X-Request-Id": "catalog-selector-contract"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Request-Id"), "catalog-selector-contract")
        self.assertEqual(response.headers.get("Cache-Control"), "public, max-age=300")
        self.assertIn("Origin", response.headers.get("Vary") or "")
        self.assertLess(len(response.data), 50_000)

        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "demo.catalog.v2")
        self.assertEqual(payload.get("response_profile"), "selector")
        self.assertEqual(payload.get("sectors"), ["gobierno", "empresas", "educacion"])
        self.assertTrue(payload.get("pillars"))
        self.assertTrue(payload.get("resources"))

        rubros = payload.get("rubros") or []
        self.assertEqual(len(rubros), 18)
        required_rubro_fields = {
            "slug",
            "key",
            "label",
            "tipo_chat",
            "tenant_slug",
            "sector",
            "pillar",
            "resources",
            "sample_prompts",
        }
        self.assertTrue(all(required_rubro_fields.issubset(rubro) for rubro in rubros))

        municipio = next(rubro for rubro in rubros if rubro.get("slug") == "municipio")
        self.assertEqual(municipio.get("tenant_slug"), "municipio")
        self.assertEqual(municipio.get("sector"), "gobierno")
        self.assertTrue(municipio.get("resources"))
        self.assertTrue(municipio.get("sample_prompts"))

        expected_tenants = {
            "gobierno": "municipio",
            "empresas": "bodega",
            "educacion": None,
        }
        grouped_slugs = set()
        for group in payload.get("sector_groups") or []:
            self.assertEqual(group.get("tenant_slug"), expected_tenants[group.get("key")])
            if group.get("key") == "educacion":
                self.assertFalse(group.get("available"))
            else:
                self.assertNotIn("available", group)
            self.assertNotIn("rubros", group)
            self.assertTrue(group.get("rubro_slugs"))
            grouped_slugs.update(group.get("rubro_slugs") or [])
        self.assertEqual(grouped_slugs, {rubro.get("slug") for rubro in rubros})

        forbidden_fields = {
            "sales_story",
            "consulting_playbook",
            "wow_flows",
            "live_modules",
            "openai_runtime",
            "survey_voting",
            "request_id",
            "server_time",
        }
        self.assertTrue(forbidden_fields.isdisjoint(set(_field_names(payload))))

    def test_selector_does_not_build_commercial_bundle(self):
        with patch("routes.v2.demo._commercial_demo_bundle") as commercial_bundle:
            response = self.client.get("/api/v2/demo/catalog?response_profile=selector")

        self.assertEqual(response.status_code, 200)
        commercial_bundle.assert_not_called()

    def test_selector_etag_is_stable_and_supports_conditional_get(self):
        first = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector",
            headers={"X-Request-Id": "selector-etag-one"},
        )
        second = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector",
            headers={"X-Request-Id": "selector-etag-two"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.headers.get("ETag"))
        self.assertEqual(first.headers.get("ETag"), second.headers.get("ETag"))
        self.assertEqual(first.data, second.data)
        self.assertNotEqual(first.headers.get("X-Request-Id"), second.headers.get("X-Request-Id"))

        conditional = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector",
            headers={
                "If-None-Match": first.headers["ETag"],
                "X-Request-Id": "selector-etag-conditional",
            },
        )

        self.assertEqual(conditional.status_code, 304)
        self.assertEqual(conditional.data, b"")
        self.assertEqual(conditional.headers.get("ETag"), first.headers.get("ETag"))
        self.assertEqual(conditional.headers.get("Cache-Control"), "public, max-age=300")
        self.assertEqual(conditional.headers.get("X-Request-Id"), "selector-etag-conditional")

    def test_selector_and_options_do_not_consume_full_catalog_budget(self):
        for _ in range(4):
            selector = self.client.get(
                "/api/v2/demo/catalog?response_profile=selector"
            )
            self.assertEqual(selector.status_code, 200)
        for _ in range(4):
            preflight = self.client.options(
                "/api/v2/demo/catalog",
                headers={"Origin": "https://chatboc.ar"},
            )
            self.assertEqual(preflight.status_code, 200)

        with (
            patch.object(
                demo_routes,
                "legacy_demo_catalog",
                wraps=demo_routes.legacy_demo_catalog,
            ) as legacy_catalog,
            patch.object(
                demo_routes,
                "_commercial_demo_bundle",
                wraps=demo_routes._commercial_demo_bundle,
            ) as commercial_bundle,
        ):
            first_full = self.client.get("/api/v2/demo/catalog")
            second_full = self.client.head(
                "/api/v2/demo/catalog?response_profile=full"
            )
            legacy_calls_before_limit = legacy_catalog.call_count
            bundle_calls_before_limit = commercial_bundle.call_count
            limited_full = self.client.get("/api/v2/demo/catalog")

            self.assertGreater(legacy_calls_before_limit, 0)
            self.assertGreater(bundle_calls_before_limit, 0)
            self.assertEqual(legacy_catalog.call_count, legacy_calls_before_limit)
            self.assertEqual(commercial_bundle.call_count, bundle_calls_before_limit)

        selector_after_limit = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector"
        )
        options_after_limit = self.client.options(
            "/api/v2/demo/catalog",
            headers={"Origin": "https://chatboc.ar"},
        )

        self.assertEqual(first_full.status_code, 200)
        self.assertEqual(second_full.status_code, 200)
        self.assertEqual(second_full.data, b"")
        self.assertEqual(limited_full.status_code, 429)
        self.assertEqual(selector_after_limit.status_code, 200)
        self.assertEqual(
            selector_after_limit.headers.get("Cache-Control"),
            "public, max-age=300",
        )
        self.assertTrue(selector_after_limit.headers.get("ETag"))
        self.assertEqual(options_after_limit.status_code, 200)
        self.assertEqual(
            options_after_limit.headers.get("Access-Control-Allow-Origin"),
            "https://chatboc.ar",
        )

    def test_full_catalog_limit_is_proxy_safe_json_cors_and_recovers(self):
        with patch.dict(
            os.environ,
            {"RENDER": "", "RENDER_SERVICE_TYPE": ""},
        ):
            first = self.client.get(
                "/api/v2/demo/catalog",
                headers={
                    "Origin": "https://chatboc.ar",
                    "X-Forwarded-For": "198.51.100.10",
                    "X-Request-Id": "full-limit-one",
                },
                environ_base={"REMOTE_ADDR": "192.0.2.44"},
            )
            second = self.client.get(
                "/api/v2/demo/catalog?response_profile=full",
                headers={
                    "Origin": "https://chatboc.ar",
                    "X-Forwarded-For": "198.51.100.11",
                    "X-Request-Id": "full-limit-two",
                },
                environ_base={"REMOTE_ADDR": "192.0.2.44"},
            )
            limited = self.client.get(
                "/api/v2/demo/catalog?response_profile=selector%00",
                headers={
                    "Origin": "https://chatboc.ar",
                    "X-Forwarded-For": "198.51.100.12",
                    "X-Request-Id": "full-limit-blocked",
                },
                environ_base={"REMOTE_ADDR": "192.0.2.44"},
            )
            other_client = self.client.get(
                "/api/v2/demo/catalog",
                headers={
                    "X-Forwarded-For": "198.51.100.12",
                    "X-Request-Id": "full-limit-client-b",
                },
                environ_base={"REMOTE_ADDR": "198.51.100.44"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json().get("request_id"), "full-limit-one")
        self.assertEqual(second.get_json().get("request_id"), "full-limit-two")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(other_client.status_code, 200)
        self.assertEqual(
            other_client.get_json().get("request_id"),
            "full-limit-client-b",
        )
        self.assertLess(len(limited.data), 2_000)
        limited_payload = limited.get_json()
        self.assertEqual(
            limited_payload.get("reason_code"),
            "demo_catalog_full_rate_limited",
        )
        self.assertEqual(limited_payload.get("rate_limit_scope"), "client")
        self.assertEqual(limited_payload.get("request_id"), "full-limit-blocked")
        self.assertNotIn("server_time", limited_payload)
        self.assertEqual(limited.headers.get("X-Request-Id"), "full-limit-blocked")
        self.assertEqual(limited.headers.get("Cache-Control"), "no-store")
        self.assertGreaterEqual(int(limited.headers.get("Retry-After") or 0), 1)
        self.assertEqual(
            limited.headers.get("Access-Control-Allow-Origin"),
            "https://chatboc.ar",
        )
        self.assertIn(
            "retry-after",
            (limited.headers.get("Access-Control-Expose-Headers") or "").lower(),
        )
        self.assertIn("Origin", limited.headers.get("Vary") or "")

        time.sleep(1.1)
        with patch.dict(
            os.environ,
            {"RENDER": "", "RENDER_SERVICE_TYPE": ""},
        ):
            recovered = self.client.get(
                "/api/v2/demo/catalog",
                headers={"X-Request-Id": "full-limit-recovered"},
                environ_base={"REMOTE_ADDR": "192.0.2.44"},
            )
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(
            recovered.get_json().get("request_id"),
            "full-limit-recovered",
        )

    def test_global_breaker_blocks_source_rotation_before_new_client_buckets(self):
        rotated_clients = [
            "192.0.2.10",
            "198.51.100.10",
            "203.0.113.10",
        ]
        with (
            patch.dict(
                self.app.config,
                {"DEMO_CATALOG_FULL_GLOBAL_RATE_LIMIT": "3 per second"},
            ),
            patch.dict(
                os.environ,
                {"RENDER": "", "RENDER_SERVICE_TYPE": ""},
            ),
            patch.object(
                demo_routes,
                "_demo_catalog_full_client_scope",
                wraps=demo_routes._demo_catalog_full_client_scope,
            ) as client_scope,
            patch.object(
                demo_routes,
                "legacy_demo_catalog",
                wraps=demo_routes.legacy_demo_catalog,
            ) as legacy_catalog,
            patch.object(
                demo_routes,
                "_commercial_demo_bundle",
                wraps=demo_routes._commercial_demo_bundle,
            ) as commercial_bundle,
        ):
            allowed = [
                self.client.get(
                    "/api/v2/demo/catalog",
                    environ_base={"REMOTE_ADDR": peer},
                    headers={"X-Forwarded-For": f"10.0.0.{index + 1}"},
                )
                for index, peer in enumerate(rotated_clients)
            ]
            scope_calls_before_limit = client_scope.call_count
            legacy_calls_before_limit = legacy_catalog.call_count
            bundle_calls_before_limit = commercial_bundle.call_count
            blocked = self.client.get(
                "/api/v2/demo/catalog",
                environ_base={"REMOTE_ADDR": "198.18.1.10"},
                headers={
                    "Origin": "https://chatboc.ar",
                    "X-Forwarded-For": "10.0.0.99",
                    "X-Request-Id": "full-global-blocked",
                },
            )
            blocked_again = self.client.get(
                "/api/v2/demo/catalog",
                environ_base={"REMOTE_ADDR": "198.19.1.10"},
                headers={"X-Forwarded-For": "10.0.0.100"},
            )

            self.assertTrue(all(response.status_code == 200 for response in allowed))
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked_again.status_code, 429)
            self.assertEqual(blocked.get_json().get("rate_limit_scope"), "global")
            self.assertEqual(blocked.get_json().get("request_id"), "full-global-blocked")
            self.assertEqual(client_scope.call_count, scope_calls_before_limit + 2)
            self.assertEqual(legacy_catalog.call_count, legacy_calls_before_limit)
            self.assertEqual(commercial_bundle.call_count, bundle_calls_before_limit)
            storage = limiter.limiter.storage
            for normalized_network in ("198.18.1.0/24", "198.19.1.0/24"):
                blocked_scope = hashlib.sha256(
                    normalized_network.encode("utf-8")
                ).hexdigest()
                _, blocked_client_key = demo_catalog_counter_keys(
                    storage,
                    blocked_scope,
                )
                self.assertEqual(storage.get(blocked_client_key), 0)

    def test_atomic_admission_with_one_global_slot_touches_one_client_bucket(self):
        storage = limiter.limiter.storage
        scopes = [f"{index:064x}" for index in range(24)]
        barrier = threading.Barrier(len(scopes))

        def attempt(scope):
            barrier.wait(timeout=5)
            return admit_demo_catalog_full(
                storage,
                client_scope=scope,
                client_limit=1,
                client_window_seconds=2,
                global_limit=1,
                global_window_seconds=1,
                allow_process_memory=True,
            )

        with ThreadPoolExecutor(max_workers=len(scopes)) as executor:
            decisions = list(executor.map(attempt, scopes))

        allowed = [decision for decision in decisions if decision.allowed]
        global_denied = [
            decision
            for decision in decisions
            if not decision.allowed and decision.scope == "global"
        ]
        touched_client_buckets = 0
        for scope in scopes:
            _, client_key = demo_catalog_counter_keys(storage, scope)
            touched_client_buckets += int(storage.get(client_key) > 0)

        self.assertEqual(len(allowed), 1)
        self.assertEqual(len(global_denied), 23)
        self.assertEqual(touched_client_buckets, 1)

    def test_client_denial_does_not_consume_global_capacity(self):
        storage = limiter.limiter.storage
        scope = "a" * 64
        global_key, _ = demo_catalog_counter_keys(storage, scope)
        kwargs = {
            "client_scope": scope,
            "client_limit": 1,
            "client_window_seconds": 1,
            "global_limit": 10,
            "global_window_seconds": 1,
            "allow_process_memory": True,
        }

        admitted = admit_demo_catalog_full(storage, **kwargs)
        global_before_denial = storage.get(global_key)
        denied = admit_demo_catalog_full(storage, **kwargs)

        self.assertTrue(admitted.allowed)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.scope, "client")
        self.assertEqual(storage.get(global_key), global_before_denial)

    def test_global_denial_does_not_create_or_consume_client_bucket(self):
        storage = limiter.limiter.storage
        first_scope = "b" * 64
        denied_scope = "c" * 64
        shared = {
            "client_limit": 1,
            "client_window_seconds": 2,
            "global_limit": 1,
            "global_window_seconds": 1,
            "allow_process_memory": True,
        }

        admitted = admit_demo_catalog_full(
            storage,
            client_scope=first_scope,
            **shared,
        )
        denied = admit_demo_catalog_full(
            storage,
            client_scope=denied_scope,
            **shared,
        )
        _, denied_client_key = demo_catalog_counter_keys(storage, denied_scope)

        self.assertTrue(admitted.allowed)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.scope, "global")
        self.assertEqual(storage.get(denied_client_key), 0)

    def test_redis_adapter_uses_one_eval_with_bounded_colocated_keys(self):
        redis_connection = MagicMock()
        redis_connection.eval.return_value = [3, 1_250]
        redis_storage = MagicMock(spec=RedisStorage)
        redis_storage.prefixed_key.side_effect = lambda key: f"LIMITS:{key}"
        redis_storage.get_connection.return_value = redis_connection

        decision = admit_demo_catalog_full(
            redis_storage,
            client_scope="d" * 64,
            client_limit=2,
            client_window_seconds=2,
            global_limit=4,
            global_window_seconds=1,
            allow_process_memory=False,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.scope, "client")
        self.assertEqual(decision.retry_after_seconds, 2)
        self.assertIsNotNone(decision.reset_at_epoch)
        self.assertGreater(decision.reset_at_epoch, time.time())
        redis_connection.eval.assert_called_once()
        eval_args = redis_connection.eval.call_args.args
        self.assertEqual(eval_args[1], 2)
        self.assertIn("{demo-catalog-full-v3}", eval_args[2])
        self.assertIn("{demo-catalog-full-v3}", eval_args[3])
        self.assertLess(len(eval_args[2]), 128)
        self.assertLess(len(eval_args[3]), 256)
        self.assertEqual(eval_args[4:], (4, 2, 1_000, 2_000))

    def test_redis_lua_failure_fails_closed_without_catalog_side_effects(self):
        redis_connection = MagicMock()
        redis_connection.eval.side_effect = ConnectionError(
            "simulated Redis EVAL failure"
        )
        redis_storage = MagicMock(spec=RedisStorage)
        redis_storage.prefixed_key.side_effect = lambda key: f"LIMITS:{key}"
        redis_storage.get_connection.return_value = redis_connection

        with (
            patch.object(
                limiter.limiter,
                "storage",
                redis_storage,
            ),
            patch.object(demo_routes, "legacy_demo_catalog") as legacy_catalog,
            patch.object(demo_routes, "_commercial_demo_bundle") as commercial_bundle,
        ):
            response = self.client.get(
                "/api/v2/demo/catalog",
                headers={
                    "Origin": "https://chatboc.ar",
                    "X-Request-Id": "full-storage-unavailable",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json().get("reason_code"),
            "demo_catalog_full_rate_limit_unavailable",
        )
        self.assertEqual(
            response.get_json().get("request_id"),
            "full-storage-unavailable",
        )
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Retry-After"), "60")
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"),
            "https://chatboc.ar",
        )
        self.assertIn(
            "retry-after",
            (response.headers.get("Access-Control-Expose-Headers") or "").lower(),
        )
        redis_connection.eval.assert_called_once()
        legacy_catalog.assert_not_called()
        commercial_bundle.assert_not_called()

    def test_process_memory_fallback_is_rejected_when_not_explicitly_allowed(self):
        storage = limiter.limiter.storage
        scope = "e" * 64
        global_key, client_key = demo_catalog_counter_keys(storage, scope)

        with self.assertRaisesRegex(RuntimeError, "shared Redis"):
            admit_demo_catalog_full(
                storage,
                client_scope=scope,
                client_limit=2,
                client_window_seconds=1,
                global_limit=4,
                global_window_seconds=1,
                allow_process_memory=False,
            )

        self.assertEqual(storage.get(global_key), 0)
        self.assertEqual(storage.get(client_key), 0)

    def test_render_web_deployment_gate_requires_shared_rate_limit_storage(self):
        with patch.dict(
            os.environ,
            {"RENDER": "true", "RENDER_SERVICE_TYPE": "web"},
        ):
            memory_errors = validate_runtime_security(
                {
                    "ENV": "production",
                    "RATELIMIT_STORAGE_URI": "memory://",
                }
            )
            shared_errors = validate_runtime_security(
                {
                    "ENV": "production",
                    "RATELIMIT_STORAGE_URI": "rediss://shared.example.test:6380/0",
                }
            )

        gate_message = (
            "RATELIMIT_STORAGE_URI debe usar Redis compartido "
            "en el servicio web de Render."
        )
        self.assertIn(gate_message, memory_errors)
        self.assertNotIn(gate_message, shared_errors)


if __name__ == "__main__":
    unittest.main()
