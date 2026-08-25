from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from database import db
from models import AnalyticsEvent, MunicipioTicket, PymePedido, TenantProfile
from routes.analytics_routes import _newest_cached_report
from services import weekly_analytics_reports as weekly


FIXED_NOW = datetime(2026, 8, 23, 0, 5, tzinfo=timezone.utc)
SAFE_PROVIDER_REPORT = {
    "summary": "Resumen semanal",
    "opportunities": ["Oportunidad medible"],
    "threats": ["Riesgo operativo"],
    "tone": "neutral",
}


class FakeRedis:
    def __init__(self):
        self._lock = threading.Lock()
        self._values: dict[str, str] = {}

    def set(self, key, value, *, nx, ex):
        assert nx is True
        assert 60 <= ex <= 3600
        with self._lock:
            if key in self._values:
                return False
            self._values[key] = value
            return True

    def eval(self, script, key_count, key, token):
        assert key_count == 1
        with self._lock:
            if self._values.get(key) != token:
                return 0
            del self._values[key]
            return 1


def _tenant(
    index: int,
    *,
    owner_id: int,
    tenant_type: str = "municipio",
) -> TenantProfile:
    owner_field = (
        {"municipio_id": owner_id}
        if tenant_type == "municipio"
        else {"pyme_id": owner_id}
    )
    return TenantProfile(
        slug=f"weekly-tenant-{index}",
        nombre=f"Tenant {index}",
        tipo=tenant_type,
        is_active=True,
        **owner_field,
    )


def _configure(app):
    app.config.update(
        WEEKLY_ANALYTICS_MAX_TENANTS_PER_RUN="5",
        WEEKLY_ANALYTICS_RESERVATION_TTL_SECONDS="900",
        WEEKLY_ANALYTICS_RESERVATION_REDIS_URL="redis://localhost:6379/15",
    )


def _summary():
    return {
        "kpis": {},
        "top_categories": [],
        "volume_by_day": [],
        "heatmap_points": [],
        "insights": [],
    }


def test_atomic_redis_reservation_admits_only_one_concurrent_owner():
    redis_client = FakeRedis()
    barrier = threading.Barrier(8)
    results: list[bool] = []
    provider_calls: list[int] = []
    results_lock = threading.Lock()

    def reserve(index: int):
        barrier.wait()
        acquired = weekly._try_acquire_tenant_reservation(
            redis_client,
            key="same-period-and-tenant",
            token=f"token-{index}",
            ttl_seconds=900,
        )
        with results_lock:
            results.append(acquired)
            if acquired:
                provider_calls.append(index)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 8
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert len(provider_calls) == 1


