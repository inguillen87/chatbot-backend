# services/ticket_service.py
import hashlib
import json
import logging
import math
import os
import random
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, Literal, Union, Iterable, Optional

from models import (
    AuditEvent,
    ArchivoAdjunto,
    MunicipioTicket,
    MunicipioTicketReplyEvent,
    PymeTicket,
    TenantTicket,
    TenantTicketReplyEvent,
    TicketComentario,
    TicketDomainEffectReceipt,
    TicketSatisfaccion,
    Conversacion,
    TenantProfile,
    User,
    db,
)
from utils.ticket_utils import normalize_category
from services.employee_ticket_access import (
    apply_employee_ticket_category_scope,
    ticket_assignee_is_compatible,
)
from services.ticket_assignment_policy import actor_can_assign_tickets
from utils.time_utils import datetime_to_iso_utc, get_local_now
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified
from .integracion_municipal import enviar_ticket_a_sigem # SIGEM Integration
from utils.heatmap import enrich_heatmap_points
from services.notification_dispatcher import notification_dispatcher
from services.tenant_ticket_scope import (
    TicketTenantScopeError,
    normalize_municipio_ticket_write_scope,
    resolve_municipio_ticket_access_tenant,
    resolve_unique_tenant_for_owner,
    scoped_municipio_ticket_query,
    tenant_owner_ids,
)
from services.user_service import build_identity_subject

logger = logging.getLogger(__name__)

_CLOSED_STATES = {"cerrado"}
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,191}$")
_WHATSAPP_TURN_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")
_EFFECT_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _is_internal_ticket_comment(comment: TicketComentario) -> bool:
    """Treat whitespace/case variants of ``internal`` as private notes."""

    return str(getattr(comment, "origen", None) or "").strip().casefold() == "internal"


def _public_ticket_comment_filter():
    """SQL predicate matching comments that may be shown outside backoffice."""

    return or_(
        TicketComentario.origen.is_(None),
        func.lower(func.trim(TicketComentario.origen)) != "internal",
    )


class TicketIdempotencyError(RuntimeError):
    """Base error for fail-closed ticket idempotency decisions."""


class TicketIdempotencyValidationError(TicketIdempotencyError, ValueError):
    """Raised when an idempotency identity has no safe tenant scope."""


class TicketIdempotencyConflict(TicketIdempotencyError):
    """Raised when one tenant-scoped key is reused for different input."""

    code = "ticket_idempotency_payload_conflict"


class TicketIdempotencyReplayUnavailable(TicketIdempotencyError):
    """Raised when a receipt exists but its domain object no longer does."""

    code = "ticket_idempotency_replay_unavailable"


class TicketReplyOwnershipError(RuntimeError):
    """Raised when the locked ticket owner no longer authorizes a reply."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _assert_locked_reply_owner(actor: User | None, assignee_id: Any) -> None:
    if actor_can_assign_tickets(actor):
        return
    try:
        normalized_assignee_id = int(assignee_id)
    except (TypeError, ValueError):
        normalized_assignee_id = None
    if normalized_assignee_id is None or normalized_assignee_id <= 0:
        raise TicketReplyOwnershipError("ticket_claim_required")
    if actor is None or normalized_assignee_id != getattr(actor, "id", None):
        raise TicketReplyOwnershipError("ticket_assigned_to_other")


def build_whatsapp_ticket_effect_key(
    tenant_id: Any,
    durable_turn_id: Any,
    effect: str,
) -> str:
    """Build a bounded tenant/turn/effect identity safe for persistence."""

    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError) as exc:
        raise TicketIdempotencyValidationError(
            "A durable ticket effect requires a positive tenant_id."
        ) from exc
    if isinstance(tenant_id, bool) or normalized_tenant_id <= 0:
        raise TicketIdempotencyValidationError(
            "A durable ticket effect requires a positive tenant_id."
        )

    normalized_turn_id = str(durable_turn_id or "").strip()
    normalized_effect = str(effect or "").strip()
    if not _WHATSAPP_TURN_RE.fullmatch(normalized_turn_id):
        raise TicketIdempotencyValidationError("Invalid durable WhatsApp turn identity.")
    if not _EFFECT_SUFFIX_RE.fullmatch(normalized_effect):
        raise TicketIdempotencyValidationError("Invalid ticket effect identity.")

    key = f"whatsapp:{normalized_tenant_id}:{normalized_turn_id}:{normalized_effect}"
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise TicketIdempotencyValidationError("Ticket idempotency key is too long or unsafe.")
    return key


def _canonicalize_idempotency_value(value: Any) -> Any:
    """Normalize JSON-compatible domain input without retaining it."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TicketIdempotencyValidationError(
                "Non-finite numbers are not valid ticket payload values."
            )
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TicketIdempotencyValidationError(
                "Non-finite decimals are not valid ticket payload values."
            )
        return {"$decimal": format(value, "f")}
    if isinstance(value, (datetime, date)):
        return {"$datetime": value.isoformat()}
    if isinstance(value, dict):
        return {
            str(key): _canonicalize_idempotency_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize_idempotency_value(item) for item in value]
    raise TicketIdempotencyValidationError(
        f"Unsupported ticket payload type: {type(value).__name__}."
    )


def canonical_ticket_payload_hash(effect_kind: str, payload: Dict[str, Any]) -> str:
    """Return a stable SHA-256 digest for one ticket domain effect."""

    canonical_payload = _canonicalize_idempotency_value(
        {"effect_kind": effect_kind, "payload": payload}
    )
    encoded = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TicketCreator:
    def create(self, ticket_data: Dict[str, Any]) -> Union[PymeTicket, MunicipioTicket]:
        raise NotImplementedError

class MunicipioTicketCreator(TicketCreator):
    def create(self, ticket_data: Dict[str, Any]) -> MunicipioTicket:
        lat = (
            ticket_data.get("latitud")
            or ticket_data.get("lat")
            or ticket_data.get("latitude")
        )
        lon = (
            ticket_data.get("longitud")
            or ticket_data.get("lon")
            or ticket_data.get("lng")
            or ticket_data.get("longitude")
        )
        return MunicipioTicket(
            user_id=ticket_data.get("user_id"),
            municipio_id=ticket_data.get("municipio_id"),
            tenant_id=ticket_data.get("tenant_id"),
            anon_id=ticket_data.get("anon_id"),
            asunto=ticket_data.get("asunto", "Sin Asunto"),
            categoria=ticket_data.get("categoria", "General"),
            pregunta=ticket_data.get("pregunta", ""),    # Reclamo original
            detalles=ticket_data.get("detalles", ""),    # Dirección, nombre, tel, etc.
            nro_ticket=ticket_data.get("nro_ticket"),
            consulta_pin=ticket_data.get("consulta_pin"),
            direccion=ticket_data.get("direccion"),
            distrito=ticket_data.get("distrito"),
            latitud=lat,
            longitud=lon,
            # Campos adicionales para información del vecino/contacto
            nombre_vecino=ticket_data.get("nombre_vecino"),
            telefono_vecino=ticket_data.get("telefono_vecino"),
            email_vecino=ticket_data.get("email_vecino"),
            dni_vecino=ticket_data.get("dni") or ticket_data.get("dni_vecino"),
            foto_url_directa=ticket_data.get("foto_url_directa"), # Para la foto inicial del reclamo
            canal_ingreso=ticket_data.get("canal_ingreso"),
            datos_extra=ticket_data.get("datos_extra"),
            estado=ticket_data.get("estado", "nuevo")
        )

class PymeTicketCreator(TicketCreator):
    def create(self, ticket_data: Dict[str, Any]) -> PymeTicket:
        tenant_id = ticket_data.get("tenant_id")
        rubro_id = ticket_data.get("rubro_id")
        pyme_id = ticket_data.get("pyme_id")
        if (tenant_id is None or rubro_id is None) and pyme_id:
            pyme_user = db.session.get(User, pyme_id)
            if pyme_user:
                if tenant_id is None:
                    tenant_id = getattr(pyme_user, "tenant_id", None)
                if rubro_id is None:
                    rubro_id = getattr(pyme_user, "rubro_id", None)

        lat = (
            ticket_data.get("latitud")
            or ticket_data.get("lat")
            or ticket_data.get("latitude")
        )
        lon = (
            ticket_data.get("longitud")
            or ticket_data.get("lon")
            or ticket_data.get("lng")
            or ticket_data.get("longitude")
        )
        telefono_contacto = (
            ticket_data.get("telefono_cliente")
            or ticket_data.get("telefono_vecino")
            or ticket_data.get("telefono")
        )
        email_contacto = (
            ticket_data.get("email_cliente")
            or ticket_data.get("email_vecino")
            or ticket_data.get("email")
        )
        return PymeTicket(
            user_id=ticket_data.get("user_id"),
            tenant_id=tenant_id,
            anon_id=ticket_data.get("anon_id"),
            asunto=ticket_data.get("asunto", "Sin Asunto"),
            categoria=ticket_data.get("categoria", "General"),
            pregunta=ticket_data.get("pregunta"),
            nro_ticket=ticket_data.get("nro_ticket"),
            consulta_pin=ticket_data.get("consulta_pin") or f"{random.randint(100000, 999999)}",
            rubro_id=rubro_id,
            direccion=ticket_data.get("direccion"),
            latitud=lat,
            longitud=lon,
            telefono=telefono_contacto,
            email=email_contacto,
            dni=ticket_data.get("dni"),
            datos_extra=ticket_data.get("datos_extra"),
            estado=ticket_data.get("estado", "nuevo"),
            estado_cliente=ticket_data.get("estado", "nuevo")
        )

