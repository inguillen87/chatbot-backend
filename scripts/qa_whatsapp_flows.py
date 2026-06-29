import json
import os
import sys
import uuid
import base64
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_LOCAL_TEST")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "local-whatsapp-qa-token")
os.environ.setdefault("CHATBOC_DEMO_WHATSAPP_NUMBERS", "+18564858589")
os.environ.setdefault("CHATBOC_DEMO_MAX_MESSAGES", "25")

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


QA_SCENARIOS = {
    "gov_claim_text_to_tracking": [
        "junin_texto_reclamo",
        "junin_imagen",
        "junin_dni_reclamo",
        "junin_confirmacion_reclamo",
    ],
    "gov_claim_location_to_tracking": [
        "junin_ubicacion_inicio",
        "junin_ubicacion_compartida",
        "junin_ubicacion_sin_foto",
        "junin_ubicacion_datos",
        "junin_ubicacion_confirmar",
    ],
    "gov_claim_audio_accessible": [
        "junin_audio",
        "junin_audio_sin_foto",
        "junin_audio_datos",
        "junin_audio_confirmar",
    ],
    "pyme_catalog_order_checkout": [
        "cuatro_fincas_pedido",
        "cuatro_fincas_confirmar",
    ],
    "chatboc_demo_hub": [
        "chatboc_demo_menu",
        "chatboc_demo_empresas",
        "chatboc_demo_order_start",
        "chatboc_demo_order_confirm",
    ],
    "survey_vote_realtime": [
        "chatboc_demo_surveys",
        "chatboc_demo_surveys_empresas",
        "chatboc_demo_survey_open",
    ],
    "school_family_case": [
        "colegio_menu_sandbox",
        "colegio_seleccion_inasistencia",
        "colegio_detalle_audio_ubicacion",
    ],
    "finance_onboarding_collection_signature": [
        "finance_alta_digital",
        "finance_revision_kyc",
        "finance_cobranza_pago_firma",
    ],
    "finance_account_servicing": [
        "finance_account_status",
        "finance_support_handoff",
    ],
    "finance_remittance_transfer": [
        "finance_remittance_transfer",
        "finance_transfer_receipt",
    ],
    "finance_insurance_claim": [
        "finance_insurance_claim",
        "finance_insurance_document",
    ],
    "finance_fee_financing_tax": [
        "finance_fee_financing",
        "finance_tax_payment",
    ],
}

FINANCE_QA_SCENARIOS = {
    "finance_onboarding_collection_signature",
    "finance_account_servicing",
    "finance_remittance_transfer",
    "finance_insurance_claim",
    "finance_fee_financing_tax",
}

QA_SCENARIO_CONTRACTS = {
    "gov_claim_text_to_tracking": {
        "alias": "municipal_claim_full",
        "required_links_any": ["chatboc.ar/chat/", "/tracking/claim/"],
        "required_delta": {"municipio_tickets": 1},
        "probes": ["claim_tracking_experience"],
    },
    "pyme_catalog_order_checkout": {
        "required_links_any": ["/checkout/", "/catalogo/", "/api/checkout/crear-preferencia"],
        "required_delta": {"pyme_commerce_records": 1},
        "probes": ["public_market_catalog", "order_tracking_experience", "checkout_route_registered"],
    },
    "survey_vote_realtime": {
        "required_links_any": ["chatboc.ar/e/", "/e/"],
        "probes": ["demo_survey_detail", "demo_survey_vote", "demo_survey_live_results"],
    },
}

for _finance_scenario in FINANCE_QA_SCENARIOS:
    QA_SCENARIO_CONTRACTS[_finance_scenario] = {
        "required_links_any": ["/finanzas/"],
        "probes": ["finance_webview_or_activation_pending"],
        "finance_activation_pending_allowed": True,
    }


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
        if _truthy_env("TESTING", "1") and not _truthy_env("QA_WHATSAPP_USE_CONFIGURED_DB"):
            token = "local-whatsapp-qa-token"
        else:
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


