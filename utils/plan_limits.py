def limite_para_usuario(user):
    """Devuelve el límite de preguntas de un ``user``.

    - ``pro``  -> 250
    - ``full`` -> None (ilimitado)
    - otro plan -> ``user.limite_preguntas``

    La lógica es tolerante a variaciones de mayúsculas en ``user.plan``.
    """
    if user is None:
        return None

    plan = getattr(user, "plan", None)
    if isinstance(plan, str):
        plan = plan.lower()

    from services.plan_config import get_plan_metadata

    metadata = get_plan_metadata(plan)
    if metadata is not None:
        return metadata.message_limit

    return getattr(user, "limite_preguntas", None)
