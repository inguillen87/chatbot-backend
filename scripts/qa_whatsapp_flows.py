import json
import os
import sys
import uuid
import base64
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("ENABLE_RUNTIME_SCHEMA_SYNC", "0")
os.environ.setdefault("ENABLE_RUNTIME_TENANT_INIT", "0")
os.environ.setdefault("STARTUP_RUNTIME_BOOTSTRAP", "0")
os.environ.setdefault("WHATSAPP_AUDIO_ENABLED", "0")
os.environ.setdefault("WELCOME_MESSAGE_DELAY_SECONDS", "0")

from twilio.request_validator import RequestValidator
from werkzeug.security import generate_password_hash

from app import create_app
from config import Config
from database import db
from models import ArchivoAdjunto, CatalogoItem, ChatSessionContext, MunicipioTicket, PymePedido, PymeTicket, Rubro, TenantProfile, User, WhatsappNumero
from models_education import AcademicLevel, Campus, CourseSection, Guardian, School, SchoolCaseAlias, Shift, Student, StudentGuardianRelation


@dataclass
class WhatsappCase:
    label: str
    to_number: str
    from_number: str
    body: str
    extra: dict


class FakeTwilioMessages:
    def __init__(self):
        self.sent = []

    def create(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(sid=f"SM_FAKE_{len(self.sent)}")


class FakeTwilioClient:
    def __init__(self):
        self.messages = FakeTwilioMessages()


class FakeHttpResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


class LocalWhatsappQAConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SECRET_KEY = "local-whatsapp-qa-secret-only-for-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    WTF_CSRF_ENABLED = False


def _safe_print(label: str, payload=None) -> None:
    if payload is None:
        text = label
    elif isinstance(payload, str):
        text = f"{label} {payload}"
    else:
        text = f"{label} {json.dumps(payload, ensure_ascii=False, default=str)}"
    print(text.encode("ascii", "backslashreplace").decode("ascii"))


def _normalize(number: str) -> str:
    return "+" + "".join(ch for ch in number if ch.isdigit())


def _signed_headers(url: str, data: dict) -> dict:
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        raise RuntimeError("TWILIO_AUTH_TOKEN no esta configurado")
    signature = RequestValidator(token).compute_signature(url, data)
    return {"X-Twilio-Signature": signature}


def _twilio_form(case: WhatsappCase) -> dict:
    profile_name = case.extra.get("_ProfileName") or f"QA {case.label}"
    form = {
        "SmsMessageSid": f"SM{uuid.uuid4().hex[:30]}",
        "MessageSid": f"SM{uuid.uuid4().hex[:30]}",
        "AccountSid": os.environ.get("TWILIO_ACCOUNT_SID", "AC_LOCAL_TEST"),
        "From": f"whatsapp:{_normalize(case.from_number)}",
        "To": f"whatsapp:{_normalize(case.to_number)}",
        "Body": case.body,
        "ProfileName": profile_name,
        "NumMedia": "0",
    }
    form.update({k: v for k, v in case.extra.items() if not k.startswith("_")})
    return form


def _row_counts(session_id_prefix: str):
    return {
        "contexts": ChatSessionContext.query.filter(ChatSessionContext.chat_session_id.like(f"{session_id_prefix}%")).count(),
        "municipio_tickets": MunicipioTicket.query.count(),
        "pyme_tickets": PymeTicket.query.count(),
        "pyme_pedidos": PymePedido.query.count(),
        "adjuntos": ArchivoAdjunto.query.count(),
        "school_case_aliases": SchoolCaseAlias.query.count(),
    }


def _truthy_env(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in {"1", "true", "yes", "si", "on"}


def _get_or_create_model(model, defaults: dict | None = None, **filters):
    obj = model.query.filter_by(**filters).first()
    if obj:
        return obj, False
    values = dict(filters)
    values.update(defaults or {})
    obj = model(**values)
    db.session.add(obj)
    db.session.flush()
    return obj, True


def _ensure_education_sandbox_setup(*, sandbox_to: str, guardian_phone: str):
    """Create a minimal colegio tenant and map Twilio Sandbox to it for QA.

    The sandbox number is unique in `whatsapp_numero`. If it is already mapped
    to another tenant, we do not reassign it unless QA_FORCE_EDUCATION_SANDBOX=1.
    """

    sandbox_to = _normalize(sandbox_to)
    guardian_phone = _normalize(guardian_phone)
    slug = os.environ.get("QA_EDUCATION_TENANT_SLUG", "qa-colegio-sandbox")
    owner_email = os.environ.get("QA_EDUCATION_OWNER_EMAIL", "qa-colegio-sandbox@chatboc.local")

    owner = User.query.filter_by(email=owner_email).first()
    if not owner:
        owner = User(
            name="Colegio Sandbox QA",
            email=owner_email,
            password_hash=generate_password_hash("qa-sandbox", method="pbkdf2:sha256"),
            rol="admin",
            tipo_chat="pyme",
            tenant_slug=slug,
            nombre_empresa="Colegio Sandbox QA",
            telefono=sandbox_to,
            plan="demo",
            email_verified=True,
            acepto_terminos=True,
        )
        db.session.add(owner)
        db.session.flush()

    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        tenant = TenantProfile(
            slug=slug,
            nombre="Colegio Sandbox QA",
            tipo="pyme",
            pyme_id=owner.id,
            vertical="educacion",
            subvertical="colegio_privado",
            plan="demo",
            whatsapp_sender_id=sandbox_to,
            is_active=True,
            capabilities_json={
                "education": {
                    "enabled": True,
                    "institution_type": "private",
                    "modules": ["attendance", "communications", "secretary_tickets", "billing"],
                }
            },
        )
        db.session.add(tenant)
        db.session.flush()
    else:
        tenant.tipo = "pyme"
        tenant.pyme_id = owner.id
        tenant.vertical = "educacion"
        tenant.subvertical = tenant.subvertical or "colegio_privado"
        tenant.whatsapp_sender_id = sandbox_to
        tenant.is_active = True
        capabilities = tenant.capabilities_json if isinstance(tenant.capabilities_json, dict) else {}
        education = capabilities.get("education") if isinstance(capabilities.get("education"), dict) else {}
        education.setdefault("institution_type", "private")
        education["enabled"] = True
        capabilities["education"] = education
        tenant.capabilities_json = capabilities

    owner.tenant_id = tenant.id
    owner.tenant_slug = slug
    owner.tipo_chat = "pyme"
    owner.nombre_empresa = owner.nombre_empresa or tenant.nombre

    school, _ = _get_or_create_model(
        School,
        tenant_id=tenant.id,
        name="Colegio Sandbox QA",
        defaults={"school_type": "private", "jurisdiction": "QA", "brand_name": "Colegio Sandbox QA"},
    )
    campus, _ = _get_or_create_model(
        Campus,
        school_id=school.id,
        name="Sede Central",
        defaults={"address": "Av. Colegio 123, Mendoza", "phone": sandbox_to, "is_main": True},
    )
    level, _ = _get_or_create_model(
        AcademicLevel,
        school_id=school.id,
        code="PRI",
        defaults={"name": "Primaria"},
    )
    shift, _ = _get_or_create_model(
        Shift,
        school_id=school.id,
        code="TM",
        defaults={"name": "Turno manana"},
    )
    section, _ = _get_or_create_model(
        CourseSection,
        campus_id=campus.id,
        academic_year=2026,
        level_id=level.id,
        grade="4",
        division="A",
        shift_id=shift.id,
    )
    phone_digits = "".join(ch for ch in guardian_phone if ch.isdigit())
    student, _ = _get_or_create_model(
        Student,
        school_id=school.id,
        external_ref=f"qa-whatsapp-{phone_digits}",
        defaults={
            "campus_id": campus.id,
            "first_name": "Sofia",
            "last_name": "Sandbox",
            "document_type": "DNI",
            "document_number": f"QA{phone_digits[-8:]}",
            "section_id": section.id,
            "grade": "4A",
            "identifier": f"QA-{phone_digits[-6:]}",
        },
    )
    guardian, _ = _get_or_create_model(
        Guardian,
        tenant_id=tenant.id,
        phone_number=guardian_phone,
        defaults={
            "school_id": school.id,
            "first_name": "Tutor",
            "last_name": "Sandbox",
            "email": f"tutor.{phone_digits[-8:]}@example.com",
            "document_number": f"T{phone_digits[-7:]}",
            "verification_status": "verified",
            "preferred_channel": "whatsapp",
            "is_billing_contact": True,
        },
    )
    _get_or_create_model(
        StudentGuardianRelation,
        student_id=student.id,
        guardian_id=guardian.id,
        defaults={
            "relationship_type": "tutor",
            "custody_scope": "general",
            "can_pickup": True,
            "can_receive_billing": True,
            "can_receive_sensitive_updates": True,
            "is_primary": True,
        },
    )

    mapping = WhatsappNumero.query.filter_by(numero_whatsapp=sandbox_to).first()
    if mapping and mapping.user_id != owner.id:
        if not _truthy_env("QA_FORCE_EDUCATION_SANDBOX"):
            raise RuntimeError(
                f"{sandbox_to} ya esta mapeado a user_id={mapping.user_id}. "
                "Seteá QA_FORCE_EDUCATION_SANDBOX=1 para reasignarlo al colegio QA."
            )
        mapping.user_id = owner.id
        mapping.is_active = True
    elif mapping:
        mapping.is_active = True
    else:
        mapping = WhatsappNumero(numero_whatsapp=sandbox_to, user_id=owner.id, is_active=True)
        db.session.add(mapping)

    db.session.commit()
    return mapping


def _ensure_core_whatsapp_setup(*, junin_to: str, bodega_to: str):
    """Seed local WhatsApp mappings for isolated QA runs.

    By default this script runs against an in-memory SQLite database so QA does
    not mutate the configured developer, staging or production database.
    """

    junin_to = _normalize(junin_to)
    bodega_to = _normalize(bodega_to)

    municipio_rubro, _ = _get_or_create_model(
        Rubro,
        clave="municipio-inteligente",
        defaults={"nombre": "Municipio Inteligente", "es_publico": True},
    )
    bodega_rubro, _ = _get_or_create_model(
        Rubro,
        clave="bodega",
        defaults={"nombre": "Bodega", "es_publico": False},
    )

    municipio_owner, _ = _get_or_create_model(
        User,
        email="qa-junin@chatboc.local",
        defaults={
            "name": "Municipio Junin QA",
            "password_hash": generate_password_hash("qa-whatsapp", method="pbkdf2:sha256"),
            "rol": "admin",
            "tipo_chat": "municipio",
            "tenant_slug": "junin",
            "nombre_empresa": "Municipio de Junin",
            "telefono": junin_to,
            "plan": "demo",
            "email_verified": True,
            "acepto_terminos": True,
            "rubro_id": municipio_rubro.id,
        },
    )
    municipio_owner.tipo_chat = "municipio"
    municipio_owner.rubro_id = municipio_rubro.id
    municipio_owner.telefono = junin_to

    municipio_tenant, _ = _get_or_create_model(
        TenantProfile,
        slug="junin",
        defaults={
            "nombre": "Municipio de Junin QA",
            "tipo": "municipio",
            "vertical": "gobierno",
            "subvertical": "municipio",
            "municipio_id": municipio_owner.id,
            "is_active": True,
            "whatsapp_sender_id": junin_to,
            "configuracion": {"widget_tokens": ["widget-junin-qa"]},
        },
    )
    municipio_tenant.tipo = "municipio"
    municipio_tenant.vertical = "gobierno"
    municipio_tenant.subvertical = "municipio"
    municipio_tenant.municipio_id = municipio_owner.id
    municipio_tenant.whatsapp_sender_id = junin_to
    municipio_tenant.is_active = True
    municipio_owner.tenant_id = municipio_tenant.id
    municipio_owner.tenant_slug = municipio_tenant.slug

    bodega_owner, _ = _get_or_create_model(
        User,
        email="qa-cuatro-fincas@chatboc.local",
        defaults={
            "name": "Cuatro Fincas QA",
            "password_hash": generate_password_hash("qa-whatsapp", method="pbkdf2:sha256"),
            "rol": "admin",
            "tipo_chat": "pyme",
            "tenant_slug": "cuatro-fincas",
            "nombre_empresa": "Cuatro Fincas Winery",
            "telefono": bodega_to,
            "plan": "demo",
            "email_verified": True,
            "acepto_terminos": True,
            "rubro_id": bodega_rubro.id,
        },
    )
    bodega_owner.tipo_chat = "pyme"
    bodega_owner.rubro_id = bodega_rubro.id
    bodega_owner.telefono = bodega_to

    bodega_tenant, _ = _get_or_create_model(
        TenantProfile,
        slug="cuatro-fincas",
        defaults={
            "nombre": "Cuatro Fincas Winery QA",
            "tipo": "pyme",
            "vertical": "pyme",
            "subvertical": "bodega",
            "pyme_id": bodega_owner.id,
            "is_active": True,
            "whatsapp_sender_id": bodega_to,
            "configuracion": {"widget_tokens": ["widget-cuatro-fincas-qa"]},
        },
    )
    bodega_tenant.tipo = "pyme"
    bodega_tenant.vertical = "pyme"
    bodega_tenant.subvertical = "bodega"
    bodega_tenant.pyme_id = bodega_owner.id
    bodega_tenant.whatsapp_sender_id = bodega_to
    bodega_tenant.is_active = True
    bodega_owner.tenant_id = bodega_tenant.id
    bodega_owner.tenant_slug = bodega_tenant.slug

    for number, owner in [(junin_to, municipio_owner), (bodega_to, bodega_owner)]:
        mapping = WhatsappNumero.query.filter_by(numero_whatsapp=number).first()
        if not mapping:
            mapping = WhatsappNumero(numero_whatsapp=number, user_id=owner.id, is_active=True)
            db.session.add(mapping)
        else:
            mapping.user_id = owner.id
            mapping.is_active = True

    existing_products = CatalogoItem.query.filter_by(tenant_id=bodega_tenant.id).count()
    if existing_products == 0:
        db.session.add_all(
            [
                CatalogoItem(
                    user_id=bodega_owner.id,
                    tenant_id=bodega_tenant.id,
                    nombre="Malbec Reserva",
                    descripcion="Vino tinto Malbec de Cuatro Fincas.",
                    precio="12000",
                    cantidad="24",
                    categoria="vinos",
                    imagen_url="https://example.com/malbec.jpg",
                    disponible=True,
                ),
                CatalogoItem(
                    user_id=bodega_owner.id,
                    tenant_id=bodega_tenant.id,
                    nombre="Cabernet Franc",
                    descripcion="Cabernet para pedidos demo por WhatsApp.",
                    precio="14000",
                    cantidad="18",
                    categoria="vinos",
                    imagen_url="https://example.com/cabernet.jpg",
                    disponible=True,
                ),
            ]
        )

    db.session.commit()


def _fake_transcribe_audio_from_url(url: str, *args, **kwargs) -> str:
    if "colegio" in str(url).lower():
        return (
            "Mi hija Sofia Sandbox de 4A no asiste hoy por fiebre. "
            "Adjunto certificado medico y pido justificar la inasistencia."
        )
    return (
        "Audio transcripto: hay una luminaria apagada en Av San Martin 123, "
        "distrito Centro, Junin. Soy QA Audio, email qa.audio@example.com, telefono "
        f"{os.environ.get('QA_AUDIO_PHONE', '')}."
    )


def _fake_create_attachment_with_thumbnail(file_storage, user_id=None, session_id=None):
    filename = getattr(file_storage, "filename", None) or f"qa_media_{uuid.uuid4().hex[:8]}"
    mime_type = getattr(file_storage, "content_type", None) or "application/octet-stream"
    try:
        pos = file_storage.stream.tell()
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(pos)
    except Exception:
        size = 0
    adjunto = ArchivoAdjunto(
        user_id=user_id,
        session_id=session_id,
        filename=filename,
        nombre_original=filename,
        mime=mime_type,
        tamano=size,
        tipo="chat_adjunto",
        url=f"https://qa.local/media/{filename}",
    )
    db.session.add(adjunto)
    db.session.flush()
    return adjunto


def main():
    isolated_db = not _truthy_env("QA_WHATSAPP_USE_CONFIGURED_DB")
    app = create_app(LocalWhatsappQAConfig if isolated_db else None)
    base_url = os.environ.get("QA_WEBHOOK_BASE_URL", "http://localhost")
    endpoint = f"{base_url.rstrip('/')}/webhook/whatsapp"
    fake_twilio = FakeTwilioClient()
    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    run_seed = f"{uuid.uuid4().int % 10000:04d}"
    junin_from = f"+54926155{run_seed}"
    junin_audio_from = f"+54926156{run_seed}"
    bodega_from = f"+54926157{run_seed}"
    junin_location_from = f"+54926158{run_seed}"
    colegio_from = f"+54926159{run_seed}"
    junin_to = os.environ.get("QA_JUNIN_TO", "+17432643718")
    bodega_to = os.environ.get("QA_BODEGA_TO", "+18564858589")
    sandbox_to = os.environ.get("QA_TWILIO_SANDBOX_TO", "+14155238886")
    os.environ["QA_AUDIO_PHONE"] = junin_audio_from

    cases = [
        WhatsappCase(
            label="junin_texto_reclamo",
            to_number=junin_to,
            from_number=junin_from,
            body=f"Hola, soy QA Texto, email qa.texto@example.com, telefono {junin_from}. Quiero registrar un reclamo por luminaria apagada en Av San Martin 123, distrito Centro, Junin.",
            extra={"_ProfileName": "QA Junin Texto"},
        ),
        WhatsappCase(
            label="junin_imagen",
            to_number=junin_to,
            from_number=junin_from,
            body="Adjunto foto del problema.",
            extra={
                "_ProfileName": "QA Junin Texto",
                "NumMedia": "1",
                "MediaUrl0": "https://media.local/qa-junin-luz.png",
                "MediaContentType0": "image/png",
                "MediaSid0": f"ME{uuid.uuid4().hex[:30]}",
            },
        ),
        WhatsappCase(
            label="junin_dni_reclamo",
            to_number=junin_to,
            from_number=junin_from,
            body="Mi DNI es 30111222.",
            extra={"_ProfileName": "QA Junin Texto"},
        ),
        WhatsappCase(
            label="junin_confirmacion_reclamo",
            to_number=junin_to,
            from_number=junin_from,
            body="1",
            extra={"_ProfileName": "QA Junin Texto"},
        ),
        WhatsappCase(
            label="junin_ubicacion_inicio",
            to_number=junin_to,
            from_number=junin_location_from,
            body="Hola, quiero registrar un reclamo por luminaria apagada.",
            extra={"_ProfileName": "QA Junin Ubicacion"},
        ),
        WhatsappCase(
            label="junin_ubicacion_compartida",
            to_number=junin_to,
            from_number=junin_location_from,
            body="Te comparto la ubicacion del reclamo.",
            extra={
                "_ProfileName": "QA Junin Ubicacion",
                "Latitude": "-34.5889",
                "Longitude": "-60.9462",
                "Address": "Av San Martin 123, Junin, Buenos Aires",
                "Label": "Alumbrado apagado",
            },
        ),
        WhatsappCase(
            label="junin_ubicacion_sin_foto",
            to_number=junin_to,
            from_number=junin_location_from,
            body="No, omitir foto.",
            extra={"_ProfileName": "QA Junin Ubicacion"},
        ),
        WhatsappCase(
            label="junin_ubicacion_datos",
            to_number=junin_to,
            from_number=junin_location_from,
            body=f"Soy QA Ubicacion, DNI 30222333, email qa.ubicacion@example.com, telefono {junin_location_from}.",
            extra={"_ProfileName": "QA Junin Ubicacion"},
        ),
        WhatsappCase(
            label="junin_ubicacion_confirmar",
            to_number=junin_to,
            from_number=junin_location_from,
            body="1",
            extra={"_ProfileName": "QA Junin Ubicacion"},
        ),
        WhatsappCase(
            label="junin_audio",
            to_number=junin_to,
            from_number=junin_audio_from,
            body="",
            extra={
                "_ProfileName": "QA Junin Audio",
                "NumMedia": "1",
                "MediaUrl0": "https://media.local/qa-junin-audio.ogg",
                "MediaContentType0": "audio/ogg",
                "MediaSid0": f"ME{uuid.uuid4().hex[:30]}",
            },
        ),
        WhatsappCase(
            label="junin_audio_sin_foto",
            to_number=junin_to,
            from_number=junin_audio_from,
            body="No, omitir foto.",
            extra={"_ProfileName": "QA Junin Audio"},
        ),
        WhatsappCase(
            label="junin_audio_datos",
            to_number=junin_to,
            from_number=junin_audio_from,
            body=f"Soy QA Audio, DNI 30333444, email qa.audio@example.com, telefono {junin_audio_from}.",
            extra={"_ProfileName": "QA Junin Audio"},
        ),
        WhatsappCase(
            label="junin_audio_confirmar",
            to_number=junin_to,
            from_number=junin_audio_from,
            body="1",
            extra={"_ProfileName": "QA Junin Audio"},
        ),
        WhatsappCase(
            label="cuatro_fincas_pedido",
            to_number=bodega_to,
            from_number=bodega_from,
            body=f"Hola, quiero comprar 2 botellas de Malbec y 1 Cabernet. Soy QA Bodega, telefono {bodega_from}. Enviar a Godoy Cruz 456.",
            extra={"_ProfileName": "QA Bodega"},
        ),
        WhatsappCase(
            label="cuatro_fincas_confirmar",
            to_number=bodega_to,
            from_number=bodega_from,
            body="Confirmar pedido.",
            extra={"_ProfileName": "QA Bodega"},
        ),
    ]

    with app.app_context():
        if isolated_db:
            db.create_all()
            _ensure_core_whatsapp_setup(junin_to=junin_to, bodega_to=bodega_to)

        if _truthy_env("QA_ENABLE_EDUCATION_SANDBOX", "1"):
            mapping = _ensure_education_sandbox_setup(sandbox_to=sandbox_to, guardian_phone=colegio_from)
            _safe_print(
                "education_sandbox",
                {
                    "numero": _normalize(sandbox_to),
                    "user_id": mapping.user_id,
                    "email": getattr(mapping.user, "email", None),
                    "tenant_slug": getattr(getattr(mapping.user, "tenant", None), "slug", None),
                },
            )
            cases.extend(
                [
                    WhatsappCase(
                        label="colegio_menu_sandbox",
                        to_number=sandbox_to,
                        from_number=colegio_from,
                        body="Hola",
                        extra={"_ProfileName": "QA Colegio Tutor"},
                    ),
                    WhatsappCase(
                        label="colegio_seleccion_inasistencia",
                        to_number=sandbox_to,
                        from_number=colegio_from,
                        body="",
                        extra={
                            "_ProfileName": "QA Colegio Tutor",
                            "ButtonPayload": "justificar_inasistencia",
                            "ButtonText": "Justificar inasistencia",
                        },
                    ),
                    WhatsappCase(
                        label="colegio_detalle_audio_ubicacion",
                        to_number=sandbox_to,
                        from_number=colegio_from,
                        body="",
                        extra={
                            "_ProfileName": "QA Colegio Tutor",
                            "NumMedia": "1",
                            "MediaUrl0": "https://media.local/qa-colegio-certificado.ogg",
                            "MediaContentType0": "audio/ogg",
                            "MediaSid0": f"ME{uuid.uuid4().hex[:30]}",
                            "Latitude": "-34.6037",
                            "Longitude": "-58.3816",
                            "Address": "Sede Central, Av Colegio 123",
                            "Label": "Sede Central",
                        },
                    ),
                ]
            )

        for number in sorted({_normalize(case.to_number) for case in cases}):
            mapping = WhatsappNumero.query.filter_by(numero_whatsapp=number, is_active=True).first()
            if not mapping:
                raise RuntimeError(f"No hay mapping activo para {number}")
            _safe_print(
                "mapping",
                {
                    "numero": number,
                    "user_id": mapping.user_id,
                    "email": getattr(mapping.user, "email", None),
                    "tipo": getattr(mapping.user, "tipo_chat", None),
                },
            )

        before = _row_counts("whatsapp_")

        with app.test_client() as client, patch("routes.whatsapp_webhook.twilio_client", fake_twilio), patch(
            "routes.whatsapp_webhook.create_attachment_with_thumbnail",
            side_effect=_fake_create_attachment_with_thumbnail,
        ), patch(
            "routes.whatsapp_webhook.requests.get",
            return_value=FakeHttpResponse(png_1x1),
        ), patch(
            "services.audio_transcription_service.transcribe_audio_from_url",
            side_effect=_fake_transcribe_audio_from_url,
        ):
            failures = []
            for case in cases:
                data = _twilio_form(case)
                response = client.post(
                    "/webhook/whatsapp",
                    base_url=base_url,
                    data=data,
                    headers=_signed_headers(endpoint, data),
                )
                _safe_print(
                    "case",
                    {
                        "label": case.label,
                        "status": response.status_code,
                        "body": response.get_data(as_text=True)[:200],
                    },
                )
                if response.status_code != 200:
                    failures.append((case.label, response.status_code, response.get_data(as_text=True)[:500]))

        after = _row_counts("whatsapp_")
        _safe_print("delta", {key: after[key] - before[key] for key in before})
        _safe_print("twilio_messages", json.dumps(fake_twilio.messages.sent, ensure_ascii=False, default=str)[:4000])
        if failures:
            raise RuntimeError(f"Fallaron casos WhatsApp QA: {failures}")

        if isolated_db:
            db.session.remove()
            db.drop_all()


if __name__ == "__main__":
    main()
