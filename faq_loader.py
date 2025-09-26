import json
import logging

from models import db, Rubro, QA, Sugerencia, User
from werkzeug.security import generate_password_hash

from services.logic import es_rubro_publico
from faq_questions import faq_data
from services.user_service import assign_whatsapp_numbers

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
            "email": "demo+ferreteria@chatboc.ar",
            "name": "Demo Ferretería",
            "nombre_empresa": "Ferretería Central",
            "password": "demo1234",
            "rubro_clave": "ferreteria"
        },
        {
            "email": "demo+local@chatboc.ar",
            "name": "Demo Local Comercial",
            "nombre_empresa": "Local Comercial Demo",
            "password": "demo1234",
            "rubro_clave": "local_comercial"
        },
        {
            "email": "demo+medico@chatboc.ar",
            "name": "Demo Médico",
            "nombre_empresa": "Clínica San Dona",
            "password": "demo1234",
            "rubro_clave": "medico"
        },
        {
            "email": "franco@cuatrofincas.com",
            "name": "Franco Cuatro Fincas",
            "nombre_empresa": "Bodega Cuatro Fincas",
            "password": "123456",
            "rubro_clave": "bodega",
            "rol": "admin",
            "plan": "premium",
            "limite_preguntas": 250,
            "token": "cuatrofincas-live-token",
            "tipo_chat": "pyme",
            "whatsapp_numbers": ["+18564858589"]
        }
    ]

    for data in usuarios_demo:
        rubro = Rubro.query.filter_by(clave=data["rubro_clave"]).first()
        if not rubro:
            print(f"❌ Rubro no encontrado: {data['rubro_clave']} (para {data['email']})")
            continue

        tipo_chat = data.get("tipo_chat") or ("municipio" if es_rubro_publico(rubro) else "pyme")
        token = data.get("token") or f"demo-token-{data['rubro_clave']}"
        plan = data.get("plan", "gratis")
        preguntas_usadas = data.get("preguntas_usadas", 0)
        limite_preguntas = data.get("limite_preguntas", 50)

        existente = User.query.filter_by(email=data["email"]).first()
        if existente:
            existente.name = data.get("name", existente.name)
            existente.nombre_empresa = data.get("nombre_empresa", existente.nombre_empresa)
            existente.rubro_id = rubro.id
            existente.tipo_chat = tipo_chat
            existente.plan = plan
            existente.preguntas_usadas = preguntas_usadas
            existente.limite_preguntas = limite_preguntas
            existente.token = token

            if data.get("rol"):
                existente.rol = data["rol"]

            if data.get("password"):
                existente.password_hash = generate_password_hash(data["password"])

            for optional_field in [
                "telefono",
                "link_web",
                "logo_url",
                "color_primario",
                "color_secundario",
                "badge_tipo",
            ]:
                if optional_field in data:
                    setattr(existente, optional_field, data[optional_field])

            user_obj = existente
            print(f"🔄 Usuario actualizado: {data['email']}")
        else:
            if not data.get("password"):
                print(f"⚠️ No se pudo crear {data['email']} sin contraseña definida.")
                continue

            nuevo_user = User(
                name=data.get("name", data["email"]),
                email=data["email"],
                nombre_empresa=data.get("nombre_empresa"),
                password_hash=generate_password_hash(data["password"]),
                token=token,
                plan=plan,
                preguntas_usadas=preguntas_usadas,
                limite_preguntas=limite_preguntas,
                rubro_id=rubro.id,
                tipo_chat=tipo_chat,
            )

            if data.get("rol"):
                nuevo_user.rol = data["rol"]

            for optional_field in [
                "telefono",
                "link_web",
                "logo_url",
                "color_primario",
                "color_secundario",
                "badge_tipo",
            ]:
                if optional_field in data:
                    setattr(nuevo_user, optional_field, data[optional_field])

            db.session.add(nuevo_user)
            user_obj = nuevo_user
            print(f"✅ Usuario demo creado: {data['email']}")

        db.session.flush()
        whatsapp_results = assign_whatsapp_numbers(
            user_obj,
            data.get("whatsapp_numbers"),
            activate=True,
            commit=False,
        )

        for result in whatsapp_results:
            number = result["number"]
            status = result["status"]
            prev_email = result.get("previous_user_email") or result.get("previous_user_id")
            reactivated = result.get("reactivated")

            if status == "created":
                print(f"✅ Número WhatsApp {number} asignado a {user_obj.email}.")
            elif status == "reassigned":
                if prev_email:
                    print(
                        f"🔁 Número WhatsApp {number} reasignado de {prev_email} a {user_obj.email}."
                    )
                else:
                    print(
                        f"🔁 Número WhatsApp {number} reasignado a {user_obj.email}."
                    )
                if reactivated:
                    print(f"♻️ Número WhatsApp {number} reactivado para {user_obj.email}.")
            elif status == "reactivated":
                print(f"♻️ Número WhatsApp {number} reactivado para {user_obj.email}.")
            elif status == "updated":
                print(f"ℹ️ Número WhatsApp {number} ya estaba activo para {user_obj.email}.")

    db.session.commit()
    print("✅ Usuarios demo listos.")


def cargar_datos_iniciales():
    print("🚀 Cargando datos iniciales...")
    cargar_faqs()
    cargar_sugerencias()
    cargar_usuarios_demo()
    print("✅ Todos los datos iniciales fueron cargados correctamente.")
