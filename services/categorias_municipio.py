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

# Mapa sencillo de palabras relacionadas que ayudan al LLM a
# elegir la categoría correcta a partir de transcripciones de audio.
CATEGORIAS_SINONIMOS = {
    "arbol caido": ["arbol", "rama", "tronco", "caido"],
    "arreglo de calle": ["bache", "pozo", "agujero", "calle rota", "pavimento"],
    "castracion de mascota": ["castrar", "esterilizacion"],
    "falta de agua, rotura de caño": ["sin agua", "caño roto", "caneria"],
    "fumigacion": ["plagas", "mosquitos", "insectos"],
    "inspeccion de comercio": ["control comercial", "habilitacion", "inspectores"],
    "limpieza": ["basura", "residuos", "suciedad", "desmalezado"],
    "luminaria": ["lampara", "luz", "poste", "foco", "alumbrado", "iluminacion"],
    "riego de calle": ["regar", "polvo", "tierra"],
    "rotura de semaforo": ["semaforo", "luces de transito", "senal de transito"],
    "tramites de obras privadas": ["permiso de obra", "construccion", "obra privada"],
    "incendio": ["fuego", "quemado"],
}
