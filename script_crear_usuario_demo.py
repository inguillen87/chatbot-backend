# script_crear_usuario_demo.py

from extensions import db
from models import User, Rubro
from services.logic import es_rubro_publico
from app import create_app

app = create_app()

with app.app_context():
    token_demo = "154485a9-6671-4161-b310-9524a58cbf37"

    # Obtener rubro de ejemplo (ID=1 por defecto)
    rubro = Rubro.query.get(1)
    tipo_chat = "municipio" if es_rubro_publico(rubro) else "pyme"

    # Verificar si ya existe
    user = User.query.filter_by(token=token_demo).first()
    if not user:
        user = User(
            nombre_empresa="Demo Empresa",
            token=token_demo,
            plan="demo",
            preguntas_usadas=0,
            limite_preguntas=15,
            rubro_id=rubro.id if rubro else 1,
            tipo_chat=tipo_chat,
        )
        db.session.add(user)
        db.session.commit()
        print("Usuario demo creado.")
    else:
        print("Usuario demo ya existe.")
