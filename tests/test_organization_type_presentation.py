"""Descriptive contracts on disposable ORM/HTTP fixtures, without providers."""
import pytest

from database import db
from models import AuditEvent, TenantConfig, TenantProfile, User
from services.organization_type_presentation import CONTRACT, organization_type_descriptor
from tests.auth_test_utils import clerk_superadmin_headers
from utils.roles import ROLE_PERMISSIONS, TENANT_TYPE_ALIASES, normalize_tenant_type, role_for_tenant_type


@pytest.mark.parametrize('kind', tuple(TENANT_TYPE_ALIASES))
def test_known_type_aliases_share_one_descriptive_label(kind):
    expected = {'municipio': 'Gobierno', 'colegio': 'Educación', 'pyme': 'Empresa'}
    assert organization_type_descriptor(kind) == {
        'organization_type_label_contract': CONTRACT,
        'organization_type_label': expected[normalize_tenant_type(kind)],
    }


@pytest.mark.parametrize('kind', (None, '', 'unknown', 'tenant_admin', 'super_admin'))
def test_unknown_type_or_role_is_not_presented_as_a_business(kind):
    assert organization_type_descriptor(kind)['organization_type_label'] is None


@pytest.mark.parametrize('kind,label', (('municipio', 'Gobierno'), ('colegio', 'Educación'), ('pyme', 'Empresa')))
def test_directory_detail_config_and_native_profile_share_labels_without_identity_changes(client, kind, label):
    owner_field = 'municipio_id' if kind == 'municipio' else 'pyme_id'
    owner = User(name='Fixture owner', email=f'label-{kind}@example.invalid',
                 rol=role_for_tenant_type(kind), tipo_chat=kind, nombre_empresa='Institutional fixture')
    owner.set_password('synthetic-label-password')
    actor = User(name='Fixture platform', email='fixture-platform@example.invalid', rol='super_admin')
    actor.set_password('synthetic-label-password')
    db.session.add_all([owner, actor]); db.session.flush()
    tenant = TenantProfile(slug=f'label-{kind}', nombre='Institutional fixture', tipo=kind,
                           **{owner_field: owner.id}, configuracion={})
    db.session.add(tenant); db.session.flush()
    owner.tenant_id = tenant.id; owner.tenant_slug = tenant.slug
    setattr(owner, owner_field, owner.id)
    db.session.commit()
    initial_identity = (tenant.tipo, tenant.nombre, tenant.municipio_id, tenant.pyme_id,
                        owner.rol, owner.tipo_chat, owner.nombre_empresa, owner.tenant_id, owner.tenant_slug)
    permissions = {role: set(values) for role, values in ROLE_PERMISSIONS.items()}
    # These are synthetic local transport fixtures, not a live Clerk acceptance.
    platform_headers = clerk_superadmin_headers(actor)
    directory = client.get('/api/admin/tenants', headers=platform_headers)
    assert directory.status_code == 200
    listed = next(row for row in directory.get_json()['tenants'] if row['id'] == tenant.id)
    detail = client.get(f'/api/admin/tenants/{tenant.slug}', headers=platform_headers)
    assert detail.status_code == 200
    native = client.application.test_client()
    login = native.post('/auth/login', json={'email': owner.email, 'password': 'synthetic-label-password'})
    assert login.status_code == 200, login.get_json()
    headers = {'X-Tenant': tenant.slug}
    config = native.get(f'/api/admin/tenants/{tenant.slug}/config', headers=headers)
    assert config.status_code == 200, config.get_json()
    profile = native.get('/api/me', headers=headers)
    assert profile.status_code == 200, profile.get_json()
    for descriptor in (listed, detail.get_json(), config.get_json()['tenant'],
                       config.get_json()['organization_profile']['ui'],
                       profile.get_json()['organization_profile']['ui']):
        assert descriptor['organization_type_label_contract'] == CONTRACT
        assert descriptor['organization_type_label'] == label
    assert listed['tipo'] == detail.get_json()['tipo'] == config.get_json()['tenant']['tipo'] == kind
    db.session.expire_all()
    assert (tenant.tipo, tenant.nombre, tenant.municipio_id, tenant.pyme_id,
            owner.rol, owner.tipo_chat, owner.nombre_empresa, owner.tenant_id, owner.tenant_slug) == initial_identity
    assert ROLE_PERMISSIONS == permissions
    denied = native.get('/api/admin/tenants', headers=headers)
    assert denied.status_code == 403
    missing = client.get('/api/admin/tenants/label-absent', headers=platform_headers)
    assert missing.status_code == 404
    anonymous = client.application.test_client(use_cookies=False)
    assert anonymous.get('/api/admin/tenants').status_code in (401, 403)


