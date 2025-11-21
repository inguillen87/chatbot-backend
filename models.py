from datetime import datetime, timezone, timedelta
from typing import Optional

from utils.time_utils import get_local_now, datetime_to_iso_utc
from enum import Enum
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Text,
    Index,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import defer, deferred, validates
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON
from sqlalchemy.dialects.postgresql import JSONB
from database import db
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
from flask_login import UserMixin
import uuid
import json
import os
import random
from services.gcs_service import resolve_attachment_thumb_url

try:  # pragma: no cover - defensive fallback for circular imports during tests
    from config import TIMEZONE_OFFSET as _CONFIG_TIMEZONE_OFFSET
except Exception:  # pragma: no cover - fallback to default offset used in prod (GMT-3)
    _CONFIG_TIMEZONE_OFFSET = -3


def _build_public_survey_timezone() -> timezone:
    """Return the timezone used to interpret naive encuesta schedules."""

    try:
        offset_hours = int(_CONFIG_TIMEZONE_OFFSET)
    except (TypeError, ValueError):
        offset_hours = -3

    # ``datetime.timezone`` supports offsets between -24 and +24 hours.  Our
    # deployments typically live in GMT-3, but we clamp the value to avoid
    # crashes caused by misconfigured environment variables.
    offset_hours = max(-12, min(14, offset_hours))
    return timezone(timedelta(hours=offset_hours))


_PUBLIC_SURVEY_LOCAL_TZ = _build_public_survey_timezone()


