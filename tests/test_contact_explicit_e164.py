"""One new proposed ORM control; no provider/HTTP delivery assertion.

Real resolver, fresh reader, route cache reader and voice identity reader. Only
synthetic SQLite records; no imports/execution of this proposed file yet.
"""
import json
from models import User, db
from services.contact_service import current_contact_phone, read_contact_projection, resolve_contact
from tests.contact_scope_fixtures import contact_env


class _NoTransport:
    def send(self, *args, **kwargs):
        raise AssertionError("unexpected_provider_transport")


def test_explicit_international_e164_retains_country_and_own_tenant_attributes(contact_env):
    from routes.whatsapp_webhook import _context_contact_name
    from services.voice_stream_service import VoiceStreamService

    e = contact_env
    # Existing +54 and local legacy behavior is an explicit positive control,
    # not a newly inferred country for an already international number.
    local = "11 0000-0201"
    for caller in (e.phone, local):
        assert current_contact_phone(caller) == e.phone
        before = resolve_contact(caller, None, tenant=e.b.tenant)
        assert before["telefono"] == e.phone
        assert before["email"] == e.b.user.email and before["user_id"] == e.b.user.id
    ar_contact = e.add_contact("b", number=e.phone, name="Synthetic canonical Argentina")
    for caller in (e.phone, local):
        before = resolve_contact(caller, None, tenant=e.b.tenant)
        assert before["telefono"] == e.phone and before["email"] == ar_contact.email
        assert read_contact_projection(before, tenant_id=e.b.tenant.id, phone=caller) == before

    failures = []
    observed = []
    for country, phone in (("US", "+12025550101"), ("CL", "+56912345678"), ("UY", "+59891234567")):
        # Stored columns already carry the explicit full country code. Foreign
        # tenant data with the same phone must never be used to repair a lookup.
        user = User(name="Synthetic international " + country,
            email="explicit-user-" + country.lower() + "@example.invalid",
            telefono=phone, tenant_id=e.b.tenant.id, tenant_slug=e.b.tenant.slug,
            empresa_id=e.b.owner.id, municipio_id=e.b.owner.id, tipo_chat="municipio",
            rol="usuario", password_hash="synthetic-not-a-login", rubro_id=e.rubro.id)
        db.session.add(user)
        db.session.commit()
        foreign = e.add_contact("a", number=phone, name="Synthetic foreign " + country,
            email="explicit-foreign-" + country.lower() + "@example.invalid")
        assert db.session.query(User.telefono).filter(User.id == user.id).scalar() == phone

        def check(model, expected_name, expected_email, expected_user_id):
            result = resolve_contact(phone, None, tenant=e.b.tenant)
            matches = {
                "current_phone": current_contact_phone(phone) == phone,
                "resolved_phone": result["telefono"] == phone,
                "own_name": result["nombre"] == expected_name,
                "own_email": result["email"] == expected_email and result["email"] != foreign.email,
                "own_user_id": result["user_id"] == expected_user_id,
                "known_contact": result["is_known_contact"] is True,
                "fresh_projection": read_contact_projection(result, tenant_id=e.b.tenant.id, phone=phone) == result,
            }
            context = {"resolved_contact": result, "contact_cache": result}
            matches["actual_welcome_reader"] = _context_contact_name(
                context, tenant_id=e.b.tenant.id, phone=phone) == expected_name
            voice = VoiceStreamService(_NoTransport(), app=e.app)
            voice.tenant_profile, voice.user, voice.from_number = e.b.tenant, user, phone
            matches["actual_voice_reader"] = voice._resolve_identity_from_context(context)["nombre"] == expected_name
            observed.append({"country": country, "source": model, "checks": matches})
            failures.extend((country, model, key) for key, passed in matches.items() if not passed)

        check("User", user.name, user.email, user.id)
        item = e.add_contact("b", number=phone, name="Synthetic canonical " + country,
            email="explicit-canonical-" + country.lower() + "@example.invalid")
        assert item.phone == item.whatsapp_id == phone
        check("Contact", item.name, item.email, None)
        assert db.session.query(User.telefono).filter(User.id == user.id).scalar() == phone
        assert item.phone == phone  # Readers must not rewrite stored identity.

    # The native failure identifies the country/attributes mismatch after all
    # three countries were observed, rather than stopping on a setup TypeError.
    print(json.dumps({"synthetic_explicit_e164_observations": observed}))
    assert failures == [], "explicit_e164_country_changed_or_own_contact_lost"

    # Same source delta must not turn malformed input into another valid phone.
    # These run only after the primary country oracle passes in a later GREEN.
    for malformed in ("+1+2025550101", "++12025550101", "+012025550101", "+12025550101x", "+1", 12025550101):
        assert current_contact_phone(malformed) is None
        result = resolve_contact(malformed, None, tenant=e.b.tenant)
        assert result["telefono"] is None and result["email"] is None and result["user_id"] is None