def _row_markers() -> dict[str, int]:
    return {
        "municipio_tickets": db.session.query(db.func.max(MunicipioTicket.id)).scalar() or 0,
        "pyme_tickets": db.session.query(db.func.max(PymeTicket.id)).scalar() or 0,
        "pyme_pedidos": db.session.query(db.func.max(PymePedido.id)).scalar() or 0,
        "adjuntos": db.session.query(db.func.max(ArchivoAdjunto.id)).scalar() or 0,
    }


def _delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in before}


def _messages_for_labels(case_results: list[dict], labels: list[str]) -> list[dict]:
    wanted = set(labels)
    messages: list[dict] = []
    for result in case_results:
        if result.get("label") in wanted:
            messages.extend(result.get("messages") or [])
    return messages


def _scenario_delta(case_results: list[dict], labels: list[str]) -> dict[str, int]:
    wanted = set(labels)
    totals = {key: 0 for key in _row_counts("whatsapp_")}
    for result in case_results:
        if result.get("label") not in wanted:
            continue
        for key, value in (result.get("delta") or {}).items():
            totals[key] = totals.get(key, 0) + int(value or 0)
    totals["pyme_commerce_records"] = (
        totals.get("pyme_pedidos", 0)
        + totals.get("pyme_tickets", 0)
    )
    return totals


def _message_bodies(messages: list[dict]) -> str:
    return "\n".join(json.dumps(message, ensure_ascii=False, default=str) for message in messages)


def _has_any_marker(text: str, markers: list[str]) -> list[str]:
    return [marker for marker in markers if marker and marker in text]


def _route_prefix_registered(app, prefix: str) -> bool:
    normalized = "/" + str(prefix or "").strip("/")
    return any(str(rule).startswith(normalized) for rule in app.url_map.iter_rules())


def _route_registered(app, route: str) -> bool:
    return any(str(rule) == route for rule in app.url_map.iter_rules())


def _probe_response(client, method: str, path: str, *, expected_statuses: set[int] | None = None, **kwargs) -> dict:
    expected = expected_statuses or {200}
    response = client.open(path, method=method, **kwargs)
    payload = response.get_json(silent=True)
    return {
        "method": method,
        "path": path,
        "status": response.status_code,
        "ok": response.status_code in expected,
        "contract_version": payload.get("contract_version") if isinstance(payload, dict) else None,
        "reason_code": payload.get("reason_code") if isinstance(payload, dict) else None,
    }


def _latest_claim_after(markers: dict[str, int]) -> MunicipioTicket | None:
    return (
        MunicipioTicket.query.filter(MunicipioTicket.id > int(markers.get("municipio_tickets") or 0))
        .order_by(MunicipioTicket.id.desc())
        .first()
    )


def _latest_order_after(markers: dict[str, int]) -> PymePedido | None:
    return (
        PymePedido.query.filter(PymePedido.id > int(markers.get("pyme_pedidos") or 0))
        .order_by(PymePedido.id.desc())
        .first()
    )


def _demo_survey_slug() -> str:
    from services.demo_surveys import build_demo_survey_chat_menu

    menu = build_demo_survey_chat_menu(sector="empresas", tenant_slug="chatboc-demo")
    first_survey = ((menu.get("surveys") or [{}])[0] or {})
    return str(first_survey.get("slug") or "demo-empresas-chatboc-demo-preferencias-productos")