def _normalize_public_survey_datetime(value: Optional[datetime]) -> Optional[datetime]:
    """Ensure encuesta schedule datetimes are timezone-aware."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=_PUBLIC_SURVEY_LOCAL_TZ)
    return value


def _coerce_reference_time(at: Optional[datetime]) -> datetime:
    """Return an aware datetime (UTC) used for actividad comparisons."""

    reference = at or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        return reference.replace(tzinfo=timezone.utc)
    return reference.astimezone(timezone.utc)


JSONType = JSONB().with_variant(SQLITE_JSON, "sqlite")

print("Importing models.py")


class CatalogoModalidad(str, Enum):
    VENTA = "venta"
    DONACION = "donacion"
    CANJE = "canje"

    @classmethod
    def from_legacy(cls, raw: Optional[str]) -> "CatalogoModalidad":
        if raw is None:
            return cls.VENTA
        value = str(raw).strip().lower()
        if value in {cls.DONACION.value, "donaciones", "donación"}:
            return cls.DONACION
        if value in {cls.CANJE.value, "puntos", "pts", "canje_puntos"}:
            return cls.CANJE
        return cls.VENTA

    @classmethod
    def infer(
        cls,
        raw: Optional[str],
        *,
        moneda: Optional[str] = None,
        precio_puntos: Optional[int] = None,
        precio_value: Optional[object] = None,
    ) -> "CatalogoModalidad":
        normalized = cls.from_legacy(raw)
        if normalized != cls.VENTA:
            return normalized

        moneda_norm = (moneda or "").strip().upper()
        if moneda_norm == "PTS" or (precio_puntos or 0) > 0:
            return cls.CANJE

        try:
            precio_float = float(precio_value) if precio_value is not None else None
        except (TypeError, ValueError):
            precio_float = None

        if precio_float == 0:
            return cls.DONACION

        return cls.VENTA


class TimestampMixin:
    """Mixin providing created/updated timestamps compatible with UTC."""

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

class Rubro(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(50), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=True)
    es_publico = db.Column(db.Boolean, default=False)
    descripcion = db.Column(db.Text, nullable=True)
    padre_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    subrubros = db.relationship('Rubro', backref=db.backref('padre', remote_side=[id]), lazy=True)
    faqs = db.relationship('QA', backref='rubro', lazy=True)
    sugerencias = db.relationship('Sugerencia', backref='rubro', lazy=True)

    def __repr__(self):
        return f"<Rubro {self.nombre}>"

class QA(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    question = db.Column(db.String(255), nullable=False)
    keywords = db.Column(db.String(255))
    answer = db.Column(db.Text, nullable=False)
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    categoria = db.Column(db.String(100), nullable=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=get_local_now)

class Sugerencia(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=False)
    texto = db.Column(db.String(255), nullable=False)

    def __repr__(self):
        return f"<Sugerencia {self.id}>"

class User(db.Model, UserMixin):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    token = db.Column(db.String(255), nullable=True)
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    saldo_puntos = db.Column(db.Integer, nullable=False, default=0)
    rol = db.Column(db.String(30), default="usuario")
    tipo_chat = db.Column(db.String(20), nullable=True)
    password_reset_selector = db.Column(db.String(64), unique=True, index=True, nullable=True)
    password_reset_verifier_hash = db.Column(db.String(255), nullable=True)
    password_reset_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Alias de conveniencia para frameworks externos
    @property
    def role(self):
        return self.rol

    @role.setter
    def role(self, value: str):
        self.rol = value
    # Campos opcionales para controlar la pertenencia a una pyme o municipio
    pyme_id = db.Column(db.Integer, nullable=True)
    municipio_id = db.Column(db.Integer, nullable=True)
    nombre_empresa = db.Column(db.String(150), nullable=True)
    direccion = db.Column(db.String(200), nullable=True)
    ciudad = db.Column(db.String(100), nullable=True)
    provincia = db.Column(db.String(100), nullable=True)
    pais = db.Column(db.String(100), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    telefono = db.Column(db.String(20), nullable=True)
    link_web = db.Column(db.String(255), nullable=True)
    logo_url = db.Column(db.String(255), nullable=True)
    color_primario = db.Column(db.String(20), nullable=True)
    color_secundario = db.Column(db.String(20), nullable=True)
    badge_tipo = db.Column(db.String(20), nullable=True)
    widget_icon_url = db.Column(db.String(255), nullable=True)
    widget_animation = db.Column(db.String(100), nullable=True)
    acepto_terminos = db.Column(Boolean, default=False)
    fecha_aceptacion_terminos = db.Column(db.DateTime(timezone=True), nullable=True)
    acepta_marketing = db.Column(Boolean, default=False)
    fecha_aceptacion_marketing = db.Column(db.DateTime(timezone=True), nullable=True)
    tags = db.Column(db.String(255), default="")
    ticket_categorias = db.Column(db.String(255), nullable=True)
    horario = db.Column(db.String(100), nullable=True)
    plan = db.Column(db.String(20), default="gratis")
    preguntas_usadas = db.Column(db.Integer, default=0)
    limite_preguntas = db.Column(db.Integer, default=50)
    last_reset = db.Column(db.DateTime(timezone=True), default=get_local_now)
    empresa_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    empresa = db.relationship('User', remote_side=[id], backref='clientes')
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    rubro = db.relationship("Rubro", backref="usuarios")
    prefers_audio = db.Column(db.Boolean, default=False)
    accesibilidad = db.Column(JSONType, nullable=True)
    catalogo_items = db.relationship('CatalogoItem', backref='user', lazy=True)
    catalogo_embeddings = db.relationship('CatalogoEmbedding', backref='user', lazy=True)
    municipio_tickets = db.relationship('MunicipioTicket', backref='municipio', lazy=True)
    fecha_creacion = db.Column(db.DateTime(timezone=True), default=get_local_now) # Nuevo campo

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def generate_password_reset_token(self) -> str:
        """Generate a secure password reset token and persist its metadata."""

        selector = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(32)
        self.password_reset_selector = selector
        self.password_reset_verifier_hash = generate_password_hash(verifier)
        self.password_reset_sent_at = datetime.now(timezone.utc)
        return f"{selector}.{verifier}"

    def verify_password_reset_token(self, verifier: str, max_age_seconds: int) -> bool:
        """Return True when the provided verifier matches the stored token."""

        if not verifier or not self.password_reset_verifier_hash:
            return False

        if not check_password_hash(self.password_reset_verifier_hash, verifier):
            return False

        if not self.password_reset_sent_at:
            return False

        current_time = datetime.now(timezone.utc)
        sent_at = self.password_reset_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)

        if current_time - sent_at > timedelta(seconds=max_age_seconds):
            return False

        return True

    def clear_password_reset_token(self) -> None:
        """Remove any persisted password reset token information."""

        self.password_reset_selector = None
        self.password_reset_verifier_hash = None
        self.password_reset_sent_at = None

    @classmethod
    def create_or_get_by_anon(
        cls,
        anon_id: Optional[str],
        display_name: Optional[str] = None,
    ) -> "User":
        """Return an existing provisional user or create a new one for ``anon_id``."""

        display = (display_name or "Ciudadano").strip() or "Ciudadano"

        if anon_id:
            existing = cls.query.filter_by(anon_id=anon_id).first()
            if existing:
                if not existing.name:
                    existing.name = display
                return existing

        placeholder_email = f"anon-{uuid.uuid4().hex}@passkey.chatboc"
        random_password = secrets.token_urlsafe(24)

        provisional = cls(
            name=display,
            email=placeholder_email,
            token=generate_token(),
            anon_id=anon_id or uuid.uuid4().hex,
            rol="usuario",
        )
        provisional.set_password(random_password)

        db.session.add(provisional)
        db.session.flush()
        return provisional

    @property
    def horario_json(self):
        if self.horario:
            try:
                return json.loads(self.horario)
            except json.JSONDecodeError:
                return None
        return None

    @property
    def categorias_lista(self) -> list[str]:
        if not self.ticket_categorias:
            return []
        return [c.strip() for c in self.ticket_categorias.split(',') if c.strip()]

    @categorias_lista.setter
    def categorias_lista(self, value):
        if isinstance(value, list):
            self.ticket_categorias = ','.join(value)
        elif isinstance(value, str):
            self.ticket_categorias = value
        else:
            self.ticket_categorias = None

    def __repr__(self):
        return f"<User {self.email}>"

class MunicipioTicket(db.Model):
    __tablename__ = "municipio_ticket"
    id = db.Column(db.Integer, primary_key=True)
    pregunta = db.Column(db.Text, nullable=False, default='')
    asunto = db.Column(db.String(200), nullable=True)
    categoria = db.Column(db.String(100), nullable=True)
    user_id = db.Column(db.Integer, nullable=True)
    municipio_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    estado = db.Column(db.String(30), default="nuevo")
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    ultima_actividad = db.Column(db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now)
    nro_ticket = db.Column(db.String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    consulta_pin = db.Column(db.String(6), nullable=False, default=lambda: f"{random.randint(100000, 999999)}")
    detalles = db.Column(db.Text, nullable=True)
    direccion = db.Column(db.String(255), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    distrito = db.Column(db.String(100), nullable=True)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)
    nombre_vecino = db.Column(db.String(150), nullable=True)
    telefono_vecino = db.Column(db.String(30), nullable=True)
    email_vecino = db.Column(db.String(120), nullable=True)
    dni_vecino = db.Column(db.String(20), nullable=True)
    foto_url_directa = db.Column(db.String(255), nullable=True) # For simple photo URL if not using full ArchivoAdjunto flow initially
    # Nuevos campos requeridos
    canal_ingreso = db.Column(db.String(50), nullable=True) # ej: WhatsApp, Web, API
    contacto_seguimiento = db.Column(db.String(255), nullable=True) # ej: link a wa.me, mailto, etc.
    nombre_display_whatsapp = db.Column(db.String(150), nullable=True)
    url_avatar_whatsapp = db.Column(db.String(255), nullable=True)
    # archivo_url = db.Column(db.String(255), nullable=True) # Campo obsoleto, se usará la relación
    comentarios = db.relationship('TicketComentario', back_populates='municipio_ticket', lazy='dynamic')
    archivos = db.relationship(
        'ArchivoAdjunto',
        foreign_keys='[ArchivoAdjunto.municipio_ticket_id]',
        backref='municipio_ticket_ref', # Usar un backref específico si PymeTicket también tiene uno
        lazy='dynamic', # O 'select'/'joined' según la necesidad de carga
        cascade="all, delete-orphan" # Opcional: si se borra el ticket, borrar sus archivos
    )


class MunicipioPost(db.Model):
    __tablename__ = "municipio_post"

    id = Column(Integer, primary_key=True)
    municipio_id = Column(Integer, ForeignKey('user.id'), nullable=False, index=True)
    tipo_post = Column(String(30), nullable=False, default="noticia")
    titulo = Column(String(255), nullable=False)
    subtitulo = Column(String(255), nullable=True)
    descripcion = Column(Text, nullable=False)
    tags = Column(JSONType, nullable=True)
    imagen_url = Column(String(500), nullable=True)
    enlace = Column(String(500), nullable=True)
    fecha_evento_inicio = Column(DateTime(timezone=True), nullable=True)
    fecha_evento_fin = Column(DateTime(timezone=True), nullable=True)
    fecha_publicacion = Column(DateTime(timezone=True), nullable=False, default=get_local_now)
    ubicacion = Column(String(255), nullable=True)
    datos_extra = Column(JSONType, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=get_local_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=get_local_now, onupdate=get_local_now)

    __table_args__ = (
        Index('ix_municipio_post_municipio_fecha', 'municipio_id', 'fecha_publicacion'),
    )

    ALLOWED_TYPES = {"noticia", "evento", "informacion", "promocion", "promocionar"}

    @validates("tipo_post")
    def _validate_tipo_post(self, key, value):  # pragma: no cover - simple normalization
        normalized = (value or "noticia").strip().lower()
        if normalized not in self.ALLOWED_TYPES:
            normalized = "noticia"
        return normalized

    @staticmethod
    def _serialize_datetime(value):
        if not value:
            return None
        try:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        except Exception:
            return None

    def to_dict(self) -> dict:
        tags_value = self.tags if isinstance(self.tags, list) else []
        if self.tipo_post not in tags_value:
            tags_value = [self.tipo_post, *(tag for tag in tags_value if tag != self.tipo_post)]
        return {
            "id": str(self.id),
            "titulo": self.titulo,
            "subtitulo": self.subtitulo,
            "descripcion": self.descripcion,
            "tipo_post": self.tipo_post,
            "tags": tags_value,
            "imagen_url": self.imagen_url,
            "enlace": self.enlace,
            "fecha_evento_inicio": self._serialize_datetime(self.fecha_evento_inicio),
            "fecha_evento_fin": self._serialize_datetime(self.fecha_evento_fin),
            "fecha_publicacion": self._serialize_datetime(self.fecha_publicacion),
            "ubicacion": self.ubicacion,
            "datos_extra": self.datos_extra or {},
        }


class TenantProfile(db.Model, TimestampMixin):
    __tablename__ = "tenant_profile"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), nullable=False, unique=True, index=True)
    nombre = db.Column(db.String(255), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    municipio_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    pyme_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    encuestas_tenant_id = db.Column(db.Integer, nullable=True)
    dominio = db.Column(db.String(255), nullable=True)
    logo_url = db.Column(db.String(512), nullable=True)
    tema = db.Column(JSONType, nullable=True)
    configuracion = db.Column(JSONType, nullable=True)

    municipio = db.relationship(
        "User",
        foreign_keys=[municipio_id],
        backref=db.backref("tenant_profile_municipio", uselist=False),
    )
    pyme = db.relationship(
        "User",
        foreign_keys=[pyme_id],
        backref=db.backref("tenant_profile_pyme", uselist=False),
    )

    followers = db.relationship(
        "TenantFollower",
        back_populates="tenant",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    tickets = db.relationship(
        "TenantTicket",
        back_populates="tenant",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    __table_args__ = (
        db.CheckConstraint(
            "(municipio_id IS NOT NULL) OR (pyme_id IS NOT NULL)",
            name="ck_tenant_profile_owner_present",
        ),
        db.CheckConstraint(
            "NOT (municipio_id IS NOT NULL AND pyme_id IS NOT NULL)",
            name="ck_tenant_profile_single_owner",
        ),
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "nombre": self.nombre,
            "tipo": self.tipo,
            "logo_url": self.logo_url,
            "dominio": self.dominio,
            "tema": self.tema or {},
        }

    def __repr__(self) -> str:  # pragma: no cover - simple representation
        return f"<TenantProfile slug={self.slug!r} tipo={self.tipo!r}>"


class TenantFollower(db.Model, TimestampMixin):
    __tablename__ = "tenant_follower"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False, index=True)
    notifications_enabled = db.Column(db.Boolean, nullable=False, default=True)

    tenant = db.relationship("TenantProfile", back_populates="followers")
    user = db.relationship("User", backref=db.backref("tenant_followers", lazy="dynamic"))

    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "tenant_id",
            name="uq_tenant_follower_user_tenant",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - simple representation
        return f"<TenantFollower user={self.user_id} tenant={self.tenant_id}>"


class TenantTicket(db.Model, TimestampMixin):
    __tablename__ = "tenant_ticket"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    categoria = db.Column(db.String(80), nullable=True)
    descripcion = db.Column(db.Text, nullable=False)
    estado = db.Column(db.String(20), nullable=False, default="nuevo")
    origen = db.Column(db.String(20), nullable=False, default="pwa")
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    datos_extra = db.Column(JSONType, nullable=True)
    fingerprint = db.Column(db.String(120), nullable=True, index=True)

    tenant = db.relationship("TenantProfile", back_populates="tickets")
    user = db.relationship("User", backref=db.backref("tenant_tickets", lazy="dynamic"))

    __table_args__ = (
        db.Index("ix_tenant_ticket_tenant_estado", "tenant_id", "estado"),
    )

    def __repr__(self) -> str:  # pragma: no cover - simple representation
        return f"<TenantTicket id={self.id} tenant={self.tenant_id} estado={self.estado}>"


class WebAuthnCredential(db.Model, TimestampMixin):
    __tablename__ = "webauthn_credential"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    credential_id = db.Column(db.String(255), unique=True, nullable=False)
    public_key = db.Column(db.Text, nullable=False)
    sign_count = db.Column(db.Integer, nullable=False, default=0)
    transports = db.Column(JSONType, nullable=True)

    user = db.relationship(
        "User",
        backref=db.backref(
            "webauthn_credentials",
            cascade="all, delete-orphan",
            lazy="dynamic",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - simple representation
        return f"<WebAuthnCredential user={self.user_id} id={self.credential_id[:8]}>"


class PymeTicket(db.Model):
    __tablename__ = "pyme_ticket"
    id = db.Column(db.Integer, primary_key=True)
    pregunta = db.Column(db.Text, nullable=False)
    asunto = db.Column(db.String(200), nullable=True)
    categoria = db.Column(db.String(100), nullable=True)
    user_id = db.Column(db.Integer, nullable=True)
    estado = db.Column(db.String(30), default="nuevo")
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    nro_ticket = db.Column(db.Integer, nullable=False, unique=True)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    # archivo_url = db.Column(db.String(255), nullable=True) # Replaced by relationship
    telefono = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    dni = db.Column(db.String(20), nullable=True)
    estado_cliente = db.Column(db.String(30), default="no_definido")
    direccion = db.Column(db.String(255), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    comentarios = db.relationship('TicketComentario', back_populates='pyme_ticket', lazy='dynamic')
    archivos = db.relationship(
        'ArchivoAdjunto',
        foreign_keys='[ArchivoAdjunto.pyme_ticket_id]',
        backref='pyme_ticket_ref',
        lazy='dynamic',
        cascade="all, delete-orphan"
    )

class PymePedido(db.Model):
    __tablename__ = "pyme_pedido"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # El cliente que hace el pedido (puede ser anónimo)
    pyme_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False) # La pyme a la que se le hace el pedido

    user = db.relationship('User', foreign_keys=[user_id], backref='pyme_pedidos_realizados')
    pyme = db.relationship('User', foreign_keys=[pyme_id], backref='pyme_pedidos_recibidos')

    nro_pedido = db.Column(db.String(50), unique=True, nullable=False)
    asunto = db.Column(db.String(255), nullable=True)
    estado = db.Column(db.String(30), default="pendiente")
    detalles = db.Column(db.Text, nullable=True) # JSON string
    monto_total = db.Column(db.Float, nullable=True)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)
    nombre_cliente = db.Column(db.String(100), nullable=True)
    email_cliente = db.Column(db.String(100), nullable=True)
    telefono_cliente = db.Column(db.String(50), nullable=True)
    direccion = db.Column(db.String(255), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)

    def __init__(self, pyme_id, asunto, detalles, monto_total=None, nombre_cliente=None, email_cliente=None, telefono_cliente=None, user_id=None, direccion=None, latitud=None, longitud=None):
        self.pyme_id = pyme_id
        self.asunto = asunto
        self.detalles = detalles
        self.monto_total = monto_total
        self.nombre_cliente = nombre_cliente
        self.email_cliente = email_cliente
        self.telefono_cliente = telefono_cliente
        self.user_id = user_id
        self.direccion = direccion
        self.latitud = latitud
        self.longitud = longitud
        self.nro_pedido = self._generate_nro_pedido()

    def _generate_nro_pedido(self):
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        random_suffix = str(uuid.uuid4().hex)[:6].upper()
        return f"PED-{timestamp}-{random_suffix}"

    def to_dict(self):
        """Convierte el objeto Pedido a un diccionario serializable."""
        try:
            detalles_json = json.loads(self.detalles) if self.detalles else []
        except json.JSONDecodeError:
            detalles_json = []

        return {
            "id": self.id,
            "pyme_id": self.pyme_id,
            "nro_pedido": self.nro_pedido,
            "asunto": self.asunto,
            "estado": self.estado,
            "detalles": detalles_json,
            "monto_total": self.monto_total,
            "fecha_creacion": self.fecha.isoformat() if self.fecha else None,
            "nombre_cliente": self.nombre_cliente,
            "email_cliente": self.email_cliente,
            "telefono_cliente": self.telefono_cliente,
            "direccion": self.direccion,
            "latitud": self.latitud,
            "longitud": self.longitud,
            "user_id": self.user_id
        }

    def __repr__(self):
        return f"<PymePedido {self.nro_pedido} - {self.asunto}>"

class ArchivoAdjunto(db.Model):
    __tablename__ = "archivo_adjunto"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    session_id = db.Column(db.String(36), nullable=True)
    filename = db.Column(db.String(255), nullable=False)
    nombre_original = db.Column(db.String(255), nullable=True)
    mime = db.Column(db.String(100), nullable=True)
    tamano = db.Column(db.Integer, nullable=True)
    tipo = db.Column(db.String(50), nullable=True)
    pyme_ticket_id = db.Column(db.Integer, db.ForeignKey("pyme_ticket.id"), nullable=True)
    municipio_ticket_id = db.Column(
        db.Integer, db.ForeignKey("municipio_ticket.id"), nullable=True
    )
    url = db.Column(db.String(255), nullable=False)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)

class AnalisisArchivo(db.Model):
    __tablename__ = "analisis_archivo"
    id = db.Column(db.Integer, primary_key=True)
    archivo_adjunto_id = db.Column(db.Integer, db.ForeignKey("archivo_adjunto.id"), nullable=False, index=True)
    resumen = db.Column(db.Text, nullable=True)
    estado_analisis = db.Column(db.String(50), nullable=True, default="pendiente") # ej: pendiente, procesando, completado, error
    fecha_analisis = db.Column(db.DateTime(timezone=True), nullable=True)
    error_analisis = db.Column(db.Text, nullable=True) # Para guardar mensajes de error si falla el análisis

    # Nuevos campos para análisis avanzado
    texto_extraido = db.Column(db.Text, nullable=True) # Para OCR completo o texto de PDF
    datos_estructurados = db.Column(JSONType, nullable=True) # Para JSON con data extraída (items, cantidades, etc.)
    tipo_analisis = db.Column(db.String(100), nullable=True) # ej: 'resumen_texto', 'vision_ocr', 'document_ai_form'

    archivo_adjunto = db.relationship("ArchivoAdjunto", backref=db.backref("analisis", uselist=False, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<AnalisisArchivo id={self.id} para archivo_id={self.archivo_adjunto_id} estado='{self.estado_analisis}'>"

class Conversacion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    pyme_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    pregunta = db.Column(db.Text, nullable=False)
    respuesta = db.Column(db.Text, nullable=False)
    fuente = db.Column(db.String(50), nullable=False)
    rubro = db.Column(db.String(100), nullable=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=get_local_now)
    session_id = db.Column(db.String(36), default=lambda: str(uuid.uuid4()), nullable=False)
    __table_args__ = (Index('ix_conversacion_session_id', 'session_id'),)

class TicketComentario(db.Model):
    __tablename__ = "ticket_comentario"
    id = db.Column(db.Integer, primary_key=True)
    pyme_ticket_id = db.Column(db.Integer, db.ForeignKey('pyme_ticket.id'), nullable=True)
    municipio_ticket_id = db.Column(db.Integer, db.ForeignKey('municipio_ticket.id'), nullable=True)
    comentario = db.Column(db.Text, nullable=False)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)
    user_id = db.Column(db.Integer, nullable=True)
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    es_admin = db.Column(db.Boolean, default=False)
    origen = db.Column(db.String(20), default='chat') # Nuevo campo para 'chat' o 'email'
    estado_ticket = db.Column(db.String(30), nullable=True)  # Registro de cambios de estado

    # New field to link a comment directly to an attachment
    archivo_adjunto_id = db.Column(db.Integer, db.ForeignKey('archivo_adjunto.id'), nullable=True)
    archivo_adjunto = db.relationship('ArchivoAdjunto', backref=db.backref('comentario_asociado', uselist=False))

    pyme_ticket = db.relationship('PymeTicket', back_populates='comentarios')
    municipio_ticket = db.relationship('MunicipioTicket', back_populates='comentarios')

    def to_dict(self):
        data = {
            "id": self.id,
            "pyme_ticket_id": self.pyme_ticket_id,
            "municipio_ticket_id": self.municipio_ticket_id,
            "comentario": self.comentario,
            "texto": self.comentario,
            "fecha": datetime_to_iso_utc(self.fecha),
            "user_id": self.user_id,
            "anon_id": self.anon_id,
            "es_admin": self.es_admin,
            "origen": self.origen,
            "estado_ticket": self.estado_ticket
        }
        if self.archivo_adjunto:
            attachment_info = {
                "id": self.archivo_adjunto.id,
                "url": self.archivo_adjunto.url,
                "name": self.archivo_adjunto.nombre_original,
                "mimeType": self.archivo_adjunto.mime,
                "size": self.archivo_adjunto.tamano,
                "uploadedAt": datetime_to_iso_utc(self.archivo_adjunto.fecha),
            }

            # Fetch metadata from AnalisisArchivo
            analisis = AnalisisArchivo.query.filter_by(
                archivo_adjunto_id=self.archivo_adjunto.id,
                tipo_analisis='thumbnail_meta'
            ).first()
            meta = analisis.datos_estructurados if analisis else {}
            if not isinstance(meta, dict):
                meta = {}

            thumb_url, meta = resolve_attachment_thumb_url(
                file_url=self.archivo_adjunto.url,
                filename=self.archivo_adjunto.filename,
                mime_type=self.archivo_adjunto.mime,
                meta=meta,
            )

            attachment_info["thumbUrl"] = thumb_url
            attachment_info["thumbnailUrl"] = thumb_url
            attachment_info['meta'] = meta

            data['attachmentInfo'] = attachment_info
        # Determine author information for clarity in timelines and chats
        autor_tipo = "municipio" if self.es_admin else "vecino"
        if self.es_admin:
            nombre_autor = "Municipio"
            if self.user_id:
                usuario = db.session.get(User, self.user_id)
                if usuario and usuario.name:
                    nombre_autor = usuario.name
        else:
            nombre_autor = None
            if self.municipio_ticket and getattr(self.municipio_ticket, "nombre_vecino", None):
                nombre_autor = self.municipio_ticket.nombre_vecino
            elif self.pyme_ticket and getattr(self.pyme_ticket, "nombre_cliente", None):
                nombre_autor = self.pyme_ticket.nombre_cliente
            if not nombre_autor:
                nombre_autor = "Vecino/a"
        data["autor"] = autor_tipo
        data["autor_nombre"] = nombre_autor

        return data

class CatalogoItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant_profile.id'), nullable=True, index=True)
    nombre = db.Column(db.String(255), nullable=False)
    descripcion = db.Column(db.String(1024))
    precio = db.Column(db.String(50))
    cantidad = db.Column(db.String(50))
    sku = db.Column(db.String(100), nullable=True, index=True)
    marca = db.Column(db.String(100), nullable=True, index=True)
    categoria = db.Column(db.String(100))
    unidad = db.Column(db.String(50))
    # Some legacy databases may not yet contain the "precio_monetario" column.
    # Mark it as deferred so ORM queries don't try to SELECT it unless explicitly
    # accessed, preventing "UndefinedColumn" errors when the column is missing.
    precio_monetario = deferred(db.Column(db.Numeric(12, 2), nullable=True))
    # Some legacy deployments still lack newer monetary fields. Mark them as
    # deferred so base queries do not attempt to select missing columns. They
    # will only be accessed (and therefore SELECTed) when explicitly used in
    # application code after the corresponding migrations are applied.
    moneda = deferred(db.Column(db.String(10), nullable=True))
    precio_puntos = db.Column(db.Integer, nullable=True)
    modalidad = db.Column(db.String(20), nullable=False, default="venta")
    precio_por_caja = deferred(db.Column(db.Numeric(12, 2), nullable=True))
    unidad_por_caja = deferred(db.Column(db.Integer, nullable=True))
    extra_metadata = db.Column("metadata", JSONType, nullable=True)
    # Nuevos campos para información más detallada del catálogo
    descripcion_corta = db.Column(db.String(512), nullable=True)
    promocion_info = db.Column(db.String(255), nullable=True) # Para texto de promociones, ej: "20% OFF"
    # 'cantidad' se usa actualmente para stock. Si se necesita diferenciar, añadir un campo 'stock' dedicado.
    # 'texto' se usa para almacenar el texto combinado que se usó para el embedding.
    texto = db.Column(db.Text, nullable=True)
    embedding = db.Column(db.PickleType, nullable=True) # Este campo podría eliminarse si los embeddings solo viven en Qdrant
    imagen_url = db.Column(db.String(512), nullable=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=get_local_now)

    def __repr__(self):
        return f"<CatalogoItem {self.id} para user {self.user_id}>"

    @validates("modalidad")
    def _normalize_modalidad(self, key, value):  # noqa: ARG002
        normalized = CatalogoModalidad.from_legacy(value)
        return normalized.value

    @property
    def modalidad_enum(self) -> CatalogoModalidad:
        return CatalogoModalidad.infer(
            self.modalidad,
            moneda=self.moneda,
            precio_puntos=self.precio_puntos,
            precio_value=self.precio_monetario or self.precio,
        )

    @property
    def es_donacion(self) -> bool:
        return self.modalidad_enum is CatalogoModalidad.DONACION

    @property
    def es_canje(self) -> bool:
        return self.modalidad_enum is CatalogoModalidad.CANJE

    @classmethod
    def legacy_safe_options(cls):
        """Loader options that skip optional monetary columns on legacy DBs.

        Some deployments still have databases created before columns like
        ``moneda`` or ``precio_por_caja`` existed.  Applying these options
        avoids selecting missing columns so catalog queries don't crash with
        ``UndefinedColumn`` errors when the schema is outdated.
        """

        return (
            defer(cls.moneda),
            defer(cls.precio_por_caja),
            defer(cls.unidad_por_caja),
            defer(cls.precio_monetario),
        )

class CatalogoEmbedding(db.Model):
    __tablename__ = "catalogo_embedding"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    nombre = db.Column(db.String(255))
    cantidad = db.Column(db.Float)
    descripcion = db.Column(db.String(1024))
    precio = db.Column(db.String(50))
    embedding_vector = db.Column(JSONType)


class PointsTransaction(db.Model, TimestampMixin):
    __tablename__ = "points_transaction"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=True, index=True)
    tipo = db.Column(db.String(50), nullable=False)
    delta = db.Column(db.Integer, nullable=False)
    saldo_final = db.Column(db.Integer, nullable=False)
    metadata_payload = db.Column("metadata", JSONType, nullable=True)

    user = db.relationship("User", backref=db.backref("points_transactions", lazy="dynamic"))
    tenant = db.relationship("TenantProfile")

    __table_args__ = (
        db.Index("ix_points_tx_user_tenant", "user_id", "tenant_id"),
    )


class CatalogoKit(db.Model, TimestampMixin):
    __tablename__ = "catalogo_kit"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=True, index=True)
    nombre = db.Column(db.String(255), nullable=False)
    descripcion = db.Column(db.Text, nullable=True)
    precio_especial = db.Column(db.Numeric(12, 2), nullable=True)
    moneda = db.Column(db.String(10), nullable=True, default="ARS")
    items = db.Column(JSONType, nullable=False, default=list)

    tenant = db.relationship("TenantProfile")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "nombre": self.nombre,
            "descripcion": self.descripcion,
            "precio_especial": float(self.precio_especial) if self.precio_especial is not None else None,
            "moneda": self.moneda,
            "items": self.items or [],
        }

class SitioWebInfo(db.Model):
    __tablename__ = 'sitio_web_info'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    rubro_id = db.Column(db.Integer, db.ForeignKey("rubro.id"), nullable=True)
    url = db.Column(db.String(255), nullable=False)
    datos_json = db.Column(db.Text, nullable=False)
    fecha_scraping = db.Column(db.DateTime(timezone=True), default=get_local_now)
    actualizado = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<SitioWebInfo id={self.id} url={self.url}>"

class Log(db.Model):
    __tablename__ = "logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    pregunta = db.Column(db.String(500), nullable=False)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)

class TicketSatisfaccion(db.Model):
    __tablename__ = "ticket_satisfaccion"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)
    puntuacion = db.Column(db.Integer, nullable=False)
    comentario = db.Column(db.Text, nullable=True)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)

class Recordatorio(db.Model):
    __tablename__ = "recordatorio"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    descripcion = db.Column(db.String(255), nullable=True)
    fecha_vencimiento = db.Column(db.DateTime(timezone=True), nullable=False)
    enviado = db.Column(db.Boolean, default=False)

class Reaccion(db.Model):
    __tablename__ = "reaccion"
    id = db.Column(db.Integer, primary_key=True)
    conversacion_id = db.Column(db.Integer, db.ForeignKey("conversacion.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    emoji = db.Column(db.String(5), nullable=False)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)
    conversacion = db.relationship("Conversacion", backref="reacciones")
    user = db.relationship("User")

class SugerenciaCiudadano(db.Model):
    __tablename__ = "sugerencia_ciudadano"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True) # Puede ser anónimo o registrado
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    municipio_id = db.Column(db.Integer, nullable=True) # Para vincular a qué municipio pertenece la sugerencia
    texto_sugerencia = db.Column(db.Text, nullable=False)
    fecha = db.Column(db.DateTime(timezone=True), default=get_local_now)
    estado = db.Column(db.String(30), default="nueva") # Ej: nueva, revisada, implementada, descartada
    categoria = db.Column(db.String(100), nullable=True) # Nueva columna para categorizar

    user = db.relationship("User", backref="sugerencias_ciudadano")

    def __repr__(self):
        return f"<SugerenciaCiudadano {self.id} por User {self.user_id or self.anon_id}>"


class PedidoConversacional(db.Model, TimestampMixin):
    __tablename__ = "pedido_conversacional"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    estado = db.Column(db.String(40), nullable=False, default="pendiente_pago")
    monto_monetario = db.Column(db.Numeric(12, 2), nullable=True)
    monto_puntos = db.Column(db.Integer, nullable=True)
    tipo = db.Column(db.String(20), nullable=False, default="compra")
    mp_preference_id = db.Column(db.String(120), nullable=True, index=True)
    mp_payment_id = db.Column(db.String(120), nullable=True, index=True)
    items = db.Column(JSONType, nullable=False, default=list)

    tenant = db.relationship("TenantProfile")
    user = db.relationship("User")

    __table_args__ = (
        db.Index("ix_pedido_conv_tenant_estado", "tenant_id", "estado"),
    )


class PublicSurvey(db.Model):
    __tablename__ = "public_survey"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    titulo = db.Column(db.String(255), nullable=False)
    descripcion = db.Column(db.Text, nullable=True)
    estado = db.Column(db.String(20), nullable=False, default="draft")
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    municipio_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True)
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_by = db.relationship("User")

    def estado_publico(self) -> str:
        """Devuelve el estado en español para las respuestas HTTP."""
        mapping = {
            "draft": "borrador",
            "published": "publicada",
            "archived": "archivada",
        }
        return mapping.get(self.estado, self.estado)


class PublicSurveyQuestion(db.Model):
    __tablename__ = "public_survey_question"

    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey("public_survey.id"), nullable=False)
    titulo = db.Column(db.String(255), nullable=False)
    descripcion = db.Column(db.Text, nullable=True)
    tipo = db.Column(db.String(30), nullable=False)
    obligatoria = db.Column(db.Boolean, default=False)
    orden = db.Column(db.Integer, default=0)

    survey = db.relationship(
        "PublicSurvey",
        backref=db.backref(
            "preguntas",
            order_by="PublicSurveyQuestion.orden",
            cascade="all, delete-orphan",
        ),
    )


class PublicSurveyOption(db.Model):
    __tablename__ = "public_survey_option"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("public_survey_question.id"), nullable=False)
    texto = db.Column(db.String(255), nullable=False)
    valor = db.Column(db.String(255), nullable=True)
    orden = db.Column(db.Integer, default=0)

    question = db.relationship(
        "PublicSurveyQuestion",
        backref=db.backref(
            "opciones",
            order_by="PublicSurveyOption.orden",
            cascade="all, delete-orphan",
        ),
    )


class PublicSurveyResponse(db.Model):
    __tablename__ = "public_survey_response"

    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey("public_survey.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    metadata_json = db.Column(JSONType, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now)

    survey = db.relationship(
        "PublicSurvey",
        backref=db.backref("respuestas", cascade="all, delete-orphan"),
    )
    user = db.relationship("User")


class PublicSurveyAnswer(db.Model):
    __tablename__ = "public_survey_answer"

    id = db.Column(db.Integer, primary_key=True)
    response_id = db.Column(db.Integer, db.ForeignKey("public_survey_response.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("public_survey_question.id"), nullable=False)
    option_id = db.Column(db.Integer, db.ForeignKey("public_survey_option.id"), nullable=True)
    valor = db.Column(db.Text, nullable=True)

    response = db.relationship(
        "PublicSurveyResponse",
        backref=db.backref("answers", cascade="all, delete-orphan"),
    )
    question = db.relationship("PublicSurveyQuestion")
    option = db.relationship("PublicSurveyOption")

def generate_token():
    return str(uuid.uuid4())

class ClienteNota(db.Model):
    __tablename__ = "cliente_nota"
    id = db.Column(db.Integer, primary_key=True)
    # ID del usuario cliente sobre quien es la nota
    cliente_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    # ID del admin/empleado que escribió la nota
    creada_por_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    nota = db.Column(db.Text, nullable=False)
    fecha_creacion = db.Column(db.DateTime(timezone=True), default=get_local_now)
    fecha_actualizacion = db.Column(db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now)

    # Relationship to the client User object
    cliente = db.relationship('User', foreign_keys=[cliente_user_id], backref=db.backref('notas_recibidas', lazy='dynamic'))
    # Relationship to the User object of the creator (admin/employee)
    creador = db.relationship('User', foreign_keys=[creada_por_user_id], backref=db.backref('notas_creadas', lazy='dynamic'))

    def __repr__(self):
        return f"<ClienteNota id={self.id} para_cliente_id={self.cliente_user_id} por_creador_id={self.creada_por_user_id}>"


class PlantillasRespuesta(db.Model):
    __tablename__ = "plantillas_respuesta"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(255), nullable=False)
    text = db.Column(db.Text, nullable=False)
    embedding = db.Column(JSONType, nullable=True) # Almacenará el embedding de Cohere
    keywords = db.Column(JSONType, nullable=True) # Array de strings
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now, nullable=False)
    # Opcional: Para vincular plantillas a un usuario/empresa específica si fuera necesario en el futuro
    # user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    # user = db.relationship('User', backref=db.backref('plantillas_respuesta', lazy='dynamic'))

    def __repr__(self):
        return f"<PlantillasRespuesta id={self.id} name='{self.name}'>"

class WhatsappNumero(db.Model):
    __tablename__ = "whatsapp_numero"
    id = db.Column(db.Integer, primary_key=True)
    numero_whatsapp = db.Column(db.String(25), unique=True, nullable=False, index=True) # e.g., "+17432643718"
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False) # FK to User.id
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now)

    # Relationship to the User model (the company/municipality account)
    user = db.relationship('User', backref=db.backref('whatsapp_numeros', lazy='dynamic'))

    def __repr__(self):
        return f"<WhatsappNumero {self.numero_whatsapp} linked to User {self.user_id}>"

    @property
    def nombre_cliente_asociado(self):
        """Helper to get the name of the associated User (company/municipality)."""
        if self.user:
            return self.user.nombre_empresa or self.user.name
        return None

    @property
    def tipo_cliente_asociado(self):
        """Helper to get the tipo_chat of the associated User."""
        if self.user:
            return self.user.tipo_chat # Assuming 'municipio' or 'pyme'
        return None

class Promocion(db.Model):
    __tablename__ = "promocion"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pyme_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    nombre_promocion = db.Column(db.String(255), nullable=False)
    descripcion_publica = db.Column(db.Text, nullable=True)

    # TIPO_PROMOCION: Define la lógica principal de la promoción
    # PORCENTAJE_PRODUCTO, PORCENTAJE_CATEGORIA, PORCENTAJE_MARCA
    # COMPRA_X_LLEVA_Y_PRODUCTOS (ej. 2x1, 3x2 donde Y es el total llevado, X el pagado)
    # CANTIDAD_MINIMA_DESCUENTO_FIJO_PRODUCTO (ej. lleva 3 de X, obtén $50 de descuento en esos 3)
    # CANTIDAD_MINIMA_DESCUENTO_PORCENTAJE_PRODUCTO (ej. lleva 3 de X, obtén 10% de descuento en esos 3)
    # TOTAL_CARRITO_DESCUENTO_PORCENTAJE (ej. 10% off en compras > $5000)
    # TOTAL_CARRITO_DESCUENTO_FIJO (ej. $200 off en compras > $3000)
    tipo_promocion = db.Column(db.String(100), nullable=False)

    valor_descuento = db.Column(db.Float, nullable=True) # Para % (ej 20.0) o monto fijo ($50)

    # Para COMPRA_X_LLEVA_Y_PRODUCTOS
    cantidad_condicion_x = db.Column(db.Integer, nullable=True) # Cantidad a pagar
    cantidad_resultado_y = db.Column(db.Integer, nullable=True) # Cantidad total que se lleva

    # Para CANTIDAD_MINIMA...
    cantidad_minima_aplicable = db.Column(db.Integer, nullable=True)

    # Para TOTAL_CARRITO...
    monto_minimo_carrito = db.Column(db.Float, nullable=True)

    fecha_inicio = db.Column(db.DateTime(timezone=True), nullable=False, default=get_local_now)
    fecha_fin = db.Column(db.DateTime(timezone=True), nullable=True) # Nullable si la promo no tiene fin
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    codigo_promocion = db.Column(db.String(50), nullable=True, unique=True, index=True) # Si requiere un código
    uso_maximo_general = db.Column(db.Integer, nullable=True) # Límite total de usos
    usos_actuales_general = db.Column(db.Integer, default=0)
    uso_maximo_por_cliente = db.Column(db.Integer, nullable=True) # Límite por cliente

    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now)

    pyme = db.relationship('User', backref=db.backref('promociones', lazy='dynamic'))
    alcances = db.relationship('PromocionAlcance', back_populates='promocion', cascade="all, delete-orphan", lazy='dynamic')

    def __repr__(self):
        return f"<Promocion id={self.id} nombre='{self.nombre_promocion}' pyme_id={self.pyme_user_id}>"

class PromocionAlcance(db.Model):
    __tablename__ = "promocion_alcance"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    promocion_id = db.Column(db.String(36), db.ForeignKey('promocion.id'), nullable=False, index=True)

    # TIPO_ALCANCE: PRODUCTO, CATEGORIA, MARCA
    # Si es PRODUCTO, catalogo_item_id es el relevante.
    # Si es CATEGORIA, nombre_categoria es el relevante.
    # Si es MARCA, nombre_marca es el relevante.
    # Una promoción puede tener múltiples alcances (ej. aplica a ProductoA Y ProductoB)
    tipo_alcance = db.Column(db.String(50), nullable=False) # PRODUCTO, CATEGORIA, MARCA

    catalogo_item_id = db.Column(db.Integer, db.ForeignKey('catalogo_item.id'), nullable=True, index=True)
    nombre_categoria = db.Column(db.String(100), nullable=True, index=True) # Debe coincidir con CatalogoItem.categoria
    nombre_marca = db.Column(db.String(100), nullable=True, index=True) # Debe coincidir con CatalogoItem.marca

    promocion = db.relationship('Promocion', back_populates='alcances')
    item_catalogo = db.relationship('CatalogoItem', backref=db.backref('aplicaciones_promocion', lazy='dynamic'))

    def __repr__(self):
        return f"<PromocionAlcance id={self.id} promocion_id={self.promocion_id} tipo='{self.tipo_alcance}'>"

# Opcional: Tabla para rastrear el uso de promociones por cliente, especialmente si hay límites.
# class PromocionUsoCliente(db.Model):
#     __tablename__ = "promocion_uso_cliente"
#     id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
#     promocion_id = db.Column(db.String(36), db.ForeignKey('promocion.id'), nullable=False, index=True)
#     cliente_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True) # El User cliente
#     pyme_pedido_id = db.Column(db.Integer, db.ForeignKey('pyme_pedido.id'), nullable=True, index=True) # Pedido donde se usó
#     fecha_uso = db.Column(db.DateTime(timezone=True), default=get_local_now)
#     # Se podría añadir info sobre el descuento aplicado si es variable o para auditoría
#
#     promocion = db.relationship('Promocion', backref='usos_por_clientes')
#     cliente = db.relationship('User', backref='promociones_usadas')
#     pedido = db.relationship('PymePedido', backref='promociones_aplicadas_en_pedido')

class ChatSessionContext(db.Model):
    __tablename__ = "chat_session_context"
    chat_session_id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    anon_id = db.Column(db.String(80), nullable=True, index=True) # Similar to MunicipioTicket.anon_id
    context_data = db.Column(JSONType, nullable=True) # Stores combined context (municipio, pyme, history, idempotency keys)
    last_updated = db.Column(db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now)

    user = db.relationship('User', backref=db.backref('chat_session_contexts', lazy='dynamic'))

    def __repr__(self):
        return f"<ChatSessionContext id={self.chat_session_id} user_id={self.user_id} anon_id={self.anon_id}>"

print("✅ models.py fue importado con éxito y contiene modelos.")

class CatalogoCompartido(db.Model):
    __tablename__ = "catalogo_compartido"
    id = db.Column(db.Integer, primary_key=True)
    catalogo_id = db.Column(db.Integer, db.ForeignKey('archivo_adjunto.id'), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    shared_with_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    fecha_compartido = db.Column(db.DateTime(timezone=True), default=get_local_now)

    catalogo = db.relationship('ArchivoAdjunto', backref='compartidos')
    owner = db.relationship('User', foreign_keys=[owner_id])
    shared_with = db.relationship('User', foreign_keys=[shared_with_id])

class CatalogMapping(db.Model):
    __tablename__ = "catalog_mapping"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pyme_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    mapping = db.Column(JSONType, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now)

    pyme = db.relationship('User', backref=db.backref('catalog_mappings', lazy='dynamic'))

    def to_dict(self):
        return {
            "id": self.id,
            "pymeId": self.pyme_id,
            "name": self.name,
            "mapping": self.mapping,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat()
        }

class LlmInteractionLog(db.Model):
    __tablename__ = "llm_interaction_log"
    id = db.Column(db.Integer, primary_key=True)
    chat_session_id = db.Column(db.String(36), db.ForeignKey('chat_session_context.chat_session_id'), nullable=False, index=True)
    user_query = db.Column(db.Text, nullable=False)
    llm_response_raw = db.Column(JSONType, nullable=True)
    status = db.Column(db.String(50), default='pending_review', nullable=False, index=True) # pending_review, converted_to_faq, rejected
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now)

    chat_session = db.relationship('ChatSessionContext', backref=db.backref('llm_interaction_logs', lazy='dynamic'))

    def __repr__(self):
        return f"<LlmInteractionLog id={self.id} session_id={self.chat_session_id} status='{self.status}'>"


class EncEncuesta(db.Model, TimestampMixin):
    __tablename__ = "enc_encuesta"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    slug = db.Column(db.String(160), unique=True, nullable=False)
    titulo = db.Column(db.String(255), nullable=False)
    descripcion = db.Column(db.Text, nullable=True)
    tipo = db.Column(db.String(50), nullable=False, default="opinion")
    estado = db.Column(db.String(30), nullable=False, default="borrador")
    inicio_at = db.Column(db.DateTime(timezone=True), nullable=True)
    fin_at = db.Column(db.DateTime(timezone=True), nullable=True)
    requiere_identidad = db.Column(db.Boolean, default=False, nullable=False)
    politica_unicidad = db.Column(db.String(30), nullable=False, default="libre")
    anonimo_permitido = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    preguntas = db.relationship(
        "EncPregunta",
        back_populates="encuesta",
        cascade="all, delete-orphan",
        order_by="EncPregunta.orden",
    )
    respuestas = db.relationship(
        "EncRespuesta",
        back_populates="encuesta",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    segmentos = db.relationship("EncSegmento", back_populates="encuesta", cascade="all, delete-orphan")
    links = db.relationship("EncLink", back_populates="encuesta", cascade="all, delete-orphan")
    snapshots = db.relationship("EncAnchorSnapshot", back_populates="encuesta", cascade="all, delete-orphan")

    def esta_activa(self, at: Optional[datetime] = None) -> bool:
        if self.estado != "publicada":
            return False
        reference = _coerce_reference_time(at)
        inicio = _normalize_public_survey_datetime(self.inicio_at)
        if inicio:
            inicio = inicio.astimezone(timezone.utc)
        fin = _normalize_public_survey_datetime(self.fin_at)
        if fin:
            fin = fin.astimezone(timezone.utc)

        if inicio and reference < inicio:
            return False
        if fin and reference > fin:
            return False
        return True


class EncPregunta(db.Model, TimestampMixin):
    __tablename__ = "enc_pregunta"
    __table_args__ = (
        UniqueConstraint("encuesta_id", "orden", name="uq_enc_pregunta_encuesta_orden"),
    )

    id = db.Column(db.Integer, primary_key=True)
    encuesta_id = db.Column(db.Integer, db.ForeignKey("enc_encuesta.id", ondelete="CASCADE"), nullable=False)
    orden = db.Column(db.Integer, nullable=False)
    tipo = db.Column(db.String(30), nullable=False)
    texto = db.Column(db.Text, nullable=False)
    obligatoria = db.Column(db.Boolean, default=False, nullable=False)
    min_selecciones = db.Column(db.Integer, nullable=True)
    max_selecciones = db.Column(db.Integer, nullable=True)

    encuesta = db.relationship("EncEncuesta", back_populates="preguntas")
    opciones = db.relationship(
        "EncOpcion",
        back_populates="pregunta",
        cascade="all, delete-orphan",
        order_by="EncOpcion.orden",
    )


class EncOpcion(db.Model, TimestampMixin):
    __tablename__ = "enc_opcion"
    __table_args__ = (
        UniqueConstraint("pregunta_id", "orden", name="uq_enc_opcion_pregunta_orden"),
    )

    id = db.Column(db.Integer, primary_key=True)
    pregunta_id = db.Column(db.Integer, db.ForeignKey("enc_pregunta.id", ondelete="CASCADE"), nullable=False)
    orden = db.Column(db.Integer, nullable=False)
    texto = db.Column(db.Text, nullable=False)
    valor = db.Column(db.String(120), nullable=True)

    pregunta = db.relationship("EncPregunta", back_populates="opciones")


class EncRespuesta(db.Model, TimestampMixin):
    __tablename__ = "enc_respuesta"
    __table_args__ = (
        UniqueConstraint("encuesta_id", "huella_unica", name="uq_enc_respuesta_huella"),
        Index("ix_enc_respuesta_encuesta_submitted", "encuesta_id", "submitted_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    encuesta_id = db.Column(db.Integer, db.ForeignKey("enc_encuesta.id", ondelete="CASCADE"), nullable=False)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    huella_unica = db.Column(db.String(255), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    dni = db.Column(db.String(32), nullable=True)
    phone = db.Column(db.String(32), nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    ua = db.Column(db.String(255), nullable=True)
    lat = db.Column(db.Float, nullable=True)
    lng = db.Column(db.Float, nullable=True)
    utm_source = db.Column(db.String(120), nullable=True)
    utm_campaign = db.Column(db.String(120), nullable=True)
    canal = db.Column(db.String(64), nullable=True)
    genero = db.Column(db.String(30), nullable=True)
    edad = db.Column(db.Integer, nullable=True)
    anio_nacimiento = db.Column(db.Integer, nullable=True)
    rango_etario = db.Column(db.String(30), nullable=True)
    barrio = db.Column(db.String(120), nullable=True)
    ciudad = db.Column(db.String(120), nullable=True)
    provincia = db.Column(db.String(120), nullable=True)
    pais = db.Column(db.String(120), nullable=True)
    metadata_payload = db.Column(JSONType, nullable=True)
    submitted_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    content_hash = db.Column(db.String(128), nullable=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey("enc_anchor_snapshot.id"), nullable=True)

    encuesta = db.relationship("EncEncuesta", back_populates="respuestas")
    detalles = db.relationship(
        "EncRespuestaDetalle",
        back_populates="respuesta",
        cascade="all, delete-orphan",
        order_by="EncRespuestaDetalle.id",
    )
    snapshot = db.relationship("EncAnchorSnapshot", back_populates="respuestas")


class EncRespuestaDetalle(db.Model, TimestampMixin):
    __tablename__ = "enc_respuesta_detalle"

    id = db.Column(db.Integer, primary_key=True)
    respuesta_id = db.Column(db.Integer, db.ForeignKey("enc_respuesta.id", ondelete="CASCADE"), nullable=False)
    pregunta_id = db.Column(db.Integer, db.ForeignKey("enc_pregunta.id", ondelete="CASCADE"), nullable=False)
    opcion_id = db.Column(db.Integer, db.ForeignKey("enc_opcion.id", ondelete="SET NULL"), nullable=True)
    texto_libre = db.Column(db.Text, nullable=True)

    respuesta = db.relationship("EncRespuesta", back_populates="detalles")
    pregunta = db.relationship("EncPregunta")
    opcion = db.relationship("EncOpcion")


class EncSegmento(db.Model, TimestampMixin):
    __tablename__ = "enc_segmento"
    __table_args__ = (
        UniqueConstraint("encuesta_id", "clave", "valor", name="uq_enc_segmento_clave_valor"),
    )

    id = db.Column(db.Integer, primary_key=True)
    encuesta_id = db.Column(db.Integer, db.ForeignKey("enc_encuesta.id", ondelete="CASCADE"), nullable=False)
    clave = db.Column(db.String(120), nullable=False)
    valor = db.Column(db.String(255), nullable=False)

    encuesta = db.relationship("EncEncuesta", back_populates="segmentos")


class EncLink(db.Model, TimestampMixin):
    __tablename__ = "enc_link"
    __table_args__ = (
        UniqueConstraint("encuesta_id", "slug_publico", name="uq_enc_link_slug"),
    )

    id = db.Column(db.Integer, primary_key=True)
    encuesta_id = db.Column(db.Integer, db.ForeignKey("enc_encuesta.id", ondelete="CASCADE"), nullable=False)
    slug_publico = db.Column(db.String(160), nullable=False, index=True)
    canal = db.Column(db.String(64), nullable=True)
    utm_source = db.Column(db.String(120), nullable=True)
    utm_campaign = db.Column(db.String(120), nullable=True)

    encuesta = db.relationship("EncEncuesta", back_populates="links")


class EncAnchorSnapshot(db.Model, TimestampMixin):
    __tablename__ = "enc_anchor_snapshot"

    id = db.Column(db.Integer, primary_key=True)
    encuesta_id = db.Column(db.Integer, db.ForeignKey("enc_encuesta.id", ondelete="CASCADE"), nullable=False)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    algo = db.Column(db.String(40), nullable=False, default="sha256")
    root_hash = db.Column(db.String(128), nullable=False)
    total_respuestas = db.Column(db.Integer, nullable=False)
    desde_at = db.Column(db.DateTime(timezone=True), nullable=False)
    hasta_at = db.Column(db.DateTime(timezone=True), nullable=False)
    chain = db.Column(db.String(40), nullable=True)
    tx_id = db.Column(db.String(120), nullable=True)
    anchor_status = db.Column(db.String(30), nullable=False, default="draft")
    anchor_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    encuesta = db.relationship("EncEncuesta", back_populates="snapshots")
    respuestas = db.relationship("EncRespuesta", back_populates="snapshot")


class FeatureToggle(db.Model, TimestampMixin):
    __tablename__ = "features"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.BigInteger, nullable=False, index=True)
    key = db.Column(db.String(64), nullable=False, index=True)
    value = db.Column(db.String(255), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("owner_id", "key", name="uq_features_owner_key"),
    )

    @property
    def bool_value(self) -> Optional[bool]:
        if self.value is None:
            return None
        normalized = str(self.value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return None
