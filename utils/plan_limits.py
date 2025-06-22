PLAN_LIMITS = {
    "pro": 200,
    "full": None,
}


def limite_para_usuario(user):
    """Devuelve el límite de preguntas según el plan.

    Si el plan es 'pro' devuelve 200, si es 'full' devuelve None (ilimitado).
    Para cualquier otro plan devuelve ``user.limite_preguntas``.
    """
    if user is None:
        return None
    limite = PLAN_LIMITS.get(getattr(user, "plan", None))
    if limite is not None or getattr(user, "plan", None) in PLAN_LIMITS:
        return limite
    return getattr(user, "limite_preguntas", None)
