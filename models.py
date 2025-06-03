from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.dialects.sqlite import JSON
from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
import uuid
import json
class Rubro(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(50), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
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
    
    # Datos de contacto y perfil
    nombre_empresa = db.Column(db.String(150), nullable=True)
    direccion = db.Column(db.String(200), nullable=True) # Permitir nulo si se actualiza después
    ciudad = db.Column(db.String(100), nullable=True)
    provincia = db.Column(db.String(100), nullable=True)
    pais = db.Column(db.String(100), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    telefono = db.Column(db.String(20), nullable=True)
    link_web = db.Column(db.String(255), nullable=True)
    acepto_terminos = db.Column(Boolean, default=False)
    fecha_aceptacion_terminos = db.Column(DateTime, nullable=True)
    
    # Este es el campo que guarda el string JSON en la base de datos
    horario = db.Column(db.String(100), nullable=True) 
    
    # Plan y uso
    plan = db.Column(db.String(20), default="gratis")
    preguntas_usadas = db.Column(db.Integer, default=0)
    limite_preguntas = db.Column(db.Integer, default=50)
    last_reset = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relaciones
    rubro_id = db.Column(db.Integer, db.ForeignKey('rubro.id'), nullable=True)
    rubro = db.relationship("Rubro", backref="usuarios")
    catalogo_items = db.relationship('CatalogoItem', backref='user', lazy=True)
    catalogo_embeddings = db.relationship('CatalogoEmbedding', backref='user', lazy=True)

    # Métodos de seguridad
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def horario_json(self):
        """
        Propiedad para obtener el campo 'horario' (que es un string JSON)
        como un diccionario Python.
        Devuelve el diccionario parseado, o None si el horario está vacío o no es JSON válido.
        """
        if self.horario: # Si hay algo en el campo horario
            try:
                # Intenta convertir el string self.horario a un diccionario Python
                return json.loads(self.horario)
            except json.JSONDecodeError:
                # Si el string en la BD no es JSON válido, devolvemos None.
                # Podrías loguear un error aquí si quisieras:
                # import logging
                # logging.warning(f"Error al parsear JSON del campo 'horario' para user {self.id}: {self.horario}")
                return None 
        return None # Devolver None si self.horario está vacío

    def __repr__(self):
        return f"<User {self.email}>"

class CatalogoItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    nombre = db.Column(db.String(255), nullable=False)
    descripcion = db.Column(db.String(1024))
    precio = db.Column(db.String(50))
    cantidad = db.Column(db.String(50))
    sku = db.Column(db.String(100), nullable=True, index=True) # Nuevo
    marca = db.Column(db.String(100), nullable=True, index=True) # Nuevo
    categoria = db.Column(db.String(100))
    unidad = db.Column(db.String(50))
    texto = db.Column(db.Text, nullable=True)  # opcional
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

class Log(db.Model):
    __tablename__ = "logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    pregunta = db.Column(db.String(500), nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

def generate_token():
    return str(uuid.uuid4())

print("✅ models.py fue importado con éxito y contiene modelos.")
