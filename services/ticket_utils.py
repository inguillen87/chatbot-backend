import re
import unicodedata

STOPWORDS = {
    "a",
    "al",
    "alguna",
    "algunas",
    "alguno",
    "algunos",
    "alrededor",
    "ante",
    "antes",
    "asi",
    "así",
    "bastante",
    "bien",
    "como",
    "con",
    "contra",
    "cuando",
    "cual",
    "cuales",
    "cualquier",
    "de",
    "del",
    "donde",
    "dónde",
    "durante",
    "e",
    "el",
    "ella",
    "ellas",
    "ellos",
    "en",
    "entre",
    "era",
    "es",
    "esa",
    "ese",
    "eso",
    "esta",
    "está",
    "estas",
    "este",
    "esto",
    "estos",
    "foto",
    "gracias",
    "gran",
    "grave",
    "habia",
    "había",
    "hay",
    "imagen",
    "la",
    "las",
    "lo",
    "los",
    "mas",
    "más",
    "mientras",
    "mitad",
    "muy",
    "nos",
    "nuestra",
    "nuestro",
    "otra",
    "otro",
    "para",
    "pero",
    "poco",
    "por",
    "porque",
    "presenta",
    "problema",
    "que",
    "qué",
    "quien",
    "quién",
    "se",
    "segun",
    "según",
    "sobre",
    "solo",
    "sólo",
    "tan",
    "tambien",
    "también",
    "tenemos",
    "tiene",
    "todos",
    "una",
    "unas",
    "uno",
    "unos",
    "ve",
    "vemos",
    "y",
    "zona",
}

PRIORITY_ISSUES = {
    "agua",
    "arbol",
    "árbol",
    "basura",
    "bache",
    "cable",
    "cables",
    "cloaca",
    "desague",
    "desagüe",
    "desborde",
    "escape",
    "fuga",
    "iluminacion",
    "iluminación",
    "inundacion",
    "inundación",
    "lampara",
    "lámpara",
    "limpieza",
    "luminaria",
    "luz",
    "perdida",
    "poda",
    "pozo",
    "ramas",
    "residuos",
    "sumidero",
}

PRIORITY_LOCATIONS = {
    "barrio",
    "calle",
    "canal",
    "cuneta",
    "desague",
    "desagüe",
    "esquina",
    "vereda",
}

LOCATION_PRIORITY_ORDER = [
    "desague",
    "desagüe",
    "cloaca",
    "cloacas",
    "cuneta",
    "canal",
    "calle",
    "vereda",
    "barrio",
    "esquina",
    "plaza",
    "cuadra",
]

LOCATION_TOKENS = PRIORITY_LOCATIONS | {
    "acera",
    "avenida",
    "boulevard",
    "camino",
    "columna",
    "cuadra",
    "lote",
    "parque",
    "pasaje",
    "plaza",
    "poste",
    "puente",
    "rotonda",
    "ruta",
    "sector",
}

DESCRIPTIVE_TOKENS = {
    "apagada",
    "apagadas",
    "apagado",
    "apagados",
    "bloqueada",
    "bloqueado",
    "cortada",
    "cortado",
    "cortados",
    "caida",
    "caidas",
    "caido",
    "caidos",
    "caída",
    "caídas",
    "caído",
    "caídos",
    "deteriorada",
    "deteriorado",
    "expuesta",
    "expuesto",
    "inundada",
    "inundado",
    "quemada",
    "quemado",
    "rebalsada",
    "rebalsado",
    "roja",
    "rota",
    "rotas",
    "roto",
    "rotos",
    "taponada",
    "taponado",
    "tapada",
    "tapado",
}

FILLER_PATTERNS = [
    r"^la\s+(foto|imagen|escena)\s+(muestra|presenta|refleja)\s+",
    r"^esta\s+(foto|imagen)\s+(muestra|presenta)\s+",
    r"^se\s+(observa|ve|aprecia)\s+",
    r"^gracias\s+a\s+la\s+imagen\s+(se\s+)?(observa|ve)\s+",
    r"^tengo\s+un?a?\s+",
    r"^hay\s+un?a?\s+",
    r"^es\s+un?a?\s+",
    r"^se\s+trata\s+de\s+",
]


