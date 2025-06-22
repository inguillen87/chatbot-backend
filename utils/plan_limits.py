PLAN_LIMITS = {
    "pro": 200,
    "full": None,
}


def limite_para_usuario(user):
    """Devuelve el límite de preguntas de un ``user``.

    - ``pro``  -> 200
    - ``full`` -> None (ilimitado)
    - otro plan -> ``user.limite_preguntas``

    La lógica es tolerante a variaciones de mayúsculas en ``user.plan``.
    """
    if user is None:
        return None

    plan = getattr(user, "plan", None)
    if isinstance(plan, str):
        plan = plan.lower()

    limite = PLAN_LIMITS.get(plan)
    if limite is not None or plan in PLAN_LIMITS:
        return limite

    return getattr(user, "limite_preguntas", None)
