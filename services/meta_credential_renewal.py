"""Revalidate an installed Meta credential without rotating its revision."""
from __future__ import annotations

import logging
import time

from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from cutover_writer_fence import cutover_writer_fence_enabled
from models import AuditEvent, ProviderConnection, db
from services import meta_credential_operations as ops
from services import meta_whatsapp_credentials as vault
from services.global_writer_authority import evaluate_global_writer_authority, global_writer_authority_lease
from services.meta_credential_admin_runtime import LOCAL_EXPIRY_SECONDS, _transaction, database_identity
from services.meta_credential_revalidation import REJECTION_REASONS, revalidate_credential
from services.meta_whatsapp_runtime import MetaRuntimeError, native_graph_request
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY
from utils.meta_credential_http import private_renewal_logs


LOG = logging.getLogger(__name__)
RENEWAL_KEY = "meta_credential_renewal"
FLAG = "VERCEL_META_CREDENTIAL_RENEWAL_CRON_ENABLED"
DAY = 86400
RENEWAL_WINDOW = 10 * DAY


def renewal_status(target, config, expiry, saved, now):
    """Only expose known states, fixed reasons and integer timestamps from JSON."""
    saved = saved if isinstance(saved, dict) else {}
    last = saved.get("last_revalidated_at")
    last = last if type(last) is int and 0 <= last <= now else None
    after = reason = None
    if target.expected_revision <= 0 or config.get(FLAG) is not True:
        state, reason = "disabled", "renewal_disabled"
    elif type(expiry) is not int:
        state, reason = "disabled", "credential_unavailable"
    elif expiry <= now:
        state, reason = "expired", "local_expiry_reached"
    elif (saved.get("state") == "failed" and isinstance(saved.get("reason"), str)
          and saved["reason"] in REJECTION_REASONS):
        state, reason = "failed", saved["reason"]
    else:
        after = max(0, expiry - RENEWAL_WINDOW)
        if last is not None:
            # A finite provider expiry can remain in the window after a success.
            after = min(expiry, max(after, last + DAY))
        state = "due" if now >= after else "ok"
    return dict(mode="automatic", state=state, last_revalidated_at=last,
                next_check_after=after, reason=reason)


def _summary(target, state, reason=None):
    return {"target": target.name, "state": state, "reason": reason}


class _SkipWrite(Exception):
    """Unwind the entire application transaction before reporting a lost race."""

    def __init__(self, state, reason):
        self.state, self.reason = state, reason


def _check_writer(config, decision, epoch):
    if (cutover_writer_fence_enabled(config) or not decision.allowed or not decision.enabled
            or type(epoch) is not int or epoch <= 0 or decision.epoch != epoch):
        raise _SkipWrite("due", "writer_unavailable")


def _check_time(now, old_expiry, proof):
    if now >= old_expiry:
        raise _SkipWrite("expired", "local_expiry_reached")
    if proof is not None and not proof.observed_at <= now < proof.valid_until:
        raise _SkipWrite("due", "proof_stale")


def _binding(session, target, *, lock=False):
    ops._inspect_tenant(session, target, lock=lock)
    connection, _ = ops.inspect_binding(session, target, lock=lock)
    return connection