def _normalize_token(token: str) -> str:
    normalized = unicodedata.normalize("NFD", token)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def construir_descripcion_breve(texto: str | None, max_chars: int = 80) -> str | None:
    """Return a concise, human-readable summary for claim descriptions."""

    if not texto:
        return texto

    texto = str(texto).strip()
    if not texto:
        return texto

    primera_frase = re.split(r"[\n!?\.]+", texto, maxsplit=1)[0].strip()
    if not primera_frase:
        primera_frase = texto

    for pattern in FILLER_PATTERNS:
        nueva = re.sub(pattern, "", primera_frase, flags=re.IGNORECASE).strip()
        if nueva:
            primera_frase = nueva

    primera_frase = re.sub(r"^(?:y|e|pero|ademas|además)\s+", "", primera_frase, flags=re.IGNORECASE)

    if len(primera_frase) <= max(max_chars - 40, 30):
        return primera_frase.rstrip(".,; ")

    primer_clausula = re.split(r",|;| - ", primera_frase, maxsplit=1)[0].strip()
    if primer_clausula and len(primer_clausula) <= max(max_chars - 30, 30):
        return primer_clausula.rstrip(".,; ")

    tokens_matches = list(re.finditer(r"\b[\wÁÉÍÓÚáéíóúÜüÑñ]+\b", primera_frase))
    if not tokens_matches:
        truncado = primera_frase[:max_chars].rstrip(".,; ")
        return truncado

    issue_tokens: list[str] = []
    issue_tokens_norm: list[str] = []
    priority_issue_norms: list[str] = []
    location_tokens: list[str] = []
    location_tokens_norm: list[str] = []

    for match in tokens_matches:
        token_original = match.group()
        token_norm = _normalize_token(token_original.lower())
        if token_norm in STOPWORDS or token_norm.isdigit():
            continue
        if token_norm in issue_tokens_norm or token_norm in location_tokens_norm:
            continue
        if token_norm in LOCATION_TOKENS:
            location_tokens.append(token_original)
            location_tokens_norm.append(token_norm)
        else:
            issue_tokens.append(token_original)
            issue_tokens_norm.append(token_norm)
            if token_norm in PRIORITY_ISSUES:
                priority_issue_norms.append(token_norm)

    if not issue_tokens and location_tokens:
        resumen = location_tokens[0].capitalize()
        return resumen[:max_chars]

    main_issue = None
    if priority_issue_norms:
        for norm in priority_issue_norms:
            try:
                idx = issue_tokens_norm.index(norm)
                main_issue = issue_tokens[idx]
                break
            except ValueError:
                continue
    if not main_issue and issue_tokens:
        main_issue = issue_tokens[0]

    if not main_issue:
        truncado = primera_frase[:max_chars].rstrip(".,; ")
        return truncado

    main_issue_norm = _normalize_token(main_issue.lower())

    secondary_issue = None
    try:
        main_issue_index = issue_tokens_norm.index(main_issue_norm)
    except ValueError:
        main_issue_index = -1

    for idx, (token, norm) in enumerate(zip(issue_tokens, issue_tokens_norm)):
        if norm == main_issue_norm:
            continue
        if norm in DESCRIPTIVE_TOKENS and idx >= main_issue_index:
            secondary_issue = token
            break

    if not secondary_issue:
        for token, norm in zip(issue_tokens, issue_tokens_norm):
            if norm == main_issue_norm:
                continue
            if norm in DESCRIPTIVE_TOKENS:
                secondary_issue = token
                break

    main_location = None
    for preferred in LOCATION_PRIORITY_ORDER:
        for token, norm in zip(location_tokens, location_tokens_norm):
            if norm == preferred:
                main_location = token
                break
        if main_location:
            break
    if not main_location and location_tokens:
        main_location = location_tokens[0]

    partes = [main_issue.capitalize()]
    if secondary_issue and _normalize_token(secondary_issue.lower()) != main_issue_norm:
        partes.append(secondary_issue.lower())

    resumen = " ".join(partes)
    if main_location:
        resumen += f" en {main_location}"

    if len(resumen) > max_chars:
        resumen = resumen[:max_chars].rstrip(".,; ")

    return resumen

