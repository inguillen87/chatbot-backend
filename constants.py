# constants.py

# Context keys
CONTEXTO_PYME_V2 = "contexto_pyme_v2"
CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

# Default replies
DEFAULT_REPLY_PYME = "No estoy seguro de cómo ayudarte con eso. ¿Puedes intentar reformular tu pregunta?"
DEFAULT_REPLY_MUNICIPIO = "No comprendí tu solicitud. Por favor, intenta de nuevo o escribe 'menu' para ver las opciones."

# LLM and Interaction Limits
LLM_MAX_CHARS = 2000
MAX_CONSECUTIVE_LLM_ERRORS = 3
MAX_INTERACTIONS_ANON_SESSION = 15