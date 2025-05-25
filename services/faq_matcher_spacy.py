import spacy
import logging
from models import QA

nlp = None

def cargar_spacy():
    global nlp
    if nlp is None:
        try:
            nlp = spacy.load("es_core_news_md")
            print("✅ spaCy cargado correctamente en faq_matcher_spacy.py")

            if not nlp.vocab.vectors:
                raise ValueError("❌ El modelo spaCy no tiene vectores.")
        except Exception as e:
            logging.error(f"❌ Error al cargar spaCy: {e}")
            raise

def buscar_en_faq_spacy(pregunta_usuario: str, rubro_id: int, threshold: float = 0.82):
    cargar_spacy()  # 👈 aseguramos que spaCy se haya cargado antes de usarlo

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
