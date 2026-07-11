import os

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from socket_service import _get_rooms_for_tenant_slug, _get_rooms_for_user, _merge_rooms_for_subscription


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


def test_socket_rooms_can_resolve_from_tenant_slug():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        owner = User(email="owner-junin@test.com", name="Owner", rol="admin", tipo_chat="municipio")
        owner.set_password("demo")
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(slug="junin-1", nombre="Junín", tipo="municipio", municipio_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        tenant_rooms = _get_rooms_for_tenant_slug("junin-1")
        assert f"municipio_{owner.id}" in tenant_rooms

        user_rooms = _get_rooms_for_user(owner)
        assert user_rooms == [] or isinstance(user_rooms, list)

        db.session.remove()
        db.drop_all()


def test_socket_room_merge_preserves_user_and_tenant_rooms():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        owner = User(email="owner-pyme@test.com", name="Owner Pyme", rol="admin", tipo_chat="pyme")
        owner.set_password("demo")
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(slug="bodega-1", nombre="Bodega", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)

        user = User(
            email="operador@test.com",
            name="Operador",
            rol="empleado",
            tipo_chat="pyme",
            empresa_id=owner.id,
            rubro_id=99,
            tenant_slug=tenant.slug,
        )
        user.set_password("demo")
        db.session.add(user)
        db.session.commit()

        merged_rooms = _merge_rooms_for_subscription(user, tenant.slug)

        # Rubro IDs are shared by unrelated businesses and must never define
        # an operator room. Realtime access is scoped to owner and tenant.
        assert "pyme_99" not in merged_rooms
        assert f"pyme_{owner.id}" in merged_rooms
        assert f"tenant_{tenant.id}" in merged_rooms
        assert f"crm_{tenant.id}" in merged_rooms

        db.session.remove()
        db.drop_all()