def test_retry_uses_committed_reservation_and_calls_provider_once(
    client,
    owner_user,
):
    app = client.application
    _configure(app)
    redis_client = FakeRedis()
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)

    db.session.add(_tenant(1, owner_id=owner_user.id))
    db.session.commit()

    with (
        patch.object(weekly.analytics_service, "get_summary", return_value=_summary()),
        patch.object(weekly.analytics_service, "get_commerce_analytics") as commerce,
    ):
        first = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            redis_client=redis_client,
            report_generator=provider,
        )
        retry = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            redis_client=redis_client,
            report_generator=provider,
        )

    assert first["status"] == "completed"
    assert first["reports_generated"] == 1
    assert retry["status"] == "completed"
    assert retry["provider_attempts"] == 0
    provider.assert_called_once()
    commerce.assert_not_called()
    assert AnalyticsEvent.query.filter(
        AnalyticsEvent.event_type == "weekly_ai_resv_20260823"
    ).count() == 1
    assert AnalyticsEvent.query.filter(
        AnalyticsEvent.event_type == "weekly_ai_report_consultant_municipio"
    ).count() == 1
    weekly_cache = AnalyticsEvent.query.filter(
        AnalyticsEvent.event_type == "weekly_ai_report_consultant_municipio"
    ).one()
    assert weekly_cache.payload == {
        "contract_version": "weekly.analytics_report_cache.v1",
        "source": "scheduled_weekly",
        "period_key": "20260816-20260823",
        "period_start": "2026-08-16T00:00:00+00:00",
        "period_end": "2026-08-23T00:00:00+00:00",
        "report_type": "consultant_municipio",
        "report": SAFE_PROVIDER_REPORT,
    }
    assert weekly.analytics_service.get_cached_report(
        weekly_cache.tenant_id,
        "consultant_municipio",
        max_age_hours=24 * 7,
    ) is None
    scheduled = weekly.analytics_service.get_cached_weekly_report(
        weekly_cache.tenant_id,
        "consultant_municipio",
        max_age_hours=24 * 7,
    )
    assert scheduled["summary"] == "Resumen semanal"
    assert scheduled["_report_metadata"] == {
        "contract_version": "weekly.analytics_report_cache.v1",
        "source": "scheduled_weekly",
        "period_start": "2026-08-16T00:00:00+00:00",
        "period_end": "2026-08-23T00:00:00+00:00",
        "period_key": "20260816-20260823",
        "report_type": "consultant_municipio",
        "generated_at": scheduled["_report_metadata"]["generated_at"],
    }
    assert scheduled["_report_metadata"]["generated_at"]

    weekly.analytics_service.cache_report(
        weekly_cache.tenant_id,
        "consultant_municipio",
        {"summary": "Informe ad hoc posterior"},
        source="ad_hoc",
        period_start=datetime(2026, 8, 18, tzinfo=timezone.utc),
        period_end=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    ad_hoc = weekly.analytics_service.get_cached_report(
        weekly_cache.tenant_id,
        "consultant_municipio",
        max_age_hours=24 * 7,
    )
    assert _newest_cached_report(scheduled, ad_hoc)["summary"] == (
        "Informe ad hoc posterior"
    )


def test_provider_success_with_cache_failure_is_never_retried(
    client,
    owner_user,
):
    app = client.application
    _configure(app)
    redis_client = FakeRedis()
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)

    db.session.add(_tenant(2, owner_id=owner_user.id))
    db.session.commit()

    with (
        patch.object(weekly.analytics_service, "get_summary", return_value=_summary()),
        patch.object(
            weekly,
            "_persist_completed_report",
            side_effect=RuntimeError("private database detail"),
        ),
    ):
        first = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            redis_client=redis_client,
            report_generator=provider,
        )
        retry = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            redis_client=redis_client,
            report_generator=provider,
        )

    assert first["status"] == "degraded"
    assert first["provider_uncertain"] == 1
    assert first["unresolved_reservations"] == 1
    assert retry["provider_attempts"] == 0
    assert retry["status"] == "degraded"
    assert retry["unresolved_reservations"] == 1
    provider.assert_called_once()
    reservation = AnalyticsEvent.query.filter(
        AnalyticsEvent.event_type == "weekly_ai_resv_20260823"
    ).one()
    assert reservation.payload["status"] == "provider_attempt_uncertain"


def test_batch_bound_progresses_across_retries_without_loading_all_tenants(
    client,
    owner_user,
):
    app = client.application
    _configure(app)
    redis_client = FakeRedis()
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)

    db.session.add_all(
        [_tenant(index, owner_id=owner_user.id) for index in range(10, 15)]
    )
    db.session.commit()

    with patch.object(
        weekly.analytics_service,
        "get_summary",
        return_value=_summary(),
    ):
        first = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            max_tenants=2,
            redis_client=redis_client,
            report_generator=provider,
        )
        second = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            max_tenants=2,
            redis_client=redis_client,
            report_generator=provider,
        )
        third = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            max_tenants=2,
            redis_client=redis_client,
            report_generator=provider,
        )

    assert [first["selected"], second["selected"], third["selected"]] == [2, 2, 1]
    assert [first["status"], second["status"], third["status"]] == [
        "batch_limit_reached",
        "batch_limit_reached",
        "completed",
    ]
    assert [first["ok"], second["ok"], third["ok"]] == [True, True, True]
    assert provider.call_count == 5
    assert AnalyticsEvent.query.filter(
        AnalyticsEvent.event_type == "weekly_ai_resv_20260823"
    ).count() == 5


