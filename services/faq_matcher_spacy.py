import spacy
from models import QA

# Cargamos el modelo español
nlp = spacy.load("es_core_news_md")

def buscar_en_faq_spacy(pregunta_usuario: str):
    doc_user = nlp(pregunta_usuario.lower())
    mejores_match = None
    mejor_score = 0.0

    for faq in QA.query.all():
        doc_faq = nlp(faq.question.lower())
        score = doc_user.similarity(doc_faq)

        if score > mejor_score:
            mejor_score = score
            mejores_match = faq

    print(f"🔎 Mejor score de match: {mejor_score:.2f} — Pregunta: {mejores_match.question if mejores_match else 'Ninguna'}")

    # ⚠️ Solo devolvemos si hay similitud alta
    if mejor_score >= 0.7:
        return mejores_match

    return None
