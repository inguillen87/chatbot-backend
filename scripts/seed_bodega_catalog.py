import sys
import os
import json
from decimal import Decimal
from datetime import date

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import CatalogoItem, User, Rubro
from utils.money_ar import parse_ars

SEED_DATA = [
  {"brand":"VINCENT","name":"Torrontes","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"15620.00","price_unit_ars":"2603.33","suggested_public_unit_ars":"5207"},
  {"brand":"VINCENT","name":"Malbec","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"15620.00","price_unit_ars":"2603.33","suggested_public_unit_ars":"5207"},
  {"brand":"VINCENT","name":"Cabernet Sauvignon","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"15620.00","price_unit_ars":"2603.33","suggested_public_unit_ars":"5207"},
  {"brand":"VINCENT","name":"Blanco Dulce","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"15950.00","price_unit_ars":"2658.33","suggested_public_unit_ars":"5317"},
  {"brand":"VINCENT","name":"Rojo Dulce","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"15950.00","price_unit_ars":"2658.33","suggested_public_unit_ars":"5317"},
  {"brand":"VINCENT","name":"Malbec Gran Corte","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"16500.00","price_unit_ars":"2750.00","suggested_public_unit_ars":"5500"},

  {"brand":"CU4TROFINCAS","name":"Malbec / Syrah","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"17270.00","price_unit_ars":"2878.33","suggested_public_unit_ars":"6332"},
  {"brand":"CU4TROFINCAS","name":"Cabernet Sauvignon / Merlot","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"17270.00","price_unit_ars":"2878.33","suggested_public_unit_ars":"6332"},
  {"brand":"CU4TROFINCAS","name":"Extra Brut","presentation":"750ml","units_per_case":6,"units_per_pallet":110,"price_case_ars":"26730.00","price_unit_ars":"4455.00","suggested_public_unit_ars":"9801"},

  {"brand":"RAICES ARGENTINAS","name":"Malbec","presentation":"750ml","units_per_case":6,"units_per_pallet":120,"price_case_ars":"21120.00","price_unit_ars":"3520.00","suggested_public_unit_ars":"8800"},
  {"brand":"RAICES ARGENTINAS","name":"Cabernet Sauvignon","presentation":"750ml","units_per_case":6,"units_per_pallet":120,"price_case_ars":"21120.00","price_unit_ars":"3520.00","suggested_public_unit_ars":"8800"},

  {"brand":"QUBO","name":"Bag in Box 3L Malbec","presentation":"3L","units_per_case":4,"units_per_pallet":90,"price_case_ars":"35200.00","price_unit_ars":"8800.00","suggested_public_unit_ars":"17600"},
  {"brand":"QUBO","name":"Bag in Box 3L Cabernet Sauvignon","presentation":"3L","units_per_case":4,"units_per_pallet":90,"price_case_ars":"35200.00","price_unit_ars":"8800.00","suggested_public_unit_ars":"17600"},

  {"brand":"NO SOS VOS SOY YO","name":"Winemaker's Project Cabernet Franc","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"24750.00","price_unit_ars":"4125.00","suggested_public_unit_ars":"10313"},
  {"brand":"NO SOS VOS SOY YO","name":"Winemaker's Project Malbec","presentation":"750ml","units_per_case":6,"units_per_pallet":140,"price_case_ars":"24750.00","price_unit_ars":"4125.00","suggested_public_unit_ars":"10313"}
]

def seed_catalog():
    from config import Config
    # Ensure SESSION_TYPE is set to filesystem to avoid SQLAlchemy session issues in standalone scripts
    Config.SESSION_TYPE = 'filesystem'

    app = create_app(Config)

    with app.app_context():
        # Force recreation of tables to pick up model changes
        # Explicitly drop the table first to ensure schema update
        CatalogoItem.__table__.drop(db.engine, checkfirst=True)
        db.create_all()

        # Ensure we have a user/pyme to attach items to
        # In a real scenario, this would be the specific Pyme User ID.
        # For this seed, we try to find "Bodega Cuatro Fincas" or create it.
        user_name = "Bodega Cuatro Fincas"
        pyme_user = User.query.filter_by(name=user_name).first()

        if not pyme_user:
            # Try to find any pyme user
            pyme_user = User.query.filter_by(tipo_chat="pyme").first()

        if not pyme_user:
            print("No Pyme user found. Creating dummy 'Bodega Cuatro Fincas'...")
            # Ensure 'bodega' rubro exists
            rubro = Rubro.query.filter_by(clave="bodega").first()
            if not rubro:
                rubro = Rubro(clave="bodega", nombre="Bodega")
                db.session.add(rubro)
                db.session.commit()

            pyme_user = User(
                name=user_name,
                email="bodega@demo.com",
                rol="empresa",
                tipo_chat="pyme",
                rubro_id=rubro.id,
                nombre_empresa=user_name
            )
            pyme_user.set_password("demo123")
            db.session.add(pyme_user)
            db.session.commit()

        print(f"Seeding catalog for user ID: {pyme_user.id} ({pyme_user.name})")

        # Clear existing items for this user to avoid duplicates on re-run
        deleted = CatalogoItem.query.filter_by(user_id=pyme_user.id).delete()
        print(f"Deleted {deleted} existing items.")

        effective_date = date(2024, 9, 1)

        count = 0
        for item_data in SEED_DATA:
            # Map JSON fields to Model fields
            item = CatalogoItem(
                user_id=pyme_user.id,
                tenant_id=pyme_user.tenant_id,
                nombre=f"{item_data['brand']} {item_data['name']}",
                marca=item_data['brand'],
                unidad=item_data['presentation'], # Presentation as 'unidad'
                unidad_por_caja=item_data['units_per_case'],
                unidades_por_pallet=item_data['units_per_pallet'],

                # Prices
                precio_por_caja=parse_ars(str(item_data['price_case_ars'])),
                precio_monetario=parse_ars(str(item_data['price_unit_ars'])), # B2B unit price
                precio_sugerido=parse_ars(str(item_data['suggested_public_unit_ars'])), # Retail suggested

                moneda="ARS",
                fecha_vigencia=effective_date,

                # Default legacy fields
                precio=str(item_data['suggested_public_unit_ars']), # Legacy string price
                cantidad="100", # Dummy stock
                disponible=True,
                modalidad="venta"
            )
            db.session.add(item)
            count += 1

        db.session.commit()
        print(f"✅ Successfully seeded {count} catalog items.")

if __name__ == "__main__":
    seed_catalog()
