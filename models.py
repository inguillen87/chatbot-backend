from datetime import datetime
from utils.time_utils import get_local_now
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, Text
from sqlalchemy import Index
from sqlalchemy.dialects.sqlite import JSON
from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
import uuid
import json

class Rubro(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(50), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=True)
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
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

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
    rol = db.Column(db.String(30), default="usuario")
    tipo_chat = db.Column(db.String(20), nullable=True)
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
    acepto_terminos = db.Column(Boolean, default=False)
    fecha_aceptacion_terminos = db.Column(DateTime, nullable=True)
    acepta_marketing = db.Column(Boolean, default=False)
    fecha_aceptacion_marketing = db.Column(DateTime, nullable=True)
    tags = db.Column(db.String(255), default="")
    ticket_categorias = db.Column(db.String(255), nullable=True)
    horario = db.Column(db.String(100), nullable=True)
    plan = db.Column(db.String(20), default="gratis")
    preguntas_usadas = db.Column(db.Integer, default=0)
    limite_preguntas = db.Column(db.Integer, default=50)
    last_reset = db.Column(db.DateTime, default=datetime.utcnow)
    empresa_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    empresa = db.relationship('User', remote_side=[id], backref='clientes')
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    rubro = db.relationship("Rubro", backref="usuarios")
    catalogo_items = db.relationship('CatalogoItem', backref='user', lazy=True)
    catalogo_embeddings = db.relationship('CatalogoEmbedding', backref='user', lazy=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow) # Nuevo campo

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

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
    pregunta = db.Column(db.Text, nullable=False)
    asunto = db.Column(db.String(200), nullable=True)
    categoria = db.Column(db.String(100), nullable=True)
    municipio_id = db.Column(db.Integer, nullable=True)
    user_id = db.Column(db.Integer, nullable=True)
    municipio_id = db.Column(db.Integer, nullable=True)
    estado = db.Column(db.String(30), default="nuevo")
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    ultima_actividad = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    nro_ticket = db.Column(db.Integer, nullable=False, unique=True)
    detalles = db.Column(db.Text)  # <-- Esto es lo que falta
    direccion = db.Column(db.String(255), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    fecha = db.Column(db.DateTime, default=get_local_now)
    # archivo_url = db.Column(db.String(255), nullable=True) # Campo obsoleto, se usará la relación
    comentarios = db.relationship('TicketComentario', back_populates='municipio_ticket', lazy='dynamic')
    archivos = db.relationship(
        'ArchivoAdjunto',
        foreign_keys='[ArchivoAdjunto.municipio_ticket_id]',
        backref='municipio_ticket_ref', # Usar un backref específico si PymeTicket también tiene uno
        lazy='dynamic', # O 'select'/'joined' según la necesidad de carga
        cascade="all, delete-orphan" # Opcional: si se borra el ticket, borrar sus archivos
    )

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
    fecha = db.Column(db.DateTime, default=get_local_now)
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    archivo_url = db.Column(db.String(255), nullable=True)
    telefono = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    dni = db.Column(db.String(20), nullable=True)
    estado_cliente = db.Column(db.String(30), default="no_definido")
    direccion = db.Column(db.String(255), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    comentarios = db.relationship('TicketComentario', back_populates='pyme_ticket', lazy='dynamic')

class PymePedido(db.Model):
    __tablename__ = "pyme_pedido"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # Permite anónimos
    user = db.relationship('User', backref='pyme_pedidos')
    nro_pedido = db.Column(db.String(50), unique=True, nullable=False)
    asunto = db.Column(db.String(255), nullable=True)
    estado = db.Column(db.String(30), default="pendiente")
    detalles = db.Column(db.Text, nullable=True) # JSON string
    monto_total = db.Column(db.Float, nullable=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    nombre_cliente = db.Column(db.String(100), nullable=True)
    email_cliente = db.Column(db.String(100), nullable=True)
    telefono_cliente = db.Column(db.String(50), nullable=True)
    rubro = db.Column(db.String(100), nullable=True)
    direccion = db.Column(db.String(255), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)

    def __init__(self, asunto, detalles, rubro, nombre_cliente=None, email_cliente=None, telefono_cliente=None, user_id=None, direccion=None, latitud=None, longitud=None):
        self.asunto = asunto
        self.detalles = detalles
        self.rubro = rubro
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
            "nro_pedido": self.nro_pedido,
            "asunto": self.asunto,
            "estado": self.estado,
            "detalles": detalles_json,
            "monto_total": self.monto_total,
            "fecha_creacion": self.fecha.isoformat() if self.fecha else None,
            "nombre_cliente": self.nombre_cliente,
            "email_cliente": self.email_cliente,
            "telefono_cliente": self.telefono_cliente,
            "rubro": self.rubro,
            "direccion": self.direccion,
            "latitud": self.latitud,
            "longitud": self.longitud,
            "user_id": self.user_id
        }

    def __repr__(self):
        return f"<PymePedido {self.nro_pedido} - {self.asunto}>"

class TicketComentario(db.Model):
    __tablename__ = "ticket_comentario"
    id = db.Column(db.Integer, primary_key=True)
    pyme_ticket_id = db.Column(db.Integer, db.ForeignKey('pyme_ticket.id'), nullable=True)
    municipio_ticket_id = db.Column(db.Integer, db.ForeignKey('municipio_ticket.id'), nullable=True)
    comentario = db.Column(db.Text, nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, nullable=True)
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    es_admin = db.Column(db.Boolean, default=False)
    pyme_ticket = db.relationship('PymeTicket', back_populates='comentarios')
    municipio_ticket = db.relationship('MunicipioTicket', back_populates='comentarios')


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
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

class AnalisisArchivo(db.Model):
    __tablename__ = "analisis_archivo"
    id = db.Column(db.Integer, primary_key=True)
    archivo_adjunto_id = db.Column(db.Integer, db.ForeignKey("archivo_adjunto.id"), nullable=False, index=True)
    resumen = db.Column(db.Text, nullable=True)
    estado_analisis = db.Column(db.String(50), nullable=True, default="pendiente") # ej: pendiente, procesando, completado, error
    fecha_analisis = db.Column(db.DateTime, nullable=True)
    error_analisis = db.Column(db.Text, nullable=True) # Para guardar mensajes de error si falla el análisis

    # Nuevos campos para análisis avanzado
    texto_extraido = db.Column(db.Text, nullable=True) # Para OCR completo o texto de PDF
    datos_estructurados = db.Column(db.JSON, nullable=True) # Para JSON con data extraída (items, cantidades, etc.)
    tipo_analisis = db.Column(db.String(100), nullable=True) # ej: 'resumen_texto', 'vision_ocr', 'document_ai_form'

    archivo_adjunto = db.relationship("ArchivoAdjunto", backref=db.backref("analisis", uselist=False, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<AnalisisArchivo id={self.id} para archivo_id={self.archivo_adjunto_id} estado='{self.estado_analisis}'>"

class CatalogoItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    nombre = db.Column(db.String(255), nullable=False)
    descripcion = db.Column(db.String(1024))
    precio = db.Column(db.String(50))
    cantidad = db.Column(db.String(50))
    sku = db.Column(db.String(100), nullable=True, index=True)
    marca = db.Column(db.String(100), nullable=True, index=True)
    categoria = db.Column(db.String(100))
    unidad = db.Column(db.String(50))
    # Nuevos campos para información más detallada del catálogo
    descripcion_corta = db.Column(db.String(512), nullable=True)
    promocion_info = db.Column(db.String(255), nullable=True) # Para texto de promociones, ej: "20% OFF"
    # 'cantidad' se usa actualmente para stock. Si se necesita diferenciar, añadir un campo 'stock' dedicado.
    # 'texto' se usa para almacenar el texto combinado que se usó para el embedding.
    texto = db.Column(db.Text, nullable=True)
    embedding = db.Column(db.PickleType, nullable=True) # Este campo podría eliminarse si los embeddings solo viven en Qdrant
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<CatalogoItem {self.id} para user {self.user_id}>"

class CatalogoEmbedding(db.Model):
    __tablename__ = "catalogo_embedding"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    nombre = db.Column(db.String(255))
    cantidad = db.Column(db.Float)
    descripcion = db.Column(db.String(1024))
    precio = db.Column(db.String(50))
    embedding_vector = db.Column(JSON)

class Conversacion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    pregunta = db.Column(db.Text, nullable=False)
    respuesta = db.Column(db.Text, nullable=False)
    fuente = db.Column(db.String(50), nullable=False)
    rubro = db.Column(db.String(100), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    session_id = db.Column(db.String(36), default=lambda: str(uuid.uuid4()), nullable=False)
    __table_args__ = (Index('ix_conversacion_session_id', 'session_id'),)

class SitioWebInfo(db.Model):
    __tablename__ = 'sitio_web_info'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    rubro_id = db.Column(db.Integer, db.ForeignKey("rubro.id"), nullable=True)
    url = db.Column(db.String(255), nullable=False)
    datos_json = db.Column(db.Text, nullable=False)
    fecha_scraping = db.Column(db.DateTime, default=datetime.utcnow)
    actualizado = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<SitioWebInfo id={self.id} url={self.url}>"

class Log(db.Model):
    __tablename__ = "logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    pregunta = db.Column(db.String(500), nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

class TicketSatisfaccion(db.Model):
    __tablename__ = "ticket_satisfaccion"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)
    puntuacion = db.Column(db.Integer, nullable=False)
    comentario = db.Column(db.Text, nullable=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

class Recordatorio(db.Model):
    __tablename__ = "recordatorio"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    descripcion = db.Column(db.String(255), nullable=True)
    fecha_vencimiento = db.Column(db.DateTime, nullable=False)
    enviado = db.Column(db.Boolean, default=False)

class Reaccion(db.Model):
    __tablename__ = "reaccion"
    id = db.Column(db.Integer, primary_key=True)
    conversacion_id = db.Column(db.Integer, db.ForeignKey("conversacion.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    emoji = db.Column(db.String(5), nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    conversacion = db.relationship("Conversacion", backref="reacciones")
    user = db.relationship("User")

class SugerenciaCiudadano(db.Model):
    __tablename__ = "sugerencia_ciudadano"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True) # Puede ser anónimo o registrado
    anon_id = db.Column(db.String(80), nullable=True, index=True)
    municipio_id = db.Column(db.Integer, nullable=True) # Para vincular a qué municipio pertenece la sugerencia
    texto_sugerencia = db.Column(db.Text, nullable=False)
    fecha = db.Column(db.DateTime, default=get_local_now)
    estado = db.Column(db.String(30), default="nueva") # Ej: nueva, revisada, implementada, descartada
    categoria = db.Column(db.String(100), nullable=True) # Nueva columna para categorizar

    user = db.relationship("User", backref="sugerencias_ciudadano")

    def __repr__(self):
        return f"<SugerenciaCiudadano {self.id} por User {self.user_id or self.anon_id}>"

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
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    embedding = db.Column(db.PickleType, nullable=True) # Almacenará el embedding de Cohere
    keywords = db.Column(db.JSON, nullable=True) # Array de strings
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    # Opcional: Para vincular plantillas a un usuario/empresa específica si fuera necesario en el futuro
    # user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    # user = db.relationship('User', backref=db.backref('plantillas_respuesta', lazy='dynamic'))

    def __repr__(self):
        return f"<PlantillasRespuesta id={self.id} name='{self.name}'>"


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

    fecha_inicio = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    fecha_fin = db.Column(db.DateTime, nullable=True) # Nullable si la promo no tiene fin
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    codigo_promocion = db.Column(db.String(50), nullable=True, unique=True, index=True) # Si requiere un código
    uso_maximo_general = db.Column(db.Integer, nullable=True) # Límite total de usos
    usos_actuales_general = db.Column(db.Integer, default=0)
    uso_maximo_por_cliente = db.Column(db.Integer, nullable=True) # Límite por cliente

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
#     fecha_uso = db.Column(db.DateTime, default=datetime.utcnow)
#     # Se podría añadir info sobre el descuento aplicado si es variable o para auditoría
#
#     promocion = db.relationship('Promocion', backref='usos_por_clientes')
#     cliente = db.relationship('User', backref='promociones_usadas')
#     pedido = db.relationship('PymePedido', backref='promociones_aplicadas_en_pedido')


print("✅ models.py fue importado con éxito y contiene modelos.")
