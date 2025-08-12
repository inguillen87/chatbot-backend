def handle(msg, meta):
    """
    Generates the payload for the main menu, incorporating the user's UX feedback.
    """
    # TODO: Personalize the title if user name is available in `meta`
    title = "¡Hola! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín."

    summary = (
        "Estoy aquí para ayudarte de una forma más inteligente. Podés escribirme, pero también podés:\n"
        "🗣️ **Enviarme un audio** con tu consulta.\n"
        "📸 **Mandar una foto** de un problema (un bache, una luminaria rota, etc.).\n"
        "📍 **Compartir tu ubicación** para reclamos o para encontrar puntos de interés.\n\n"
        "¿Cómo te puedo ayudar hoy? Elegí una opción:"
    )

    items = [
        {"n": "1", "label": "Iniciar un Reclamo", "key": "iniciar_reclamo"},
        {"n": "2", "label": "Realizar una Denuncia", "key": "denuncias"},
        {"n": "3", "label": "Licencia de Conducir", "key": "licencia_de_conducir"},
        {"n": "4", "label": "Pagar Tasas", "key": "pago_de_tasas"},
        {"n": "5", "label": "Consultar otros trámites", "key": "consultar_tramites"},
        {"n": "6", "label": "Veterinaria y Bromatología", "key": "veterinaria"},
        {"n": "7", "label": "Solicitar Turnos", "key": "solicitar_turnos"},
        {"n": "8", "label": "Agenda Cultural y Turística", "key": "agenda"},
        {"n": "9", "label": "Últimas Novedades", "key": "noticias"},
        {"n": "10", "label": "Defensa del Consumidor", "key": "defensa_consumidor"},
    ]

    return {
        "type": "menu",
        "title": title,
        "summary": summary,
        "data": {
            "items": items
        },
        "tags": ["menu", "principal"],
        "source": "manual"
    }
