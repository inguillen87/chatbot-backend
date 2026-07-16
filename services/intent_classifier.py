import json
import os
import re
import unicodedata
import logging
from fuzzywuzzy import process, fuzz

logger = logging.getLogger(__name__)

class IntentClassifier:
    def __init__(self, intents_file_path, min_confidence=85):
        self.intents_file_path = intents_file_path
        self.min_confidence = min_confidence
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
        normalized_text = " ".join(self._normalize_text(text).split())
        if not normalized_text:
            return None, 0

        rubro_intents = self.intents_by_rubro.get(rubro, [])
        if not rubro_intents:
            logger.warning(f"No se encontraron intents para el rubro: {rubro}")
            return None, 0

        best_match = None
        highest_score = 0

        # Prefer the longest complete example contained in the message. This
        # keeps the deterministic fallback useful for explicit commands while
        # avoiding short greetings overriding a more specific request.
        phrase_matches = []
        for intent in rubro_intents:
            for example in intent.get("ejemplos", []):
                normalized_example = " ".join(self._normalize_text(example).split())
                if not normalized_example:
                    continue
                if normalized_text == normalized_example:
                    phrase_matches.append((len(normalized_example.split()), intent, 100))
                    continue
                if len(normalized_example.split()) < 2:
                    continue
                phrase_pattern = rf"(?<!\w){re.escape(normalized_example)}(?!\w)"
                if re.search(phrase_pattern, normalized_text):
                    phrase_matches.append((len(normalized_example.split()), intent, 100))

        if phrase_matches:
            _, best_match, highest_score = max(phrase_matches, key=lambda item: item[0])
            logger.info(
                "Intent clasificado como '%s' por coincidencia explícita para el texto: '%s'",
                best_match.get("categoria"),
                text,
            )
            return best_match, highest_score

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
