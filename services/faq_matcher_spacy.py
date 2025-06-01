# services/faq_matcher_spacy.py
import spacy
import logging
from models import QA
from .utils import limpiar_texto_base # <--- IMPORTACIÓN AÑADIDA

logger = logging.getLogger(__name__)
NLP_SPACY_FAQ = None 

def _cargar_spacy_modelo_faq():
    global NLP_SPACY_FAQ
    if NLP_SPACY_FAQ is None:
        try:
            NLP_SPACY_FAQ = spacy.load("es_core_news_md")
            logger.info("✅ Modelo spaCy 'es_core_news_md' cargado para FAQ Matcher.")
            if NLP_SPACY_FAQ.vocab.vectors.shape[0] == 0: # CORREGIDO: Usar .shape[0]
                 logger.warning("⚠️ El modelo spaCy 'es_core_news_md' (FAQ) se cargó pero no tiene vectores.")
        except OSError:
            logger.error("❌ Error al cargar spaCy 'es_core_news_md' (FAQ): Modelo no encontrado.")
        except Exception as e:
            logger.error(f"❌ Error inesperado al cargar spaCy (FAQ): {e}", exc_info=True)

def buscar_en_faq_spacy(pregunta_usuario: str, rubro_id: int, threshold: float = 0.80) -> QA | None:
    _cargar_spacy_modelo_faq()
    
    if NLP_SPACY_FAQ is None:
        logger.error("[FAQ] Imposible buscar: modelo spaCy no está cargado.")
        return None
    if not pregunta_usuario or not isinstance(pregunta_usuario, str) or not pregunta_usuario.strip():
        logger.warning("[FAQ] Pregunta de usuario vacía o inválida para búsqueda en FAQ.")
        return None
    if not isinstance(rubro_id, int):
        logger.warning(f"[FAQ] Rubro ID inválido ({rubro_id}) para búsqueda en FAQ.")
        return None

    try:
        faqs = QA.query.filter_by(rubro_id=rubro_id).all()
    except Exception as e_db:
        logger.error(f"[FAQ] Error al consultar FAQs de BD para rubro {rubro_id}: {e_db}", exc_info=True)
        return None

    if not faqs:
        logger.info(f"[FAQ] No se encontraron FAQs en BD para rubro ID {rubro_id}.")
        return None

    pregunta_limpia = limpiar_texto_base(pregunta_usuario) # CORREGIDO: Usa la función importada
    doc_user = NLP_SPACY_FAQ(pregunta_limpia)

    if not doc_user.has_vector or not doc_user.vector_norm: 
        logger.warning(f"[FAQ] No se pudo generar vector para pregunta: '{pregunta_limpia}'.")
        return None

    mejor_match: Optional[QA] = None
    mejor_score: float = -1.0 

    for faq_item in faqs:
        if not faq_item.question or not faq_item.question.strip():
            continue 

        doc_faq = NLP_SPACY_FAQ(limpiar_texto_base(faq_item.question)) 
        if not doc_faq.has_vector or not doc_faq.vector_norm:
            continue
        
        try:
            score = doc_user.similarity(doc_faq)
            if score > mejor_score:
                mejor_score = score
                mejor_match = faq_item
        except Exception as e_sim: 
            logger.error(f"[FAQ] Error calculando similitud para FAQ ID {faq_item.id} ('{faq_item.question[:50]}...'): {e_sim}", exc_info=True)
            continue

    if mejor_match and mejor_score >= threshold:
        logger.info(f"✅ [FAQ] Match encontrado para '{pregunta_limpia}' (Rubro {rubro_id}): FAQ ID {mejor_match.id} ('{mejor_match.question[:50]}...') con score {mejor_score:.3f}")
        return mejor_match
    else:
        logger.info(f"📉 [FAQ] No se encontró FAQ con similitud >= {threshold} para '{pregunta_limpia}' (Rubro {rubro_id}). Mejor score: {mejor_score:.3f}")
        return None