def _probe_contract(app, client, probe_id: str, markers: dict[str, int]) -> dict[str, Any]:
    if probe_id == "claim_tracking_experience":
        ticket = _latest_claim_after(markers)
        if not ticket:
            return {"id": probe_id, "ok": False, "reason": "claim_not_created"}
        return {
            "id": probe_id,
            **_probe_response(
                client,
                "GET",
                f"/api/public/tracking/experience?kind=claim&code=M-{ticket.nro_ticket}&pin={ticket.consulta_pin}",
            ),
        }
    if probe_id == "order_tracking_experience":
        order = _latest_order_after(markers)
        if not order:
            return {"id": probe_id, "ok": False, "reason": "order_not_created"}
        return {
            "id": probe_id,
            **_probe_response(
                client,
                "GET",
                f"/api/public/tracking/experience?kind=order&code={order.nro_pedido}",
            ),
        }
    if probe_id == "public_market_catalog":
        return {
            "id": probe_id,
            **_probe_response(client, "GET", "/api/public/market/cuatro-fincas/productos"),
        }
    if probe_id == "checkout_route_registered":
        return {
            "id": probe_id,
            "route": "/api/checkout/crear-preferencia",
            "ok": _route_registered(app, "/api/checkout/crear-preferencia"),
        }
    if probe_id == "demo_survey_detail":
        slug = _demo_survey_slug()
        return {
            "id": probe_id,
            **_probe_response(client, "GET", f"/api/public/encuestas/v1/{slug}"),
        }
    if probe_id == "demo_survey_vote":
        slug = _demo_survey_slug()
        detail = client.get(f"/api/public/encuestas/v1/{slug}")
        detail_payload = detail.get_json(silent=True) or {}
        question = ((detail_payload.get("preguntas") or [{}])[0] or {})
        option = ((question.get("opciones") or [{}])[0] or {})
        if not question.get("id") or not option.get("id"):
            return {"id": probe_id, "ok": False, "reason": "survey_question_or_option_missing"}
        return {
            "id": probe_id,
            **_probe_response(
                client,
                "POST",
                f"/api/public/encuestas/v1/{slug}/responder",
                expected_statuses={201},
                json={"answers": [{"question_id": question["id"], "option_id": option["id"]}]},
            ),
        }
    if probe_id == "demo_survey_live_results":
        slug = _demo_survey_slug()
        return {
            "id": probe_id,
            **_probe_response(client, "GET", f"/api/public/encuestas/v1/{slug}/live-results?include_heatmap=0"),
        }
    if probe_id == "finance_webview_or_activation_pending":
        return _finance_webview_or_activation_pending(app)
    return {"id": probe_id, "ok": False, "reason": "unknown_probe"}


def _finance_webview_or_activation_pending(app) -> dict[str, Any]:
    has_finance_route = _route_prefix_registered(app, "/finanzas")
    if has_finance_route:
        return {"id": "finance_webview_or_activation_pending", "ok": True, "route_prefix": "/finanzas"}
    return {
        "id": "finance_webview_or_activation_pending",
        "ok": True,
        "ready": False,
        "route_prefix": "/finanzas",
        "activation_contract": {
            "contract_version": "finance.activation_plan.v1",
            "state": "activation_pending",
            "reason_code": "finance_webviews_not_registered",
            "required_next_step": "register_and_probe_finance_webview_routes_before_marking_ready",
        },
    }


def _assert_whatsapp_copy_quality(sent_messages: list[dict]) -> None:
    failures: list[str] = []
    banned_fragments = [
        "Deje abierto",
        "MenÃ",
        "CategorÃ",
        "DirecciÃ",
        "DescripciÃ",
        "TelÃ",
        "Â¡",
        "â",
        "ð",
    ]
    for index, message in enumerate(sent_messages, start=1):
        body = str(message.get("body") or "")
        for fragment in banned_fragments:
            if fragment in body:
                failures.append(f"mensaje {index}: contiene texto/encoding invalido {fragment!r}")
        if "1. Menu\n2. Cancelar" in body and "*1*. Men" in body:
            failures.append(f"mensaje {index}: menu manual duplicado con opciones formateadas")
        if "❌ Cancelar" in body and "\n*5*. Cancelar" in body:
            failures.append(f"mensaje {index}: cancelar de flujo duplicado con cancelar generico")
    if failures:
        raise RuntimeError("Calidad UX WhatsApp invalida: " + "; ".join(failures))