def _renew(target, config, engine, graph_get, clock):
    if cutover_writer_fence_enabled(config):
        raise _SkipWrite("due", "writer_unavailable")
    database_identity(engine, config)
    # Close the read transaction before Graph I/O. Never hold a credential
    # row lock while the provider responds or open an already-expired token.
    with _transaction(engine, read_only=True) as session:
        connection = _binding(session, target)
        envelope = connection.config[PRIVATE_CONFIG_KEY]
        old_expiry = envelope.get("expires_at")
        saved = connection.config.get(RENEWAL_KEY)
        now = clock()
        status = renewal_status(target, config, old_expiry, saved, now)
        if status["state"] != "due":
            return _summary(target, status["state"], status["reason"]), False
        credential = vault.open_token(connection=connection, tenant_id=target.tenant_id,
                                     now=now, app_config=config)

    decision = evaluate_global_writer_authority(config)
    epoch = decision.epoch
    _check_writer(config, decision, epoch)
    proof = provider_expiry = failure = None
    try:
        proof, provider_expiry = revalidate_credential(
            target, credential, config, graph_get=graph_get, clock=clock)
    except MetaRuntimeError as exc:
        if exc.transient or exc.code not in REJECTION_REASONS:
            _check_time(clock(), old_expiry, None)
            return _summary(target, "due", "revalidation_unavailable"), False
        failure = exc.code

    with global_writer_authority_lease(config) as lease:
        try:
            _check_writer(config, lease.revalidate(config), epoch)
            with _transaction(engine, read_only=False) as session:
                if engine.dialect.name == "postgresql":
                    # Take the ordinary UPDATE table lock before any row locks,
                    # in the same order as a manual apply's stronger table lock.
                    session.execute(text("LOCK TABLE public.provider_connection IN ROW EXCLUSIVE MODE"))
                connection = _binding(session, target, lock=True)
                prior = connection.config
                # Compare the observed revision and expiry, as well as the nonce
                # and last outcome for simultaneous checks of a provider-capped token.
                if prior[PRIVATE_CONFIG_KEY] != envelope or prior.get(RENEWAL_KEY) != saved:
                    raise _SkipWrite("due", "credential_changed")
                now = clock()
                _check_time(now, old_expiry, proof)
                replacement = dict(prior)
                if failure:
                    replacement[RENEWAL_KEY] = {**status, "state": "failed", "reason": failure,
                                                 "next_check_after": None}
                    event = "provider_credential.meta_renewal_failed"
                else:
                    expiry = proof.observed_at + LOCAL_EXPIRY_SECONDS
                    if provider_expiry is not None:
                        expiry = min(expiry, provider_expiry)
                    replacement[PRIVATE_CONFIG_KEY] = vault.seal_token(
                        connection=connection, tenant_id=target.tenant_id, access_token=credential.access_token,
                        revision=credential.revision, expires_at=expiry, now=now, app_config=config)
                    replacement[RENEWAL_KEY] = renewal_status(
                        target, config, expiry, {"last_revalidated_at": proof.observed_at}, now)
                    event = "provider_credential.meta_revalidated"

                changed = session.execute(update(ProviderConnection).where(
                    ProviderConnection.id == connection.id, ProviderConnection.config == prior)
                    .values(config=replacement).returning(ProviderConnection.id)
                    .execution_options(synchronize_session=False)).scalar_one_or_none()
                if changed is None:
                    raise _SkipWrite("due", "credential_changed")
                result = replacement[RENEWAL_KEY]
                session.add(AuditEvent(tenant_id=target.tenant_id, actor_user_id=None,
                    event_type=event, resource_type="provider_connection", resource_id=str(connection.id),
                    details={"revision": credential.revision, "reason": result["reason"],
                             "expires_at": replacement[PRIVATE_CONFIG_KEY]["expires_at"]}))
                session.flush()
                _check_writer(config, lease.revalidate(config), epoch)
                _check_time(clock(), old_expiry, proof)
        except _SkipWrite as skip:
            # Leave the lease normally after the application transaction rolls
            # back. The shared authority adapter otherwise masks body errors.
            return _summary(target, skip.state, skip.reason), False
        except DBAPIError as exc:
            # PostgreSQL can detect the competing write at lock or commit time.
            if (getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)) == "40001":
                return _summary(target, "due", "concurrent_update"), False
            raise
    return _summary(target, result["state"], result["reason"]), bool(failure)


def renew_target(target_name, *, config, graph_get=native_graph_request,
                 clock=lambda: int(time.time()), engine=None):
    target = ops.TARGETS[target_name]
    if config.get(FLAG) is not True or target.expected_revision <= 0:
        return _summary(target, "disabled", "renewal_disabled")
    with private_renewal_logs():
        try:
            result, alert = _renew(target, config, db.engine if engine is None else engine, graph_get, clock)
        except _SkipWrite as skip:
            return _summary(target, skip.state, skip.reason)
        except Exception:
            # Driver/adapter exceptions may contain SQL parameters or request URLs.
            # Never copy them, including their traceback, into a log or response.
            result, alert = _summary(target, "due", "renewal_unavailable"), False
    if alert:
        LOG.error("meta_credential_renewal_failed")
    elif result["reason"] == "renewal_unavailable":
        LOG.warning("meta_credential_renewal_unavailable")
    return result


def run_renewal(config):
    return [renew_target(target.name, config=config) for target in ops.TARGETS.values()
            if target.expected_revision > 0]
