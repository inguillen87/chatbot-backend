import spacy
from models import QA

# Cargar el modelo de spaCy en español
nlp = spacy.load("es_core_news_md")

def buscar_en_faq_spacy(pregunta: str) -> str | None:
    pregunta_doc = nlp(pregunta.lower())
    faqs = QA.query.all()

    coincidencias = []

    for faq in faqs:
        faq_doc = nlp(faq.question.lower())
        similitud = pregunta_doc.similarity(faq_doc)

        # Bonus si hay keywords y alguna coincide con la pregunta
        if faq.keywords:
            for palabra in faq.keywords.lower().split(","):
                if palabra.strip() in pregunta.lower():
                    similitud += 0.1  # Podes ajustar esto según resultados

        coincidencias.append((faq.answer, similitud))

    # Buscar la mejor respuesta
    mejor_respuesta, mejor_score = max(coincidencias, key=lambda x: x[1], default=(None, 0))

    if mejor_score > 0.70:
        return mejor_respuesta
    return None
