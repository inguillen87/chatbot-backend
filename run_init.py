import os
os.environ["ALLOW_DB_INIT"] = "1"

print("🔥 INICIO del archivo run_init.py")

from app import app
from extensions import db
from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
from init_tenants import init_tenants

def cargar_datos_directamente():
    print("⚙️ Ejecutando carga de datos directamente...")
    with app.app_context():
        db.create_all()
        print("✅ DB creada correctamente.")

        # Carga rubros y FAQs primero
        cargar_faqs()

        # Luego usuarios legacy
        cargar_usuarios_demo()

        # Sugerencias
        cargar_sugerencias()

        # Carga tenants completos desde demo_rubros.json (Fix definitivo)
        init_tenants()

        print("✅ Carga completa terminada.")

if __name__ == "__main__":
    print("✅ Entró en el bloque main")
    cargar_datos_directamente()
