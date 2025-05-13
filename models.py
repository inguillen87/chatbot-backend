from datetime import datetime
from extensions import db
import uuid

class QA(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    question = db.Column(db.String(255), nullable=False)
    keywords = db.Column(db.String(255))
    answer = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

def generate_token():
    return str(uuid.uuid4())

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    token = db.Column(db.String(255), unique=True, default=generate_token)
    plan = db.Column(db.String(20), default="free")
    preguntas_usadas = db.Column(db.Integer, default=0)
    last_reset = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<User {self.email}>"


