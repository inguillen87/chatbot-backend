from models import QA
import spacy
import logging

try:
    # Carga modelo con vectores reales (no usar 'xx_sent_ud_sm')
    nlp = spacy.load("es_core_news_md")
    if not nlp.vocab.vectors:
        raise ValueError("❌ El modelo 'es_core_news_md' no tiene vectores cargados.")
except Exception as e:
    logging.warning(f"❌ Error al cargar spaCy: {e}")
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
    if not doc_user.vector_norm:
        logging.warning("⚠️ Vectores del usuario vacíos. Pregunta no procesable.")
        return None

    mejor_match = None
    mejor_score = 0.0

    for faq in faqs:
        doc_faq = nlp(faq.question.lower())
        if not doc_faq.vector_norm:
            continue  # Skip si vector vacío

        score = doc_user.similarity(doc_faq)
        logging.info(f"🧠 Comparando con: '{faq.question}' | Score: {score:.3f}")

        if score > mejor_score:
            mejor_score = score
            mejor_match = faq

    if mejor_match and mejor_score >= threshold:
        logging.info(f"✅ Mejor match: '{mejor_match.question}' con score {mejor_score:.2f}")
        return mejor_match

    logging.info("❌ No se alcanzó el umbral de similitud. No hay respuesta suficientemente parecida.")
    return None
