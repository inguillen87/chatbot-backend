from extensions import db
from models import Rubro, Sugerencia
from data.sugerencias_data import sugerencias_data

def cargar_sugerencias():
    for rubro_nombre, lista_sugerencias in sugerencias_data.items():
        rubro = Rubro.query.filter_by(nombre=rubro_nombre).first()
        if rubro:
            for texto in lista_sugerencias:
                existe = Sugerencia.query.filter_by(rubro_id=rubro.id, texto=texto).first()
                if not existe:
                    nueva = Sugerencia(rubro_id=rubro.id, texto=texto)
                    db.session.add(nueva)
        else:
            print(f"⚠️ Rubro no encontrado: {rubro_nombre}")
    db.session.commit()
    print("✅ Sugerencias cargadas correctamente.")
