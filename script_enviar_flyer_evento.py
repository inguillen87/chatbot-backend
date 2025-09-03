"""Envía un flyer de evento por WhatsApp a los usuarios de un municipio."""

from app import create_app
from models import User
from utils.whatsapp import enviar_imagen_whatsapp
from sqlalchemy import and_

MUNICIPIO_ID = 4
TITULO = "¡Se viene un gran evento en tu municipio!"
DESCRIPCION = "Mirá la imagen para más info y no te lo pierdas."
LINK = "https://example.com/evento"
URL_IMAGEN = "https://example.com/flyer_evento.jpg"


def enviar_flyer_a_municipio(municipio_id: int, titulo: str, descripcion: str, link: str, url_imagen: str):
    app = create_app()
    with app.app_context():
        usuarios = User.query.filter(
            and_(
                User.municipio_id == municipio_id,
                User.telefono.isnot(None),
                User.acepta_marketing.is_(True),
            )
        ).all()

        mensaje = f"{titulo}\n\n{descripcion}\n{link}"
        for usuario in usuarios:
            numero = usuario.telefono
            enviar_imagen_whatsapp(numero, mensaje, url_imagen)
            print(f"Enviado a {numero}")


if __name__ == "__main__":
    enviar_flyer_a_municipio(MUNICIPIO_ID, TITULO, DESCRIPCION, LINK, URL_IMAGEN)
