# script_crear_usuario_demo.py

from extensions import db
from models import User
from app import create_app

app = create_app()

with app.app_context():
    token_demo = "154485a9-6671-4161-b310-9524a58cbf37"

    # Verificar si ya existe
    user = User.query.filter_by(token=token_demo).first()
    if not user:
        user = User(
            nombre_empresa="Demo Empresa",
            token=token_demo,
            plan="demo",
            preguntas_usadas=0,
            limite_preguntas=15,
            rubro_id=1,
            tipo_chat="pyme",
        )
        db.session.add(user)
        db.session.commit()
        print("Usuario demo creado.")
    else:
        print("Usuario demo ya existe.")
