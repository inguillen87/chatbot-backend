from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from flask import current_app
from sqlalchemy import func

from models import QA, Rubro, User
from services.logic import es_rubro_publico


@dataclass(slots=True)
class DemoRubro:
    """Metadata used to bootstrap curated demo experiences."""

    key: str
    label: str
    descripcion: Optional[str]
    tipo_chat: str
    owner_user_id: int
    rubro_id: int
    rubro_clave: Optional[str]
    prompt_context: Optional[str] = None
    welcome_message: Optional[str] = None
    resources: List[Dict[str, object]] = field(default_factory=list)
    faq_preview: List[Dict[str, str]] = field(default_factory=list)

    def to_internal_dict(self) -> Dict[str, object]:
        """Return a dict representation used by the chat routes."""

        return {
            "key": self.key,
            "label": self.label,
            "descripcion": self.descripcion,
            "tipo_chat": self.tipo_chat,
            "owner_user_id": self.owner_user_id,
            "rubro_id": self.rubro_id,
            "rubro_clave": self.rubro_clave,
            "prompt_context": self.prompt_context,
            "welcome_message": self.welcome_message,
            "resources": [dict(item) for item in self.resources],
            "faq_preview": [dict(item) for item in self.faq_preview],
        }

    def to_public_dict(self) -> Dict[str, object]:
        """Sanitized payload exposed via the /rubros endpoint."""

        return {
            "key": self.key,
            "label": self.label,
            "descripcion": self.descripcion,
            "tipo_chat": self.tipo_chat,
            "rubro_id": self.rubro_id,
            "rubro_clave": self.rubro_clave,
            "prompt_context": self.prompt_context,
            "welcome_message": self.welcome_message,
            "resources": [dict(item) for item in self.resources],
            "faq_preview": [dict(item) for item in self.faq_preview],
        }


def _normalize_key(value: object) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text.replace(" ", "_")


def _guess_tipo_chat(entry: Dict[str, object], rubro: Optional[Rubro], owner: Optional[User]) -> str:
    tipo_chat = entry.get("tipo_chat")
    if isinstance(tipo_chat, str) and tipo_chat.strip():
        return tipo_chat.strip().lower()
    if rubro and es_rubro_publico(rubro):
        return "municipio"
    if owner and getattr(owner, "tipo_chat", None):
        return owner.tipo_chat
    return "pyme"


def _faq_preview_for_rubro(rubro: Rubro, limit: int = 3) -> List[Dict[str, str]]:
    resultados: List[Dict[str, str]] = []
    if not rubro or not rubro.id:
        return resultados

    faqs: List[QA] = (
        QA.query.filter(QA.rubro_id == rubro.id)
        .order_by(QA.id.asc())
        .limit(limit)
        .all()
    )

    for faq in faqs:
        pregunta = (faq.question or "").strip()
        if not pregunta:
            continue
        respuesta = (faq.answer or "").strip()
        if len(respuesta) > 220:
            respuesta = f"{respuesta[:197].rstrip()}…"
        resultados.append({"pregunta": pregunta, "respuesta": respuesta})

    return resultados


