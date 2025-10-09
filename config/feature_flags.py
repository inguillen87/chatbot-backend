import os

FEATURE_ENCUESTAS = os.getenv("FEATURE_ENCUESTAS", "false").lower() == "true"
