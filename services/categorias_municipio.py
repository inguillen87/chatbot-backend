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
    "arbol caido": [
        "arbol",
        "rama",
        "ramas",
        "tronco",
        "caido",
        "caida de arbol",
        "arbol caido",
        "arbol tumbado",
        "arbol derribado",
        "ramas caidas",
        "poda",
        "podar",
        "arbol del vecino",
        "medianera",
        "raices"
    ],
    "arreglo de calle": [
        "bache",
        "pozo",
        "agujero",
        "calle rota",
        "pavimento",
        "socavon",
        "calzada dañada",
        "asfalto roto",
        "losa levantada",
        "vereda rota",
        "vereda levantada",
        "calle en mal estado"
    ],
    "castracion de mascota": [
        "castrar",
        "esterilizacion",
        "esterilizar",
        "operar mascota",
        "castrar perro",
        "castrar gato"
    ],
    "falta de agua, rotura de caño": [
        "sin agua",
        "corte de agua",
        "caño roto",
        "caneria",
        "fuga de agua",
        "perdida",
        "escape de agua",
        "sin suministro",
        "canilla",
        "canilla rota",
        "sin servicio de agua"
    ],
    "fumigacion": [
        "plagas",
        "mosquitos",
        "insectos",
        "roedores",
        "alacranes",
        "hormigas",
        "cucarachas",
        "fumigar"
    ],
    "inspeccion de comercio": [
        "control comercial",
        "habilitacion",
        "inspectores",
        "clausura",
        "licencia",
        "negocio",
        "local comercial",
        "verificacion"
    ],
    "limpieza": [
        "basura",
        "residuos",
        "suciedad",
        "desmalezado",
        "escombros",
        "yuyos",
        "malezas",
        "limpiar",
        "baldio",
        "pasto alto",
        "basural"
    ],
    "luminaria": [
        "lampara",
        "luz",
        "poste",
        "foco",
        "alumbrado",
        "iluminacion",
        "farola",
        "columna",
        "luz quemada",
        "luz apagada",
        "sin luz",
        "poste sin luz",
        "farol apagado"
    ],
    "riego de calle": [
        "regar",
        "polvo",
        "tierra",
        "camion de agua",
        "riego",
        "calle de tierra",
        "camion cisterna"
    ],
    "rotura de semaforo": [
        "semaforo",
        "luces de transito",
        "senal de transito",
        "semaforo apagado",
        "semaforo roto",
        "semaforo intermitente",
        "semaforo fuera de servicio"
    ],
    "tramites de obras privadas": [
        "permiso de obra",
        "construccion",
        "obra privada",
        "planos",
        "expediente",
        "ampliacion",
        "refaccion",
        "obra nueva",
        "habilitacion de obra"
    ],
    "incendio": [
        "fuego",
        "quemado",
        "llamas",
        "humo",
        "incendio",
        "quema de basura",
        "fuego en pastizal"
    ],
    "Sugerencia": [
        "sugerencia",
        "idea",
        "propuesta",
        "recomendacion",
        "mejora"
    ],
    "otro motivo": [
        "otro",
        "otros",
        "ninguna de las anteriores"
    ],
}
