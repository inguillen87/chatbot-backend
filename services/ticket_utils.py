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
    "pido",
    "pedir",
    "poco",
    "por",
    "porque",
    "porfavor",
    "favor",
    "presenta",
    "problema",
    "queremos",
    "queria",
    "quería",
    "quisiera",
    "quisiéramos",
    "quiero",
    "que",
    "qué",
    "consulta",
    "consulto",
    "solicito",
    "solicitar",
    "necesito",
    "agradezco",
    "agradecer",
    "vengo",
    "hola",
    "buenos",
    "buenas",
    "buen",
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
    "cruzada",
    "cruzado",
    "cruzando",
    "cruzan",
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
    "ensucia",
    "ensucian",
    "ensuciada",
    "ensuciado",
    "ensuciando",
    "sucia",
    "sucias",
    "sucio",
    "sucios",
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

IMPACT_TOKENS = {
    "medianera",
    "medianeras",
    "pileta",
    "piletas",
    "patio",
    "patios",
    "casa",
    "casas",
    "techo",
    "techos",
    "pared",
    "paredes",
    "vereda",
    "veredas",
    "calle",
    "calles",
    "entrada",
    "garaje",
    "garage",
    "cochera",
    "auto",
    "autos",
    "vehiculo",
    "vehículos",
    "vehiculos",
    "jardin",
    "jardín",
    "jardines",
    "raiz",
    "raíz",
    "raices",
    "raíces",
    "poste",
    "postes",
}

IMPACT_KEYWORDS = {
    "medianera",
    "pileta",
    "patio",
    "casa",
    "techo",
    "pared",
    "paredes",
    "vereda",
    "calle",
    "entrada",
    "garaje",
    "garage",
    "cochera",
    "auto",
    "vehiculo",
    "vehículos",
    "vehiculos",
    "jardin",
    "jardín",
    "raiz",
    "raíz",
    "raices",
    "raíces",
    "poste",
    "postes",
    "paredon",
    "paredón",
    "medianil",
}

IMPACT_SUBSTRINGS = (
    "ensuci",
    "bloque",
    "tap",
    "cruz",
    "romp",
    "invad",
    "moja",
    "inund",
    "golpe",
    "derrib",
)

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


VERB_ENDINGS = (
    "ando",
    "iendo",
    "yendo",
    "aran",
    "aran",
    "aran",
    "aron",
    "aron",
    "eron",
    "ieron",
    "iran",
    "iran",
    "aban",
    "abas",
    "amos",
    "emos",
    "imos",
    "aste",
    "iste",
    "ado",
    "ido",
    "ando",
    "endo",
    "en",
    "an",
    "as",
    "es",
    "ará",
    "erá",
    "irá",
    "arán",
    "erán",
    "irán",
)

VERB_EXCEPTIONS = {
    "orden",
    "origen",
    "imagen",
    "region",
    "camion",
    "camiones",
    "precision",
    "vision",
    "television",
    "decision",
    "fusion",
    "median",
}


def _looks_like_conjugated_verb(token_norm: str) -> bool:
    if not token_norm:
        return False
    if token_norm in PRIORITY_ISSUES:
        return False
    if token_norm in DESCRIPTIVE_TOKENS or token_norm in IMPACT_TOKENS:
        return False
    if token_norm in VERB_EXCEPTIONS:
        return False
    if len(token_norm) <= 3:
        return False
    for ending in VERB_ENDINGS:
        if token_norm.endswith(ending):
            if ending in {"en", "an", "as", "es"} and len(token_norm) <= 4:
                continue
            return True
    return False


def _clean_clause_text(text: str | None) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", str(text))
    return cleaned.strip(" ,;:.\n")


def _pluralize_spanish_verb(verb: str, subject: str | None) -> str:
    if not verb:
        return ""
    verb = verb.lower()
    subject_norm = (subject or "").strip().lower()
    if subject_norm.endswith("s"):
        if verb.endswith(("an", "en", "ón", "on", "án", "én")):
            return verb
        if verb.endswith("a"):
            return verb[:-1] + "an"
        if verb.endswith("e") or verb.endswith("i"):
            return verb[:-1] + "en"
        if verb.endswith("o"):
            return verb[:-1] + "an"
        if verb.endswith("u"):
            return verb + "n"
    return verb


