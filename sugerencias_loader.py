import json
from extensions import db
from models import Rubro, Sugerencia

def cargar_sugerencias():
    try:
        with open("data/sugerencias.json", "r", encoding="utf-8") as f:
            sugerencias_data = json.load(f)
    except FileNotFoundError:
        print("❌ No se encontró el archivo data/sugerencias.json")
        return

    for clave_rubro, sugerencias in sugerencias_data.items():
        rubro = Rubro.query.filter_by(clave=clave_rubro).first()
        if not rubro:
            print(f"⚠️ Rubro no encontrado: {clave_rubro}")
            continue

        nuevas = 0
        for texto in sugerencias:
            if not Sugerencia.query.filter_by(rubro_id=rubro.id, texto=texto).first():
                db.session.add(Sugerencia(rubro_id=rubro.id, texto=texto))
                nuevas += 1

        if nuevas:
            print(f"💡 {nuevas} sugerencias cargadas para '{clave_rubro}'")

    db.session.commit()
    print("✅ Sugerencias cargadas correctamente.")
