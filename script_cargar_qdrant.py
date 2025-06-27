import sys
import os
from app import create_app
from models import User
from services.upload_processor import procesar_y_embedear_catalogo
from services.logic import es_rubro_publico
from services.qdrant_search import CATALOGO_PYME, CATALOGO_MUNICIPIO

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python script_cargar_qdrant.py <archivo_catalogo> <user_id>")
        sys.exit(1)

    archivo = sys.argv[1]
    user_id = int(sys.argv[2])

    if not os.path.isfile(archivo):
        print(f"Archivo no encontrado: {archivo}")
        sys.exit(1)

    app = create_app()
    with app.app_context():
        user = User.query.get(user_id)
        if not user:
            print(f"Usuario {user_id} no existe")
            sys.exit(1)
        coleccion = (
            CATALOGO_MUNICIPIO if es_rubro_publico(user.rubro) else CATALOGO_PYME
        )
        cantidad = procesar_y_embedear_catalogo(
            archivo, user_id, pyme_rubro_nombre=user.rubro.nombre if user.rubro else "generico", coleccion=coleccion
        )
        print(f"{cantidad} productos cargados en Qdrant")

