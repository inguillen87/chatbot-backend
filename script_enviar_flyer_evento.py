"""Envía un flyer de evento por WhatsApp a los usuarios de un municipio."""

from app import create_app
from models import User
from utils.whatsapp import enviar_imagen_whatsapp
from sqlalchemy import and_

MUNICIPIO_ID = 4
MENSAJE = "¡Se viene un gran evento en tu municipio! \nMirá la imagen para más info."
URL_IMAGEN = "https://example.com/flyer_evento.jpg"


def enviar_flyer_a_municipio(municipio_id: int, mensaje: str, url_imagen: str):
    app = create_app()
    with app.app_context():
        usuarios = User.query.filter(
            and_(
                User.municipio_id == municipio_id,
                User.telefono.isnot(None),
                User.acepta_marketing.is_(True),
            )
        ).all()

        for usuario in usuarios:
            numero = usuario.telefono
            enviar_imagen_whatsapp(numero, mensaje, url_imagen)
            print(f"Enviado a {numero}")


if __name__ == "__main__":
    enviar_flyer_a_municipio(MUNICIPIO_ID, MENSAJE, URL_IMAGEN)