class ServicioTickets:
    def __init__(self):
        self.creators: Dict[str, TicketCreator] = {
            "municipio": MunicipioTicketCreator(),
            "pyme": PymeTicketCreator()
        }
        self.auto_assign_enabled = (
            str(os.getenv("AUTO_ASSIGN_TICKETS", "false")).strip().lower()
            in {"1", "true", "yes"}
        )

    def _employee_channel_filter(self, channel: str):
        """Accept legacy employees without tipo_chat and scoped employees for a channel."""

        return or_(
            User.tipo_chat == channel,
            User.tipo_chat.is_(None),
            User.tipo_chat == "",
        )

    def _municipal_employee_scope_filter(self, ticket: MunicipioTicket):
        try:
            tenant = resolve_municipio_ticket_access_tenant(ticket)
        except TicketTenantScopeError:
            return User.id == None  # noqa: E711

        conditions = [User.tenant_id == tenant.id]
        owners = tenant_owner_ids(tenant)
        if len(owners) == 1:
            owner_resolution = resolve_unique_tenant_for_owner(owners[0])
        else:
            owner_resolution = None
        if owner_resolution is not None and owner_resolution.status == "unique":
            owner_id = owners[0]
            conditions.extend(
                [
                    User.empresa_id == owner_id,
                    User.municipio_id == owner_id,
                    User.id == owner_id,
                ]
            )
        return or_(*conditions)

    def _pyme_employee_scope_filter(self, ticket: PymeTicket, owner_id: Optional[int]):
        conditions = []
        if owner_id:
            conditions.extend(
                [
                    User.empresa_id == owner_id,
                    User.pyme_id == owner_id,
                    User.id == owner_id,
                ]
            )
        if getattr(ticket, "tenant_id", None):
            conditions.append(User.tenant_id == ticket.tenant_id)
        if getattr(ticket, "rubro_id", None):
            conditions.append(User.rubro_id == ticket.rubro_id)
        if not conditions:
            conditions.append(User.id == None)  # noqa: E711
        return or_(*conditions)

    def _empleados_para_ticket_municipal(self, ticket: MunicipioTicket) -> list[User]:
        if not ticket.municipio_id and not getattr(ticket, "tenant_id", None):
            return []

        query = User.query.filter(
            self._employee_channel_filter("municipio"),
            User.rol.in_(["empleado", "admin"]),
            self._municipal_employee_scope_filter(ticket),
        )

        candidatos = query.order_by(User.id.asc()).all()
        return [
            empleado
            for empleado in candidatos
            if ticket_assignee_is_compatible(empleado, ticket)
        ]

    def _calcular_carga_empleado_municipal(
        self,
        empleado: User,
        ticket: MunicipioTicket,
    ) -> int:
        try:
            tenant = resolve_municipio_ticket_access_tenant(ticket)
        except TicketTenantScopeError:
            return 0
        return (
            scoped_municipio_ticket_query(tenant).filter(
                MunicipioTicket.asignado_a_id == empleado.id,
                ~MunicipioTicket.estado.in_(list(_CLOSED_STATES)),
            )
            .with_entities(func.count(MunicipioTicket.id))
            .scalar()
            or 0
        )

    def asignar_ticket_municipal(
        self,
        ticket: MunicipioTicket,
        empleado_id: Optional[int] = None,
        *,
        auto: bool = False,
        actor_id: Optional[int] = None,
    ) -> Optional[User]:
        """Asigna el ticket a un empleado compatible con la categoría y municipio."""

        if not ticket:
            return None

        if empleado_id:
            empleado = User.query.filter(
                User.id == empleado_id,
                self._employee_channel_filter("municipio"),
                User.rol.in_(["empleado", "admin"]),
                self._municipal_employee_scope_filter(ticket),
            ).first()
            if not empleado:
                raise ValueError("El agente seleccionado no pertenece a este municipio.")
            if not ticket_assignee_is_compatible(empleado, ticket):
                raise ValueError("assignee_category_scope_mismatch")
        else:
            candidatos = self._empleados_para_ticket_municipal(ticket)
            if not candidatos or (not auto and not self.auto_assign_enabled):
                return None
            empleado = min(
                candidatos,
                key=lambda emp: self._calcular_carga_empleado_municipal(emp, ticket),
            )

        if not empleado:
            return None

        if ticket.asignado_a_id == empleado.id:
            return empleado

        ticket.asignado_a = empleado
        ticket.asignado_en = get_local_now()
        if hasattr(ticket, "ultima_actividad"):
            ticket.ultima_actividad = get_local_now()

        comentario = TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario=f"Ticket asignado a {empleado.name}",
            user_id=actor_id,
            es_admin=True,
            origen="sistema",
        )
        db.session.add(comentario)

        return empleado

    def _resolve_pyme_owner_id(self, ticket: PymeTicket, actor_id: Optional[int]) -> Optional[int]:
        if actor_id:
            actor = db.session.get(User, actor_id)
            if actor and actor.tipo_chat == "pyme":
                return actor.id if actor.rol == "admin" else actor.empresa_id

        admin_for_rubro = (
            User.query.filter(
                User.rubro_id == ticket.rubro_id,
                User.tipo_chat == "pyme",
                User.rol == "admin",
            )
            .order_by(User.id.asc())
            .first()
        )
        return admin_for_rubro.id if admin_for_rubro else None

    def _empleados_para_ticket_pyme(self, ticket: PymeTicket, actor_id: Optional[int]) -> list[User]:
        owner_id = self._resolve_pyme_owner_id(ticket, actor_id)
        if not owner_id and not getattr(ticket, "tenant_id", None):
            return []

        candidatos = (
            User.query.filter(
                self._pyme_employee_scope_filter(ticket, owner_id),
                User.rol.in_(["empleado", "admin"]),
                self._employee_channel_filter("pyme"),
            )
            .order_by(User.id.asc())
            .all()
        )

        return [
            empleado
            for empleado in candidatos
            if ticket_assignee_is_compatible(empleado, ticket)
        ]

    def _calcular_carga_empleado_pyme(self, empleado: User, rubro_id: int) -> int:
        return (
            PymeTicket.query.filter(
                PymeTicket.rubro_id == rubro_id,
                PymeTicket.asignado_a_id == empleado.id,
                ~PymeTicket.estado.in_(list(_CLOSED_STATES)),
            )
            .with_entities(func.count(PymeTicket.id))
            .scalar()
            or 0
        )

    def asignar_ticket_pyme(
        self,
        ticket: PymeTicket,
        empleado_id: Optional[int] = None,
        *,
        auto: bool = False,
        actor_id: Optional[int] = None,
    ) -> Optional[User]:
        """Asigna el ticket de pyme a un empleado compatible."""

        if not ticket:
            return None

        if empleado_id:
            owner_id = self._resolve_pyme_owner_id(ticket, actor_id)
            empleado = User.query.filter(
                User.id == empleado_id,
                self._employee_channel_filter("pyme"),
                User.rol.in_(["empleado", "admin"]),
                self._pyme_employee_scope_filter(ticket, owner_id),
            ).first()
            if empleado and not ticket_assignee_is_compatible(empleado, ticket):
                raise ValueError("assignee_category_scope_mismatch")
        else:
            candidatos = self._empleados_para_ticket_pyme(ticket, actor_id)
            if not candidatos or (not auto and not self.auto_assign_enabled):
                return None
            empleado = min(
                candidatos,
                key=lambda emp: self._calcular_carga_empleado_pyme(emp, ticket.rubro_id),
            )

        if not empleado:
            return None

        if ticket.asignado_a_id == empleado.id:
            return empleado

        ticket.asignado_a = empleado
        ticket.asignado_en = get_local_now()
        if hasattr(ticket, "ultima_actividad"):
            ticket.ultima_actividad = get_local_now()

        comentario = TicketComentario(
            pyme_ticket_id=ticket.id,
            comentario=f"Ticket asignado a {empleado.name}",
            user_id=actor_id,
            es_admin=True,
            origen="sistema",
        )
        db.session.add(comentario)

        return empleado

    @staticmethod
    def _prepare_idempotency_identity(
        idempotency_key: Optional[str],
        idempotency_tenant_id: Optional[int],
    ) -> Optional[tuple[str, int]]:
        if idempotency_key is None and idempotency_tenant_id is None:
            return None
        if idempotency_key is None or idempotency_tenant_id is None:
            raise TicketIdempotencyValidationError(
                "Ticket idempotency requires both key and tenant_id."
            )

        normalized_key = str(idempotency_key).strip()
        if not _IDEMPOTENCY_KEY_RE.fullmatch(normalized_key):
            raise TicketIdempotencyValidationError("Invalid ticket idempotency key.")
        try:
            normalized_tenant_id = int(idempotency_tenant_id)
        except (TypeError, ValueError) as exc:
            raise TicketIdempotencyValidationError(
                "Ticket idempotency requires a positive tenant_id."
            ) from exc
        if isinstance(idempotency_tenant_id, bool) or normalized_tenant_id <= 0:
            raise TicketIdempotencyValidationError(
                "Ticket idempotency requires a positive tenant_id."
            )
        return normalized_key, normalized_tenant_id

    @staticmethod
    def _ticket_payload_for_hash(ticket_data: Dict[str, Any]) -> Dict[str, Any]:
        volatile_fields = {
            "nro_ticket",
            "consulta_pin",
            "fecha",
            "created_at",
            "updated_at",
            "ultima_actividad",
        }
        return {
            key: value
            for key, value in ticket_data.items()
            if key not in volatile_fields and value is not None
        }

    @staticmethod
    def _comment_payload_for_hash(
        ticket_id: int,
        tipo_ticket: str,
        comentario_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "ticket_id": int(ticket_id),
            "tipo_ticket": tipo_ticket,
            "comentario": comentario_data.get("comentario"),
            "user_id": comentario_data.get("user_id"),
            "anon_id": comentario_data.get("anon_id"),
            "es_admin": bool(comentario_data.get("es_admin", False)),
            "archivo_adjunto_id": comentario_data.get("archivo_adjunto_id"),
            "origen": comentario_data.get("origen", "chat"),
            "estado_ticket": comentario_data.get("estado_ticket"),
            "emit_notifications": bool(
                comentario_data.get("emit_notifications", True)
            ),
            "emit_socket": bool(comentario_data.get("emit_socket", True)),
        }

    @staticmethod
    def _find_effect_receipt(
        tenant_id: int,
        idempotency_key: str,
    ) -> Optional[TicketDomainEffectReceipt]:
        return TicketDomainEffectReceipt.query.filter_by(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        ).first()

    @staticmethod
    def _verify_effect_receipt(
        receipt: TicketDomainEffectReceipt,
        *,
        effect_kind: str,
        payload_hash: str,
        resource_type: str,
    ) -> None:
        if (
            receipt.effect_kind != effect_kind
            or receipt.payload_hash != payload_hash
            or receipt.resource_type != resource_type
        ):
            logger.warning(
                "Ticket idempotency conflict receipt_id=%s tenant_id=%s effect_kind=%s",
                getattr(receipt, "id", None),
                getattr(receipt, "tenant_id", None),
                effect_kind,
            )
            raise TicketIdempotencyConflict(
                "The tenant-scoped idempotency key was already used for a different ticket effect."
            )

    @staticmethod
    def _serialize_ticket(ticket: Union[PymeTicket, MunicipioTicket], tipo_ticket: str) -> dict:
        ticket_dict = {
            "id": ticket.id,
            "nro_ticket": ticket.nro_ticket,
            "asunto": ticket.asunto,
            "categoria": ticket.categoria,
            "estado": ticket.estado,
            "direccion": ticket.direccion,
            "user_id": ticket.user_id,
            "anon_id": ticket.anon_id,
        }
        if tipo_ticket == "municipio":
            ticket_dict["detalles"] = ticket.detalles
            ticket_dict["nombre_vecino"] = getattr(ticket, "nombre_vecino", None)
            ticket_dict["telefono_vecino"] = getattr(ticket, "telefono_vecino", None)
            ticket_dict["email_vecino"] = getattr(ticket, "email_vecino", None)
            ticket_dict["municipio_id"] = getattr(ticket, "municipio_id", None)
            ticket_dict["consulta_pin"] = getattr(ticket, "consulta_pin", None)
            ticket_dict["datos_extra"] = getattr(ticket, "datos_extra", None) or {}
        else:
            ticket_dict["consulta_pin"] = getattr(ticket, "consulta_pin", None)
            ticket_dict["detalles"] = ticket.pregunta
            ticket_dict["rubro_id"] = getattr(ticket, "rubro_id", None)
            ticket_dict["datos_extra"] = getattr(ticket, "datos_extra", None) or {}
        return ticket_dict

    def _replay_ticket_effect(
        self,
        receipt: TicketDomainEffectReceipt,
        *,
        tipo_ticket: Literal["municipio", "pyme"],
        effect_kind: str,
        payload_hash: str,
        return_object: bool,
    ) -> Union[PymeTicket, MunicipioTicket, dict]:
        if tipo_ticket not in {"municipio", "pyme"}:
            raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'.")
        resource_type = f"{tipo_ticket}_ticket"
        self._verify_effect_receipt(
            receipt,
            effect_kind=effect_kind,
            payload_hash=payload_hash,
            resource_type=resource_type,
        )
        model = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        ticket = db.session.get(model, receipt.resource_id)
        if ticket is None:
            raise TicketIdempotencyReplayUnavailable(
                "The ticket idempotency receipt exists but its ticket is unavailable."
            )
        logger.info(
            "Replaying ticket effect receipt_id=%s tenant_id=%s resource_id=%s",
            receipt.id,
            receipt.tenant_id,
            receipt.resource_id,
        )
        if return_object:
            return ticket
        return self._serialize_ticket(ticket, tipo_ticket)

    def _replay_comment_effect(
        self,
        receipt: TicketDomainEffectReceipt,
        *,
        effect_kind: str,
        payload_hash: str,
    ) -> TicketComentario:
        self._verify_effect_receipt(
            receipt,
            effect_kind=effect_kind,
            payload_hash=payload_hash,
            resource_type="ticket_comentario",
        )
        comment = db.session.get(TicketComentario, receipt.resource_id)
        if comment is None:
            raise TicketIdempotencyReplayUnavailable(
                "The ticket idempotency receipt exists but its comment is unavailable."
            )
        logger.info(
            "Replaying ticket comment effect receipt_id=%s tenant_id=%s resource_id=%s",
            receipt.id,
            receipt.tenant_id,
            receipt.resource_id,
        )
        return comment

    def _replay_tenant_reply_effect(
        self,
        receipt: TicketDomainEffectReceipt,
        *,
        payload_hash: str,
        reply_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._verify_effect_receipt(
            receipt,
            effect_kind="ticket.comment.tenant",
            payload_hash=payload_hash,
            resource_type="tenant_ticket",
        )
        ticket = TenantTicket.query.filter_by(
            id=receipt.resource_id,
            tenant_id=receipt.tenant_id,
        ).one_or_none()
        if ticket is None:
            raise TicketIdempotencyReplayUnavailable(
                "The tenant ticket reply receipt exists but its ticket is unavailable."
            )

        result = receipt.result_json if isinstance(receipt.result_json, dict) else {}
        event_id = str(result.get("event_id") or "").strip()
        reply_record = None
        reply_record_id = result.get("reply_event_record_id")
        if reply_record_id is not None:
            try:
                normalized_reply_record_id = int(reply_record_id)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TicketIdempotencyReplayUnavailable(
                    "The tenant ticket reply receipt has an invalid durable event."
                ) from exc
            reply_record = TenantTicketReplyEvent.query.filter_by(
                id=normalized_reply_record_id,
                tenant_id=receipt.tenant_id,
                ticket_id=ticket.id,
                event_id=event_id,
            ).one_or_none()
            if reply_record is None:
                raise TicketIdempotencyReplayUnavailable(
                    "The tenant ticket reply receipt exists but its durable event is unavailable."
                )
            event = reply_record.to_event_dict()
        else:
            # Compatibility for receipts written before durable reply events.
            # Their bounded timeline remains the only historical source.
            extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
            comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
            event = next(
                (
                    dict(item)
                    for item in comments
                    if isinstance(item, dict) and str(item.get("id") or "") == event_id
                ),
                None,
            )
            if event is None:
                event = {
                    "id": event_id,
                    "origin": "admin_panel",
                    "action": "reply",
                    "body": str(reply_data.get("body") or ""),
                    "visibility": str(reply_data.get("visibility") or "public"),
                    "created_at": (
                        receipt.created_at.isoformat()
                        if getattr(receipt, "created_at", None)
                        else None
                    ),
                    "actor": {
                        "id": reply_data.get("actor_user_id"),
                        "name": reply_data.get("actor_name"),
                        "role": reply_data.get("actor_role"),
                    },
                }
        logger.info(
            "Replaying TenantTicket reply receipt_id=%s tenant_id=%s ticket_id=%s",
            receipt.id,
            receipt.tenant_id,
            ticket.id,
        )
        return {
            "ticket": ticket,
            "event": event,
            "aggregate_ref": str(result.get("aggregate_ref") or f"{ticket.id}:{event_id}"),
            "reply_record": reply_record,
            "replayed": True,
            "effects_queued": False,
        }

    def _replay_municipio_reply_effect(
        self,
        receipt: TicketDomainEffectReceipt,
        *,
        payload_hash: str,
    ) -> Dict[str, Any]:
        self._verify_effect_receipt(
            receipt,
            effect_kind="ticket.comment.municipio",
            payload_hash=payload_hash,
            resource_type="ticket_comentario",
        )
        result = receipt.result_json if isinstance(receipt.result_json, dict) else {}
        try:
            ticket_id = int(result.get("ticket_id"))
            reply_record_id = int(result.get("reply_event_record_id"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise TicketIdempotencyReplayUnavailable(
                "The municipal reply receipt has an invalid durable identity."
            ) from exc
        event_id = str(result.get("event_id") or "").strip()
        if ticket_id <= 0 or reply_record_id <= 0 or not event_id:
            raise TicketIdempotencyReplayUnavailable(
                "The municipal reply receipt has an invalid durable identity."
            )
        ticket = MunicipioTicket.query.filter_by(
            id=ticket_id,
            tenant_id=receipt.tenant_id,
        ).one_or_none()
        comment = TicketComentario.query.filter_by(
            id=receipt.resource_id,
            municipio_ticket_id=ticket_id,
            pyme_ticket_id=None,
        ).one_or_none()
        reply_record = MunicipioTicketReplyEvent.query.filter_by(
            id=reply_record_id,
            tenant_id=receipt.tenant_id,
            source_model="MunicipioTicket",
            ticket_id=ticket_id,
            comment_id=receipt.resource_id,
            event_id=event_id,
        ).one_or_none()
        if ticket is None or comment is None or reply_record is None:
            raise TicketIdempotencyReplayUnavailable(
                "The municipal reply receipt exists but its durable objects are unavailable."
            )
        logger.info(
            "Replaying MunicipioTicket reply receipt_id=%s tenant_id=%s ticket_id=%s",
            receipt.id,
            receipt.tenant_id,
            ticket.id,
        )
        return {
            "ticket": ticket,
            "comment": comment,
            "event": reply_record.to_event_dict(),
            "aggregate_ref": str(
                result.get("aggregate_ref") or f"{ticket.id}:{event_id}"
            ),
            "reply_record": reply_record,
            "replayed": True,
            "effects_queued": False,
        }

    def crear_nuevo_ticket(
        self,
        tipo_ticket: Literal["municipio", "pyme"],
        ticket_data: Dict[str, Any],
        *,
        return_object: bool = False,
        idempotency_key: Optional[str] = None,
        idempotency_tenant_id: Optional[int] = None,
    ) -> Union[PymeTicket, MunicipioTicket, None, dict]:
        creator = self.creators.get(tipo_ticket)
        if not creator:
            raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'.")

        # Generated numbers and normalization must not alter caller state or
        # become part of the canonical request digest.
        ticket_data = dict(ticket_data or {})

        # Inferir categoría a partir de campos alternativos si no fue provista
        if not ticket_data.get("categoria"):
            tipo_general = (
                ticket_data.get("tipo")
                or ticket_data.get("tipo_ticket")
                or ticket_data.get("tipo_reclamo")
            )
            if tipo_general:
                ticket_data["categoria"] = tipo_general

        # Normalizar categoría para evitar duplicados como variantes de 'luminarias'
        if "categoria" in ticket_data:
            ticket_data["categoria"] = normalize_category(ticket_data.get("categoria"))

        # Eliminar el prefijo "Reclamo (LLM):" del asunto si existe.
        if ticket_data.get("asunto", "").startswith("Reclamo (LLM):"):
            ticket_data["asunto"] = ticket_data["asunto"].replace("Reclamo (LLM):", "").strip()

        idempotency_identity = self._prepare_idempotency_identity(
            idempotency_key,
            idempotency_tenant_id,
        )
        effect_kind = f"ticket.create.{tipo_ticket}"
        payload_hash = None
        if idempotency_identity:
            normalized_key, normalized_tenant_id = idempotency_identity
            payload_tenant_id = ticket_data.get("tenant_id")
            if payload_tenant_id is not None:
                try:
                    payload_tenant_id = int(payload_tenant_id)
                except (TypeError, ValueError) as exc:
                    raise TicketIdempotencyValidationError(
                        "Ticket payload has an invalid tenant_id."
                    ) from exc
                if payload_tenant_id != normalized_tenant_id:
                    raise TicketIdempotencyValidationError(
                        "Ticket payload tenant_id does not match its idempotency tenant."
                    )
            ticket_data["tenant_id"] = normalized_tenant_id

        if tipo_ticket == "municipio":
            ticket_data = normalize_municipio_ticket_write_scope(ticket_data)

        if idempotency_identity:
            normalized_key, normalized_tenant_id = idempotency_identity
            payload_hash = canonical_ticket_payload_hash(
                effect_kind,
                self._ticket_payload_for_hash(ticket_data),
            )
            existing_receipt = self._find_effect_receipt(
                normalized_tenant_id,
                normalized_key,
            )
            if existing_receipt is not None:
                return self._replay_ticket_effect(
                    existing_receipt,
                    tipo_ticket=tipo_ticket,
                    effect_kind=effect_kind,
                    payload_hash=payload_hash,
                    return_object=return_object,
                )

        ticket_data["nro_ticket"] = random.randint(100000, 999999)
        effects_queued = False
        try:
            ticket = creator.create(ticket_data)
            db.session.add(ticket)
            db.session.flush() # flush para obtener el ID del ticket para el comentario

            if (
                self.auto_assign_enabled
                and tipo_ticket == "municipio"
                and isinstance(ticket, MunicipioTicket)
            ):
                try:
                    self.asignar_ticket_municipal(ticket, auto=True)
                except Exception:
                    logger.exception("No se pudo asignar automáticamente el ticket municipal")

            # Si viene un comentario opcional, lo agregamos
            if ticket_data.get("comentario"):
                comentario = TicketComentario(
                    comentario=ticket_data.get("comentario"),
                    user_id=ticket_data.get("user_id"),
                    es_admin=False
                )
                if tipo_ticket == "municipio":
                    comentario.municipio_ticket = ticket
                else:
                    comentario.pyme_ticket = ticket
                db.session.add(comentario)

            if idempotency_identity:
                normalized_key, normalized_tenant_id = idempotency_identity
                db.session.add(
                    TicketDomainEffectReceipt(
                        tenant_id=normalized_tenant_id,
                        idempotency_key=normalized_key,
                        effect_kind=effect_kind,
                        payload_hash=payload_hash,
                        resource_type=f"{tipo_ticket}_ticket",
                        resource_id=ticket.id,
                        result_json={
                            "id": ticket.id,
                            "nro_ticket": (
                                str(ticket.nro_ticket)
                                if tipo_ticket == "municipio"
                                else ticket.nro_ticket
                            ),
                            "tipo_ticket": tipo_ticket,
                        },
                    )
                )

            # Canary tenants stage every external ticket-created effect in the
            # same transaction as the ticket.  The helper deliberately does
            # not commit: a staging/configuration failure must roll the whole
            # domain write back instead of silently falling through to a
            # best-effort direct send.
            try:
                from flask import has_app_context

                if has_app_context():
                    from services.ticket_domain_effects import stage_ticket_created_effects

                    effects_queued = stage_ticket_created_effects(
                        ticket,
                        tipo_ticket=tipo_ticket,
                        expected_owner_id=(
                            ticket_data.get("municipio_id")
                            if tipo_ticket == "municipio"
                            else ticket_data.get("pyme_id")
                        ),
                        session=db.session,
                    )
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Ticket effect staging failed type=%s tenant_id=%s",
                    tipo_ticket,
                    getattr(ticket, "tenant_id", None),
                )
                raise

            db.session.commit()
            # Ticket models contain phone numbers, email, DNI, free-form text
            # and the public tracking PIN.  Logging ``__dict__`` exposed all of
            # that in provider logs.  Keep only operational identifiers and
            # state; incident correlation does not require citizen PII.
            logger.info(
                "Ticket persisted id=%s number=%s type=%s tenant_id=%s municipio_id=%s status=%s",
                getattr(ticket, "id", None),
                getattr(ticket, "nro_ticket", None),
                tipo_ticket,
                getattr(ticket, "tenant_id", None),
                getattr(ticket, "municipio_id", None),
                getattr(ticket, "estado", None),
            )

            if effects_queued:
                logger.info(
                    "Ticket external effects queued id=%s type=%s tenant_id=%s",
                    ticket.id,
                    tipo_ticket,
                    getattr(ticket, "tenant_id", None),
                )
                # The database poller remains authoritative.  Celery is only a
                # best-effort post-commit wakeup, so a broker failure must not
                # roll back or duplicate the already committed ticket/effects.
                try:
                    from services.domain_effect_worker import (
                        enqueue_domain_effect_dispatch,
                    )

                    enqueue_domain_effect_dispatch(
                        tenant_id=int(getattr(ticket, "tenant_id")),
                    )
                except Exception as exc:
                    logger.warning(
                        "Ticket effect wakeup failed id=%s tenant_id=%s error_type=%s",
                        ticket.id,
                        getattr(ticket, "tenant_id", None),
                        type(exc).__name__,
                    )
            else:
                # Integración con SIGEM para tickets municipales
                if tipo_ticket == "municipio" and isinstance(ticket, MunicipioTicket):
                    try:
                        sigem_success = enviar_ticket_a_sigem(ticket)
                        if sigem_success:
                            logger.info(f"Ticket #{ticket.nro_ticket} enviado a SIGEM exitosamente.")
                        else:
                            logger.warning(f"Ticket #{ticket.nro_ticket} NO pudo ser enviado a SIGEM (función devolvió False).")
                    except Exception as exc:
                        # Loggear el error pero no revertir la creación local del ticket.
                        # La integración externa no debe impedir el funcionamiento primario.
                        logger.error(
                            "Error durante el envío de ticket a SIGEM ticket_id=%s error_type=%s",
                            ticket.id,
                            type(exc).__name__,
                        )

                # Notificaciones centralizadas (email, whatsapp, admin)
                # notification_dispatcher no tiene un metodo especifico para tickets aun,
                # pero podemos adaptar o llamar a _notificar_ticket_por_email por ahora
                # y extender dispatcher despues.
                # Para mantener consistencia con el pedido del usuario:
                self._notificar_ticket_por_email(ticket, tipo_ticket, ticket_data)

            # Notificar panel en tiempo real
            # This logic was moved to the action handlers to avoid circular imports
            # try:
            #     from routes.ticket import serialize_ticket_to_json # Importar la nueva función
            #
            #     # Serializar el ticket completo para la notificación
            #     ticket_json = serialize_ticket_to_json(ticket, tipo_ticket)
            #
            #     # El evento 'ticket_update' ahora enviará el objeto de ticket completo
            #     emit_ticket_update(ticket_json)
            #
            # except Exception as e_notify:
            #     logger.error(f"Error enviando notificación en tiempo real para ticket #{ticket.nro_ticket}: {e_notify}", exc_info=True)

            if return_object:
                return ticket
            return self._serialize_ticket(ticket, tipo_ticket)
        except IntegrityError as e:
            db.session.rollback()
            if idempotency_identity and payload_hash:
                normalized_key, normalized_tenant_id = idempotency_identity
                winning_receipt = self._find_effect_receipt(
                    normalized_tenant_id,
                    normalized_key,
                )
                if winning_receipt is not None:
                    return self._replay_ticket_effect(
                        winning_receipt,
                        tipo_ticket=tipo_ticket,
                        effect_kind=effect_kind,
                        payload_hash=payload_hash,
                        return_object=return_object,
                    )
            logger.error(
                "Integrity error al crear ticket: %s",
                type(e).__name__,
            )
            return None
        except SQLAlchemyError as exc:
            db.session.rollback()
            logger.error(
                "Error de DB al crear ticket error_type=%s",
                type(exc).__name__,
            )
            return None

    def _notificar_ticket_por_email(self, ticket, tipo_ticket: str, ticket_data: Dict[str, Any]) -> None:
        """Envía notificaciones por email al administrador y al cliente si corresponde."""
        if not ticket:
            return

        try:
            from services.email_service import (
                enviar_email_ticket_admin,
                enviar_email_ticket_cliente,
            )

            admin_user = None
            owner_id = None

            if tipo_ticket == "municipio":
                owner_id = ticket_data.get("municipio_id") or getattr(ticket, "municipio_id", None)
            elif tipo_ticket == "pyme":
                owner_id = ticket_data.get("pyme_id")

            if owner_id:
                try:
                    admin_user = db.session.get(User, owner_id)
                except Exception:  # pragma: no cover - defensive, should not happen in tests
                    admin_user = None

            if (
                not admin_user
                and tipo_ticket == "pyme"
                and getattr(ticket, "rubro_id", None)
                and hasattr(User, "query")
            ):
                try:
                    admin_user = User.query.filter_by(rubro_id=ticket.rubro_id).first()
                except Exception:  # pragma: no cover - defensive fallback
                    admin_user = None

            enviar_email_ticket_admin(
                ticket,
                admin_user=admin_user,
                tipo_ticket=tipo_ticket,
                ticket_data=ticket_data,
            )
            enviar_email_ticket_cliente(
                ticket,
                tipo_ticket=tipo_ticket,
                admin_user=admin_user,
                ticket_data=ticket_data,
            )
        except Exception as e:  # pragma: no cover - logging only
            logger.error(
                f"Error enviando notificaciones por email para ticket {getattr(ticket, 'id', 'N/A')}: {e}",
                exc_info=True,
            )

    def crear_comentario(
        self,
        ticket_id: int,
        tipo_ticket: Literal["municipio", "pyme"],
        comentario_data: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        idempotency_tenant_id: Optional[int] = None,
        legacy_effects_owned_by_caller: bool = False,
        reply_actor: User | None = None,
    ) -> Union[TicketComentario, None]:
        """Persist one comment and stage canary effects in the same transaction.

        ``legacy_effects_owned_by_caller`` only suppresses the post-commit
        direct-send fallback. It never suppresses durable outbox staging, so a
        caller can preserve an existing legacy dispatcher without duplicating
        effects for canary tenants.
        """
        if tipo_ticket not in {"municipio", "pyme"}:
            raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'.")

        comentario_data = dict(comentario_data or {})
        idempotency_identity = self._prepare_idempotency_identity(
            idempotency_key,
            idempotency_tenant_id,
        )
        effect_kind = f"ticket.comment.{tipo_ticket}"
        payload_hash = None
        if idempotency_identity:
            normalized_key, normalized_tenant_id = idempotency_identity
            payload_hash = canonical_ticket_payload_hash(
                effect_kind,
                self._comment_payload_for_hash(ticket_id, tipo_ticket, comentario_data),
            )
            existing_receipt = self._find_effect_receipt(
                normalized_tenant_id,
                normalized_key,
            )
            if existing_receipt is not None:
                return self._replay_comment_effect(
                    existing_receipt,
                    effect_kind=effect_kind,
                    payload_hash=payload_hash,
                )

        TicketModel = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        if reply_actor is not None:
            ticket = (
                db.session.query(TicketModel)
                .filter(TicketModel.id == ticket_id)
                .with_for_update()
                .populate_existing()
                .one_or_none()
            )
        else:
            ticket = db.session.get(TicketModel, ticket_id)
        if not ticket:
            return None
        if reply_actor is not None:
            _assert_locked_reply_owner(reply_actor, getattr(ticket, "asignado_a_id", None))
        attachment_id = comentario_data.get("archivo_adjunto_id")
        if attachment_id is not None:
            try:
                attachment_id = int(attachment_id)
            except (TypeError, ValueError):
                return None
            attachment = db.session.get(ArchivoAdjunto, attachment_id)
            if attachment is None:
                return None
            if tipo_ticket == "municipio":
                attachment_matches = (
                    attachment.municipio_ticket_id == ticket.id
                    and attachment.pyme_ticket_id is None
                )
            else:
                attachment_matches = (
                    attachment.pyme_ticket_id == ticket.id
                    and attachment.municipio_ticket_id is None
                )
            if not attachment_matches:
                logger.warning(
                    "Rejected cross-ticket attachment comment ticket_type=%s ticket_id=%s attachment_id=%s",
                    tipo_ticket,
                    ticket_id,
                    attachment_id,
                )
                return None
            comentario_data["archivo_adjunto_id"] = attachment_id
        if idempotency_identity:
            normalized_key, normalized_tenant_id = idempotency_identity
            ticket_tenant_id = getattr(ticket, "tenant_id", None)
            try:
                ticket_tenant_id = int(ticket_tenant_id)
            except (TypeError, ValueError) as exc:
                raise TicketIdempotencyValidationError(
                    "A durable comment requires a tenant-scoped ticket."
                ) from exc
            if ticket_tenant_id != normalized_tenant_id:
                raise TicketIdempotencyValidationError(
                    "Ticket tenant_id does not match its comment idempotency tenant."
                )
        comment_effects_queued = False
        try:
            nuevo_comentario = TicketComentario(
                comentario=comentario_data.get("comentario"),
                user_id=comentario_data.get("user_id"),
                anon_id=comentario_data.get("anon_id"),
                es_admin=comentario_data.get("es_admin", False),
                archivo_adjunto_id=comentario_data.get("archivo_adjunto_id"), # Add the new field
                origen=comentario_data.get("origen", "chat"), # Guardar el origen
                estado_ticket=comentario_data.get("estado_ticket"),
            )
            if tipo_ticket == "municipio":
                nuevo_comentario.municipio_ticket = ticket
            else:
                nuevo_comentario.pyme_ticket = ticket
            db.session.add(nuevo_comentario)
            if hasattr(ticket, "ultima_actividad"):
                ticket.ultima_actividad = get_local_now()
            db.session.flush()
            if idempotency_identity:
                normalized_key, normalized_tenant_id = idempotency_identity
                db.session.add(
                    TicketDomainEffectReceipt(
                        tenant_id=normalized_tenant_id,
                        idempotency_key=normalized_key,
                        effect_kind=effect_kind,
                        payload_hash=payload_hash,
                        resource_type="ticket_comentario",
                        resource_id=nuevo_comentario.id,
                        result_json={
                            "id": nuevo_comentario.id,
                            "ticket_id": int(ticket_id),
                            "tipo_ticket": tipo_ticket,
                        },
                    )
                )

            try:
                from flask import has_app_context

                if has_app_context():
                    from services.ticket_domain_effects import (
                        stage_ticket_comment_effects,
                    )

                    comment_effects_queued = stage_ticket_comment_effects(
                        nuevo_comentario,
                        ticket,
                        tipo_ticket=tipo_ticket,
                        emit_notifications=bool(
                            comentario_data.get("emit_notifications", True)
                        ),
                        emit_socket=bool(comentario_data.get("emit_socket", True)),
                        requested_channels=comentario_data.get(
                            "requested_channels"
                        ),
                        session=db.session,
                    )
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Ticket comment effect staging failed type=%s tenant_id=%s",
                    tipo_ticket,
                    getattr(ticket, "tenant_id", None),
                )
                raise
            db.session.commit()
            should_notify = bool(
                comentario_data.get("emit_notifications", True)
            ) and not legacy_effects_owned_by_caller
            if comment_effects_queued:
                logger.info(
                    "Ticket comment external effects queued comment_id=%s ticket_id=%s "
                    "type=%s tenant_id=%s",
                    nuevo_comentario.id,
                    ticket.id,
                    tipo_ticket,
                    getattr(ticket, "tenant_id", None),
                )
                # The database poller is authoritative; this only reduces
                # notification latency after the transaction is durable.
                try:
                    from services.domain_effect_worker import (
                        enqueue_domain_effect_dispatch,
                    )

                    enqueue_domain_effect_dispatch(
                        tenant_id=int(getattr(ticket, "tenant_id")),
                    )
                except Exception as exc:
                    logger.warning(
                        "Ticket comment effect wakeup failed comment_id=%s "
                        "tenant_id=%s error_type=%s",
                        nuevo_comentario.id,
                        getattr(ticket, "tenant_id", None),
                        type(exc).__name__,
                    )
            elif should_notify:
                try:
                    from services.email_service import (
                        enviar_email_ticket_novedad,
                        enviar_sms_ticket_novedad,
                        enviar_whatsapp_ticket_novedad, # <--- IMPORTAR NUEVA FUNCIÓN
                        enviar_email_ticket_admin,
                    )
                    comentario_texto = comentario_data.get('comentario', '') or ''
                    mensaje_notificacion = f"Nuevo comentario en tu ticket #{ticket.nro_ticket}: {comentario_texto[:50]}..."
                    mensaje_completo = comentario_texto.strip() or mensaje_notificacion
                    if nuevo_comentario.es_admin: # Notificar al usuario/cliente
                        enviar_email_ticket_novedad(
                            ticket,
                            mensaje_completo,
                            comentario_reciente=nuevo_comentario,
                        )
                        enviar_sms_ticket_novedad(ticket, mensaje_notificacion)
                        from services.ticket_domain_effects import (
                            pyme_whatsapp_chat_enabled,
                        )

                        if tipo_ticket == "municipio" or (
                            tipo_ticket == "pyme"
                            and pyme_whatsapp_chat_enabled()
                        ):
                            enviar_whatsapp_ticket_novedad(ticket, mensaje_notificacion)
                    else: # Notificar al admin/empleado
                        enviar_email_ticket_admin(
                            ticket,
                            tipo_ticket=tipo_ticket,
                            comentario_reciente=nuevo_comentario,
                            mensaje_resumen=mensaje_completo,
                        )
                except Exception as exc:  # pragma: no cover - not essential for tests
                    logger.error(
                        "Error enviando notificaciones tras crear comentario ticket_id=%s error_type=%s",
                        ticket.id if ticket else None,
                        type(exc).__name__,
                    )

            emit_socket = bool(
                comentario_data.get("emit_socket", True)
            ) and not legacy_effects_owned_by_caller
            # Emitir evento de socket para Live Chat (Admin Panel) solo cuando
            # la ruta llamadora no emite un evento normalizado propio.
            if emit_socket and not comment_effects_queued:
                try:
                    from socket_service import emit_new_chat_message

                    emit_new_chat_message({
                        "tenant_type": tipo_ticket,
                        "ticket_id": ticket_id,
                        "tenant_profile_id": getattr(ticket, "tenant_id", None),
                        "municipio_id": getattr(ticket, "municipio_id", None),
                        "message": nuevo_comentario.to_dict(),
                    })

                except Exception as exc:
                    logger.error(
                        "Error emitting socket event for ticket comment ticket_id=%s error_type=%s",
                        ticket_id,
                        type(exc).__name__,
                    )

            return nuevo_comentario
        except IntegrityError as e:
            db.session.rollback()
            if idempotency_identity and payload_hash:
                normalized_key, normalized_tenant_id = idempotency_identity
                winning_receipt = self._find_effect_receipt(
                    normalized_tenant_id,
                    normalized_key,
                )
                if winning_receipt is not None:
                    return self._replay_comment_effect(
                        winning_receipt,
                        effect_kind=effect_kind,
                        payload_hash=payload_hash,
                    )
            logger.error(
                "Integrity error al crear comentario: %s",
                type(e).__name__,
            )
            return None
        except SQLAlchemyError as exc:
            db.session.rollback()
            logger.error(
                "Error de DB al crear comentario error_type=%s",
                type(exc).__name__,
            )
            return None

    def crear_respuesta_municipio(
        self,
        ticket: MunicipioTicket,
        reply_data: Dict[str, Any],
        *,
        idempotency_key: str,
        idempotency_tenant_id: int,
        reply_actor: User,
    ) -> Dict[str, Any]:
        """Persist and queue one tenant-bound municipal WhatsApp reply."""

        if not isinstance(ticket, MunicipioTicket):
            raise TicketIdempotencyValidationError(
                "A MunicipioTicket reply requires a MunicipioTicket aggregate."
            )
        reply_data = dict(reply_data or {})
        idempotency_identity = self._prepare_idempotency_identity(
            idempotency_key,
            idempotency_tenant_id,
        )
        if idempotency_identity is None:
            raise TicketIdempotencyValidationError(
                "A MunicipioTicket reply requires an idempotency identity."
            )
        normalized_key, normalized_tenant_id = idempotency_identity
        if int(getattr(ticket, "tenant_id", 0) or 0) != normalized_tenant_id:
            raise TicketIdempotencyValidationError(
                "MunicipioTicket tenant_id does not match its reply tenant."
            )
        if (
            reply_actor is None
            or not getattr(reply_actor, "id", None)
            or int(getattr(reply_actor, "tenant_id", 0) or 0)
            != normalized_tenant_id
            or int(reply_data.get("actor_user_id") or 0) != int(reply_actor.id)
        ):
            raise TicketReplyOwnershipError("ticket_reply_actor_scope_mismatch")

        body = str(reply_data.get("body") or "").strip()
        if not body:
            raise TicketIdempotencyValidationError(
                "A MunicipioTicket reply requires a non-empty body."
            )
        raw_visibility = reply_data.get("visibility", "public")
        if not isinstance(raw_visibility, str) or raw_visibility.strip().lower() != "public":
            raise TicketIdempotencyValidationError(
                "MunicipioTicket WhatsApp replies must be public."
            )
        visibility = "public"

        from services.tenant_ticket_reply_delivery import (
            TenantTicketReplyDeliveryError,
            normalize_template_variables,
        )

        raw_channels = reply_data.get("requested_channels") or []
        if not isinstance(raw_channels, (list, tuple)):
            raise TenantTicketReplyDeliveryError(
                "municipio_ticket_reply_channels_invalid"
            )
        requested_channels: list[str] = []
        for raw_channel in raw_channels:
            if not isinstance(raw_channel, str):
                raise TenantTicketReplyDeliveryError(
                    "municipio_ticket_reply_channels_invalid"
                )
            channel = raw_channel.strip().lower()
            if channel not in requested_channels:
                requested_channels.append(channel)
        if requested_channels != ["whatsapp"]:
            raise TenantTicketReplyDeliveryError(
                "municipio_ticket_reply_channels_invalid"
            )

        raw_template_registry_id = reply_data.get("template_registry_id")
        template_registry_id = None
        if raw_template_registry_id not in (None, ""):
            try:
                template_registry_id = int(raw_template_registry_id)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TenantTicketReplyDeliveryError(
                    "whatsapp_template_registry_invalid"
                ) from exc
            if isinstance(raw_template_registry_id, bool) or template_registry_id <= 0:
                raise TenantTicketReplyDeliveryError(
                    "whatsapp_template_registry_invalid"
                )
        template_variables = normalize_template_variables(
            reply_data.get("template_variables")
        )

        from flask import current_app, has_app_context
        from services.domain_effect_gate import resolve_domain_effect_outbox_policy

        if not has_app_context():
            raise TenantTicketReplyDeliveryError(
                "domain_effect_app_context_required"
            )
        outbox_policy = resolve_domain_effect_outbox_policy(
            current_app.config,
            tenant_id=normalized_tenant_id,
        )
        if not outbox_policy.enabled:
            raise TenantTicketReplyDeliveryError(
                "whatsapp_outbox_cutover_required"
            )

        emit_socket = bool(reply_data.get("emit_socket", True))
        effect_kind = "ticket.comment.municipio"
        payload_hash = canonical_ticket_payload_hash(
            effect_kind,
            {
                "ticket_id": int(ticket.id),
                "source_model": "MunicipioTicket",
                "body": body,
                "visibility": visibility,
                "actor_user_id": int(reply_actor.id),
                "requested_channels": requested_channels,
                "template_registry_id": template_registry_id,
                "template_variables": template_variables,
                "emit_socket": emit_socket,
            },
        )
        existing_receipt = self._find_effect_receipt(
            normalized_tenant_id,
            normalized_key,
        )
        if existing_receipt is not None:
            return self._replay_municipio_reply_effect(
                existing_receipt,
                payload_hash=payload_hash,
            )

        locked_ticket = (
            MunicipioTicket.query.filter_by(
                id=ticket.id,
                tenant_id=normalized_tenant_id,
            )
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if locked_ticket is None:
            raise TicketIdempotencyReplayUnavailable(
                "MunicipioTicket is unavailable while persisting its reply."
            )
        ticket = locked_ticket
        tenant_profile = db.session.get(TenantProfile, normalized_tenant_id)
        if (
            tenant_profile is None
            or str(getattr(tenant_profile, "tipo", "")).strip() != "municipio"
            or not bool(getattr(tenant_profile, "is_active", False))
            or int(getattr(tenant_profile, "municipio_id", 0) or 0)
            != int(getattr(ticket, "municipio_id", 0) or 0)
        ):
            raise TicketReplyOwnershipError("ticket_tenant_binding_invalid")
        _assert_locked_reply_owner(reply_actor, ticket.asignado_a_id)
        if (
            not actor_can_assign_tickets(reply_actor)
            and not ticket_assignee_is_compatible(reply_actor, ticket)
        ):
            raise TicketReplyOwnershipError("assignee_category_scope_mismatch")

        from services.tenant_twilio_messaging import (
            resolve_tenant_twilio_sender_snapshot,
        )
        from services.tenant_ticket_reply_delivery import (
            prepare_whatsapp_reply_policy,
        )
        from utils.validators import normalize_phone

        normalized_phone = normalize_phone(
            str(getattr(ticket, "telefono_vecino", None) or "").strip()
        )
        if not normalized_phone:
            raise TenantTicketReplyDeliveryError("contact_phone_invalid")
        sender_snapshot = resolve_tenant_twilio_sender_snapshot(
            tenant_id=normalized_tenant_id,
            channel="whatsapp",
            session=db.session,
        )
        if sender_snapshot.reason_code or sender_snapshot.sender is None:
            raise TenantTicketReplyDeliveryError(
                sender_snapshot.reason_code
                or "whatsapp_tenant_sender_resolution_invalid"
            )
        (
            template_registry_id,
            template_variables,
            whatsapp_policy_snapshot,
        ) = prepare_whatsapp_reply_policy(
            tenant_id=normalized_tenant_id,
            provider_sender_id=int(sender_snapshot.sender.id),
            provider_sender_binding=sender_snapshot.binding,
            recipient=normalized_phone,
            template_registry_id=template_registry_id,
            template_variables=template_variables,
            session=db.session,
            dispatch_enabled=True,
        )
        content_source = "operator_free_form"
        if template_registry_id is not None:
            delivery_binding = (
                whatsapp_policy_snapshot.get("_delivery_binding")
                if isinstance(whatsapp_policy_snapshot, dict)
                else None
            )
            delivery_body = str(
                (delivery_binding or {}).get("delivery_body_snapshot") or ""
            ).strip()
            if not delivery_body:
                raise TenantTicketReplyDeliveryError(
                    "whatsapp_template_body_snapshot_missing"
                )
            if body != delivery_body:
                raise TenantTicketReplyDeliveryError(
                    "whatsapp_template_body_mismatch"
                )
            body = delivery_body
            content_source = "approved_whatsapp_template"

        event_created_at = get_local_now()
        event_id = uuid.uuid4().hex
        aggregate_ref = f"{ticket.id}:{event_id}"
        event = {
            "id": event_id,
            "origin": "admin_panel",
            "action": "reply",
            "body": body,
            "content_source": content_source,
            "visibility": visibility,
            "created_at": datetime_to_iso_utc(event_created_at),
            "actor": {
                "id": int(reply_actor.id),
                "name": str(reply_actor.name or "").strip(),
                "role": str(reply_actor.rol or "").strip(),
            },
        }
        try:
            comment = TicketComentario(
                municipio_ticket_id=ticket.id,
                comentario=body,
                user_id=int(reply_actor.id),
                es_admin=True,
                origen="admin_panel",
                fecha=event_created_at,
            )
            db.session.add(comment)
            if str(ticket.estado or "").strip().lower() in {"nuevo", "open"}:
                ticket.estado = "en_proceso"
            ticket.ultima_actividad = event_created_at
            db.session.add(ticket)
            db.session.flush()

            reply_record = MunicipioTicketReplyEvent(
                tenant_id=normalized_tenant_id,
                source_model="MunicipioTicket",
                ticket_id=ticket.id,
                comment_id=comment.id,
                event_id=event_id,
                body=body,
                visibility=visibility,
                actor_user_id=int(reply_actor.id),
                actor_name=str(reply_actor.name or "").strip() or "Operador",
                actor_role=str(reply_actor.rol or "").strip() or "empleado",
                recipient_phone=normalized_phone,
                whatsapp_template_registry_id=template_registry_id,
                whatsapp_template_variables=template_variables,
                whatsapp_policy_snapshot=whatsapp_policy_snapshot,
                whatsapp_delivery_status="saved",
                whatsapp_provider_sender_id=int(sender_snapshot.sender.id),
                whatsapp_status_updated_at=event_created_at,
                created_at=event_created_at,
            )
            db.session.add(reply_record)
            db.session.flush()
            receipt = TicketDomainEffectReceipt(
                tenant_id=normalized_tenant_id,
                idempotency_key=normalized_key,
                effect_kind=effect_kind,
                payload_hash=payload_hash,
                resource_type="ticket_comentario",
                resource_id=comment.id,
                result_json={
                    "ticket_id": ticket.id,
                    "comment_id": comment.id,
                    "event_id": event_id,
                    "reply_event_record_id": reply_record.id,
                    "aggregate_ref": aggregate_ref,
                    "source_model": "MunicipioTicket",
                },
            )
            db.session.add(receipt)
            db.session.add(
                AuditEvent(
                    tenant_id=normalized_tenant_id,
                    actor_user_id=int(reply_actor.id),
                    event_type="municipio_ticket.reply.whatsapp.queued",
                    resource_type="municipio_ticket_reply_event",
                    resource_id=str(reply_record.id),
                    details={
                        "contract_version": (
                            MunicipioTicketReplyEvent.DELIVERY_CONTRACT_VERSION
                        ),
                        "source_model": "MunicipioTicket",
                        "ticket_id": ticket.id,
                        "comment_id": comment.id,
                        "event_id": event_id,
                        "provider_sender_id": int(sender_snapshot.sender.id),
                        "template_registry_id": template_registry_id,
                        "service_window_status": whatsapp_policy_snapshot.get(
                            "status"
                        ),
                        "idempotency_key_sha256": hashlib.sha256(
                            normalized_key.encode("utf-8")
                        ).hexdigest(),
                    },
                )
            )
            db.session.flush()
            from services.ticket_domain_effects import (
                stage_municipio_ticket_reply_effects,
            )

            effects_queued = stage_municipio_ticket_reply_effects(
                ticket,
                reply_record,
                requested_channels=requested_channels,
                emit_socket=emit_socket,
                session=db.session,
            )
            if not effects_queued:
                raise TenantTicketReplyDeliveryError(
                    "whatsapp_outbox_cutover_required"
                )
            reply_record.whatsapp_delivery_status = "queued"
            reply_record.whatsapp_status_updated_at = event_created_at
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            winning_receipt = self._find_effect_receipt(
                normalized_tenant_id,
                normalized_key,
            )
            if winning_receipt is not None:
                return self._replay_municipio_reply_effect(
                    winning_receipt,
                    payload_hash=payload_hash,
                )
            raise
        except TenantTicketReplyDeliveryError:
            db.session.rollback()
            raise
        except Exception:
            db.session.rollback()
            logger.exception(
                "MunicipioTicket reply persistence failed tenant_id=%s ticket_id=%s",
                normalized_tenant_id,
                getattr(ticket, "id", None),
            )
            raise

        try:
            from services.domain_effect_worker import enqueue_domain_effect_dispatch

            enqueue_domain_effect_dispatch(tenant_id=normalized_tenant_id)
        except Exception as exc:
            logger.warning(
                "MunicipioTicket reply outbox wakeup failed tenant_id=%s ticket_id=%s error_type=%s",
                normalized_tenant_id,
                ticket.id,
                type(exc).__name__,
            )
        return {
            "ticket": ticket,
            "comment": comment,
            "event": event,
            "aggregate_ref": aggregate_ref,
            "reply_record": reply_record,
            "replayed": False,
            "effects_queued": True,
        }

    def crear_respuesta_tenant(
        self,
        ticket: TenantTicket,
        reply_data: Dict[str, Any],
        *,
        idempotency_key: str,
        idempotency_tenant_id: int,
        reply_actor: User | None = None,
    ) -> Dict[str, Any]:
        """Persist one TenantTicket operator reply and its durable effects.

        The timeline mutation, tenant-scoped receipt and optional outbox rows
        share one transaction.  The receipt is global within the tenant, so a
        retry cannot duplicate a reply or reuse the same client identity for a
        different ticket.
        """

        if not isinstance(ticket, TenantTicket):
            raise TicketIdempotencyValidationError(
                "A TenantTicket reply requires a TenantTicket aggregate."
            )
        reply_data = dict(reply_data or {})
        idempotency_identity = self._prepare_idempotency_identity(
            idempotency_key,
            idempotency_tenant_id,
        )
        if idempotency_identity is None:
            raise TicketIdempotencyValidationError(
                "A TenantTicket reply requires an idempotency identity."
            )
        normalized_key, normalized_tenant_id = idempotency_identity
        if int(getattr(ticket, "tenant_id", 0) or 0) != normalized_tenant_id:
            raise TicketIdempotencyValidationError(
                "TenantTicket tenant_id does not match its reply idempotency tenant."
            )

        body = str(reply_data.get("body") or "").strip()
        if not body:
            raise TicketIdempotencyValidationError(
                "A TenantTicket reply requires a non-empty body."
            )
        raw_visibility = (
            reply_data["visibility"] if "visibility" in reply_data else "public"
        )
        if not isinstance(raw_visibility, str):
            raise TicketIdempotencyValidationError(
                "TenantTicket reply visibility must be public or internal."
            )
        visibility = raw_visibility.strip().lower()
        if visibility not in {"public", "internal"}:
            raise TicketIdempotencyValidationError(
                "TenantTicket reply visibility must be public or internal."
            )
        from services.tenant_ticket_reply_delivery import (
            TenantTicketReplyDeliveryError,
            normalize_template_variables,
        )

        raw_requested_channels = reply_data.get("requested_channels") or []
        if not isinstance(raw_requested_channels, (list, tuple)):
            raise TenantTicketReplyDeliveryError(
                "tenant_ticket_reply_channels_invalid"
            )
        requested_channels_set: set[str] = set()
        for raw_channel in raw_requested_channels:
            if not isinstance(raw_channel, str):
                raise TenantTicketReplyDeliveryError(
                    "tenant_ticket_reply_channels_invalid"
                )
            normalized_channel = raw_channel.strip().lower()
            if normalized_channel not in {"email", "whatsapp"}:
                raise TenantTicketReplyDeliveryError(
                    "tenant_ticket_reply_channels_invalid"
                )
            requested_channels_set.add(normalized_channel)
        requested_channels = sorted(requested_channels_set)

        raw_template_registry_id = reply_data.get("template_registry_id")
        template_registry_id = None
        if raw_template_registry_id not in (None, ""):
            try:
                template_registry_id = int(raw_template_registry_id)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TenantTicketReplyDeliveryError(
                    "whatsapp_template_registry_invalid"
                ) from exc
            if isinstance(raw_template_registry_id, bool) or template_registry_id <= 0:
                raise TenantTicketReplyDeliveryError(
                    "whatsapp_template_registry_invalid"
                )
        template_variables = normalize_template_variables(
            reply_data.get("template_variables")
        )
        if (template_registry_id is not None or template_variables is not None) and (
            "whatsapp" not in requested_channels
        ):
            raise TenantTicketReplyDeliveryError(
                "whatsapp_template_requires_whatsapp_channel"
            )
        whatsapp_dispatch_enabled = False
        if "whatsapp" in requested_channels:
            from flask import has_app_context

            if has_app_context():
                from flask import current_app
                from services.domain_effect_gate import (
                    resolve_domain_effect_outbox_policy,
                )

                whatsapp_dispatch_enabled = resolve_domain_effect_outbox_policy(
                    current_app.config,
                    tenant_id=normalized_tenant_id,
                ).enabled
        emit_socket = bool(reply_data.get("emit_socket", True))
        effect_kind = "ticket.comment.tenant"
        payload_hash = canonical_ticket_payload_hash(
            effect_kind,
            {
                "ticket_id": int(ticket.id),
                "body": body,
                "visibility": visibility,
                "actor_user_id": reply_data.get("actor_user_id"),
                "requested_channels": requested_channels,
                "template_registry_id": template_registry_id,
                "template_variables": template_variables,
                "emit_socket": emit_socket,
            },
        )
        existing_receipt = self._find_effect_receipt(
            normalized_tenant_id,
            normalized_key,
        )
        if existing_receipt is not None:
            return self._replay_tenant_reply_effect(
                existing_receipt,
                payload_hash=payload_hash,
                reply_data={
                    **reply_data,
                    "body": body,
                    "visibility": visibility,
                },
            )

        locked_ticket = (
            TenantTicket.query.filter_by(
                id=ticket.id,
                tenant_id=normalized_tenant_id,
            )
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if locked_ticket is None:
            raise TicketIdempotencyReplayUnavailable(
                "TenantTicket is unavailable while persisting its reply."
            )
        ticket = locked_ticket
        if reply_actor is not None:
            locked_extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
            _assert_locked_reply_owner(reply_actor, locked_extra.get("assignee_id"))

        event_id = uuid.uuid4().hex
        aggregate_ref = f"{ticket.id}:{event_id}"
        event_created_at = get_local_now()
        event = {
            "id": event_id,
            "origin": "admin_panel",
            "action": "reply",
            "body": body,
            "content_source": "operator_free_form",
            "visibility": visibility,
            "created_at": datetime_to_iso_utc(event_created_at),
            "actor": {
                "id": reply_data.get("actor_user_id"),
                "name": reply_data.get("actor_name"),
                "role": reply_data.get("actor_role"),
            },
        }

        effects_queued = False
        reply_record = None
        try:
            from services.ticket_domain_effects import tenant_ticket_reply_contact
            from utils.validators import normalize_phone

            contact = tenant_ticket_reply_contact(ticket)
            contact_email = str(contact.get("email") or "").strip().casefold()
            contact_phone = str(contact.get("phone") or "").strip()
            normalized_contact_phone = (
                normalize_phone(contact_phone) if contact_phone else None
            )
            whatsapp_policy_snapshot = None
            if "whatsapp" in requested_channels:
                if not contact_phone:
                    raise TenantTicketReplyDeliveryError(
                        "contact_phone_missing"
                    )
                if not normalized_contact_phone:
                    raise TenantTicketReplyDeliveryError(
                        "contact_phone_invalid"
                    )
                from services.tenant_ticket_reply_delivery import (
                    TenantTicketReplyDeliveryError,
                    prepare_whatsapp_reply_policy,
                )
                from services.tenant_twilio_messaging import (
                    resolve_tenant_twilio_sender_snapshot,
                )

                sender_snapshot = resolve_tenant_twilio_sender_snapshot(
                    tenant_id=normalized_tenant_id,
                    channel="whatsapp",
                    session=db.session,
                )
                if sender_snapshot.reason_code or sender_snapshot.sender is None:
                    raise TenantTicketReplyDeliveryError(
                        sender_snapshot.reason_code
                        or "whatsapp_tenant_sender_resolution_invalid"
                    )

                (
                    template_registry_id,
                    template_variables,
                    whatsapp_policy_snapshot,
                ) = prepare_whatsapp_reply_policy(
                    tenant_id=normalized_tenant_id,
                    provider_sender_id=int(sender_snapshot.sender.id),
                    provider_sender_binding=sender_snapshot.binding,
                    recipient=normalized_contact_phone,
                    template_registry_id=template_registry_id,
                    template_variables=template_variables,
                    session=db.session,
                    dispatch_enabled=whatsapp_dispatch_enabled,
                )
                if template_registry_id is not None:
                    delivery_binding = (
                        whatsapp_policy_snapshot.get("_delivery_binding")
                        if isinstance(whatsapp_policy_snapshot, dict)
                        else None
                    )
                    delivery_body = str(
                        (delivery_binding or {}).get("delivery_body_snapshot") or ""
                    ).strip()
                    if not delivery_body:
                        raise TenantTicketReplyDeliveryError(
                            "whatsapp_template_body_snapshot_missing"
                        )
                    if body != delivery_body:
                        raise TenantTicketReplyDeliveryError(
                            "whatsapp_template_body_mismatch"
                        )
                    event["body"] = delivery_body
                    event["content_source"] = "approved_whatsapp_template"
            extra = dict(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
            comments = list(extra.get("comments")) if isinstance(extra.get("comments"), list) else []
            comments.append(event)
            extra["comments"] = comments[-100:]
            ticket.datos_extra = extra
            if visibility == "public":
                from services.v2.sla_service import (
                    apply_operator_response_sla,
                    get_policies_for_tenant,
                    is_sla_operator_role,
                )

                if is_sla_operator_role(reply_data.get("actor_role")):
                    tenant_profile = db.session.get(TenantProfile, normalized_tenant_id)
                    if tenant_profile is None:
                        raise TicketIdempotencyValidationError(
                            "TenantTicket SLA response requires an existing tenant."
                        )
                    apply_operator_response_sla(
                        ticket,
                        get_policies_for_tenant(tenant_profile),
                        occurred_at=event_created_at,
                    )
            flag_modified(ticket, "datos_extra")
            if str(ticket.estado or "").strip().lower() in {"nuevo", "open"}:
                ticket.estado = "en_proceso"
            ticket.updated_at = get_local_now()
            db.session.add(ticket)
            reply_record = TenantTicketReplyEvent(
                tenant_id=normalized_tenant_id,
                ticket_id=ticket.id,
                event_id=event_id,
                body=str(event["body"]),
                visibility=visibility,
                actor_user_id=reply_data.get("actor_user_id"),
                actor_name=str(reply_data.get("actor_name") or "").strip() or None,
                actor_role=str(reply_data.get("actor_role") or "").strip() or None,
                recipient_email=(
                    contact_email or None
                    if "email" in requested_channels
                    else None
                ),
                recipient_phone=(
                    normalized_contact_phone
                    if "whatsapp" in requested_channels
                    else None
                ),
                whatsapp_template_registry_id=template_registry_id,
                whatsapp_template_variables=template_variables,
                whatsapp_policy_snapshot=whatsapp_policy_snapshot,
                whatsapp_delivery_status="saved",
                whatsapp_status_updated_at=event_created_at,
                created_at=event_created_at,
            )
            db.session.add(reply_record)
            db.session.flush()
            receipt = TicketDomainEffectReceipt(
                tenant_id=normalized_tenant_id,
                idempotency_key=normalized_key,
                effect_kind=effect_kind,
                payload_hash=payload_hash,
                resource_type="tenant_ticket",
                resource_id=ticket.id,
                result_json={
                    "ticket_id": ticket.id,
                    "event_id": event_id,
                    "reply_event_record_id": reply_record.id,
                    "aggregate_ref": aggregate_ref,
                    "source_model": "TenantTicket",
                },
            )
            db.session.add(receipt)
            db.session.flush()

            from flask import has_app_context

            if has_app_context():
                from services.ticket_domain_effects import (
                    stage_tenant_ticket_reply_effects,
                )

                effects_queued = stage_tenant_ticket_reply_effects(
                    ticket,
                    reply_record,
                    requested_channels=requested_channels,
                    emit_socket=emit_socket,
                    session=db.session,
                )
                if effects_queued and "whatsapp" in requested_channels:
                    reply_record.whatsapp_delivery_status = "queued"
                    reply_record.whatsapp_status_updated_at = event_created_at
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            winning_receipt = self._find_effect_receipt(
                normalized_tenant_id,
                normalized_key,
            )
            if winning_receipt is not None:
                return self._replay_tenant_reply_effect(
                    winning_receipt,
                    payload_hash=payload_hash,
                    reply_data={
                        **reply_data,
                        "body": body,
                        "visibility": visibility,
                    },
                )
            raise
        except TenantTicketReplyDeliveryError:
            db.session.rollback()
            raise
        except Exception:
            db.session.rollback()
            logger.exception(
                "TenantTicket reply persistence failed tenant_id=%s ticket_id=%s",
                normalized_tenant_id,
                getattr(ticket, "id", None),
            )
            raise

        if effects_queued:
            try:
                from services.domain_effect_worker import enqueue_domain_effect_dispatch

                enqueue_domain_effect_dispatch(tenant_id=normalized_tenant_id)
            except Exception as exc:
                logger.warning(
                    "TenantTicket reply outbox wakeup failed tenant_id=%s ticket_id=%s error_type=%s",
                    normalized_tenant_id,
                    ticket.id,
                    type(exc).__name__,
                )
        return {
            "ticket": ticket,
            "event": event,
            "aggregate_ref": aggregate_ref,
            "reply_record": reply_record,
            "replayed": False,
            "effects_queued": effects_queued,
        }

    def guardar_encuesta(
        self,
        ticket_id: int,
        tipo_ticket: Literal["municipio", "pyme"],
        puntuacion: int,
        comentario: str | None = None,
    ) -> Union[TicketSatisfaccion, None]:
        if tipo_ticket not in {"municipio", "pyme"}:
            return None
        try:
            encuesta = TicketSatisfaccion(
                ticket_id=ticket_id,
                tipo=tipo_ticket,
                puntuacion=puntuacion,
                comentario=comentario,
            )
            db.session.add(encuesta)
            # db.session.commit() # <<< ELIMINADO
            return encuesta
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(
                f"Error de DB al guardar encuesta: {e}", exc_info=True
            )
            return None

    def obtener_locations_de_tickets(
        self,
        *,
        municipio_id: int,
        actor: User | None = None,
    ) -> list[dict]:
        """
        Devuelve una lista de coordenadas de todos los tickets para un municipio
        que tengan ubicación registrada. Formato: [{ "lat": lat, "lng": lng }]
        """
        try:
            resolution = resolve_unique_tenant_for_owner(municipio_id)
            if resolution.status != "unique" or resolution.tenant is None:
                return []
            query = apply_employee_ticket_category_scope(
                scoped_municipio_ticket_query(resolution.tenant),
                actor,
                MunicipioTicket,
            )
            tickets = (
                query
                .filter(MunicipioTicket.latitud.isnot(None), MunicipioTicket.longitud.isnot(None))
                .all()
            )
            return [{"lat": t.latitud, "lng": t.longitud} for t in tickets]
        except (SQLAlchemyError, TicketTenantScopeError) as e:
            logger.error(
                f"Error de DB al obtener locations de tickets para municipio {municipio_id}: {e}", exc_info=True
            )
            return []


    def obtener_tickets_con_ubicacion_para_mapa( # Nombre modificado
        self,
        tipo_ticket: Literal["municipio", "pyme"],
        *,
        actor: User | None = None,
        municipio_id: int | None = None,
        rubro_id: int | None = None,
        tenant_id: int | None = None,
        fecha_inicio: str | None = None,
        fecha_fin: str | None = None,
        categoria: str | Iterable[str] | None = None,
        distrito: str | None = None,
        estado: str | Iterable[str] | None = None, # Nuevo parámetro de estado
        satisfactorio: bool | None = None,
    ) -> list[dict]:
        """
        Devuelve los tickets con ubicación, opcionalmente filtrados por estado,
        agrupados por ubicación y con un peso (cantidad de tickets).
        Permite filtrar por municipio/rubro, rango de fechas y categoría.
        """
        if tipo_ticket not in {"municipio", "pyme"}:
            logger.warning("[TICKET_SERVICE_MAPA] Invalid ticket type: %s", tipo_ticket)
            return []
        Model = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        try:
            logger.info(
                "[TICKET_SERVICE_MAPA] tipo=%s municipio_id=%s rubro_id=%s tenant_id=%s fecha_inicio=%s fecha_fin=%s categoria=%s distrito=%s estado=%s",
                tipo_ticket,
                municipio_id,
                rubro_id,
                tenant_id,
                fecha_inicio,
                fecha_fin,
                categoria,
                distrito,
                estado,
            )

            query = Model.query.filter(Model.latitud.isnot(None), Model.longitud.isnot(None))
            query = apply_employee_ticket_category_scope(query, actor, Model)
            if tipo_ticket == "municipio":
                tenant = None
                if tenant_id is not None:
                    from models import TenantProfile

                    tenant = db.session.get(TenantProfile, tenant_id)
                elif municipio_id is not None:
                    resolution = resolve_unique_tenant_for_owner(municipio_id)
                    if resolution.status == "unique":
                        tenant = resolution.tenant
                if tenant is None:
                    logger.warning(
                        "[TICKET_SERVICE_MAPA] Municipal heatmap rejected without exact tenant scope"
                    )
                    return []
                query = scoped_municipio_ticket_query(tenant, query=query)

            distrito_filtrado = distrito.strip() if isinstance(distrito, str) else None
            if distrito_filtrado and hasattr(Model, "distrito"):
                query = query.filter(Model.distrito == distrito_filtrado)

            estados_filtrar_set: set[str] | None = None
            # Filtrar por estado si se proporciona. Si el estado solicitado es
            # "resuelto", también incluimos aquellos marcados como "cerrado" para
            # que el frontend pueda tratarlos como reclamos resueltos.
            if estado:
                if isinstance(estado, str):
                    estados_solicitados = [estado.strip()] if estado.strip() else []
                else:
                    estados_solicitados = [
                        valor.strip()
                        for valor in estado
                        if isinstance(valor, str) and valor.strip()
                    ]

                if estados_solicitados:
                    estados_expandidos: list[str] = []
                    for estado_solicitado in estados_solicitados:
                        if estado_solicitado == "resuelto":
                            estados_expandidos.extend(["resuelto", "cerrado"])
                        else:
                            estados_expandidos.append(estado_solicitado)

                    # Quitar duplicados preservando el orden
                    estados_unicos: list[str] = []
                    vistos_estados: set[str] = set()
                    for estado_unico in estados_expandidos:
                        if estado_unico not in vistos_estados:
                            estados_unicos.append(estado_unico)
                            vistos_estados.add(estado_unico)

                    if estados_unicos:
                        estados_filtrar_set = set(estados_unicos)
                        query = query.filter(Model.estado.in_(estados_unicos))
            # else: # Comportamiento por defecto si no se especifica estado (ej: no cerrados)
            #     query = query.filter(Model.estado != "cerrado") # Opcional: mantener un filtro por defecto

            # Si se solicita solo tickets satisfactorios, unimos con
            # TicketSatisfaccion para asegurar que exista una encuesta asociada.
            if satisfactorio:
                query = query.join(
                    TicketSatisfaccion,
                    (TicketSatisfaccion.ticket_id == Model.id)
                    & (TicketSatisfaccion.tipo == tipo_ticket),
                )

            if tipo_ticket == "pyme":
                if tenant_id is None:
                    logger.warning(
                        "[TICKET_SERVICE_MAPA] PyME heatmap rejected without exact tenant_id"
                    )
                    return []
                query = query.filter_by(tenant_id=tenant_id)

            if fecha_inicio:
                try:
                    query = query.filter(Model.fecha >= datetime.fromisoformat(fecha_inicio))
                except ValueError:
                    logger.warning(f"Formato de fecha_inicio inválido: {fecha_inicio}")
            if fecha_fin:
                try:
                    # Añadimos un día para incluir todo el día de fecha_fin
                    fecha_fin_dt = datetime.fromisoformat(fecha_fin) + timedelta(days=1) # Ajustar para incluir el día completo
                    query = query.filter(Model.fecha < fecha_fin_dt)
                except ValueError:
                    logger.warning(f"Formato de fecha_fin inválido: {fecha_fin}")

            categorias_filtrar_lower: list[str] = []
            # Check if model has a 'categoria' column before filtering by it
            has_categoria_column = hasattr(Model, 'categoria')

            # PymeTicket typically stores category in 'categoria' (String) as per schema,
            # but log error 'column pyme_ticket.categoria_id does not exist' suggests
            # something else might have been trying to join or filter by ID.
            # The code block below filters by `Model.categoria` string column.

            if categoria and has_categoria_column:
                if isinstance(categoria, str):
                    raw_values = [categoria]
                else:
                    raw_values = list(categoria)

                vistos: set[str] = set()
                for raw in raw_values:
                    if not isinstance(raw, str):
                        continue
                    texto = raw.strip()
                    if not texto:
                        continue
                    clave = texto.lower()
                    if clave in vistos:
                        continue
                    vistos.add(clave)
                    categorias_filtrar_lower.append(clave)

                if categorias_filtrar_lower:
                    query = query.filter(
                        db.func.lower(Model.categoria).in_(categorias_filtrar_lower)
                    )

            tickets = query.all()
            logger.info(
                "[TICKET_SERVICE_MAPA] tickets_raw=%s",
                len(tickets),
            )

            if distrito_filtrado and hasattr(Model, "distrito"):
                tickets = [
                    t for t in tickets if getattr(t, "distrito", None) == distrito_filtrado
                ]

            if estados_filtrar_set:
                tickets = [
                    t for t in tickets if getattr(t, "estado", None) in estados_filtrar_set
                ]

            if categorias_filtrar_lower and hasattr(Model, "categoria"):
                tickets = [
                    t
                    for t in tickets
                    if isinstance(getattr(t, "categoria", None), str)
                    and getattr(t, "categoria").strip().lower() in categorias_filtrar_lower
                ]

            # El agrupamiento por ubicación y el cálculo de 'weight' permanecen igual.
            # Si se desea devolver todos los puntos individualmente para que el frontend agrupe/clusterice:
            # return [
            #     {
            #         "id": t.id, "lat": t.latitud, "lng": t.longitud, "estado": t.estado,
            #         "asunto": t.asunto, "nro_ticket": t.nro_ticket, "categoria": t.categoria
            #     } for t in tickets
            # ]
            # Por ahora, mantendremos la agrupación existente que devuelve 'weight'.

            ubicaciones_agrupadas = {}  # (lat, lng, categoria) -> count
            categoria_ids_por_ubicacion: dict[tuple, set[int]] = {}

            for t in tickets:
                # Redondear lat/lng a un número de decimales para agrupar puntos cercanos.
                # Ajustar el número de decimales según la precisión deseada.
                # 5 decimales dan una precisión de ~1.1 metros.
                # 4 decimales dan una precisión de ~11 metros.
                # 3 decimales dan una precisión de ~110 metros.
                # Consideremos 4 decimales para agrupar problemáticas en una misma "zona pequeña".
                lat_lng_key = (
                    round(t.latitud, 4),
                    round(t.longitud, 4),
                    getattr(t, "categoria", None),
                )
                if lat_lng_key not in ubicaciones_agrupadas:
                    ubicaciones_agrupadas[lat_lng_key] = 0
                ubicaciones_agrupadas[lat_lng_key] += 1
                category_id = getattr(t, "categoria_id", None)
                if isinstance(category_id, int) and category_id > 0:
                    categoria_ids_por_ubicacion.setdefault(lat_lng_key, set()).add(
                        category_id
                    )

            resultado_heatmap = []
            for (lat, lng, cat), weight in ubicaciones_agrupadas.items():
                point = {
                    "location": {"lat": lat, "lng": lng},
                    "lat": lat,
                    "lng": lng,
                    "weight": weight,
                    "categoria": cat,
                }
                category_ids = categoria_ids_por_ubicacion.get((lat, lng, cat), set())
                if len(category_ids) == 1:
                    point["categoria_id"] = next(iter(category_ids))
                resultado_heatmap.append(point)
            enrich_heatmap_points(
                resultado_heatmap,
                property_keys=("categoria", "estado", "barrio", "fuente"),
            )
            logger.info(
                "[TICKET_SERVICE_MAPA] puntos_heatmap=%s ejemplo=%s",
                len(resultado_heatmap),
                resultado_heatmap[:3] if resultado_heatmap else [],
            )
            return resultado_heatmap
        except (SQLAlchemyError, TicketTenantScopeError) as e:
            logger.error(
                f"Error de DB al obtener tickets para mapa de calor: {e}", exc_info=True
            )
            return []

    def obtener_historial_chat(
        self,
        ticket: Union[MunicipioTicket, PymeTicket],
        *,
        include_internal: bool = True,
    ) -> list[dict]:
        """Devuelve el historial completo de conversación para un ticket.

        Combina el historial previo almacenado en ``Conversacion`` (pregunta/
        respuesta del bot) con los comentarios posteriores guardados en
        ``TicketComentario``. Cada entrada se normaliza con metadatos de autor
        para que el frontend pueda distinguir entre mensajes del municipio y
        del vecino.
        """
        mensajes: list[dict] = []

        # --- Conversaciones previas al ticket (chatbot) ---
        if getattr(ticket, "anon_id", None):
            try:
                conversaciones = (
                    Conversacion.query.filter_by(session_id=ticket.anon_id)
                    .order_by(Conversacion.timestamp.asc())
                    .all()
                )
            except Exception:
                conversaciones = []

            nombre_vecino = (
                getattr(ticket, "nombre_vecino", None)
                or getattr(ticket, "nombre_cliente", None)
                or "Vecino/a"
            )
            vecino_identity = build_identity_subject(
                display_name=nombre_vecino,
                anon_id=getattr(ticket, "anon_id", None),
                source_context="ticket_chat_history",
            )
            chatbot_identity = build_identity_subject(
                display_name="Chatbot",
                source_context="ticket_chatbot_message",
            )

            for conv in conversaciones:
                if conv.pregunta:
                    mensajes.append(
                        {
                            "texto": conv.pregunta,
                            "fecha": datetime_to_iso_utc(conv.timestamp),
                            "autor": "vecino",
                            "autor_nombre": nombre_vecino,
                            "actor_identity": vecino_identity,
                            "es_admin": False,
                        }
                    )
                if conv.respuesta:
                    # Añadir un pequeño delta para conservar el orden pregunta-respuesta
                    respuesta_fecha = datetime_to_iso_utc(
                        conv.timestamp + timedelta(milliseconds=1)
                    )
                    mensajes.append(
                        {
                            "texto": conv.respuesta,
                            "fecha": respuesta_fecha,
                            "autor": "municipio",
                            "autor_nombre": "Chatbot",
                            "actor_identity": chatbot_identity,
                            "es_admin": True,
                        }
                    )

        # --- Comentarios del ticket (posteriores) ---
        try:
            comentarios_query = ticket.comentarios
            if not include_internal:
                comentarios_query = comentarios_query.filter(_public_ticket_comment_filter())
            comentarios = comentarios_query.order_by(TicketComentario.fecha.asc()).all()
        except Exception:
            comentarios = []

        for c in comentarios:
            if not include_internal and _is_internal_ticket_comment(c):
                continue
            data = c.to_dict()
            data["texto"] = data.pop("comentario")
            data["fecha"] = datetime_to_iso_utc(c.fecha)
            mensajes.append(data)

        # Orden cronológico por fecha
        mensajes.sort(key=lambda x: x["fecha"])
        return mensajes

    def obtener_timeline_ticket(
        self,
        ticket: Union[MunicipioTicket, PymeTicket],
        *,
        include_internal: bool = True,
    ) -> list[dict]:
        """Construye la línea de tiempo de un ticket con comentarios y cambios de estado."""
        try:
            comentarios_query = ticket.comentarios
            if not include_internal:
                comentarios_query = comentarios_query.filter(_public_ticket_comment_filter())
            comentarios = comentarios_query.order_by(TicketComentario.fecha.asc()).all()
        except Exception:
            comentarios = []

        def _estado_publico(estado: str) -> str:
            """Normaliza estados internos para mostrarlos al público."""
            return "resuelto" if estado == "cerrado" else estado

        timeline = [
            {
                "tipo": "ticket_creado",
                "estado": "nuevo",
                "fecha": datetime_to_iso_utc(ticket.fecha),
            }
        ]

        for c in comentarios:
            if not include_internal and _is_internal_ticket_comment(c):
                continue
            if c.estado_ticket:
                timeline.append(
                    {
                        "tipo": "estado",
                        "estado": _estado_publico(c.estado_ticket),
                        "fecha": datetime_to_iso_utc(c.fecha),
                    }
                )
            else:
                autor_tipo = "municipio" if c.es_admin else "vecino"
                if c.es_admin:
                    nombre_autor = "Municipio"
                    actor_user = None
                    if c.user_id:
                        usuario = db.session.get(User, c.user_id)
                        if usuario and usuario.name:
                            nombre_autor = usuario.name
                        actor_user = usuario
                else:
                    actor_user = db.session.get(User, c.user_id) if c.user_id else None
                    nombre_autor = None
                    if c.municipio_ticket and getattr(c.municipio_ticket, "nombre_vecino", None):
                        nombre_autor = c.municipio_ticket.nombre_vecino
                    elif c.pyme_ticket and getattr(c.pyme_ticket, "nombre_cliente", None):
                        nombre_autor = c.pyme_ticket.nombre_cliente
                    if not nombre_autor:
                        nombre_autor = "Vecino/a"
                timeline.append(
                    {
                        "tipo": "comentario",
                        "texto": c.comentario,
                        "fecha": datetime_to_iso_utc(c.fecha),
                        "es_admin": c.es_admin,
                        "user_id": c.user_id,
                        "autor": autor_tipo,
                        "autor_nombre": nombre_autor,
                        "actor_identity": build_identity_subject(
                            user=actor_user,
                            display_name=nombre_autor,
                            anon_id=c.anon_id,
                            source_context="ticket_timeline",
                        ),
                    }
                )

        estado_actual = _estado_publico(getattr(ticket, "estado", None))
        if estado_actual:
            tiene_eventos_de_estado = any(evento.get("tipo") == "estado" for evento in timeline)
            estado_ya_registrado = any(
                evento.get("tipo") == "estado" and evento.get("estado") == estado_actual
                for evento in timeline
            )
            if not estado_ya_registrado and not (tiene_eventos_de_estado and estado_actual in {"nuevo", "abierto", "open"}):
                fecha_estado = getattr(ticket, "ultima_actividad", None) or ticket.fecha
                timeline.append(
                    {
                        "tipo": "estado",
                        "estado": estado_actual,
                        "fecha": datetime_to_iso_utc(fecha_estado),
                    }
                )

        timeline.sort(key=lambda evento: evento.get("fecha") or "")

        return timeline

    def obtener_estado_progreso(
        self,
        ticket: Union[MunicipioTicket, PymeTicket],
        *,
        include_internal: bool = True,
    ) -> list[dict]:
        """Genera una lista ordenada con los estados principales del ticket."""
        timeline = self.obtener_timeline_ticket(ticket, include_internal=include_internal)

        estados = {
            "nuevo": {"completado": False, "fecha": None},
            "en_proceso": {"completado": False, "fecha": None},
            "completado": {"completado": False, "fecha": None},
            "resuelto": {"completado": False, "fecha": None},
        }

        for evento in timeline:
            if evento.get("tipo") == "ticket_creado":
                estados["nuevo"] = {"completado": True, "fecha": evento.get("fecha")}
            elif evento.get("tipo") == "estado":
                nombre_estado = evento.get("estado")
                if nombre_estado in ("en_progreso", "en progreso", "en_proceso"):
                    estados["en_proceso"] = {"completado": True, "fecha": evento.get("fecha")}
                elif nombre_estado == "completado":
                    estados["completado"] = {"completado": True, "fecha": evento.get("fecha")}
                elif nombre_estado in ("resuelto", "cerrado"):
                    estados["completado"] = {"completado": True, "fecha": evento.get("fecha")}
                    estados["resuelto"] = {"completado": True, "fecha": evento.get("fecha")}

        return [
            {"estado": "nuevo", **estados["nuevo"]},
            {"estado": "en_proceso", **estados["en_proceso"]},
            {"estado": "completado", **estados["completado"]},
            {"estado": "resuelto", **estados["resuelto"]},
        ]

    def migrar_tickets_de_anonimo(
        self,
        anon_id: str,
        nuevo_user_id: int,
        *,
        tenant_id: int | None = None,
    ) -> int:
        """Atomically adopt anonymous records inside one proven tenant.

        Kept as a compatibility facade for legacy callers.  The authoritative
        implementation also scopes chat, survey and commerce records and uses
        compare-and-set ownership predicates so a later request cannot steal a
        row already claimed by another account.
        """

        from services.user_merge import merge_anon_into_user

        user = db.session.get(User, nuevo_user_id) if nuevo_user_id else None
        stats = merge_anon_into_user(
            anon_id,
            user,
            tenant_id=tenant_id,
        )
        return int(stats.get("tickets", 0)) + int(stats.get("ticket_comentarios", 0))

servicio_tickets = ServicioTickets()
