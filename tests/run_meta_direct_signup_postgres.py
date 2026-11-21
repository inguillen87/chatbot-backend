"""Two-worker signup races against a verified empty, dedicated, loopback PostgreSQL 18."""
from concurrent.futures import ThreadPoolExecutor
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
    from flask import Flask
    from sqlalchemy import text
    from sqlalchemy.engine import URL
    from models import AuditEvent, ProviderConnection, ProviderSender, TenantProfile, User, db
    from services import meta_direct_signup as service
    from tests.test_meta_direct_signup import Provider, NOW

    with psycopg.connect(**target, connect_timeout=3) as pg:
        verify_empty_fixture(pg)
    url = URL.create("postgresql+psycopg", username=target["user"], password=target["password"],
                     host="127.0.0.1", port=target["port"], database=target["dbname"])
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY=secrets.token_hex(32), SQLALCHEMY_DATABASE_URI=url,
                      SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"options": "-c lock_timeout=8s -c statement_timeout=12s"}},
                      META_WHATSAPP_DIRECT_EMBEDDED_SIGNUP_ENABLED=True, CUTOVER_WRITER_FENCE_ENABLED=False,
                      TENANT_PROVIDER_CREDENTIAL_KEYRING=json.dumps({"test": base64.b64encode(secrets.token_bytes(32)).decode()}),
                      TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID="test")
    db.init_app(app)

    def authorize(org, account):
        return org.id == account[0] and org.pyme_id == account[1]

    def issue(account):
        with app.app_context():
            data, status = service.start_signup(db.session, app.config, *account,
                authorize=lambda org: authorize(org, account), clock=lambda: NOW)
            assert status == 201
            return data["signup_state"]

    def complete(account, state, graph, asset):
        with app.app_context():
            return service.finish_signup(db.session, app.config, *account, state=state,
                sdk_session="sdk-" + state, waba=f"80000000{asset}", phone_id=f"90000000{asset}",
                code="fixture-" + state, authorize=lambda org: authorize(org, account),
                graph_factory=lambda *_args, **_kwargs: graph, clock=lambda: NOW)

    with app.app_context():
        db.create_all()
        try:
            accounts = []
            for index in range(3):
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
            def await_replay():
                arrived.set()
                assert release.wait(12)
            graph.after_exchange = await_replay
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
            pending = [issue(account) for account in accounts[1:]]
            simultaneous = Barrier(2, timeout=10)
            providers = [Provider(), Provider()]
            for provider in providers:
                # A second synthetic number shared by just these two requests.
                original = provider.proof
                def proof(make=original):
                    from dataclasses import replace
                    return replace(make(), phone="+15550000102")
                provider.proof = proof
                provider.after_exchange = simultaneous.wait
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(complete, account, claimed, provider, 2)
                           for account, claimed, provider in zip(accounts[1:], pending, providers)]
                outcomes = [future.result(timeout=25)[1] for future in futures]
            assert sorted(outcomes) == [200, 409]
            assert sum(p.exchanges for p in providers) == 2 and sum(p.subscriptions for p in providers) == 1
            db.session.expire_all()
            assert ProviderConnection.query.count() == ProviderSender.query.count() == 2
            assert AuditEvent.query.count() == 4
            winner = accounts[1 + outcomes.index(200)][0]
            assert ProviderSender.query.filter_by(waba_id="800000002").one().tenant_id == winner
        finally:
            db.session.remove()
            # The verified empty fixture owns public. Legacy cyclic foreign keys
            # between user and tenant_profile prevent SQLAlchemy's drop_all().
            with db.engine.begin() as cleanup:
                cleanup.execute(text("DROP SCHEMA public CASCADE"))
                cleanup.execute(text("CREATE SCHEMA public"))
            db.engine.dispose()
    print("2 PostgreSQL signup races passed on a dedicated loopback fixture")


if __name__ == "__main__":
    main()
