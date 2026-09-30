"""Disposable full-app runtime. Never loads a developer or customer's .env.

Only the standalone acceptance command may call prepare_process. Identity,
authorization, route decorators and persistence are the application originals.
"""
from pathlib import Path
import os
import secrets
import socket


_PREPARED = False

def prepare_process():
    global _PREPARED
    keep = {k: v for k, v in os.environ.items() if k.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE',
        'APPDATA', 'LOCALAPPDATA', 'PYTHONIOENCODING', 'SSL_CERT_FILE',
        'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432'}}
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update(TESTING='1', FLASK_SKIP_GLOBAL_APP='1', ENV='testing',
        PYTHON_DOTENV_DISABLED='1', SECRET_KEY=secrets.token_hex(32),
        DATABASE_URL='sqlite:///:memory:', REDIS_URL='',
        ENABLE_RUNTIME_SCHEMA_SYNC='false', ENABLE_RUNTIME_TENANT_INIT='false',
        OUTBOUND_NOTIFICATIONS_ENABLED='false', CUTOVER_WRITER_FENCE_ENABLED='false')
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_dns = socket.getaddrinfo
    original_sendto = socket.socket.sendto
    def check(host):
        if host not in ('127.0.0.1', 'localhost', '::1', None):
            raise AssertionError('Acceptance runtime cannot access external networks')
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6): check(address[0])
        return original_connect(sock, address)
    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6): check(address[0])
        return original_connect_ex(sock, address)
    def dns(host, *args, **kwargs):
        check(host)
        return original_dns(host, *args, **kwargs)
    def sendto(sock, data, *args):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and args: check(args[-1][0])
        return original_sendto(sock, data, *args)
    socket.socket.sendto = sendto
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = dns
    _PREPARED = True


def create_disposable_app(directory):
    if not _PREPARED:
        raise RuntimeError('Run acceptance in its dedicated isolated process')
    from app import create_app
    from config import TestingConfig
    database = (Path(directory) / 'profile-acceptance.sqlite').resolve()
    if database.exists():
        raise RuntimeError('Acceptance never reuses an existing database')
    class AcceptanceConfig(TestingConfig):
        SQLALCHEMY_DATABASE_URI = 'sqlite:///' + database.as_posix()
        SECRET_KEY = os.environ['SECRET_KEY']
        SESSION_TYPE = 'null'
        SESSION_COOKIE_SAMESITE = 'Lax'
        CUTOVER_WRITER_FENCE_ENABLED = False
        RATELIMIT_STORAGE_URI = 'memory://'
        OUTBOUND_NOTIFICATIONS_ENABLED = False
    app = create_app(AcceptanceConfig)
    from database import db
    from models import User, TenantProfile, Role, UserRole
    password = secrets.token_urlsafe(24)
    accounts = {}
    with app.app_context():
        assert db.engine.url.database == str(database).replace('\\', '/')
        for slug in ('acceptance-a', 'acceptance-b'):
            owner = User(name='Institution ' + slug, email=slug + '@example.invalid',
                rol='admin', tipo_chat='municipio', nombre_empresa=slug)
            owner.set_password(password)
            db.session.add(owner); db.session.flush()
            tenant = TenantProfile(slug=slug, nombre=slug, tipo='municipio',
                municipio_id=owner.id, vertical='government', configuracion={})
            db.session.add(tenant); db.session.flush()
            owner.tenant_id = tenant.id; owner.tenant_slug = slug
            owner.municipio_id = owner.id
            accounts[slug] = {'id': owner.id, 'email': owner.email, 'tenant_id': tenant.id}
            if slug == 'acceptance-a':
                for label, role in [('second', 'admin'), ('viewer', 'empleado'), ('delegated', 'empleado')]:
                    user = User(name=label, email=label+'@example.invalid', rol=role,
                        tenant_id=tenant.id, tenant_slug=slug, municipio_id=owner.id,
                        tipo_chat='municipio')
                    user.set_password(password); db.session.add(user); db.session.flush()
                    accounts[label] = {'id': user.id, 'email': user.email, 'tenant_id': tenant.id}
                    if label == 'delegated':
                        role = Role.query.filter_by(name='tenant_admin').first()
                        if role is None:
                            role = Role(name='tenant_admin'); db.session.add(role); db.session.flush()
                        db.session.add(UserRole(user_id=user.id, role_id=role.id, tenant_id=tenant.id))
        db.session.commit()
    return app, accounts, password
