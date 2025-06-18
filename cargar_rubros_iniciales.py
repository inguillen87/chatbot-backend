from app import app
from faq_loader import crear_rubro_si_no_existe
from faq_questions import faq_data


def cargar_rubros():
    for clave, data in faq_data.items():
        nombre = data.get("nombre", clave.capitalize())
        descripcion = data.get("descripcion", "")
        parent_clave = data.get("padre")
        rubro = crear_rubro_si_no_existe(clave, nombre, descripcion, parent_clave)
        if rubro:
            print(f"✅ Rubro listo: {rubro.clave}")
    print("✅ Carga de rubros completa.")


if __name__ == "__main__":
    with app.app_context():
        cargar_rubros()