def test_pyme_aggregation_uses_bounded_order_sample_and_half_open_window(
    client,
    owner_user,
):
    app = client.application
    _configure(app)
    redis_client = FakeRedis()
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)
    commerce_payload = {
        "revenue": 100.0,
        "sales_by_product": [],
        "data_coverage": {
            "sales_by_product": "bounded_recent_order_sample",
            "order_sample_limit": 500,
            "total_orders": 0,
        },
    }

    tenant = _tenant(20, owner_id=owner_user.id, tenant_type="pyme")
    db.session.add(tenant)
    db.session.commit()
    tenant_id = tenant.id

    with (
        patch.object(
            weekly.analytics_service,
            "get_summary",
            return_value=_summary(),
        ) as summary,
        patch.object(
            weekly.analytics_service,
            "get_commerce_analytics",
            return_value=commerce_payload,
        ) as commerce,
    ):
        report = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            redis_client=redis_client,
            report_generator=provider,
        )

    assert report["reports_generated"] == 1
    expected_start = datetime(2026, 8, 16, tzinfo=timezone.utc)
    expected_query_end = datetime(
        2026,
        8,
        22,
        23,
        59,
        59,
        999999,
        tzinfo=timezone.utc,
    )
    summary.assert_called_once_with(
        tenant_id=tenant_id,
        start_date=expected_start,
        end_date=expected_query_end,
        context="pyme",
    )
    commerce.assert_called_once_with(
        tenant_id=tenant_id,
        start_date=expected_start,
        end_date=expected_query_end,
        product_sample_limit=500,
    )
    provider_summary = provider.call_args.args[0]
    assert provider_summary["data_coverage"]["order_sample_limit"] == 500
    assert provider_summary["report_window"]["end"] == "2026-08-23T00:00:00+00:00"


def test_batch_limit_above_hard_max_fails_before_db_and_provider(app):
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)

    with patch.object(weekly, "_candidate_tenants") as candidates:
        report = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            max_tenants=11,
            redis_client=FakeRedis(),
            report_generator=provider,
        )

    assert report["status"] == "configuration_unavailable"
    candidates.assert_not_called()
    provider.assert_not_called()


def test_redis_failure_stops_before_aggregation_and_provider(
    client,
    owner_user,
    caplog,
):
    class BrokenRedis:
        def set(self, *args, **kwargs):
            raise RuntimeError("redis://private:secret@cache.internal/0")

    app = client.application
    _configure(app)
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)
    db.session.add(_tenant(30, owner_id=owner_user.id))
    db.session.commit()

    with patch.object(weekly.analytics_service, "get_summary") as summary:
        report = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            redis_client=BrokenRedis(),
            report_generator=provider,
        )

    assert report["status"] == "degraded"
    assert report["reservation_failures"] == 1
    assert report["provider_attempts"] == 0
    summary.assert_not_called()
    provider.assert_not_called()
    assert "redis://private" not in caplog.text


def test_invalid_or_missing_real_redis_configuration_fails_before_db_and_provider(app):
    app.config.update(
        WEEKLY_ANALYTICS_RESERVATION_REDIS_URL="memory://",
        RATELIMIT_STORAGE_URI="memory://",
    )
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)

    with patch.object(weekly, "_candidate_tenants") as candidates:
        report = weekly.run_weekly_analytics_batch(
            app,
            now=FIXED_NOW,
            report_generator=provider,
        )

    assert report["status"] == "configuration_unavailable"
    assert report["ok"] is False
    candidates.assert_not_called()
    provider.assert_not_called()


