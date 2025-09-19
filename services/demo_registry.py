from __future__ import annotations

import re
import unicodedata

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from flask import current_app
from sqlalchemy import func

from models import QA, Rubro, User
from services.logic import es_rubro_publico


_MISCONFIGURED_DEMOS_LOGGED: set[str] = set()


@dataclass(slots=True)
class DemoRubro:
    """Metadata used to bootstrap curated demo experiences."""

    key: str
    label: str
    descripcion: Optional[str]
    tipo_chat: str
    owner_user_id: Optional[int]
    rubro_id: Optional[int]
    rubro_clave: Optional[str]
    token: Optional[str] = None
    prompt_context: Optional[str] = None
    welcome_message: Optional[str] = None
    resources: List[Dict[str, object]] = field(default_factory=list)
    faq_preview: List[Dict[str, str]] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    quick_actions: List[Dict[str, object]] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)

    def to_internal_dict(self) -> Dict[str, object]:
        """Return a dict representation used by the chat routes."""

        payload = {
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
            "quick_actions": [dict(item) for item in self.quick_actions],
            "capabilities": list(self.capabilities),
            "keywords": list(self.keywords),
        }
        if self.token:
            payload["token"] = self.token
        if self.aliases:
            payload["aliases"] = list(self.aliases)
        return payload

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
            "quick_actions": [dict(item) for item in self.quick_actions],
            "capabilities": list(self.capabilities),
            "keywords": list(self.keywords),
        }


def _normalize_key(value: object) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text.replace(" ", "_")


def _normalize_alias_value(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None

    decomposed = unicodedata.normalize("NFKD", text)
    sanitized = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    sanitized = re.sub(r"[^a-z0-9]+", "_", sanitized)
    sanitized = sanitized.strip("_")
    return sanitized or None


def _alias_variants(*values: object) -> set[str]:
    variants: set[str] = set()
    for value in values:
        normalized = _normalize_alias_value(value)
        if not normalized:
            continue
        variants.add(normalized)
        collapsed = normalized.replace("_", "")
        if collapsed:
            variants.add(collapsed)
        parts = [part for part in normalized.split("_") if part]
        if len(parts) > 1:
            for start in range(len(parts)):
                for end in range(start + 1, len(parts) + 1):
                    fragment = "_".join(parts[start:end])
                    if fragment:
                        variants.add(fragment)
                        collapsed_fragment = fragment.replace("_", "")
                        if collapsed_fragment:
                            variants.add(collapsed_fragment)
    return variants


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
            if key not in _MISCONFIGURED_DEMOS_LOGGED:
                current_app.logger.warning(
                    "[demo] No se pudo preparar la demo '%s' porque falta owner o rubro válido.",
                    key,
                )
                _MISCONFIGURED_DEMOS_LOGGED.add(key)
            continue

        if not rubro_obj:
            rubro_obj = owner_user.rubro

        if not rubro_obj:
            if key not in _MISCONFIGURED_DEMOS_LOGGED:
                current_app.logger.warning(
                    "[demo] El owner '%s' no tiene rubro asociado para la demo '%s'.",
                    owner_user.id,
                    key,
                )
                _MISCONFIGURED_DEMOS_LOGGED.add(key)
            continue

        _MISCONFIGURED_DEMOS_LOGGED.discard(key)

        tipo_chat = _guess_tipo_chat(entry, rubro_obj, owner_user)
        descripcion_final = descripcion or getattr(rubro_obj, "descripcion", None) or getattr(rubro_obj, "nombre", None)

        prompt_context = entry.get("prompt_context") or entry.get("prompt")
        welcome_message = entry.get("welcome_message")
        resources = entry.get("resources") or []
        raw_aliases = entry.get("aliases") or entry.get("alias") or entry.get("alias_tokens")
        aliases: List[str] = []
        if isinstance(raw_aliases, (list, tuple, set)):
            for alias in raw_aliases:
                if alias is None:
                    continue
                alias_text = str(alias).strip()
                if alias_text:
                    aliases.append(alias_text)
        elif isinstance(raw_aliases, str):
            alias_text = raw_aliases.strip()
            if alias_text:
                aliases.append(alias_text)

        quick_actions_raw = entry.get("quick_actions") or []
        quick_actions = [dict(item) for item in quick_actions_raw if isinstance(item, dict)]

        capabilities_raw = (
            entry.get("capabilities")
            or entry.get("demo_capabilities")
            or entry.get("features")
            or []
        )
        capabilities: List[str] = []
        if isinstance(capabilities_raw, (list, tuple, set)):
            for item in capabilities_raw:
                text = str(item).strip() if item is not None else ""
                if text:
                    capabilities.append(text)
        elif isinstance(capabilities_raw, str):
            text = capabilities_raw.strip()
            if text:
                capabilities.append(text)

        keywords_raw = (
            entry.get("keywords")
            or entry.get("palabras_clave")
            or entry.get("keyword_list")
            or []
        )
        keywords: List[str] = []
        if isinstance(keywords_raw, (list, tuple, set)):
            for item in keywords_raw:
                text = str(item).strip() if item is not None else ""
                if text:
                    keywords.append(text)
        elif isinstance(keywords_raw, str):
            text = keywords_raw.strip()
            if text:
                keywords.append(text)

        demo_rubro = DemoRubro(
            key=key,
            label=str(nombre),
            descripcion=descripcion_final,
            tipo_chat=tipo_chat,
            owner_user_id=owner_user.id,
            rubro_id=rubro_obj.id,
            rubro_clave=getattr(rubro_obj, "clave", None),
            token=entry.get("token"),
            prompt_context=prompt_context,
            welcome_message=welcome_message,
            resources=[dict(item) for item in resources if isinstance(item, dict)],
            faq_preview=_faq_preview_for_rubro(rubro_obj),
            aliases=aliases,
            quick_actions=quick_actions,
            capabilities=capabilities,
            keywords=keywords,
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


def demo_rubro_for_token(token: Optional[str]) -> Optional[DemoRubro]:
    """Return the configured demo associated with an entity token, if any."""

    if not token:
        return None

    normalized = str(token).strip().lower()
    if not normalized:
        return None

    demos = load_demo_rubros()

    for demo in demos:
        if demo.token and demo.token.strip().lower() == normalized:
            return demo
        if any(str(alias).strip().lower() == normalized for alias in demo.aliases):
            return demo

    alias_patterns = (
        r"^demo[-_]?anon[-_]?(.+)$",
        r"^demo[-_]?token[-_]?(.+)$",
        r"^demo[-_]?(.+)$",
    )

    for pattern in alias_patterns:
        match = re.match(pattern, normalized)
        if not match:
            continue

        candidate_key = match.group(1)
        slug = _normalize_alias_value(candidate_key)
        if not slug:
            continue

        for demo in demos:
            alias_candidates = _alias_variants(
                demo.key,
                demo.rubro_clave,
                demo.label,
                *demo.aliases,
            )
            if slug in alias_candidates:
                return demo

            slug_tokens = {token for token in slug.split("_") if token}
            if slug_tokens:
                alias_token_pool: set[str] = set()
                for candidate in alias_candidates:
                    alias_token_pool.update(part for part in candidate.split("_") if part)
                if slug_tokens.issubset(alias_token_pool):
                    return demo

    return None
