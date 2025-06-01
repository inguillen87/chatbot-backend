# services/faq_matcher_spacy.py
import spacy
import logging
from models import QA # Asumiendo que QA está en tu archivo models.py principal

logger = logging.getLogger(__name__)
NLP_SPACY = None # Variable global para el modelo spaCy

def _cargar_spacy_modelo():
    """Carga el modelo spaCy si aún no está cargado."""
    global NLP_SPACY
    if NLP_SPACY is None:
        try:
            NLP_SPACY = spacy.load("es_core_news_md")
            logger.info("✅ Modelo spaCy 'es_core_news_md' cargado exitosamente para FAQ Matcher.")
            if not NLP_SPACY.vocab.vectors.length: # Comprobar si los vectores están realmente cargados
                 logger.warning("⚠️ El modelo spaCy 'es_core_news_md' se cargó pero parece no tener vectores. La similitud podría no funcionar como se espera.")
        except OSError: # Error común si el modelo no está descargado
            logger.error("❌ Error al cargar spaCy 'es_core_news_md': Modelo no encontrado. "
                         "Asegúrate de haberlo descargado (python -m spacy download es_core_news_md).")
            # No levantar excepción aquí para permitir que la app inicie, pero las búsquedas fallarán.
            # O podrías levantar una excepción si es crítico:
            # raise RuntimeError("Modelo spaCy 'es_core_news_md' no encontrado.")
        except Exception as e:
            logger.error(f"❌ Error inesperado al cargar spaCy: {e}", exc_info=True)
            # raise # Opcional: relanzar si es crítico

def buscar_en_faq_spacy(pregunta_usuario: str, rubro_id: int, threshold: float = 0.80) -> QA | None: # Ajustado threshold
    """Busca la FAQ más similar en la base de datos para un rubro dado."""
    _cargar_spacy_modelo() # Asegurar que el modelo esté cargado
    
    if NLP_SPACY is None:
        logger.error("Imposible buscar en FAQ: modelo spaCy no está cargado.")
        return None
    if not pregunta_usuario or not isinstance(pregunta_usuario, str) or not pregunta_usuario.strip():
        logger.warning("⚠️ Pregunta de usuario vacía o inválida para búsqueda en FAQ.")
        return None
    if not isinstance(rubro_id, int):
        logger.warning(f"⚠️ Rubro ID inválido ({rubro_id}) para búsqueda en FAQ.")
        return None

    try:
        faqs = QA.query.filter_by(rubro_id=rubro_id).all()
    except Exception as e_db:
        logger.error(f"Error al consultar FAQs de la base de datos para rubro_id {rubro_id}: {e_db}", exc_info=True)
        return None

    if not faqs:
        logger.info(f"ℹ️ No se encontraron FAQs en la base de datos para el rubro ID {rubro_id}.")
        return None

    pregunta_limpia = limpiar_texto_base(pregunta_usuario) # Usar la misma limpieza que en otros lados
    doc_user = NLP_SPACY(pregunta_limpia)

    if not doc_user.has_vector or not doc_user.vector_norm:
        logger.warning(f"⚠️ No se pudo generar un vector para la pregunta del usuario: '{pregunta_limpia}'. No se puede calcular similitud.")
        return None

    mejor_match = None
    mejor_score = -1.0 # Iniciar con -1 para asegurar que cualquier score sea mayor

    for faq_item in faqs:
        if not faq_item.question or not faq_item.question.strip():
            continue # Saltar FAQs sin pregunta

        doc_faq = NLP_SPACY(limpiar_texto_base(faq_item.question))
        if not doc_faq.has_vector or not doc_faq.vector_norm:
            # logger.debug(f"FAQ ID {faq_item.id} no tiene vector, se omite.")
            continue

        try:
            score = doc_user.similarity(doc_faq)
            # logger.debug(f"Comparando '{pregunta_limpia}' con FAQ ID {faq_item.id} ('{faq_item.question[:50]}...'): Score {score:.3f}")
            if score > mejor_score:
                mejor_score = score
                mejor_match = faq_item
        except Exception as e_sim:
            logger.error(f"Error calculando similitud para FAQ ID {faq_item.id}: {e_sim}", exc_info=True)
            continue


    if mejor_match and mejor_score >= threshold:
        logger.info(f"✅ FAQ encontrada para '{pregunta_limpia}' (Rubro {rubro_id}): '{mejor_match.question[:50]}...' con score {mejor_score:.3f}")
        return mejor_match
    else:
        logger.info(f"📉 No se encontró FAQ con similitud suficiente para '{pregunta_limpia}' (Rubro {rubro_id}). Mejor score: {mejor_score:.3f}, Umbral: {threshold}")
        return None