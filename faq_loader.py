from app import app
from models import db, Rubro, QA, User
from faq_questions import faq_data
from datetime import datetime

DEMO_TOKEN = "demo-token"
DEMO_EMAIL = "demo@chatboc.ar"

def crear_rubro_si_no_existe(clave, nombre=None, descripcion=None, parent_clave=None):
    rubro = Rubro.query.filter_by(clave=clave).first()
    if rubro:
        return rubro

    parent = None
    if parent_clave:
        parent = Rubro.query.filter_by(clave=parent_clave).first()
        if not parent:
            print(f"❌ No se encontró el rubro padre '{parent_clave}' para '{clave}'")

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

def crear_usuario_demo():
    usuario = User.query.filter_by(token=DEMO_TOKEN).first()
    if usuario:
        print("🔁 Usuario demo ya existe.")
        return

    rubro_medico = Rubro.query.filter_by(clave="medico").first()
    if not rubro_medico:
        print("❌ No se encontró el rubro 'medico' para asignar al usuario demo.")
        return

    demo = User(
        name="Usuario Demo",
        email=DEMO_EMAIL,
        token=DEMO_TOKEN,
        plan="gratis",
        preguntas_usadas=0,
        last_reset=datetime.utcnow(),
        rubro_id=rubro_medico.id
    )
    demo.set_password("demo1234")

    db.session.add(demo)
    db.session.commit()
    print("✅ Usuario demo creado con éxito.")

def crear_usuarios_demo_por_rubro():
    for clave, datos in faq_data.items():
        rubro = Rubro.query.filter_by(clave=clave).first()
        if not rubro:
            print(f"❌ No se encontró el rubro '{clave}'")
            continue

        email = f"demo+{clave}@chatboc.ar"
        token = f"demo-{clave}"
        nombre = f"Demo {datos.get('nombre', clave.capitalize())}"

        existente = User.query.filter_by(token=token).first()
        if existente:
            print(f"🔁 Usuario demo ya existe para rubro '{clave}'")
            continue

        nuevo = User(
            name=nombre,
            email=email,
            token=token,
            plan="gratis",
            preguntas_usadas=0,
            last_reset=datetime.utcnow(),
            rubro_id=rubro.id
        )
        nuevo.set_password("demo1234")
        db.session.add(nuevo)
        print(f"✅ Usuario demo creado para rubro '{clave}'")

    db.session.commit()
    print("🎉 Todos los usuarios demo por rubro fueron creados.")

def cargar_rubros_y_faqs_con_categorias():
    for clave, contenido in faq_data.items():
        nombre = contenido.get("nombre", clave.capitalize())
        descripcion = contenido.get("descripcion", "")
        parent_clave = contenido.get("padre")
        categorias = contenido.get("categorias", {})

        rubro = crear_rubro_si_no_existe(clave, nombre, descripcion, parent_clave)

        for categoria, faqs in categorias.items():
            nuevas_faqs = []
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
                print(f"📌 {len(nuevas_faqs)} FAQs agregadas para '{clave}' > '{categoria}'")
            else:
                print(f"📚 Ya existían FAQs para '{clave}' > '{categoria}'")

if __name__ == "__main__":
    with app.app_context():
        cargar_rubros_y_faqs_con_categorias()
        crear_usuario_demo()
        crear_usuarios_demo_por_rubro()
