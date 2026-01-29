import sys
import os
from decimal import Decimal

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import CatalogoItem, User
from config import Config

Config.SESSION_TYPE = 'filesystem'
app = create_app(Config)

with app.app_context():
    items = CatalogoItem.query.all()
    print(f"Total items found: {len(items)}")

    # Check a specific item to ensure fields are correct
    torrontes = CatalogoItem.query.filter(CatalogoItem.nombre.like("%Torrontes%")).first()
    if torrontes:
        print(f"Item found: {torrontes.nombre}")
        print(f"  Brand: {torrontes.marca}")
        print(f"  Unit: {torrontes.unidad}")
        print(f"  Units per case: {torrontes.unidad_por_caja}")
        print(f"  Units per pallet: {torrontes.unidades_por_pallet}")
        print(f"  Price case: {torrontes.precio_por_caja}")
        print(f"  Price unit (B2B): {torrontes.precio_monetario}")
        print(f"  Suggested Price (Retail): {torrontes.precio_sugerido}")
        print(f"  Effective Date: {torrontes.fecha_vigencia}")
    else:
        print("Torrontes not found!")
