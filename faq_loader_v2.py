# data_loader.py

from app import app
from models import db, Rubro, QA

def cargar_rubros():
    rubros = [
        "bodega", "restaurante", "heladeria", "peluqueria",
        "servicio_tecnico", "medico", "dentista", "kinesiologo",
        "nutricionista", "oftalmologo"
    ]

    rubros_creados = []

    for clave in rubros:
        existente = Rubro.query.filter_by(clave=clave).first()
        if not existente:
            nuevo = Rubro(clave=clave, nombre=clave.capitalize())
            db.session.add(nuevo)
            rubros_creados.append(clave)

    if rubros_creados:
        db.session.commit()
        print(f"✅ Rubros creados: {', '.join(rubros_creados)}")
    else:
        print("📌 Todos los rubros ya existen.")


def cargar_faqs():
    rubros = {r.clave: r for r in Rubro.query.all()}
    if not rubros:
        print("❌ No se encontraron rubros. Abortando carga de FAQs.")
        return

    faqs = [
        QA(question="¿Qué tipos de vinos ofrecen?", answer="Tenemos vinos tintos, blancos, rosados y espumantes.", rubro=rubros["bodega"]),
        QA(question="¿Tienen menú del día?", answer="Sí, ofrecemos opciones diarias con bebida incluida.", rubro=rubros["restaurante"]),
        QA(question="¿Cómo saco un turno?", answer="Podés agendar turno por WhatsApp o desde nuestro sistema online.", rubro=rubros["peluqueria"]),
        QA(question="¿Qué sabores tienen hoy?", answer="Contamos con más de 20 sabores disponibles todos los días.", rubro=rubros["heladeria"]),
        QA(question="¿Qué tipo de equipos reparan?", answer="Celulares, notebooks, PCs, tablets y electrodomésticos pequeños.", rubro=rubros["servicio_tecnico"]),
        QA(question="¿Atienden con obra social?", answer="Sí, trabajamos con las principales obras sociales y prepagas.", rubro=rubros["medico"]),
        QA(question="¿Cómo saco un turno con el odontólogo?", answer="Podés reservar tu turno por WhatsApp o llamando al consultorio.", rubro=rubros["dentista"]),
        QA(question="¿Atienden por derivación médica?", answer="Sí, aceptamos derivaciones de médicos clínicos o traumatólogos.", rubro=rubros["kinesiologo"]),
        QA(question="¿Hacen planes personalizados?", answer="Sí, adaptados a cada paciente según su objetivo y antecedentes.", rubro=rubros["nutricionista"]),
        QA(question="¿Hacen exámenes visuales completos?", answer="Sí, revisamos agudeza, fondo de ojo y presión ocular.", rubro=rubros["oftalmologo"]),
    ]

    nuevas = []
    for faq in faqs:
        existe = QA.query.filter_by(question=faq.question, rubro_id=faq.rubro.id).first()
        if not existe:
            nuevas.append(faq)

    if nuevas:
        db.session.bulk_save_objects(nuevas)
        db.session.commit()
        print(f"✅ Se cargaron {len(nuevas)} FAQs.")
    else:
        print("📚 Las FAQs ya estaban cargadas.")


if __name__ == "__main__":
    with app.app_context():
        cargar_rubros()
        cargar_faqs()