def load_demo_rubros() -> List[DemoRubro]:
    """Build the curated demo catalog based on configuration and database state."""

    demo_entries = current_app.config.get("DEMO_RUBROS") or []
    opciones: List[DemoRubro] = []
    seen_keys: set[str] = set()

    for entry in demo_entries:
        if not isinstance(entry, dict):
            continue

        raw_key = entry.get("key") or entry.get("rubro_clave") or entry.get("nombre")
        key = _normalize_key(raw_key)
        if not key or key in seen_keys:
            continue

        nombre = entry.get("nombre") or entry.get("display_name") or str(raw_key)
        descripcion = entry.get("descripcion") or entry.get("description")

        rubro_id_conf = entry.get("rubro_id")
        rubro_clave_conf = entry.get("rubro_clave")
        token_conf = entry.get("token")
        user_id_conf = entry.get("user_id")

        owner_user = None
        rubro_obj = None

        if user_id_conf:
            owner_user = User.query.get(user_id_conf)

        if not owner_user and token_conf:
            owner_user = User.query.filter_by(token=token_conf).first()

        if not owner_user and rubro_id_conf:
            rubro_obj = Rubro.query.get(rubro_id_conf)
            if rubro_obj:
                owner_user = (
                    User.query.filter_by(rubro_id=rubro_obj.id, empresa_id=None).first()
                    or User.query.filter_by(rubro_id=rubro_obj.id, rol="admin").first()
                )

        if not owner_user and rubro_clave_conf:
            rubro_obj = Rubro.query.filter(func.lower(Rubro.clave) == func.lower(str(rubro_clave_conf))).first()
            if rubro_obj:
                owner_user = (
                    User.query.filter_by(rubro_id=rubro_obj.id, empresa_id=None).first()
                    or User.query.filter_by(rubro_id=rubro_obj.id, rol="admin").first()
                )

        if not owner_user and entry.get("tipo_chat", "").strip().lower() == "municipio":
            owner_user = User.query.filter_by(tipo_chat="municipio", rol="admin").first()
            if owner_user and not rubro_obj:
                rubro_obj = owner_user.rubro

        if owner_user and not rubro_obj:
            rubro_obj = owner_user.rubro

        if rubro_obj and not owner_user:
            owner_user = (
                User.query.filter_by(rubro_id=rubro_obj.id, empresa_id=None).first()
                or User.query.filter_by(rubro_id=rubro_obj.id, rol="admin").first()
            )

        if rubro_obj and not owner_user:
            fallback_owner = (
                User.query.filter_by(rubro_id=rubro_obj.id)
                .order_by(User.id.asc())
                .first()
            )
            if fallback_owner:
                current_app.logger.info(
                    "[demo] Usando usuario %s como owner alternativo para el rubro %s.",
                    fallback_owner.id,
                    rubro_obj.id,
                )
                owner_user = fallback_owner

        if not owner_user and rubro_obj and es_rubro_publico(rubro_obj):
            owner_user = User.query.filter_by(tipo_chat="municipio", rol="admin").first()

        if not owner_user:
            current_app.logger.warning(
                "[demo] No se pudo preparar la demo '%s' porque falta owner o rubro válido.",
                key,
            )
            continue

        if not rubro_obj:
            rubro_obj = owner_user.rubro

        if not rubro_obj:
            current_app.logger.warning(
                "[demo] El owner '%s' no tiene rubro asociado para la demo '%s'.", owner_user.id, key
            )
            continue

        tipo_chat = _guess_tipo_chat(entry, rubro_obj, owner_user)
        descripcion_final = descripcion or getattr(rubro_obj, "descripcion", None) or getattr(rubro_obj, "nombre", None)

        prompt_context = entry.get("prompt_context") or entry.get("prompt")
        welcome_message = entry.get("welcome_message")
        resources = entry.get("resources") or []

        demo_rubro = DemoRubro(
            key=key,
            label=str(nombre),
            descripcion=descripcion_final,
            tipo_chat=tipo_chat,
            owner_user_id=owner_user.id,
            rubro_id=rubro_obj.id,
            rubro_clave=getattr(rubro_obj, "clave", None),
            prompt_context=prompt_context,
            welcome_message=welcome_message,
            resources=[dict(item) for item in resources if isinstance(item, dict)],
            faq_preview=_faq_preview_for_rubro(rubro_obj),
        )

        opciones.append(demo_rubro)
        seen_keys.add(key)

    return opciones


def demo_rubros_by_key() -> Dict[str, DemoRubro]:
    return {demo.key: demo for demo in load_demo_rubros()}


def demo_rubros_for_rubros(ids: Iterable[int]) -> Dict[int, DemoRubro]:
    lookup: Dict[int, DemoRubro] = {}
    id_set = {int(value) for value in ids}
    if not id_set:
        return lookup

    for demo in load_demo_rubros():
        if demo.rubro_id in id_set:
            lookup[demo.rubro_id] = demo
    return lookup
