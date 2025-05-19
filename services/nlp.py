import os
from dotenv import load_dotenv
from models import User
import openai
from random import choice

load_dotenv()

client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"

def get_gpt_response(question, user: User = None):
    nombre = user.name if user else "usuario"
    rubro = getattr(user, "industry", "empresa")

    prompt = f"""
        Sos el asistente virtual de la empresa '{nombre_empresa}'. Respondé consultas de clientes en lenguaje claro, profesional y amable. 
        Usá respuestas breves, útiles y concretas. 
        Si no sabés algo, decilo con honestidad. Si tenés datos precargados (FAQs o sugerencias), priorizalos antes que improvisar.

        Información adicional:
        - Nombre de empresa: {nombre_empresa}
        - Rubro: {rubro_nombre}
        - Plan actual: {user.plan}
        """


    if DEMO_MODE:
        respuestas_fake = [
         f"Hola {nombre}, ¿cómo estás? Estoy acá para ayudarte.",
        f"Consultanos sobre envíos, pagos o soporte, {nombre}.",
        f"Gracias por tu pregunta. En breve te damos más información.",
        f"Podés mejorar tu experiencia si actualizás tu plan, {nombre}.",
        f"Nuestro equipo se especializa en atención automatizada para {rubro}.",
        f"Estamos procesando tu consulta. ¿Deseás contactar soporte humano?"
]

        return choice(respuestas_fake)

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres un asistente amigable, profesional y confiable."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            max_tokens=300
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"❌ Error en OpenAI:", e)
        return "Lo siento, hubo un problema al generar la respuesta. Intentá nuevamente."
