from models import QA


def buscar_en_faq_spacy(pregunta_usuario: str, rubro_id: int, threshold: float = 0.82):
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
            continue

        score = doc_user.similarity(doc_faq)
        logging.debug(f"🔍 '{pregunta_usuario}' vs '{faq.question}' → Score: {score:.3f}")

        if score > mejor_score:
            mejor_score = score
            mejor_match = faq

    if mejor_match and mejor_score >= threshold:
        logging.info(f"✅ FAQ match válido: '{mejor_match.question}' (score: {mejor_score:.3f})")
        return mejor_match

    logging.warning(f"❌ FAQ descartado (mejor score: {mejor_score:.3f} < {threshold})")
    return None
