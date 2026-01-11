import re
import unicodedata

from services.vocabulary_loader import get_ticket_vocabulary

VOCABULARY = get_ticket_vocabulary()

STOPWORDS = VOCABULARY.stopwords
PRIORITY_ISSUES = VOCABULARY.priority_issues
PRIORITY_LOCATIONS = VOCABULARY.priority_locations
LOCATION_PRIORITY_ORDER = list(VOCABULARY.location_priority_order)
LOCATION_TOKENS = VOCABULARY.location_tokens
DESCRIPTIVE_TOKENS = VOCABULARY.descriptive_tokens
IMPACT_TOKENS = VOCABULARY.impact_tokens
IMPACT_KEYWORDS = VOCABULARY.impact_keywords
IMPACT_SUBSTRINGS = VOCABULARY.impact_substrings
GREETING_TOKEN_NORMS = {
    "hola",
    "buenas",
    "buenos",
    "buen",
    "dias",
    "dia",
    "tardes",
    "noches",
}
FILLER_PATTERNS = VOCABULARY.filler_patterns
VERB_ENDINGS = VOCABULARY.verb_endings
VERB_EXCEPTIONS = VOCABULARY.verb_exceptions


def _normalize_token(token: str) -> str:
    normalized = unicodedata.normalize("NFD", token)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


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

    # Short-circuit for short texts to prevent aggressive summarization (e.g. "Quiera...")
    if len(texto) <= max_chars:
        cleaned = texto.replace("\n", " ").strip(" .,;")
        return cleaned

    def _clean_candidate(sentence: str) -> str:
        candidate = sentence.strip()
        if not candidate:
            return ""
        for pattern in FILLER_PATTERNS:
            nueva = re.sub(pattern, "", candidate, flags=re.IGNORECASE).strip()
            if nueva:
                candidate = nueva
        candidate = re.sub(
            r"^(?:y|e|pero|ademas|además)\s+",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        return candidate.strip(" .,;\n")

    def _summarize_phrase(phrase: str, max_chars: int) -> tuple[str | None, bool]:
        if not phrase:
            return None, False

        esta_clause = None
        me_clause_data: tuple[str, str] | None = None

        match_esta = re.search(
            r"est[áa]\s+([^,.\n]+?)(?:\s+me\b|[,.\n]|$)",
            phrase,
            flags=re.IGNORECASE,
        )
        if match_esta:
            esta_clause = match_esta.group(1).strip()

        match_me = re.search(
            r"\bme\s+([a-záéíóúñ]+)\s+([^,.\n]+)",
            phrase,
            flags=re.IGNORECASE,
        )
        if match_me:
            me_clause_data = (
                match_me.group(1).strip(),
                match_me.group(2).strip(),
            )

        tokens_matches = list(re.finditer(r"\b[\wÁÉÍÓÚáéíóúÜüÑñ]+\b", phrase))
        if not tokens_matches:
            truncado = phrase[:max_chars].rstrip(".,; ")
            return truncado, False

        issue_tokens: list[str] = []
        issue_tokens_norm: list[str] = []
        priority_issue_norms: list[str] = []
        location_tokens: list[str] = []
        location_tokens_norm: list[str] = []

        for index, match in enumerate(tokens_matches):
            token_original = match.group()
            token_norm = _normalize_token(token_original.lower())
            if token_norm in STOPWORDS or token_norm.isdigit():
                continue
            if token_norm in issue_tokens_norm or token_norm in location_tokens_norm:
                continue
            if token_norm in LOCATION_TOKENS:
                next_norm = None
                if index + 1 < len(tokens_matches):
                    next_token = tokens_matches[index + 1].group()
                    next_norm = _normalize_token(next_token.lower())
                if next_norm and (
                    next_norm in DESCRIPTIVE_TOKENS
                    or next_norm in IMPACT_TOKENS
                    or any(next_norm.startswith(substr) for substr in IMPACT_SUBSTRINGS)
                ):
                    issue_tokens.append(token_original)
                    issue_tokens_norm.append(token_norm)
                    if token_norm in PRIORITY_ISSUES:
                        priority_issue_norms.append(token_norm)
                    continue
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

        if issue_tokens and all(
            norm in GREETING_TOKEN_NORMS for norm in issue_tokens_norm
        ):
            issue_tokens = []
            issue_tokens_norm = []

        has_issue_tokens = bool(issue_tokens)

        if not has_issue_tokens and location_tokens:
            resumen_loc = location_tokens[0].capitalize()
            return resumen_loc[:max_chars], False

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
            truncado = phrase[:max_chars].rstrip(".,; ")
            return truncado, False

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

        resumen_clean = resumen.rstrip(".,; ")
        meaningful_issue = has_issue_tokens and (
            len(resumen_clean.split()) >= 2
            or any(
                norm in PRIORITY_ISSUES
                or norm in DESCRIPTIVE_TOKENS
                or norm in IMPACT_TOKENS
                for norm in issue_tokens_norm
            )
        )

        return resumen_clean, meaningful_issue

    raw_sentences = re.split(r"[\n!?\.]+", texto)
    candidate_phrases: list[str] = []
    for sentence in raw_sentences:
        cleaned_sentence = _clean_candidate(sentence)
        if cleaned_sentence:
            candidate_phrases.append(cleaned_sentence)

    if not candidate_phrases:
        cleaned_text = _clean_candidate(texto)
        candidate_phrases = [cleaned_text or texto]

    fallback_summary: str | None = None
    for phrase in candidate_phrases:
        resumen, has_issue = _summarize_phrase(phrase, max_chars)
        if not resumen:
            continue
        if not fallback_summary:
            fallback_summary = resumen.rstrip(".,; ")
        if has_issue and resumen.strip():
            return resumen.rstrip(".,; ")

    if fallback_summary:
        return fallback_summary.rstrip(".,; ")

    resumen_total, _ = _summarize_phrase(texto, max_chars)
    if resumen_total:
        return resumen_total.rstrip(".,; ")

    return texto[:max_chars].rstrip(".,; ")

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

    def _normalize_label(value: str) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", value)
        normalized = ''.join(
            ch for ch in normalized if unicodedata.category(ch) != "Mn"
        )
        normalized = re.sub(r"[\s\*]+", " ", normalized)
        normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip().casefold()

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
            normalized_prefix = _normalize_label(prefix)
            normalized_prefix_no_stars = _normalize_label(prefix_no_stars)
            normalized_button = _normalize_label(texto)

            if normalized_button:
                if (
                    (normalized_prefix.startswith(normalized_button) or normalized_prefix_no_stars.startswith(normalized_button))
                    and not has_bullet
                ):
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


def remove_buttons_with_urls_in_message(message_body, options_list):
    """Return a copy of ``options_list`` without buttons whose URL is already present in the message body."""

    if not options_list:
        return options_list

    message_text = str(message_body or "")
    if not message_text:
        return options_list

    filtered_options: list[dict] = []
    for option in options_list:
        if not isinstance(option, dict):
            filtered_options.append(option)
            continue

        url = option.get('url')
        if url and str(url) and str(url) in message_text:
            continue

        filtered_options.append(option)

    return filtered_options

def formatear_ticket_respuesta(
    tipo,
    nombre_usuario,
    descripcion,
    categoria,
    id_ticket=None,
    contacto_especializado=None,
    base_chat_url=None,
    dni=None,
    consulta_pin=None,
    include_links_in_message=True,
):
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
        "chat": "Chat en vivo",
        "pedido": "Pedido",
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
        if include_links_in_message:
            seguimiento_lineas.append(f"• *Ver mi Ticket:* {chat_url}")
        else:
            seguimiento_lineas.append(
                "• *Ver mi Ticket:* Usá el botón \"Ver mi Ticket\" que aparece debajo."
            )

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
