from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey
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
    nombre_empresa = db.Column(db.String(150), nullable=True)
    direccion = db.Column(db.String(200), nullable=True)
    ciudad = db.Column(db.String(100), nullable=True)
    provincia = db.Column(db.String(100), nullable=True)
    pais = db.Column(db.String(100), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    telefono = db.Column(db.String(20), nullable=True)
    link_web = db.Column(db.String(255), nullable=True)
    acepto_terminos = db.Column(Boolean, default=False)
    fecha_aceptacion_terminos = db.Column(DateTime, nullable=True)
    horario = db.Column(db.String(100), nullable=True) 
    plan = db.Column(db.String(20), default="gratis")
    preguntas_usadas = db.Column(db.Integer, default=0)
    limite_preguntas = db.Column(db.Integer, default=50)
    last_reset = db.Column(db.DateTime, default=datetime.utcnow)
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    rubro = db.relationship("Rubro", backref="usuarios")
    catalogo_items = db.relationship('CatalogoItem', backref='user', lazy=True)
    catalogo_embeddings = db.relationship('CatalogoEmbedding', backref='user', lazy=True)
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
    def __repr__(self):
        return f"<User {self.email}>"

class MunicipioTicket(db.Model):
    __tablename__ = "municipio_ticket"
    id = db.Column(db.Integer, primary_key=True)
    pregunta = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    estado = db.Column(db.String(30), default="nuevo")
    nro_ticket = db.Column(db.Integer, nullable=False, unique=True)
    fecha = db.Column(db.DateTime, default=db.func.now())
    archivo_url = db.Column(db.String(255), nullable=True)
    comentarios = db.relationship(
        'TicketComentario',
        primaryjoin="and_(MunicipioTicket.id==foreign(TicketComentario.ticket_id), TicketComentario.tipo_ticket=='municipio')",
        backref='municipio_ticket',
        lazy='dynamic'
    )

class PymeTicket(db.Model):
    __tablename__ = "pyme_ticket"
    id = db.Column(db.Integer, primary_key=True)
    pregunta = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    estado = db.Column(db.String(30), default="nuevo")
    nro_ticket = db.Column(db.Integer, nullable=False, unique=True)
    fecha = db.Column(db.DateTime, default=db.func.now())
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    archivo_url = db.Column(db.String(255), nullable=True)
    telefono = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    dni = db.Column(db.String(20), nullable=True)
    estado_cliente = db.Column(db.String(30), default="no_definido")
    comentarios = db.relationship(
        'TicketComentario',
        primaryjoin="and_(PymeTicket.id==foreign(TicketComentario.ticket_id), TicketComentario.tipo_ticket=='pyme')",
        backref='pyme_ticket',
        lazy='dynamic'
    )

class TicketComentario(db.Model):
    __tablename__ = "ticket_comentario"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, nullable=False)   # No FK por polimorfismo
    tipo_ticket = db.Column(db.String(20), nullable=False)  # 'pyme' o 'municipio'
    comentario = db.Column(db.Text, nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, nullable=True)
    telefono = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    dni = db.Column(db.String(20), nullable=True)
    estado_cliente = db.Column(db.String(30), default="no_definido")
    es_admin = db.Column(db.Boolean, default=False)  # Si es admin el que responde

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
    texto = db.Column(db.Text, nullable=True)
    embedding = db.Column(db.PickleType, nullable=True)
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
    # --- LÍNEAS QUE VAMOS A AGREGAR ---
    # 1. El nuevo campo para agrupar los chats de una misma conversación
    session_id = db.Column(UUID(as_uuid=True), default=uuid.uuid4, nullable=False)

    # 2. El nuevo índice para que las búsquedas por sesión sean súper rápidas
    __table_args__ = (Index('ix_conversacion_session_id', 'session_id'),)

class SitioWebInfo(db.Model):
    __tablename__ = 'sitio_web_info'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)  # Pyme, Municipio, etc.
    rubro_id = db.Column(db.Integer, db.ForeignKey("rubro.id"), nullable=True)
    url = db.Column(db.String(255), nullable=False)
    datos_json = db.Column(db.Text, nullable=False)  # Guarda todo el dict serializado
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

def generate_token():
    return str(uuid.uuid4())

print("✅ models.py fue importado con éxito y contiene modelos.")
