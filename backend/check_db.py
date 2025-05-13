from app import create_app
from extensions import db
from models import QA

app = create_app()

with app.app_context():
    all_qas = QA.query.all()
    print(f"Total de preguntas cargadas: {len(all_qas)}")
    for qa in all_qas:
        print(f"- {qa.question}")