def test_legacy_config_name_rejects_all_partial_writes_and_profile_cas_still_syncs(client):
    owner = User(name='Fixture owner', email='label-config-owner@example.invalid',
                 rol='admin_municipio', tipo_chat='municipio', nombre_empresa='Existing institution')
    owner.set_password('synthetic-label-password')
    db.session.add(owner); db.session.flush()
    tenant = TenantProfile(slug='label-config', nombre='Existing institution', tipo='municipio',
                           municipio_id=owner.id, configuracion={})
    db.session.add(tenant); db.session.flush()
    owner.tenant_id = tenant.id; owner.tenant_slug = tenant.slug; owner.municipio_id = owner.id
    existing = TenantConfig(tenant_id=tenant.id, key='contacts', channel='default',
                            json_value={'keep': 'Existing contact'})
    db.session.add(existing); db.session.commit()
    native = client.application.test_client()
    assert native.post('/auth/login', json={'email': owner.email, 'password': 'synthetic-label-password'}).status_code == 200
    headers = {'X-Tenant': tenant.slug}
    path = f'/api/admin/tenants/{tenant.slug}/config'
    initial = native.get(path, headers=headers).get_json()['organization_profile']
    before_fields = (tenant.nombre, tenant.logo_url, tenant.dispatch_email, tenant.dispatch_phone,
                     tenant.send_dispatch_email, owner.nombre_empresa)
    before_audit = AuditEvent.query.count()
    for name in ('Changed name', 'Existing institution ', '', None, True, {'name': 'Changed name'}):
        response = native.put(path, headers=headers, json={
            'tenant': {'nombre': name, 'logo_url': 'https://assets.example.invalid/new.png',
                       'dispatch_email': 'new-dispatch@example.invalid', 'dispatch_phone': '+5491111111111',
                       'send_dispatch_email': True},
            'configs': {'contacts': {'default': {'replace': True}}},
        })
        assert response.status_code == 409, response.get_json()
        assert response.get_json()['reason_code'] == 'organization_name_requires_profile_update'
        assert response.get_json()['save_endpoint'] == path
        db.session.commit(); db.session.expire_all()
        assert (tenant.nombre, tenant.logo_url, tenant.dispatch_email, tenant.dispatch_phone,
                tenant.send_dispatch_email, owner.nombre_empresa) == before_fields
        assert existing.json_value == {'keep': 'Existing contact'}
        assert TenantConfig.query.filter_by(tenant_id=tenant.id).count() == 1
        assert AuditEvent.query.count() == before_audit
    for value in (None, [], 'invalid'):
        invalid = native.put(path, headers=headers, json={'tenant': value, 'configs': {'contacts': {'default': {'replace': True}}}})
        assert invalid.status_code == 400
        db.session.commit(); db.session.expire_all()
        assert existing.json_value == {'keep': 'Existing contact'}
        assert AuditEvent.query.count() == before_audit
    # Name-only idempotence permits the legacy shape, without assigning either name.
    from sqlalchemy import event
    assigned = []
    def capture(target, value, oldvalue, initiator):
        assigned.append(value)
    event.listen(TenantProfile.nombre, 'set', capture)
    try:
        unchanged = native.put(path, headers=headers, json={'tenant': {'nombre': tenant.nombre}})
    finally:
        event.remove(TenantProfile.nombre, 'set', capture)
    assert unchanged.status_code == 200, unchanged.get_json()
    assert assigned == []
    # The verified profile endpoint remains the single synchronized name write.
    saved = native.put(path, headers=headers, json={'organization_profile': {'nombre_empresa': 'Revised institution'},
                                                  'expected_revision': initial['revision']})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['profile']['values']['nombre_empresa'] == 'Revised institution'
    db.session.expire_all()
    assert tenant.nombre == owner.nombre_empresa == 'Revised institution'
    assert AuditEvent.query.filter_by(tenant_id=tenant.id, event_type='organization.profile.updated').count() == 1
    assert saved.get_json()['profile']['ui']['organization_type_label'] == 'Gobierno'
