import os
import ipaddress
import socket
import sys

os.environ.setdefault("TESTING", "1")

import pytest

# Evita que app.py cree una instancia global conectada a Postgres durante las pruebas.
os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import TestingConfig


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback_or_unspecified_host(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    normalized = str(host).strip().lower().strip("[]")
    if normalized in {"", "localhost", socket.gethostname().lower()}:
        return True
    try:
        address = ipaddress.ip_address(normalized.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


class ExternalNetworkBlockedError(AssertionError):
    """Raised before a test can resolve or connect to an external host."""


@pytest.fixture(autouse=True)
def block_external_network_by_default(monkeypatch, request):
    """Make every pytest test offline unless marker and environment opt in."""

    explicitly_marked = request.node.get_closest_marker("external_network") is not None
    environment_opt_in = _truthy(
        os.getenv("CHATBOC_ALLOW_EXTERNAL_NETWORK_TESTS")
    )
    if explicitly_marked and environment_opt_in:
        return

    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def _blocked_message(operation, sock=None):
        family = getattr(sock, "family", "dns")
        return (
            "External network is disabled during tests "
            f"operation={operation} family={family}. "
            "Use the external_network marker together with "
            "CHATBOC_ALLOW_EXTERNAL_NETWORK_TESTS=1 for an intentional integration test."
        )

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback_or_unspecified_host(host):
            raise ExternalNetworkBlockedError(_blocked_message("dns"))
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            host = address[0] if isinstance(address, tuple) and address else None
            if not _is_loopback_or_unspecified_host(host):
                raise ExternalNetworkBlockedError(_blocked_message("connect", sock))
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            host = address[0] if isinstance(address, tuple) and address else None
            if not _is_loopback_or_unspecified_host(host):
                raise ExternalNetworkBlockedError(_blocked_message("connect_ex", sock))
        return original_connect_ex(sock, address)

    def guarded_sendto(sock, data, *args):
        address = args[-1] if args else None
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            host = address[0] if isinstance(address, tuple) and address else None
            if not _is_loopback_or_unspecified_host(host):
                raise ExternalNetworkBlockedError(_blocked_message("sendto", sock))
        return original_sendto(sock, data, *args)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)


@pytest.fixture(scope='session')
def app():
    """Create a new app instance for each test session."""
    app = create_app(TestingConfig)
    return app

@pytest.fixture(scope='function')
def client(app):
    """A test client for the app."""
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            yield client
            db.session.remove()
            db.drop_all()

@pytest.fixture(scope='function')
def init_database(client):
    """Fixture to set up the database and create some initial users."""
    from models import User, Rubro

    # Reuse seeded data when available to avoid primary-key collisions.
    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(clave="municipio", nombre="Municipalidad")
        db.session.add(rubro)
        db.session.flush()

    # Create or reuse an owner user (admin)
    owner_user = User.query.filter_by(email="admin@test.com").first()
    if not owner_user:
        owner_user = User(
            name="Admin User",
            email="admin@test.com",
            rol="admin",
            municipio_id=1,
            rubro_id=rubro.id,
            tipo_chat="municipio",
        )
        owner_user.set_password("admin")
        db.session.add(owner_user)

    # Create or reuse a viewer user (citizen)
    viewer_user = User.query.filter_by(email="viewer@test.com").first()
    if not viewer_user:
        viewer_user = User(name="Test Viewer", email="viewer@test.com", rol="usuario")
        viewer_user.set_password("viewer")
        db.session.add(viewer_user)

    db.session.commit()

    yield db

@pytest.fixture
def owner_user(init_database):
    from models import User
    return User.query.filter_by(email="admin@test.com").first()

@pytest.fixture
def viewer_user(init_database):
    from models import User
    return User.query.filter_by(email="viewer@test.com").first()
