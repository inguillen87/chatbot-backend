"""Proposed synthetic ORM fixtures only; loaded after prepare_process in future cohort."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import pytest
from models import ChatSessionContext, Role, Rubro, TenantProfile, User, UserRole, db
from models_memory import Contact


@pytest.fixture
def contact_env(client):
    assert client.application.testing
    assert str(db.engine.url) == "sqlite:///:memory:"
    phone = "+5491100000201"
    other_phone = "+5491100000202"
    rubro = Rubro(nombre="Synthetic scope trade", clave="synthetic-scope-trade")
    db.session.add(rubro)
    db.session.flush()
    rows = {}
    for label in ("a", "b"):
        owner = User(name="Synthetic scope owner " + label,
            email="scope-owner-" + label + "@example.invalid", rol="admin", tipo_chat="municipio",
            password_hash="synthetic-not-a-login", rubro_id=rubro.id)
        db.session.add(owner)
        db.session.flush()
        owner.municipio_id = owner.id
        tenant = TenantProfile(slug="scope-contact-" + label, nombre="Synthetic scope " + label,
            tipo="municipio", municipio_id=owner.id, is_active=True)
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        owner.tenant_slug = tenant.slug
        user = User(name="Synthetic local person " + label,
            email="scope-person-" + label + "@example.invalid", telefono=phone,
            tenant_id=tenant.id, tenant_slug=tenant.slug, empresa_id=owner.id,
            municipio_id=owner.id, tipo_chat="municipio", rol="usuario",
            password_hash="synthetic-not-a-login", rubro_id=rubro.id)
        db.session.add(user)
        db.session.flush()
        rows[label] = SimpleNamespace(owner=owner, tenant=tenant, user=user)
    session = ChatSessionContext(chat_session_id="synthetic-scope-contact-b",
        user_id=rows["b"].owner.id, tenant_id=rows["b"].tenant.id, anon_id=phone, context_data={})
    db.session.add(session)
    db.session.commit()
    def add_contact(label="b", *, number=None, name=None, email=None):
        contact = Contact(id=str(uuid4()), tenant_id=rows[label].tenant.id,
            phone=number or phone, whatsapp_id=number or phone,
            name=name or "Synthetic canonical " + label,
            email=email if email is not None else "scope-canonical-" + label + "@example.invalid")
        db.session.add(contact)
        db.session.commit()
        return contact
    def cache_context(projection, *, explicit=False):
        value = {"resolved_contact": deepcopy(projection), "contact_cache": deepcopy(projection)}
        if explicit:
            value.update(profile_name="Explicit synthetic B", action_data={"nombre":"Explicit action B"},
                contexto_municipio_v2={"contacto_usuario":{"nombre":"Explicit draft B", "email":"explicit-b@example.invalid", "dni":"SYNTHETIC-NOT-DNI"}})
        return value
    return SimpleNamespace(client=client, app=client.application, a=rows["a"], b=rows["b"],
        phone=phone, other_phone=other_phone, session=session, rubro=rubro,
        add_contact=add_contact, cache_context=cache_context)