def _build_me_clause(verb: str, complement: str, subject: str | None, max_chars: int) -> str | None:
    if not verb or not complement:
        return None
    clause = _clean_clause_text(complement)
    if not clause:
        return None
    clause_lower = clause.lower()
    if not any(keyword in clause_lower for keyword in IMPACT_KEYWORDS):
        return None
    pluralized = _pluralize_spanish_verb(verb, subject)
    phrase = f"{pluralized} {clause_lower}".strip()
    if max_chars > 0 and len(phrase) > max_chars:
        phrase = phrase[:max_chars].rstrip(" ,;:.")
    return phrase or None


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

    esta_clause = None
    me_clause_data: tuple[str, str] | None = None

    match_esta = re.search(
        r"est[áa]\s+([^,.\n]+?)(?:\s+me\b|[,.\n]|$)",
        primera_frase,
        flags=re.IGNORECASE,
    )
    if match_esta:
        esta_clause = match_esta.group(1).strip()

    match_me = re.search(
        r"\bme\s+([a-záéíóúñ]+)\s+([^,.\n]+)",
        primera_frase,
        flags=re.IGNORECASE,
    )
    if match_me:
        me_clause_data = (
            match_me.group(1).strip(),
            match_me.group(2).strip(),
        )

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

    if issue_tokens:
        filtered_issue_tokens: list[str] = []
        filtered_issue_norms: list[str] = []
        for token_original, token_norm in zip(issue_tokens, issue_tokens_norm):
            if _looks_like_conjugated_verb(token_norm):
                continue
            filtered_issue_tokens.append(token_original)
            filtered_issue_norms.append(token_norm)
        if filtered_issue_tokens:
            issue_tokens = filtered_issue_tokens
            issue_tokens_norm = filtered_issue_norms

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
        if (norm in DESCRIPTIVE_TOKENS or norm in IMPACT_TOKENS) and idx >= main_issue_index:
            secondary_issue = token
            break

    if not secondary_issue:
        for token, norm in zip(issue_tokens, issue_tokens_norm):
            if norm == main_issue_norm:
                continue
            if norm in DESCRIPTIVE_TOKENS or norm in IMPACT_TOKENS:
                secondary_issue = token
                break

    if not secondary_issue:
        for token, norm in zip(issue_tokens, issue_tokens_norm):
            if norm == main_issue_norm:
                continue
            if any(norm.startswith(substr) for substr in IMPACT_SUBSTRINGS):
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

    effect_texts: list[str] = []
    cleaned_esta = _clean_clause_text(esta_clause)
    if cleaned_esta:
        effect_texts.append(cleaned_esta.lower())
    if me_clause_data and main_issue:
        verb_raw, complement_raw = me_clause_data
        me_clause_text = _build_me_clause(
            verb_raw,
            complement_raw,
            main_issue,
            max_chars - len(main_issue) - 5,
        )
        if me_clause_text:
            effect_texts.append(me_clause_text)

    if effect_texts:
        effect_summary = effect_texts[0]
        for extra in effect_texts[1:]:
            if extra:
                effect_summary += f" y {extra}"
        candidate = f"{main_issue.capitalize()} {effect_summary}".strip()
        if main_location and main_location.lower() not in candidate.lower():
            candidate = f"{candidate} en {main_location}".strip()
        if len(candidate) > max_chars:
            candidate = candidate[:max_chars].rstrip(".,; ")
        if candidate and (len(resumen.split()) <= 3 or len(candidate) > len(resumen)):
            resumen = candidate

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
    button_entries: list[tuple[str, str]] = []
    for option in options_list:
        if not isinstance(option, dict):
            continue
        url = option.get('url')
        if not url:
            continue
        texto = str(option.get('texto', '') or '').strip()
        button_entries.append((url, texto))

    if not button_entries:
        return message_body_str

    lines = message_body_str.splitlines()
    cleaned_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append(line)
            continue

        remove_line = False
        for url, texto in button_entries:
            if url not in stripped:
                continue
            has_bullet = stripped.startswith(("•", "-", "*"))
            prefix = re.sub(r"^[•\-\s]+", "", stripped)
            prefix_no_stars = prefix.lstrip("* ").strip()
            texto_cf = texto.casefold()

            if texto_cf:
                if (prefix.casefold().startswith(texto_cf) or prefix_no_stars.casefold().startswith(texto_cf)) and not has_bullet:
                    remove_line = True
                    break
            if not texto_cf or stripped == url or stripped.lower() == url.lower():
                remove_line = True
                break

        if not remove_line:
            cleaned_lines.append(line)

    message_body_str = "\n".join(cleaned_lines)

    # Clean up common leftover phrases and extra spaces
    message_body_str = re.sub(
        r'por favor\s+ingresá\s+al\s+siguiente\s+enlace\s*:?',
        '',
        message_body_str,
        flags=re.IGNORECASE,
    ).strip()
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
