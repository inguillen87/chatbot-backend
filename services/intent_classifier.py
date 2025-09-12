import json
import os
import unicodedata
import logging
from fuzzywuzzy import process, fuzz

from .herramientas_municipio import categorizar_reclamo_por_palabra_clave


# Umbral reducido para permitir lenguaje natural más libre
INTENT_THRESHOLD = 0.35

# Palabras clave que disparan el flujo de reclamo
KW_RECLAMO = [
    "árbol",
    "arbol",
    "rama",
    "luminaria",
    "poste",
    "bache",
    "pozo",
    "basura",
    "residuos",
]

# Mapeo rápido de palabras clave a categorías de reclamo
KEYWORDS_CATEGORIA = {
    "arbol": "Arbolado",
    "árbol": "Arbolado",
    "ramas": "Arbolado",
    "hoja": "Arbolado",
    "hojas": "Arbolado",
    "luminaria": "Luminaria",
    "luz": "Luminaria",
    "poste": "Luminaria",
    "alumbrado": "Luminaria",
    "bache": "Bacheo",
    "pozo": "Bacheo",
    "basura": "Limpieza",
    "residuos": "Limpieza",
    "perdida": "Agua",
    "pérdida": "Agua",
    "fuga": "Agua",
    "agua": "Agua",
    "perro": "Animales",
    "animal": "Animales",
}


def clasificar_por_kw_y_cv(texto: str) -> str:
    return categorizar_reclamo_por_palabra_clave(texto)


def enrutar_a_reclamo(categoria: str) -> dict:
    return {"intent": "reclamo", "categoria": categoria}


def fast_reclamo_detect(text: str):
    """Detecta rápidamente si el texto contiene palabras clave de reclamo."""
    t = text.lower()
    if any(k in t for k in KW_RECLAMO):
        categoria = clasificar_por_kw_y_cv(text)
        return enrutar_a_reclamo(categoria)
    for k, cat in KEYWORDS_CATEGORIA.items():
        if k in t:
            return {"intent": "reclamo", "categoria": cat}
    return None

logger = logging.getLogger(__name__)

class IntentClassifier:
    def __init__(self, intents_file_path, min_confidence: float = INTENT_THRESHOLD * 100):
        self.intents_file_path = intents_file_path
        self.min_confidence = int(min_confidence)
        self.intents_by_rubro = self._load_intents()

    def _load_intents(self):
        """Carga los intents desde el archivo JSON."""
        try:
            with open(self.intents_file_path, "r", encoding="utf-8") as f:
                intents_data = json.load(f)
            logger.info(f"Intents cargados exitosamente desde {self.intents_file_path}")
            return intents_data
        except FileNotFoundError:
            logger.error(f"El archivo de intents no se encontró en: {self.intents_file_path}")
            return {}
        except json.JSONDecodeError:
            logger.error(f"Error al decodificar el archivo JSON de intents: {self.intents_file_path}")
            return {}
        except Exception as e:
            logger.error(f"Ocurrió un error inesperado al cargar los intents: {e}", exc_info=True)
            return {}

    def _normalize_text(self, text):
        """Normaliza el texto para la comparación: minúsculas y sin acentos."""
        if not text:
            return ""
        return "".join(
            c for c in unicodedata.normalize("NFD", text.lower())
            if unicodedata.category(c) != "Mn"
        )

    def classify(self, text, rubro='municipios'):
        """
        Clasifica el texto del usuario y devuelve el intent con la mejor correspondencia si supera el umbral de confianza.
        """
        normalized_text = self._normalize_text(text)
        if not normalized_text:
            return None, 0

        rubro_intents = self.intents_by_rubro.get(rubro, [])
        if not rubro_intents:
            logger.warning(f"No se encontraron intents para el rubro: {rubro}")
            return None, 0

        best_match = None
        highest_score = 0

        for intent in rubro_intents:
            # Usar process.extractOne para encontrar la mejor coincidencia en los ejemplos
            # Se usa token_sort_ratio para manejar el desorden de palabras
            result = process.extractOne(
                normalized_text,
                [self._normalize_text(ej) for ej in intent.get("ejemplos", [])],
                scorer=fuzz.token_sort_ratio,
                score_cutoff=self.min_confidence,
            )

            if result:
                _, score = result
                if score > highest_score:
                    highest_score = score
                    best_match = intent

        if best_match:
            logger.info(f"Intent clasificado como '{best_match.get('categoria')}' con confianza {highest_score}% para el texto: '{text}'")
            return best_match, highest_score

        logger.info(f"No se encontró un intent con suficiente confianza para el texto: '{text}'")
        return None, 0

# Instancia global para ser usada en la aplicación
INTENTS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "intents.json")
intent_classifier = IntentClassifier(INTENTS_FILE)
