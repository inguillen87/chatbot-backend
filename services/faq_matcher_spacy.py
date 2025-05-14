from models import QA
import spacy
import logging

try:
    nlp = spacy.load("es_core_news_md")
except OSError:
    logging.warning("❌ No se encontró el modelo 'es_core_news_md'. Ejecutá: python -m spacy download es_core_news_md")
    raise

def buscar_en_faq_spacy(pregunta_usuario: str, rubro_id: int, threshold: float = 0.75):
    if not pregunta_usuario or not rubro_id:
        logging.warning("⚠️ Entrada inválida para búsqueda en FAQ.")
        return None

    faqs = QA.query.filter_by(rubro_id=rubro_id).all()
    if not faqs:
        logging.warning(f"⚠️ No se encontraron FAQs para el rubro ID {rubro_id}.")
        return None

    doc_user = nlp(pregunta_usuario.lower())

    mejor_match = None
    mejor_score = 0.0

    for faq in faqs:
        doc_faq = nlp(faq.question.lower())
        score = doc_user.similarity(doc_faq)

        if score > mejor_score:
            mejor_score = score
            mejor_match = faq

    if mejor_match:
        logging.info(f"🔍 Mejor match: {mejor_match.question} | Score: {mejor_score:.2f}")

    if mejor_score >= threshold:
        return mejor_match

    return None