def test_render_drain_processes_more_tenants_than_one_batch(
    client,
    owner_user,
):
    app = client.application
    _configure(app)
    redis_client = FakeRedis()
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)
    db.session.add_all(
        [_tenant(index, owner_id=owner_user.id) for index in range(50, 56)]
    )
    db.session.commit()

    with patch.object(
        weekly.analytics_service,
        "get_summary",
        return_value=_summary(),
    ):
        report = weekly.run_weekly_analytics_drain(
            app,
            now=FIXED_NOW,
            max_batches=3,
            max_tenants=2,
            redis_client=redis_client,
            report_generator=provider,
        )

    assert report["contract_version"] == "weekly.analytics_report_drain.v1"
    assert report["status"] == "completed"
    assert report["ok"] is True
    assert report["batches_run"] == 3
    assert report["selected"] == 6
    assert report["reports_generated"] == 6
    assert report["has_more"] is False
    assert provider.call_count == 6


def test_render_drain_continues_after_partial_degradation(
    client,
    owner_user,
):
    app = client.application
    _configure(app)
    redis_client = FakeRedis()
    provider = Mock(return_value=SAFE_PROVIDER_REPORT)
    db.session.add_all(
        [_tenant(index, owner_id=owner_user.id) for index in range(70, 76)]
    )
    db.session.commit()
    aggregation_calls = 0

    def transient_aggregation(*args, **kwargs):
        nonlocal aggregation_calls
        aggregation_calls += 1
        if aggregation_calls == 1:
            raise RuntimeError("transient local aggregation failure")
        return _summary()

    with patch.object(
        weekly.analytics_service,
        "get_summary",
        side_effect=transient_aggregation,
    ):
        report = weekly.run_weekly_analytics_drain(
            app,
            now=FIXED_NOW,
            max_batches=2,
            max_tenants=5,
            redis_client=redis_client,
            report_generator=provider,
        )

    assert report["status"] == "completed"
    assert report["ok"] is True
    assert report["batches_run"] == 2
    assert report["selected"] == 7
    assert report["failed_before_provider"] == 1
    assert report["reports_generated"] == 6
    assert provider.call_count == 6


def test_ad_hoc_report_cache_is_bound_to_its_exact_period(
    client,
    owner_user,
):
    tenant = _tenant(60, owner_id=owner_user.id)
    db.session.add(tenant)
    db.session.commit()
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 8, tzinfo=timezone.utc)
    report = {"summary": "Periodo exacto"}

    weekly.analytics_service.cache_report(
        tenant.id,
        "consultant_municipio",
        report,
        source="ad_hoc",
        period_start=start,
        period_end=end,
    )

    cached = weekly.analytics_service.get_cached_report(
        tenant.id,
        "consultant_municipio",
        max_age_hours=24 * 30,
        source="ad_hoc",
        period_start=start,
        period_end=end,
    )
    assert cached["summary"] == "Periodo exacto"
    assert cached["_report_metadata"]["source"] == "ad_hoc"
    assert weekly.analytics_service.get_cached_report(
        tenant.id,
        "consultant_municipio",
        max_age_hours=24 * 30,
        source="ad_hoc",
        period_start=start,
        period_end=end + timedelta(days=1),
    ) is None


def test_render_weekly_cron_reuses_real_redis_reservation_store():
    repository_root = Path(weekly.__file__).resolve().parents[1]
    blueprint = yaml.safe_load(
        (repository_root / "render.yaml").read_text(encoding="utf-8")
    )
    service = next(
        item
        for item in blueprint["services"]
        if item.get("name") == "weekly-analytics-report"
    )
    env = {item["key"]: item for item in service["envVars"]}

    assert service["schedule"] == "0 0 * * 0"
    assert env["ENV"]["value"] == "production"
    assert env["FLASK_ENV"]["value"] == "production"
    assert env["CHATBOC_PROCESS_ROLE"]["value"] == "weekly-analytics-cron"
    assert env["DATABASE_URL"]["fromService"] == {
        "type": "web",
        "name": "chatboc-backend",
        "envVarKey": "DATABASE_URL",
    }
    assert env["SECRET_KEY"]["fromService"] == {
        "type": "web",
        "name": "chatboc-backend",
        "envVarKey": "SECRET_KEY",
    }
    assert env["OPENAI_API_KEY"]["fromService"] == {
        "type": "web",
        "name": "chatboc-backend",
        "envVarKey": "OPENAI_API_KEY",
    }
    assert env["WEEKLY_ANALYTICS_RESERVATION_REDIS_URL"]["fromService"] == {
        "type": "web",
        "name": "chatboc-backend",
        "envVarKey": "RATELIMIT_STORAGE_URI",
    }
    assert env["WEEKLY_ANALYTICS_MAX_TENANTS_PER_RUN"]["value"] == "5"
    assert env["WEEKLY_ANALYTICS_MAX_BATCHES_PER_DRAIN"]["value"] == "12"
    assert env["WEEKLY_ANALYTICS_RESERVATION_TTL_SECONDS"]["value"] == "900"