"""New finite GREEN controls; appended to the immutable canonical RED body."""
import pytest
from models_memory import Contact


E164_VARIANTS = (
    ("US", "+12025550101", "+1 (202) 555-0101"),
    ("CL", "+56912345678", "+56 9 1234-5678"),
    ("UY", "+59891234567", "+598 91.234.567"),
    ("AR", "+5491100000201", "+54 9 11 0000-0201"),
)


@pytest.mark.parametrize("country,canonical,formatted", E164_VARIANTS)
@pytest.mark.parametrize("source", ("User", "Contact"))
@pytest.mark.parametrize("form", ("canonical", "formatted", "whatsapp-alias"))
def test_explicit_country_formats_reach_own_contact_and_actual_readers(contact_env, country, canonical, formatted, source, form):
    from routes.whatsapp_webhook import _context_contact_name
    from services.voice_stream_service import VoiceStreamService

    e = contact_env
    if country == "AR":
        own, foreign_user = e.b.user, e.a.user
    else:
        users = []
        for label in ("a", "b"):
            scope = getattr(e, label)
            user = User(name="Synthetic " + country + " " + label,
                email="e164-" + country.lower() + "-" + label + "@example.invalid",
                telefono=canonical, tenant_id=scope.tenant.id, tenant_slug=scope.tenant.slug,
                empresa_id=scope.owner.id, municipio_id=scope.owner.id, tipo_chat="municipio",
                rol="usuario", password_hash="synthetic-not-a-login", rubro_id=e.rubro.id)
            db.session.add(user)
            users.append(user)
        db.session.commit()
        foreign_user, own = users
    foreign = e.add_contact("a", number=canonical, name="Synthetic foreign " + country,
        email="e164-foreign-" + country.lower() + "@example.invalid")
    record = own if source == "User" else e.add_contact("b", number=canonical,
        name="Synthetic canonical " + country, email="e164-own-" + country.lower() + "@example.invalid")
    caller = {"canonical": canonical, "formatted": formatted, "whatsapp-alias": "whatsapp:" + formatted}[form]
    before_counts = (User.query.count(), Contact.query.count())
    expected_name, expected_email = record.name, record.email
    assert current_contact_phone(caller) == canonical
    result = resolve_contact(caller, None, tenant=e.b.tenant)
    assert result["telefono"] == canonical
    assert result["nombre"] == expected_name and result["email"] == expected_email
    assert result["email"] not in {foreign.email, foreign_user.email}
    assert result["user_id"] == (own.id if source == "User" else None)
    assert result["_scope"]["source"] == source and result["_scope"]["tenant_id"] == e.b.tenant.id
    assert read_contact_projection(result, tenant_id=e.b.tenant.id, phone=caller) == result
    assert _context_contact_name({"contact_cache": result}, tenant_id=e.b.tenant.id, phone=caller) == expected_name
    voice = VoiceStreamService(_NoTransport(), app=e.app)
    voice.tenant_profile, voice.user, voice.from_number = e.b.tenant, own, caller
    assert voice._resolve_identity_from_context({"resolved_contact": result})["nombre"] == expected_name
    assert own.telefono == foreign_user.telefono == canonical
    if source == "Contact":
        assert record.phone == record.whatsapp_id == canonical
    assert (User.query.count(), Contact.query.count()) == before_counts
    assert not db.session.new and not db.session.dirty and not db.session.deleted