def _remove_redundant_urls_from_message(message_body, options_list):
    """
    Removes URLs from the message body if they are already present in the buttons.
    """
    if not message_body or not options_list:
        return message_body

    message_body_str = str(message_body)

    for option in options_list:
        if not isinstance(option, dict) or 'url' not in option:
            continue

        url = option['url']
        if url not in message_body_str:
            continue

        index = message_body_str.find(url)
        keep_url = False
        if index != -1:
            context_window = message_body_str[max(0, index - 40):index].lower()
            if re.search(r"ver\s+mi\s+ticket", context_window, flags=re.IGNORECASE):
                keep_url = True
            elif re.search(r"ver\s+ticket", context_window, flags=re.IGNORECASE):
                keep_url = True

        if keep_url:
            continue

        message_body_str = message_body_str.replace(url, '')

    # Clean up common leftover phrases and extra spaces
    message_body_str = re.sub(r'por favor\s+ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body_str, flags=re.IGNORECASE).strip()
    message_body_str = re.sub(r'ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body_str, flags=re.IGNORECASE).strip()
    message_body_str = re.sub(r'enlace\s*:?', '', message_body_str, flags=re.IGNORECASE).strip()

    # Replace multiple spaces with a single space and clean up punctuation
    message_body_str = re.sub(r'[ \t]{2,}', ' ', message_body_str)
    message_body_str = re.sub(r'[ \t]*\n[ \t]*', '\n', message_body_str)
    message_body_str = re.sub(r'\n{3,}', '\n\n', message_body_str)
    message_body_str = message_body_str.strip()
    message_body_str = message_body_str.replace(' .', '.').strip()
    message_body_str = re.sub(r'[,:]\s*\.', '.', message_body_str)
    if message_body_str == ':':
        message_body_str = ''

    return message_body_str

def formatear_ticket_respuesta(tipo, nombre_usuario, descripcion, categoria, id_ticket=None, contacto_especializado=None, base_chat_url=None, dni=None, consulta_pin=None):
    nombre_asesor = None
    titulo_asesor = None
    telefono_asesor = None
    horario_asesor = None
    link_informacion = None
    link_whatsapp = None
    botones = []
    chat_url = None

    if contacto_especializado:
        nombre_asesor = contacto_especializado.get("nombre")
        titulo_asesor = contacto_especializado.get("titulo")
        telefono_asesor = contacto_especializado.get("telefono")
        horario_asesor = contacto_especializado.get("horario")
        link_informacion = contacto_especializado.get("link")
        if nombre_asesor and telefono_asesor:
            # telefono_numerico = ''.join(filter(str.isdigit, str(telefono_asesor)))
            # link_whatsapp = f"https://wa.me/{telefono_numerico}?text=Hola,%20quiero%20hacer%20seguimiento%20de%20mi%20{tipo}%20(ID:{id_ticket})"
            # botones.append({
            #     "texto": f"📱 Contactar a {nombre_asesor}",
            #     "url": str(link_whatsapp),
            #     "type": "url"
            # })
            pass
        if link_informacion:
            botones.append({
                "texto": "🌐 Más información",
                "url": str(link_informacion),
                "type": "url"
            })

    if id_ticket and base_chat_url:
        if base_chat_url.endswith('/'):
            base_chat_url = base_chat_url[:-1]

        ticket_id_numeric = id_ticket.replace('M-', '').replace('S-', '')
        chat_url = f"{base_chat_url}/{ticket_id_numeric}"
        if consulta_pin:
            chat_url += f"?pin={consulta_pin}"
        botones.append({
            "texto": "💬 Ver mi Ticket",
            "url": chat_url,
            "type": "url"
        })

    tipos = {
        "reclamo": "Reclamo",
        "sugerencia": "Sugerencia",
        "tramite": "Trámite",
        "chat": "Chat en vivo"
    }
    texto_tipo = tipos.get(tipo, "Consulta")

    def _resumir_descripcion(texto: str) -> str:
        """Build a concise summary leveraging the shared short-description helper."""
        if not texto:
            return texto
        texto = texto.strip()
        texto = re.sub(r"^tengo\s+un?\s+", "", texto, flags=re.IGNORECASE)
        texto = re.sub(r"^hay\s+un?\s+", "", texto, flags=re.IGNORECASE)
        resumen = construir_descripcion_breve(texto, max_chars=70)
        return resumen or texto

    descripcion_resumen = _resumir_descripcion(descripcion)

    resumen_lineas: list[str] = []
    if id_ticket:
        resumen_lineas.append(f"• *Ticket:* `{id_ticket}`")
    if categoria:
        resumen_lineas.append(f"• *Categoría:* {categoria}")
    if descripcion_resumen:
        resumen_lineas.append(f"• *Descripción:* {descripcion_resumen}")
    if dni:
        resumen_lineas.append(f"• *DNI:* `{dni}`")

    seguimiento_lineas: list[str] = []
    if consulta_pin:
        seguimiento_lineas.append(f"• *PIN:* `{consulta_pin}`")
    if chat_url:
        seguimiento_lineas.append(f"• *Ver mi Ticket:* {chat_url}")

    respuesta_lineas: list[str] = [f"✅ *¡{texto_tipo} recibido, {nombre_usuario}!*"]
    if resumen_lineas:
        respuesta_lineas.append("")
        respuesta_lineas.append("📄 *Resumen:*")
        respuesta_lineas.extend(resumen_lineas)

    if seguimiento_lineas:
        respuesta_lineas.append("")
        respuesta_lineas.append("🔗 *Seguimiento:*")
        respuesta_lineas.extend(seguimiento_lineas)

    if nombre_asesor:
        respuesta_lineas.append("")
        respuesta_lineas.append("📞 *Contacto para seguimiento:*")
        respuesta_lineas.append(f"• *Nombre:* {nombre_asesor}")
        if titulo_asesor:
            respuesta_lineas.append(f"• *Cargo:* {titulo_asesor}")
        if telefono_asesor:
            respuesta_lineas.append(f"• *Teléfono:* {telefono_asesor}")
        if horario_asesor:
            respuesta_lineas.append(f"• *Horario:* {horario_asesor}")

    respuesta = "\n".join(respuesta_lineas).strip()

    # Limpiar URLs redundantes del cuerpo del mensaje
    respuesta_limpia = _remove_redundant_urls_from_message(respuesta, botones)

    return respuesta_limpia, botones
