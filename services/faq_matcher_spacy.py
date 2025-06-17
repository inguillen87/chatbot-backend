# services/faq_matcher_spacy.py
import logging
from typing import Optional, List, Tuple
from models import QA
from .utils import limpiar_texto_base
from .spacy_loader import get_spacy_model

logger = logging.getLogger(__name__)
NLP_SPACY_FAQ = None

def _cargar_spacy_modelo_faq():
    global NLP_SPACY_FAQ
    if NLP_SPACY_FAQ is None:
        NLP_SPACY_FAQ = get_spacy_model()
        if NLP_SPACY_FAQ is None:
            logger.error("❌ No se pudo cargar el modelo spaCy para FAQ Matcher.")

def buscar_en_faq_spacy(pregunta_usuario: str, rubro_id: int, threshold: float = 0.80) -> Optional[QA]:
    _cargar_spacy_modelo_faq()    
    if NLP_SPACY_FAQ is None: logger.error("[FAQ] Imposible buscar: modelo spaCy no cargado."); return None
    if not pregunta_usuario or not isinstance(pregunta_usuario, str) or not pregunta_usuario.strip(): logger.warning("[FAQ] Pregunta vacía."); return None
    if not isinstance(rubro_id, int): logger.warning(f"[FAQ] Rubro ID inválido ({rubro_id})."); return None
    try: faqs: List[QA] = QA.query.filter_by(rubro_id=rubro_id).all() # Tipado
    except Exception as e_db: logger.error(f"[FAQ] Error consultando FAQs BD para rubro {rubro_id}: {e_db}", exc_info=True); return None
    if not faqs: logger.info(f"[FAQ] No FAQs en BD para rubro ID {rubro_id}."); return None
    pregunta_limpia = limpiar_texto_base(pregunta_usuario); doc_user = NLP_SPACY_FAQ(pregunta_limpia)
    if not doc_user.has_vector or not doc_user.vector_norm: logger.warning(f"[FAQ] No vector para pregunta: '{pregunta_limpia}'."); return None
    mejor_match: Optional[QA] = None; mejor_score: float = -1.0 
    for faq_item in faqs:
        if not faq_item.question or not faq_item.question.strip(): continue 
        doc_faq = NLP_SPACY_FAQ(limpiar_texto_base(faq_item.question)) 
        if not doc_faq.has_vector or not doc_faq.vector_norm: continue
        try:
            score = doc_user.similarity(doc_faq)
            if score > mejor_score: mejor_score = score; mejor_match = faq_item
        except Exception as e_sim: logger.error(f"[FAQ] Error similitud FAQ ID {faq_item.id}: {e_sim}", exc_info=True); continue
    if mejor_match and mejor_score >= threshold:
        logger.info(f"✅ [FAQ] Match: '{pregunta_limpia}' (Rubro {rubro_id}) -> FAQ ID {mejor_match.id} ('{mejor_match.question[:30]}...') score {mejor_score:.3f}")
        return mejor_match
    else:
        logger.info(f"📉 [FAQ] No match >= {threshold} para '{pregunta_limpia}' (Rubro {rubro_id}). Mejor score: {mejor_score:.3f}")
        return None

def buscar_top_n_faqs(pregunta_usuario: str, rubro_id: int, n: int = 3, threshold: float = 0.5) -> List[Tuple[QA, float]]:
    """Devuelve las mejores ``n`` FAQs ordenadas por similitud."""
    _cargar_spacy_modelo_faq()
    if NLP_SPACY_FAQ is None:
        logger.error("[FAQ] Imposible buscar: modelo spaCy no cargado.")
        return []
    if not pregunta_usuario or not isinstance(pregunta_usuario, str) or not pregunta_usuario.strip():
        logger.warning("[FAQ] Pregunta vacía.")
        return []
    if not isinstance(rubro_id, int):
        logger.warning(f"[FAQ] Rubro ID inválido ({rubro_id}).")
        return []

    try:
        faqs: List[QA] = QA.query.filter_by(rubro_id=rubro_id).all()
    except Exception as e_db:
        logger.error(f"[FAQ] Error consultando FAQs BD para rubro {rubro_id}: {e_db}", exc_info=True)
        return []

    if not faqs:
        logger.info(f"[FAQ] No FAQs en BD para rubro ID {rubro_id}.")
        return []

    pregunta_limpia = limpiar_texto_base(pregunta_usuario)
    doc_user = NLP_SPACY_FAQ(pregunta_limpia)
    if not doc_user.has_vector or not doc_user.vector_norm:
        logger.warning(f"[FAQ] No vector para pregunta: '{pregunta_limpia}'.")
        return []

    resultados: List[Tuple[QA, float]] = []
    for faq_item in faqs:
        if not faq_item.question or not faq_item.question.strip():
            continue
        doc_faq = NLP_SPACY_FAQ(limpiar_texto_base(faq_item.question))
        if not doc_faq.has_vector or not doc_faq.vector_norm:
            continue
        try:
            score = doc_user.similarity(doc_faq)
            if score >= threshold:
                resultados.append((faq_item, score))
        except Exception as e_sim:
            logger.error(f"[FAQ] Error similitud FAQ ID {faq_item.id}: {e_sim}", exc_info=True)
            continue

    resultados.sort(key=lambda x: x[1], reverse=True)
    return resultados[:n]
