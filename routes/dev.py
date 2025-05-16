from flask import Blueprint, jsonify
from models import User
from extensions import db
from datetime import datetime
import uuid

dev_bp = Blueprint('dev', __name__)

@dev_bp.route('/crear-usuarios-prueba-reales')
def crear_usuarios_prueba_reales():
    usuarios = [
        {"nombre": "TiendaGratis", "plan": "free", "rubro_id": 1},
        {"nombre": "EstudioPro", "plan": "pro", "rubro_id": 2},
        {"nombre": "CobranzasVIP", "plan": "premium", "rubro_id": 3},
    ]

    creados = []
    for u in usuarios:
        token = str(uuid.uuid4())
        user = User(
            name=u["nombre"],
            token=token,
            plan=u["plan"],
            preguntas_usadas=0,
            last_reset=datetime.utcnow(),
            rubro_id=u["rubro_id"]
        )
        db.session.add(user)
        creados.append({
            "nombre": u["nombre"],
            "plan": u["plan"],
            "token": token,
            "rubro_id": u["rubro_id"]
        })

    db.session.commit()
    return jsonify({"usuarios_creados": creados})
