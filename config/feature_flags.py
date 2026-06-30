import os


def _parse_feature_flag(env_name: str, default: bool = True) -> bool:
    """Return the boolean value of an environment flag.

    The previous implementation defaulted to ``False`` when the environment
    variable was missing, which meant newly deployed environments never saw
    the surveys module even if there were published surveys available.  By
    centralising the parsing logic we can provide a more sensible default
    (enabled) while still letting operators override it explicitly.
    """

    raw_value = os.getenv(env_name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    return normalized in {"1", "true", "yes", "on", "enabled"}


FEATURE_ENCUESTAS = _parse_feature_flag("FEATURE_ENCUESTAS", default=True)
FEATURE_AI_OPS_QUEUE = _parse_feature_flag("FEATURE_AI_OPS_QUEUE", default=False)
