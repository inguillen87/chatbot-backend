"""New correction controls; not executed. No provider/HMAC/authentication assertion."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from sqlalchemy import event
from models import Role, TenantProfile, User, UserRole, db
from models_memory import Contact
from services.contact_service import (
    PROJECTION_SCHEMA, _parse_contact_projection, current_contact_phone,
    read_contact_projection, resolve_contact, store_contact_projection,
)


def test_unscoped_lookup_keeps_only_channel_fields(contact_env):
    e=contact_env
    result=resolve_contact(e.phone,"Synthetic channel profile")
    assert result["email"] is None and result["user_id"] is None
    assert result["nombre"] == "Synthetic channel profile"
    assert result["is_known_contact"] is False


@pytest.mark.parametrize("label",["a","b"])
def test_same_phone_uses_the_tenants_own_canonical_record(contact_env,label):
    e=contact_env
    own=e.add_contact(label)
    foreign=e.add_contact("a" if label=="b" else "b")
    scope=getattr(e,label)
    result=resolve_contact(e.phone,None,tenant=scope.tenant)
    assert result["email"] == own.email and result["nombre"] == own.name
    assert result["email"] != foreign.email and result["user_id"] is None
    assert read_contact_projection(result,tenant_id=scope.tenant.id,phone=e.phone) == result


@pytest.mark.parametrize("model",["Contact","User"])
def test_duplicate_local_records_never_choose_first_or_fall_back(contact_env,model):
    e=contact_env
    if model=="Contact":
        e.add_contact("b",name="First local")
        e.add_contact("b",name="Second local",email="scope-second-contact@example.invalid")
    else:
        duplicate=User(name="Duplicate local",email="scope-user-duplicate@example.invalid",
            telefono=e.phone,tenant_id=e.b.tenant.id,tenant_slug=e.b.tenant.slug,
            empresa_id=e.b.owner.id,municipio_id=e.b.owner.id,tipo_chat="municipio",
            password_hash="synthetic-not-a-login")
        db.session.add(duplicate);db.session.commit()
    before=(User.query.count(),Contact.query.count())
    result=resolve_contact(e.phone,"Channel",tenant=e.b.tenant)
    assert result["email"] is None and result["user_id"] is None
    assert result["is_known_contact"] is False
    assert (User.query.count(),Contact.query.count()) == before


@pytest.mark.parametrize("case",["unique-owner","foreign-explicit","shared-owner","role-only"])
def test_legacy_membership_is_consistent_and_roles_do_not_copy_profiles(contact_env,case):
    e=contact_env
    e.b.user.tenant_id=None;e.b.user.tenant_slug=None
    if case=="foreign-explicit":
        e.b.user.tenant_id=e.a.tenant.id
    elif case=="shared-owner":
        db.session.add(TenantProfile(slug="scope-shared-owner",nombre="Shared owner",
            tipo="municipio",municipio_id=e.b.owner.id,is_active=True))
    elif case=="role-only":
        e.b.user.telefono=e.other_phone
        role=Role(name="synthetic-contact-role")
        db.session.add(role);db.session.flush()
        db.session.add(UserRole(user_id=e.a.user.id,role_id=role.id,tenant_id=e.b.tenant.id))
    db.session.commit()
    result=resolve_contact(e.phone,None,tenant=e.b.tenant)
    if case=="unique-owner":
        assert result["user_id"] == e.b.user.id and result["email"] == e.b.user.email
    else:
        assert result["user_id"] is None and result["email"] is None
        assert result["is_known_contact"] is False


@pytest.mark.parametrize("case",["inactive","unknown"])
def test_inactive_or_missing_persisted_tenant_stays_unknown(contact_env,case):
    e=contact_env
    if case=="inactive":
        e.b.tenant.is_active=False;db.session.commit();target=e.b.tenant
    else:
        target=SimpleNamespace(id=max(e.a.tenant.id,e.b.tenant.id)+10000)
    result=resolve_contact(e.phone,"Channel",tenant=target)
    assert result["email"] is None and result["user_id"] is None
    assert result["_scope"] is None


def test_projection_user_id_bool_is_rejected_before_any_fresh_query(contact_env):
    e=contact_env
    value={"nombre":"Synthetic","email":"synthetic@example.invalid","dni":None,
        "telefono":e.phone,"user_id":True,"is_known_contact":True,
        "_scope":{"schema":PROJECTION_SCHEMA,"tenant_id":e.b.tenant.id,
            "phone":e.phone,"source":"User","source_id":1}}
    assert _parse_contact_projection(value,tenant_id=e.b.tenant.id,phone=e.phone) == {}


@pytest.mark.parametrize("case",["missing-marker","wrong-tenant","wrong-phone","missing-phone","ambiguous-phone","extra-dni"])
def test_invalid_scope_or_current_phone_never_reuses_projection(contact_env,case):
    e=contact_env
    value=resolve_contact(e.phone,None,tenant=e.b.tenant)
    target=e.b.tenant.id;phone=e.phone
    if case=="missing-marker":value.pop("_scope")
    elif case=="wrong-tenant":target=e.a.tenant.id
    elif case=="wrong-phone":phone=e.other_phone
    elif case=="missing-phone":phone=None
    elif case=="ambiguous-phone":phone=current_contact_phone(e.phone,e.other_phone)
    elif case=="extra-dni":value["dni"]="SYNTHETIC-NOT-DNI"
    assert read_contact_projection(value,tenant_id=target,phone=phone) == {}


def test_forged_marker_or_changed_fields_are_not_rehydrated(contact_env):
    e=contact_env
    original=resolve_contact(e.phone,None,tenant=e.b.tenant)
    forged=deepcopy(original)
    forged["email"]=e.a.user.email
    assert read_contact_projection(forged,tenant_id=e.b.tenant.id,phone=e.phone) == {}
    forged=deepcopy(original)
    forged["_scope"]["source_id"]=e.a.user.id;forged["user_id"]=e.a.user.id
    assert read_contact_projection(forged,tenant_id=e.b.tenant.id,phone=e.phone) == {}
    assert e.b.user.tenant_id==e.b.tenant.id


def test_revocation_and_aba_require_current_db_data_not_old_marker(contact_env):
    e=contact_env
    old=resolve_contact(e.phone,None,tenant=e.b.tenant)
    assert read_contact_projection(old,tenant_id=e.b.tenant.id,phone=e.phone) == old
    e.b.user.tenant_id=e.a.tenant.id;e.b.user.tenant_slug=e.a.tenant.slug
    e.b.user.empresa_id=e.a.owner.id;e.b.user.municipio_id=e.a.owner.id
    db.session.commit()
    assert read_contact_projection(old,tenant_id=e.b.tenant.id,phone=e.phone) == {}
    e.b.user.tenant_id=e.b.tenant.id;e.b.user.tenant_slug=e.b.tenant.slug
    e.b.user.empresa_id=e.b.owner.id;e.b.user.municipio_id=e.b.owner.id
    e.b.user.email="scope-returned-b@example.invalid"
    db.session.commit()
    # A→B→A values cannot make the old email current again.
    assert read_contact_projection(old,tenant_id=e.b.tenant.id,phone=e.phone) == {}
    fresh=resolve_contact(e.phone,None,tenant=e.b.tenant)
    assert fresh["email"]=="scope-returned-b@example.invalid"
    assert read_contact_projection(fresh,tenant_id=e.b.tenant.id,phone=e.phone)==fresh
    e.b.tenant.is_active=False;db.session.commit()
    assert read_contact_projection(fresh,tenant_id=e.b.tenant.id,phone=e.phone)=={}


@pytest.mark.parametrize("kind",["unknown","partial"])
def test_store_retires_old_auto_values_but_preserves_declared_draft(contact_env,kind):
    e=contact_env
    old={"nombre":"Old foreign","email":e.a.user.email,"user_id":e.a.user.id}
    context=e.cache_context(old,explicit=True)
    preserved=deepcopy({k:v for k,v in context.items() if k not in {"resolved_contact","contact_cache"}})
    if kind=="partial":
        item=e.add_contact("b");item.email=None;db.session.commit()
        value=resolve_contact(e.phone,None,tenant=e.b.tenant)
    else:value=resolve_contact(e.phone,None)
    result=store_contact_projection(context,value,tenant_id=e.b.tenant.id,phone=e.phone)
    assert {k:v for k,v in context.items() if k not in {"resolved_contact","contact_cache"}}==preserved
    if kind=="unknown":
        assert result=={} and "resolved_contact" not in context and "contact_cache" not in context
    else:
        assert context["resolved_contact"]["email"] is None
        assert context["contact_cache"]["user_id"] is None
        assert context["contact_cache"]["nombre"]=="Synthetic canonical b"


def test_lookup_and_fresh_reader_preserve_pending_caller_changes(contact_env):
    e=contact_env
    projection=resolve_contact(e.phone,None,tenant=e.b.tenant)
    prior_name=e.b.user.name
    e.b.user.name="Pending user WIP";e.b.tenant.nombre="Pending tenant WIP"
    dirty=set(db.session.dirty)
    result=resolve_contact(e.phone,None,tenant=e.b.tenant)
    assert result["nombre"]==prior_name
    assert read_contact_projection(projection,tenant_id=e.b.tenant.id,phone=e.phone)==projection
    assert e.b.user.name=="Pending user WIP" and e.b.tenant.nombre=="Pending tenant WIP"
    assert set(db.session.dirty)==dirty
    with db.session.no_autoflush:
        assert db.session.query(User.name).filter(User.id==e.b.user.id).scalar()==prior_name


@pytest.mark.parametrize("kind",["Contact","User"])
def test_each_fresh_lookup_and_reader_has_a_bounded_query_count(contact_env,kind):
    e=contact_env
    if kind=="Contact":e.add_contact("b")
    tenant_id=e.b.tenant.id
    projection=resolve_contact(e.phone,None,tenant=e.b.tenant)
    statements=[]
    def observe(*args):statements.append(args[2])
    event.listen(db.engine,"before_cursor_execute",observe)
    try:
        result=read_contact_projection(projection,tenant_id=tenant_id,phone=e.phone)
        assert result==projection
        assert 1<=len(statements)<=5
        statements.clear()
        assert read_contact_projection(projection,tenant_id=tenant_id,phone=e.other_phone)=={}
        assert statements==[]
    finally:event.remove(db.engine,"before_cursor_execute",observe)
