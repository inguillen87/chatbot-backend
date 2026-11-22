"""Two-worker signup races against a verified empty, dedicated, loopback PostgreSQL 18."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier, Event
import base64
import json
import secrets

from tests.run_auth_session_lifecycle_postgres import fixture_configuration, verify_empty_fixture


def main(argv=None):
    target = fixture_configuration(argv)
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    import psycopg
    from psycopg import sql

    # Signup locks tables in public by name. Give it its own database so the
    # schema, owner and grants used by subsequent CI steps stay intact.
    database = "meta_direct_signup_" + secrets.token_hex(12)
    identifier = sql.Identifier(database)
    with psycopg.connect(**target, connect_timeout=3, autocommit=True) as control:
        with control.transaction():
            verify_empty_fixture(control)
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(identifier))
        try:
            _exercise_signup({**target, "dbname": database})
        finally:
            control.execute(sql.SQL("DROP DATABASE {}").format(identifier))
    print("3 PostgreSQL signup races passed on a dedicated loopback fixture")


def _exercise_signup(target):
    from flask import Flask
    from sqlalchemy.engine import URL
    from models import AuditEvent, ProviderConnection, ProviderSender, TenantProfile, User, db
    from services import meta_direct_signup as service
    from services import meta_whatsapp_credentials as vault
    from tests.test_meta_direct_signup import Provider, NOW

    url = URL.create("postgresql+psycopg", username=target["user"], password=target["password"],
                     host="127.0.0.1", port=target["port"], database=target["dbname"])
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY=secrets.token_hex(32), SQLALCHEMY_DATABASE_URI=url,
                      SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"options": "-c lock_timeout=8s -c statement_timeout=12s"}},
                      META_WHATSAPP_DIRECT_EMBEDDED_SIGNUP_ENABLED=True, CUTOVER_WRITER_FENCE_ENABLED=False,
                      TENANT_PROVIDER_CREDENTIAL_KEYRING=json.dumps({"test": base64.b64encode(secrets.token_bytes(32)).decode()}),
                      TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID="test")
    db.init_app(app)
    now = NOW

    def authorize(org, account):
        return org.id == account[0] and org.pyme_id == account[1]

    def issue(account):
        with app.app_context():
            data, status = service.start_signup(db.session, app.config, *account,
                authorize=lambda org: authorize(org, account), clock=lambda: now)
            assert status == 201
            return data["signup_state"]

    def complete(account, state, graph, asset):
        with app.app_context():
            return service.finish_signup(db.session, app.config, *account, state=state,
                sdk_session="sdk-" + state, waba=f"80000000{asset}", phone_id=f"90000000{asset}",
                code="fixture-" + state, authorize=lambda org: authorize(org, account),
                graph_factory=lambda *_args, **_kwargs: graph, clock=lambda: now)

    def hold_exchange(arrived, release):
        arrived.set()
        assert release.wait(12)

    with app.app_context():
        try:
            db.create_all()
            accounts = []
            for index in range(4):
                actor = User(name="Synthetic owner", email=f"pg-signup-{index}@example.invalid",
                             rol="admin", password_hash="synthetic-not-a-login")
                db.session.add(actor)
                db.session.flush()
                tenant = TenantProfile(slug=f"pg-signup-{index}", nombre="Synthetic business",
                                       tipo="pyme", pyme_id=actor.id)
                db.session.add(tenant)
                db.session.flush()
                accounts.append((tenant.id, actor.id))
            db.session.commit()
            state = issue(accounts[0])
            arrived, release = Event(), Event()
            graph = Provider()
            graph.after_exchange = partial(hold_exchange, arrived, release)
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(complete, accounts[0], state, graph, 1)
                try:
                    assert arrived.wait(8)
                    replay = executor.submit(complete, accounts[0], state, graph, 1).result(timeout=8)
                    assert replay[1] == 202 and replay[0]["reason_code"] == "meta_signup_exchange_unconfirmed"
                finally:
                    release.set()
                assert first.result(timeout=15)[1] == 200
            assert graph.exchanges == graph.subscriptions == 1
            assert complete(accounts[0], state, graph, 1)[1] == 200 and graph.exchanges == 1

            # Both tenants have committed a distinct code claim before either may reserve the shared WABA.
            competing = accounts[1:3]
            pending = [issue(account) for account in competing]
            simultaneous = Barrier(2, timeout=10)
            providers = [Provider(phone="+15550000102") for _ in competing]
            for provider in providers:
                provider.after_exchange = simultaneous.wait
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(complete, account, claimed, provider, 2)
                           for account, claimed, provider in zip(competing, pending, providers)]
                outcomes = [future.result(timeout=25)[1] for future in futures]
            assert sorted(outcomes) == [200, 409]
            assert sum(p.exchanges for p in providers) == 2 and sum(p.subscriptions for p in providers) == 1
            db.session.expire_all()
            assert ProviderConnection.query.count() == ProviderSender.query.count() == 2
            assert AuditEvent.query.count() == 4
            winner = accounts[1 + outcomes.index(200)][0]
            assert ProviderSender.query.filter_by(waba_id="800000002").one().tenant_id == winner

            # Hold one exchange open until the competing session commits revision 2
            # and that new credential also expires. Only the fresh claim may rotate.
            account = accounts[3]
            phone = "+15550000103"
            original = Provider(phone=phone, expires_at=now + 10)
            original.registered = False
            assert complete(account, issue(account), original, 3)[1] == 202
            sender = ProviderSender.query.filter_by(tenant_id=account[0]).one()
            original_pair = (sender.provider_connection_id, sender.id)
            now = original.expires_at
            slow_state, fast_state = issue(account), issue(account)
            slow = Provider(phone=phone, token="synthetic-stale", expires_at=now + 3600)
            fast = Provider(phone=phone, token="synthetic-winner", expires_at=now + 2)
            fast.registered = False
            arrived, release = Event(), Event()
            slow.after_exchange = partial(hold_exchange, arrived, release)
            with ThreadPoolExecutor(max_workers=2) as executor:
                delayed = executor.submit(complete, account, slow_state, slow, 3)
                try:
                    assert arrived.wait(8)
                    accepted = executor.submit(complete, account, fast_state, fast, 3).result(timeout=8)
                    assert accepted[1] == 202 and accepted[0]["status"] == "pending"
                    now = fast.expires_at
                finally:
                    release.set()
                rejected = delayed.result(timeout=15)
            assert rejected[1] == 409 and complete(account, slow_state, slow, 3) == rejected
            assert slow.exchanges == fast.exchanges == 1 and slow.subscriptions == fast.subscriptions == 0
            db.session.expire_all()
            connection = ProviderConnection.query.filter_by(tenant_id=account[0]).one()
            sender = ProviderSender.query.filter_by(tenant_id=account[0]).one()
            assert (connection.id, sender.id) == original_pair and sender.provider_connection_id == connection.id
            assert connection.status == sender.status == "pending"
            credential = vault.open_token(connection=connection, tenant_id=account[0], now=now - 1, app_config=app.config)
            assert credential == vault.MetaCredential(2, fast.expires_at, fast.token)
            events = AuditEvent.query.filter_by(tenant_id=account[0]).order_by(AuditEvent.id).all()
            assert [(event.event_type, event.details["revision"]) for event in events] == [
                ("provider_credential.meta_signup_sealed", 1), ("provider_credential.meta_signup_reauthorized", 2)]
        finally:
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    main()
