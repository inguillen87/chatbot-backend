import json
from datetime import datetime
from models import db, Rubro, QA, Sugerencia, User
from werkzeug.security import generate_password_hash
from faq_questions import faq_data
import logging

def crear_rubro_si_no_existe(clave, nombre=None, descripcion=None, parent_clave=None):
    try:
        rubro = Rubro.query.filter_by(clave=clave).first()
    except Exception as e:
        logging.error(f"❌ Error al consultar Rubro: {e}")
        return None

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
        if not rubro:
            print(f"❌ Error creando rubro '{clave}'")
            continue

        nuevas_faqs = []
        for categoria, faqs in categorias.items():
            for pregunta, respuesta in faqs:
                try:
                    existe = QA.query.filter_by(question=pregunta, rubro_id=rubro.id).first()
                except Exception as e:
                    logging.error(f"❌ Error al buscar FAQ existente: {e}")
                    continue

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
    try:
        with open("data/sugerencias.json", "r", encoding="utf-8") as f:
            sugerencias_data = json.load(f)
    except FileNotFoundError:
        print("❌ No se encontró el archivo data/sugerencias.json")
        return

    for rubro_nombre, sugerencias in sugerencias_data.items():
        rubro = Rubro.query.filter_by(clave=rubro_nombre).first()
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
    print("✅ Sugerencias cargadas correctamente.")

def cargar_usuarios_demo():
    usuarios_demo = [
        {
            "email": "demo+almacen@chatboc.ar",
            "name": "Demo Almacén",
            "nombre_empresa": "ByM almacen de bebidas",
            "password": "demo1234",
            "rubro_clave": "almacen"
        },
        {
            "email": "demo+bodega@chatboc.ar",
            "name": "Demo Bodega",
            "nombre_empresa": "Bodega Cuatro Fincas Winery",
            "password": "demo1234",
            "rubro_clave": "bodega"
        },
        {
            "email": "demo+medico@chatboc.ar",
            "name": "Demo Médico",
            "nombre_empresa": "Clínica San Dona",
            "password": "demo1234",
            "rubro_clave": "medico"
        }
    ]

    for data in usuarios_demo:
        rubro = Rubro.query.filter_by(clave=data["rubro_clave"]).first()
        if not rubro:
            print(f"❌ Rubro no encontrado: {data['rubro_clave']} (para {data['email']})")
            continue

        existente = User.query.filter_by(email=data["email"]).first()
        if existente:
            existente.nombre_empresa = data["nombre_empresa"]
            existente.rubro_id = rubro.id
            db.session.commit()
            print(f"🔄 Usuario actualizado: {data['email']}")
            continue

        nuevo_user = User(
            name=data["name"],
            email=data["email"],
            nombre_empresa=data["nombre_empresa"],
            password_hash=generate_password_hash(data["password"]),
            token=f"demo-token-{data['rubro_clave']}",
            plan="gratis",
            preguntas_usadas=0,
            limite_preguntas=50,
            rubro_id=rubro.id
        )
        db.session.add(nuevo_user)
        print(f"✅ Usuario demo creado: {data['email']}")

    db.session.commit()
    print("✅ Usuarios demo listos.")