def test_summary_fallback_models_honor_the_requested_end_date(
    client,
    owner_user,
):
    app = client.application
    _configure(app)
    start = datetime(2026, 8, 16, tzinfo=timezone.utc)
    end = datetime(2026, 8, 23, tzinfo=timezone.utc)

    municipio = _tenant(40, owner_id=owner_user.id)
    pyme = _tenant(41, owner_id=owner_user.id, tenant_type="pyme")
    db.session.add_all([municipio, pyme])
    db.session.flush()

    for category, moment in (
        ("before", start - timedelta(microseconds=1)),
        ("at_start", start),
        ("at_end", end),
        ("after", end + timedelta(microseconds=1)),
    ):
        ticket = MunicipioTicket(
            municipio_id=owner_user.id,
            tenant_id=municipio.id,
            pregunta=category,
            categoria=category,
        )
        ticket.fecha = moment
        db.session.add(ticket)

    for state, moment in (
        ("before", start - timedelta(microseconds=1)),
        ("at_start", start),
        ("at_end", end),
        ("after", end + timedelta(microseconds=1)),
    ):
        order = PymePedido(
            pyme_id=owner_user.id,
            tenant_id=pyme.id,
            asunto=state,
            detalles="[]",
        )
        order.estado = state
        order.fecha = moment
        db.session.add(order)

    db.session.add(
        AnalyticsEvent(
            tenant_id=pyme.id,
            target_type="pyme",
            event_type="message_in",
            user_id=owner_user.id,
            timestamp=start + timedelta(days=1),
            payload={},
        )
    )
    db.session.commit()

    municipio_summary = weekly.analytics_service.get_summary(
        tenant_id=municipio.id,
        start_date=start,
        end_date=end,
        context="municipio",
    )
    assert {
        row["category"]: row["count"]
        for row in municipio_summary["top_categories"]
    } == {"at_start": 1, "at_end": 1}

    pyme_summary = weekly.analytics_service.get_summary(
        tenant_id=pyme.id,
        start_date=start,
        end_date=end,
        context="pyme",
    )
    assert {
        row["category"]: row["count"]
        for row in pyme_summary["top_categories"]
    } == {"at_start": 1, "at_end": 1}
    assert pyme_summary["kpis"]["conversion_rate"] == 200.0


def test_legacy_flask_cli_delegates_to_the_safe_bounded_drain(client):
    app = client.application
    safe_report = {
        "contract_version": "weekly.analytics_report_drain.v1",
        "ok": True,
        "status": "completed",
        "batch_budget": 12,
        "batches_run": 1,
        "selected": 0,
        "provider_attempts": 0,
        "reports_generated": 0,
        "contended": 0,
        "failed_before_provider": 0,
        "provider_uncertain": 0,
        "reservation_failures": 0,
        "unresolved_reservations": 0,
        "has_more": False,
    }

    with patch.object(
        weekly,
        "run_weekly_analytics_drain",
        return_value=safe_report,
    ) as run:
        result = app.test_cli_runner().invoke(args=["generate-weekly-reports"])

    assert result.exit_code == 0
    assert '"status": "completed"' in result.output
    run.assert_called_once_with(app)
