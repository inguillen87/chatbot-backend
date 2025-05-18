import json
from datetime import datetime
from app import app
from models import db, Rubro, QA, Sugerencia
from faq_questions import faq_data

def crear_rubro_si_no_existe(clave, nombre=None, descripcion=None, parent_clave=None):
    rubro = Rubro.query.filter_by(clave=clave).first()
    if rubro:
        return rubro

    parent = None
    if parent_clave:
        parent = Rubro.query.filter_by(clave=parent_clave).first()

    nuevo_rubro = Rubro(
        clave=clave,
        nombre=nombre or clave.capitalize(),
        descripcion=descripcion or "",
        padre=parent
    )
    db.session.add(nuevo_rubro)
    db.session.commit()
    print(f"✅ Rubro creado: {clave}")
    return nuevo_rubro

def cargar_faqs():
    for clave, contenido in faq_data.items():
        nombre = contenido.get("nombre", clave.capitalize())
        descripcion = contenido.get("descripcion", "")
        parent_clave = contenido.get("padre")
        categorias = contenido.get("categorias", {})

        rubro = crear_rubro_si_no_existe(clave, nombre, descripcion, parent_clave)

        nuevas_faqs = []
        for categoria, faqs in categorias.items():
            for pregunta, respuesta in faqs:
                existe = QA.query.filter_by(question=pregunta, rubro_id=rubro.id).first()
                if not existe:
                    nuevas_faqs.append(QA(
                        question=pregunta,
                        answer=respuesta,
                        rubro_id=rubro.id,
                        categoria=categoria
                    ))
        if nuevas_faqs:
            db.session.bulk_save_objects(nuevas_faqs)
            db.session.commit()
            print(f"📌 {len(nuevas_faqs)} FAQs agregadas para '{clave}'")
        else:
            print(f"📚 Ya existían FAQs para '{clave}'")

def cargar_sugerencias():
    with open("data/sugerencias.json", "r", encoding="utf-8") as f:
        sugerencias_data = json.load(f)

    for rubro_nombre, sugerencias in sugerencias_data.items():
        rubro = Rubro.query.filter_by(nombre=rubro_nombre).first()
        if not rubro:
            print(f"❌ No se encontró el rubro '{rubro_nombre}' para cargar sugerencias")
            continue

        nuevas = 0
        for texto in sugerencias:
            if not Sugerencia.query.filter_by(rubro_id=rubro.id, texto=texto).first():
                db.session.add(Sugerencia(rubro_id=rubro.id, texto=texto))
                nuevas += 1

        if nuevas:
            print(f"💡 {nuevas} sugerencias cargadas para '{rubro_nombre}'")

    db.session.commit()

if __name__ == "__main__":
    with app.app_context():
        cargar_faqs()
        cargar_sugerencias()
        print("✅ Base de datos inicializada correctamente.")
