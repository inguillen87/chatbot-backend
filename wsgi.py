from app import create_app
from extensions import migrate  # 👈 IMPORTANTE: esto activa los comandos 'flask db'

app = create_app()
