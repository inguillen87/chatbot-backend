import unicodedata
import re

def normalizar_texto(texto: str) -> str:
    """Normaliza un texto eliminando acentos y puntuación sin modificar palabras."""

    if not texto:
        return ""

    texto = texto.lower().strip()

    # Quitar diacríticos (acentos)
    texto = ''.join(
        c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c)
    )

    # Mantener solo caracteres alfanuméricos y espacios
    texto = re.sub(r"[^a-z0-9\s]", "", texto)

    # Normalizar espacios múltiples
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto

CATEGORIAS_RECLAMO = ["arbol caido", "arreglo de calle", "castracion de mascota", "falta de agua, rotura de caño", "fumigacion", "inspeccion de comercio", "limpieza", "luminaria", "riego de calle", "rotura de semaforo", "tramites de obras privadas", "incendio", "otro motivo", "Sugerencia"]
categorias_normalizadas = [normalizar_texto(c) for c in CATEGORIAS_RECLAMO]