@pytest.mark.parametrize("malformed", (
    "1202+5550101", "+1 (202)foo555-0101", "+1 (202)/555-0101",
    "+１２３４５６７８９", "+1234567890123456", "whatsapp:++12025550101", "", True,
))
def test_malformed_explicit_phone_is_not_repaired_into_an_identity(contact_env, malformed):
    e = contact_env
    before_counts = (User.query.count(), Contact.query.count())
    assert current_contact_phone(malformed) is None
    result = resolve_contact(malformed, "Synthetic channel display", tenant=e.b.tenant)
    assert result["telefono"] is None and result["email"] is None and result["user_id"] is None
    assert result["is_known_contact"] is False and result["_scope"] is None
    assert (User.query.count(), Contact.query.count()) == before_counts


@pytest.mark.parametrize("country,canonical,formatted", E164_VARIANTS)
def test_same_number_aliases_agree_but_different_current_phones_stay_ambiguous(contact_env, country, canonical, formatted):
    e = contact_env
    assert current_contact_phone(canonical, formatted, "whatsapp:" + formatted) == canonical
    assert current_contact_phone(formatted, e.other_phone) is None


"""Two new privacy controls; no migration or repair of historical records."""
from copy import deepcopy


@pytest.mark.parametrize("source", ("User", "Contact"))
def test_explicit_country_does_not_reuse_legacy_mangled_record_or_projection(contact_env, source):
    from routes.whatsapp_webhook import _context_contact_name

    e = contact_env
    canonical = "+12025550101"
    mangled = "+5412025550101"
    if source == "User":
        records = []
        for label, phone in (("a", canonical), ("b", mangled)):
            scope = getattr(e, label)
            item = User(name="Synthetic collision " + label,
                email="e164-collision-" + label + "@example.invalid", telefono=phone,
                tenant_id=scope.tenant.id, tenant_slug=scope.tenant.slug,
                empresa_id=scope.owner.id, municipio_id=scope.owner.id, tipo_chat="municipio",
                rol="usuario", password_hash="synthetic-not-a-login", rubro_id=e.rubro.id)
            db.session.add(item)
            records.append(item)
        db.session.commit()
        foreign, legacy = records
    else:
        foreign = e.add_contact("a", number=canonical, name="Synthetic foreign canonical",
            email="e164-collision-foreign@example.invalid")
        legacy = e.add_contact("b", number=mangled, name="Synthetic local legacy",
            email="e164-collision-legacy@example.invalid")

    # Capture the old convenience projection through the REAL current resolver
    # for the number that is actually persisted; no fabricated marker or ID.
    old = resolve_contact(mangled, None, tenant=e.b.tenant)
    assert old["is_known_contact"] is True and old["_scope"]["source"] == source
    assert old["telefono"] == mangled and old["nombre"] == legacy.name
    assert old["email"] == legacy.email
    context = {"resolved_contact": deepcopy(old), "contact_cache": deepcopy(old),
        "synthetic_explicit_draft": {"nombre": "Explicit draft stays unchanged"}}
    original_context = deepcopy(context)

    def columns():
        return (
            db.session.query(User.id, User.tenant_id, User.telefono, User.name, User.email).order_by(User.id).all(),
            db.session.query(Contact.id, Contact.tenant_id, Contact.phone, Contact.whatsapp_id, Contact.name, Contact.email)
                .order_by(Contact.id).all(),
        )
    original_columns = columns()
    original_counts = (User.query.count(), Contact.query.count())
    assert current_contact_phone(canonical) == canonical
    current = resolve_contact(canonical, None, tenant=e.b.tenant)
    assert current["telefono"] == canonical
    assert current["is_known_contact"] is False and current["_scope"]["source"] == "channel_profile"
    assert current["nombre"] is None and current["email"] is None and current["user_id"] is None
    assert current["email"] not in {foreign.email, legacy.email}
    assert read_contact_projection(old, tenant_id=e.b.tenant.id, phone=canonical) == {}
    assert _context_contact_name(context, tenant_id=e.b.tenant.id, phone=canonical) is None
    assert context == original_context
    assert columns() == original_columns
    assert (User.query.count(), Contact.query.count()) == original_counts
    assert not db.session.new and not db.session.dirty and not db.session.deleted