def _assert_case_matrix(case_results: list[dict]) -> list[dict]:
    by_label = {item["label"]: item for item in case_results}
    reports: list[dict] = []
    blocking: list[str] = []
    for scenario_id, labels in QA_SCENARIOS.items():
        present = [label for label in labels if label in by_label]
        passed = [
            label
            for label in present
            if int(by_label[label].get("status") or 0) == 200
        ]
        missing = [label for label in labels if label not in by_label]
        failed = [
            label
            for label in present
            if int(by_label[label].get("status") or 0) != 200
        ]
        ready = len(passed) == len(labels)
        reports.append(
            {
                "scenario": scenario_id,
                "ready": ready,
                "passed": len(passed),
                "total": len(labels),
                "missing": missing,
                "failed": failed,
            }
        )
        if failed or (scenario_id != "school_family_case" and missing):
            blocking.append(f"{scenario_id}: missing={missing}, failed={failed}")
    if blocking:
        raise RuntimeError("Matriz QA WhatsApp incompleta: " + "; ".join(blocking))
    return reports


def _assert_contract_matrix(app, client, *, case_results: list[dict], markers: dict[str, int]) -> list[dict]:
    by_label = {item["label"]: item for item in case_results}
    reports: list[dict] = []
    blocking: list[str] = []

    for scenario_id, contract in QA_SCENARIO_CONTRACTS.items():
        labels = QA_SCENARIOS[scenario_id]
        messages = _messages_for_labels(case_results, labels)
        bodies = _message_bodies(messages)
        delta = _scenario_delta(case_results, labels)
        missing = [label for label in labels if label not in by_label]
        failed = [
            label
            for label in labels
            if label in by_label and int(by_label[label].get("status") or 0) != 200
        ]

        link_markers = list(contract.get("required_links_any") or [])
        found_links = _has_any_marker(bodies, link_markers)
        probes = [_probe_contract(app, client, probe_id, markers) for probe_id in (contract.get("probes") or [])]
        required_delta = contract.get("required_delta") or {}
        delta_failures = [
            f"{key}>={minimum} actual={delta.get(key, 0)}"
            for key, minimum in required_delta.items()
            if int(delta.get(key, 0)) < int(minimum)
        ]

        finance_pending = next(
            (
                probe
                for probe in probes
                if isinstance(probe.get("activation_contract"), dict)
                and probe["activation_contract"].get("state") == "activation_pending"
            ),
            None,
        )
        if finance_pending and contract.get("finance_activation_pending_allowed"):
            ready = False
            status = "activation_pending"
            link_failures: list[str] = []
            probe_failures = []
        else:
            link_failures = [] if (not link_markers or found_links) else [f"missing_any_link={link_markers}"]
            probe_failures = [probe for probe in probes if not probe.get("ok")]
            ready = not (missing or failed or link_failures or probe_failures or delta_failures)
            status = "ready" if ready else "blocked"

        report = {
            "scenario": scenario_id,
            "alias": contract.get("alias"),
            "ready": ready,
            "status": status,
            "missing": missing,
            "failed": failed,
            "delta": delta,
            "required_links_found": found_links,
            "probes": probes,
            "delta_failures": delta_failures,
        }
        reports.append(report)

        if status == "blocked":
            blocking.append(
                f"{scenario_id}: missing={missing}, failed={failed}, "
                f"links={link_failures}, probes={probe_failures}, delta={delta_failures}"
            )

    if blocking:
        raise RuntimeError("Contratos QA WhatsApp incompletos: " + "; ".join(blocking))
    return reports


def _assert_artifact_health(*, before: dict, after: dict, sent_messages: list[dict]) -> dict:
    delta = {key: after[key] - before[key] for key in before}
    bodies = "\n".join(str(message.get("body") or "") for message in sent_messages)
    link_markers = [
        "chatboc.ar/chat/",
        "/api/public/tracking/experience",
        "chatboc.ar/e/",
        "/demo?sector=empresas",
    ]
    found_links = [marker for marker in link_markers if marker in bodies]
    report = {
        "delta": delta,
        "messages_sent": len(sent_messages),
        "link_markers_found": found_links,
        "has_claim_ticket": delta.get("municipio_tickets", 0) >= 1 or after.get("municipio_tickets", 0) >= 1,
        "has_commerce_record": delta.get("pyme_pedidos", 0) >= 1
        or delta.get("pyme_tickets", 0) >= 1
        or after.get("pyme_tickets", 0) >= 1,
        "has_media_or_audio_artifact": delta.get("adjuntos", 0) >= 1 or after.get("adjuntos", 0) >= 1,
        "has_webview_or_demo_link": bool(found_links),
    }
    failures: list[str] = []
    if not report["has_claim_ticket"]:
        failures.append("no se genero ningun ticket municipal")
    if not report["has_commerce_record"]:
        failures.append("no se genero registro comercial pyme/demo")
    if not report["has_webview_or_demo_link"]:
        failures.append("no se emitio ningun link de tracking/demo/encuesta")
    if failures:
        raise RuntimeError("Evidencia QA WhatsApp insuficiente: " + "; ".join(failures))
    return report


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
    bodega_to = os.environ.get("QA_BODEGA_TO", "+18564858588")
    demo_to = os.environ.get("QA_CHATBOC_DEMO_TO", "+18564858589")
    sandbox_to = os.environ.get("QA_TWILIO_SANDBOX_TO", "+14155238886")
    os.environ["QA_AUDIO_PHONE"] = junin_audio_from
    demo_from = f"+54926160{run_seed}"
    finance_from = f"+54926161{run_seed}"

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
        WhatsappCase(
            label="chatboc_demo_menu",
            to_number=demo_to,
            from_number=demo_from,
            body="Hola",
            extra={"_ProfileName": "QA Chatboc Demo"},
        ),
        WhatsappCase(
            label="chatboc_demo_empresas",
            to_number=demo_to,
            from_number=demo_from,
            body="",
            extra={
                "_ProfileName": "QA Chatboc Demo",
                "ButtonPayload": "chatboc_demo:empresas",
                "ButtonText": "Empresa y pedidos",
            },
        ),
        WhatsappCase(
            label="chatboc_demo_order_start",
            to_number=demo_to,
            from_number=demo_from,
            body="",
            extra={
                "_ProfileName": "QA Chatboc Demo",
                "ButtonPayload": "chatboc_demo_order_start",
                "ButtonText": "Crear pedido demo",
            },
        ),
        WhatsappCase(
            label="chatboc_demo_order_confirm",
            to_number=demo_to,
            from_number=demo_from,
            body="",
            extra={
                "_ProfileName": "QA Chatboc Demo",
                "ButtonPayload": "chatboc_demo_order_confirm",
                "ButtonText": "Confirmar pedido demo",
            },
        ),
        WhatsappCase(
            label="chatboc_demo_surveys",
            to_number=demo_to,
            from_number=demo_from,
            body="encuestas",
            extra={"_ProfileName": "QA Chatboc Demo"},
        ),
        WhatsappCase(
            label="chatboc_demo_surveys_empresas",
            to_number=demo_to,
            from_number=demo_from,
            body="",
            extra={
                "_ProfileName": "QA Chatboc Demo",
                "ButtonPayload": "chatboc_surveys:empresas:1",
                "ButtonText": "Encuestas empresas",
            },
        ),
        WhatsappCase(
            label="chatboc_demo_survey_open",
            to_number=demo_to,
            from_number=demo_from,
            body="",
            extra={
                "_ProfileName": "QA Chatboc Demo",
                "ButtonPayload": "chatboc_survey_open::empresas-experiencia-cliente",
                "ButtonText": "Votar ahora",
            },
        ),
        WhatsappCase(
            label="finance_alta_digital",
            to_number=demo_to,
            from_number=finance_from,
            body="Hola, quiero abrir una cuenta digital y validar mi identidad por WhatsApp.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_revision_kyc",
            to_number=demo_to,
            from_number=finance_from,
            body="Tengo DNI y comprobante para continuar el KYC.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_cobranza_pago_firma",
            to_number=demo_to,
            from_number=finance_from,
            body="Necesito ver una deuda, pedir plan de pago, pagar y firmar el acuerdo.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_account_status",
            to_number=demo_to,
            from_number=finance_from,
            body="Quiero ver el estado de cuenta sin compartir datos sensibles por chat.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_support_handoff",
            to_number=demo_to,
            from_number=finance_from,
            body="Necesito que un operador revise mi caso financiero.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_remittance_transfer",
            to_number=demo_to,
            from_number=finance_from,
            body="Quiero enviar una transferencia y seguir el estado hasta el comprobante.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_transfer_receipt",
            to_number=demo_to,
            from_number=finance_from,
            body="Ya complete la transferencia, necesito el comprobante.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_insurance_claim",
            to_number=demo_to,
            from_number=finance_from,
            body="Quiero denunciar un siniestro del seguro y adjuntar documentacion.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_insurance_document",
            to_number=demo_to,
            from_number=finance_from,
            body="Adjunto foto y PDF del siniestro para seguimiento.",
            extra={
                "_ProfileName": "QA Finance",
                "NumMedia": "1",
                "MediaUrl0": "https://media.local/qa-finance-document.png",
                "MediaContentType0": "image/png",
                "MediaSid0": f"ME{uuid.uuid4().hex[:30]}",
            },
        ),
        WhatsappCase(
            label="finance_fee_financing",
            to_number=demo_to,
            from_number=finance_from,
            body="Quiero financiar una cuota o tasa vencida en varias cuotas.",
            extra={"_ProfileName": "QA Finance"},
        ),
        WhatsappCase(
            label="finance_tax_payment",
            to_number=demo_to,
            from_number=finance_from,
            body="Necesito pagar una tasa con descuento y recibir el recibo.",
            extra={"_ProfileName": "QA Finance"},
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

        demo_numbers = {_normalize(demo_to)}
        for number in sorted({_normalize(case.to_number) for case in cases}):
            if number in demo_numbers:
                _safe_print("mapping", {"numero": number, "tipo": "chatboc_demo_autocreated_on_first_webhook"})
                continue
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
        markers = _row_markers()

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
            case_results = []
            for case in cases:
                case_before = _row_counts("whatsapp_")
                sent_before = len(fake_twilio.messages.sent)
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
                case_after = _row_counts("whatsapp_")
                case_results.append(
                    {
                        "label": case.label,
                        "status": response.status_code,
                        "body": response.get_data(as_text=True)[:200],
                        "delta": _delta(case_before, case_after),
                        "messages": fake_twilio.messages.sent[sent_before:],
                    }
                )
                if response.status_code != 200:
                    failures.append((case.label, response.status_code, response.get_data(as_text=True)[:500]))

            after = _row_counts("whatsapp_")
            _safe_print("delta", _delta(before, after))
            _safe_print("twilio_messages", json.dumps(fake_twilio.messages.sent, ensure_ascii=False, default=str)[:4000])
            _assert_whatsapp_copy_quality(fake_twilio.messages.sent)
            _safe_print("qa_matrix", _assert_case_matrix(case_results))
            _safe_print(
                "contract_matrix",
                _assert_contract_matrix(app, client, case_results=case_results, markers=markers),
            )
            _safe_print(
                "artifact_health",
                _assert_artifact_health(before=before, after=after, sent_messages=fake_twilio.messages.sent),
            )
            if failures:
                raise RuntimeError(f"Fallaron casos WhatsApp QA: {failures}")

        if isolated_db:
            db.session.remove()
            db.drop_all()


if __name__ == "__main__":
    main()
