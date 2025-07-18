import json
import logging
import re
from typing import Dict, List, Any, Optional # Added Optional
from utils.validators import (
    extract_email,
    extract_phone,
    extract_name,
    extract_address,
)
from google.cloud import documentai

# Intenta importar errores específicos de Cohere.
# El nombre exacto puede variar según la versión de la librería 'cohere'.
# Comunes son cohere.CohereError, cohere.APIError, cohere.CohereAPIError
try:
    import cohere
    # Prioriza el error más específico si existe y luego el más general de la librería
    if hasattr(cohere, "CohereAPIError"):
        CohereAPIError = cohere.CohereAPIError
    elif hasattr(getattr(cohere, "errors", None), "CohereAPIError"):
        CohereAPIError = cohere.errors.CohereAPIError  # type: ignore[attr-defined]
    elif hasattr(cohere, "APIError"):
        CohereAPIError = cohere.APIError
    elif hasattr(cohere, "CohereError"):
        CohereAPIError = cohere.CohereError
    else:
        CohereAPIError = None  # No se pudo encontrar un error específico de Cohere API
except ImportError:
    cohere = None
    CohereAPIError = None
    # robust_chat también dependería de 'cohere', así que el mock es importante si 'cohere' no está.


try:
    from services.cohere_ai import robust_chat
except ImportError:
    # This is a fallback for environments where robust_chat might not be available initially
    # or for simpler testing. Replace with a proper mock if robust_chat is critical.
    def robust_chat(message: str, **kwargs) -> str:
        logger.warning("Using mock robust_chat. LLM calls will not be real.")
        if "Extract contact details" in message:
            # Simulate LLM response for contact extraction
            if "John Doe" in message and "123 Main St" in message:
                return json.dumps({
                    "nombre_cliente": "John Doe",
                    "direccion_cliente": "123 Main St, Anytown",
                    "telefono_cliente": "555-1234",
                    "email_cliente": "john.doe@example.com"
                })
            elif "Jane Smith" in message:
                 return json.dumps({"nombre_cliente": "Jane Smith"})
            return json.dumps({})
        elif "Extract complaint details" in message:
            # Simulate LLM response for complaint extraction
            if "broken streetlight" in message and "Elm Street" in message:
                return json.dumps({
                    "tipo_problema": "Alumbrado público",
                    "ubicacion_problema": "Calle Elm, cerca del poste 123",
                    "descripcion_problema": "La farola en la esquina de Elm Street y Oak Avenue está rota y no enciende desde hace 3 días."
                })
            return json.dumps({"descripcion_problema": "El usuario reportó un problema."})
        elif "Update summary" in message:
            # Simulate LLM response for summary update
            # This is a very basic mock, real implementation would be more complex
            summary_match = re.search(r"Current summary: '''(.*?)'''", message, re.DOTALL)
            data_match = re.search(r"New data: '''(.*?)'''", message, re.DOTALL)
            if summary_match and data_match:
                current_summary = summary_match.group(1)
                new_data_str = data_match.group(1)
                try:
                    new_data = json.loads(new_data_str)
                    updated_summary = current_summary
                    for key, value in new_data.items():
                        updated_summary += f"\n- {key.replace('_', ' ').capitalize()}: {value}"
                    return updated_summary
                except json.JSONDecodeError:
                    return current_summary + "\nError processing new data."
            return "Mocked summary update."
        return "Mocked LLM response."

logger = logging.getLogger(__name__)

def _clean_llm_json_output(llm_output: str) -> str:
    """
    Cleans potential markdown code fences and trailing commas from LLM JSON output.
    """
    if not llm_output:
        return ""

    # Remove markdown code fences (```json ... ```)
    match = re.match(r"^\s*```json\s*([\s\S]*?)\s*```\s*$", llm_output, re.DOTALL)
    if match:
        cleaned_output = match.group(1)
    else:
        cleaned_output = llm_output

    # Remove trailing commas before closing braces or brackets
    cleaned_output = re.sub(r",\s*(?=[}\]])", "", cleaned_output)

    return cleaned_output.strip()

def extract_multiple_contact_details_llm(text: str, potential_fields: List[str]) -> Dict[str, Any]:
    """
    Uses an LLM to extract multiple contact details from a given text.

    Args:
        text: The user's input string.
        potential_fields: A list of field names (e.g., "nombre_cliente", "telefono_cliente")
                          to guide the LLM.

    Returns:
        A dictionary with extracted field names as keys and their values.
        Returns an empty dictionary if no details are found or an error occurs.
    """
    if not text or not potential_fields:
        return {}

    prompt = (
        "You are an expert data extraction assistant. Extract the following contact details "
        f"if present in the USER MESSAGE: {', '.join(potential_fields)}. "
        "Return the information ONLY as a valid JSON object where keys are the field names "
        f"from the list: {potential_fields}. If a field is not found, omit it from the JSON. "
        "Do not add any explanations or conversational text. Ensure phone numbers are "
        "extracted as accurately as possible, including area codes if provided.\n\n"
        "Example fields:\n"
        "- nombre_cliente: Full name of the customer.\n"
        "- telefono_cliente: Phone number.\n"
        "- direccion_cliente: Full delivery address.\n"
        "- email_cliente: Email address.\n\n"
        f"USER MESSAGE: \"{text}\"\n\n"
        "JSON RESPONSE:"
    )

    extracted_data = {}
    try:
        response_content = robust_chat(message=prompt) # Removed model_override
        if response_content:
            cleaned_response = _clean_llm_json_output(response_content)
            if cleaned_response:
                extracted_data = json.loads(cleaned_response)
                # Ensure only requested fields are returned
                extracted_data = {k: v for k, v in extracted_data.items() if k in potential_fields and v}
            else:
                logger.info(f"[LLM_CONTACT_EXTRACT] LLM response was empty after cleaning for text: {text}")
        else:
            logger.info(f"[LLM_CONTACT_EXTRACT] LLM returned empty response for text: {text}")

    except json.JSONDecodeError as e:
        logger.error(f"[LLM_CONTACT_EXTRACT] JSONDecodeError parsing LLM response: {e}. Response: '{response_content}' for text: '{text}'")
        # Optionally, try a more lenient parsing or regex for simple cases if JSON fails often
    except Exception as e:
        logger.error(f"[LLM_CONTACT_EXTRACT] Error in extract_multiple_contact_details_llm: {e} for text: '{text}'")

    # Fallback heuristics for fields not provided by LLM
    for field in potential_fields:
        if field not in extracted_data or not extracted_data.get(field):
            heuristic_value = None
            if field == "nombre_cliente":
                heuristic_value = extract_name(text)
            elif field == "telefono_cliente":
                heuristic_value = extract_phone(text)
            elif field == "direccion_cliente":
                heuristic_value = extract_address(text)
            elif field == "email_cliente":
                heuristic_value = extract_email(text)
            if heuristic_value:
                extracted_data[field] = heuristic_value

    return extracted_data

def extract_complaint_details_llm(text: str, default_localidad: str | None = None, default_provincia: str | None = None) -> Dict[str, str]:
    """
    Uses an LLM to extract key details from a user's complaint message,
    considering default location context.

    Args:
        text: The user's complaint description.
        default_localidad: The default city/locality for the bot's context.
        default_provincia: The default province for the bot's context.

    Returns:
        A dictionary with keys like "tipo_problema", "ubicacion_problema",
        "descripcion_problema". Returns an empty dictionary on error.
    """
    if not text:
        return {}

    location_context_instruction = ""
    if default_localidad and default_provincia and default_localidad != 'N/A' and default_provincia != 'N/A':
        location_context_instruction = (
            f"Este reclamo es para el municipio de {default_localidad}, {default_provincia}. "
            "Si el usuario menciona una calle y número pero no una ciudad o provincia, "
            f"asumí que la dirección corresponde a {default_localidad}, {default_provincia}. "
            "Solo usa estos valores por defecto para localidad y provincia si el usuario NO los especifica."
        )
    elif default_localidad and default_localidad != 'N/A':
        location_context_instruction = (
            f"Este reclamo es para el municipio de {default_localidad}. "
            "Si el usuario menciona una calle y número pero no una ciudad, "
            f"asumí que la dirección corresponde a {default_localidad}. "
            "Solo usa este valor por defecto para localidad si el usuario NO lo especifica."
        )


    prompt = (
        "You are an expert complaint analysis assistant. From the USER'S COMPLAINT below, "
        "extract the following details: "
        "1. 'tipo_problema': The general category or type of the issue (e.g., 'Alumbrado público', 'Recolección de residuos', 'Fuga de agua', 'Ruidos molestos', 'Problema con vecino'). "
        "2. 'ubicacion_problema': The specific location of the problem, including street names, numbers, landmarks, or neighborhood if mentioned. "
        "3. 'descripcion_problema': A concise summary of the complaint itself, capturing the core issue. "
        f"{location_context_instruction} " # Added location context instruction
        "Return the information ONLY as a valid JSON object with these exact keys. "
        "If a detail is not found, omit its key from the JSON or set its value to an empty string. "
        "Do not add any explanations or conversational text.\n\n"
        f"USER'S COMPLAINT: \"{text}\"\n\n"
        "JSON RESPONSE:"
    )

    extracted_details = {}
    try:
        response_content = robust_chat(message=prompt) # Removed model_override
        if response_content:
            cleaned_response = _clean_llm_json_output(response_content)
            if cleaned_response:
                extracted_details = json.loads(cleaned_response)
                # Filter out empty values, but keep all three primary keys if possible, even if empty.
                # This might be better handled by the caller if specific keys are always expected.
                # For now, just ensure the main keys are what we expect.
                valid_keys = ["tipo_problema", "ubicacion_problema", "descripcion_problema"]
                extracted_details = {k: v for k, v in extracted_details.items() if k in valid_keys and v} # Only keep non-empty values for expected keys
            else:
                 logger.info(f"[LLM_COMPLAINT_EXTRACT] LLM response was empty after cleaning for text: {text}")
        else:
            logger.info(f"[LLM_COMPLAINT_EXTRACT] LLM returned empty response for text: {text}")

    except json.JSONDecodeError as e:
        logger.error(f"[LLM_COMPLAINT_EXTRACT] JSONDecodeError parsing LLM response: {e}. Response: '{response_content}' for text: '{text}'")
    except Exception as e: # Captura genérica al final
        # Si hemos identificado un error específico de Cohere y es de ese tipo, loguearlo específicamente.
        if CohereAPIError and isinstance(e, CohereAPIError):
            logger.error(f"[LLM_COMPLAINT_EXTRACT] Cohere API Error in extract_complaint_details_llm: {e} (Type: {type(e)}). Text: '{text}'")
        else:
            # Log de error genérico para otros tipos de excepciones
            logger.error(f"[LLM_COMPLAINT_EXTRACT] Generic Error in extract_complaint_details_llm: {e} (Type: {type(e)}). Text: '{text}'", exc_info=True) # exc_info=True para traceback

    return extracted_details

def update_summary_with_llm_extraction(current_summary: str, extracted_data: Dict[str, Any]) -> str:
    """
    Updates an existing summary string with new information extracted by an LLM.
    This is a simplified version; a more sophisticated approach might involve an LLM call
    to intelligently merge the information.

    Args:
        current_summary: The existing summary string.
        extracted_data: A dictionary of new data to incorporate.

    Returns:
        The updated summary string.
    """
    if not extracted_data:
        return current_summary

    # For this version, we'll use a simple LLM call to re-summarize if the robust_chat is real.
    # If using the mock, it will do a basic append.

    # Check if robust_chat is the mock or real one by checking its __module__ or specific attribute
    # This is a bit hacky; a better way would be dependency injection or a config flag.
    is_mock_chat = hasattr(robust_chat, '__module__') and robust_chat.__module__ == __name__ # if defined in this file

    if not is_mock_chat: # Attempt to use LLM for a more intelligent merge
        prompt = (
            "You are a text summarization assistant. Given a CURRENT SUMMARY and NEW DATA, "
            "intelligently update the summary. Integrate the new data naturally, avoid redundancy, "
            "and maintain clarity. If new data contradicts or refines existing points, reflect that. "
            "Return only the updated summary text. \n\n"
            f"CURRENT SUMMARY: '''{current_summary}'''\n\n"
            f"NEW DATA (in JSON format): '''{json.dumps(extracted_data, indent=2)}'''\n\n"
            "UPDATED SUMMARY:"
        )
        try:
            updated_summary = robust_chat(message=prompt, model_override="gpt-4o-mini") # Cheaper model for summary
            if updated_summary:
                return updated_summary.strip()
            else: # Fallback if LLM returns empty
                logger.warning("[LLM_UPDATE_SUMMARY] LLM returned empty for summary update. Using basic append.")
        except Exception as e:
            logger.error(f"[LLM_UPDATE_SUMMARY] Error calling LLM for summary update: {e}. Using basic append.")
            # Fall through to basic append on error

    # Basic append logic (fallback or if mock is used)
    summary_lines = [current_summary] if current_summary else []
    for key, value in extracted_data.items():
        if value: # Only add if there's a value
            # Try to make keys more readable
            readable_key = key.replace("_", " ").capitalize()
            # Avoid adding duplicate lines if the info seems to be already there (very basic check)
            if f"{readable_key}: {value}" not in current_summary:
                 summary_lines.append(f"- {readable_key}: {value}")

    return "\n".join(filter(None, summary_lines))


if __name__ == '__main__':
    # Basic Test Examples (run this file directly to test)
    logging.basicConfig(level=logging.INFO)

    print("\n--- Testing extract_multiple_contact_details_llm ---")
    test_contact_text_1 = "Hola, soy Juan Pérez y mi teléfono es 11-5555-1234. Vivo en Av. Siempre Viva 742. Mi email es juan.perez@example.com"
    fields_to_get = ["nombre_cliente", "telefono_cliente", "direccion_cliente", "email_cliente", "otro_campo_no_presente"]
    contact_details_1 = extract_multiple_contact_details_llm(test_contact_text_1, fields_to_get)
    print(f"Input: \"{test_contact_text_1}\"\nExtracted: {json.dumps(contact_details_1, indent=2, ensure_ascii=False)}")

    test_contact_text_2 = "Necesito ayuda. Soy Ana Gómez."
    contact_details_2 = extract_multiple_contact_details_llm(test_contact_text_2, ["nombre_cliente", "telefono_cliente"])
    print(f"Input: \"{test_contact_text_2}\"\nExtracted: {json.dumps(contact_details_2, indent=2, ensure_ascii=False)}")

    test_contact_text_3 = "Mi dirección es Falsa 123 y mi teléfono 98765432."
    contact_details_3 = extract_multiple_contact_details_llm(test_contact_text_3, ["direccion_cliente", "telefono_cliente"])
    print(f"Input: \"{test_contact_text_3}\"\nExtracted: {json.dumps(contact_details_3, indent=2, ensure_ascii=False)}")

    print("\n--- Testing extract_complaint_details_llm ---")
    test_complaint_text_1 = "Hay una farola rota en la esquina de Calle Falsa y Avenida Verdadera, no funciona desde ayer. Es un peligro."
    complaint_details_1 = extract_complaint_details_llm(test_complaint_text_1)
    print(f"Input: \"{test_complaint_text_1}\"\nExtracted: {json.dumps(complaint_details_1, indent=2, ensure_ascii=False)}")

    test_complaint_text_2 = "Los vecinos del departamento 3B hacen mucho ruido todas las noches con música alta."
    complaint_details_2 = extract_complaint_details_llm(test_complaint_text_2)
    print(f"Input: \"{test_complaint_text_2}\"\nExtracted: {json.dumps(complaint_details_2, indent=2, ensure_ascii=False)}")

    print("\n--- Testing update_summary_with_llm_extraction ---")
    summary1 = "El cliente reportó un problema con el servicio."
    data1 = {"tipo_problema": "Corte de suministro", "duracion_estimada": "2 horas"}
    updated_summary1 = update_summary_with_llm_extraction(summary1, data1)
    print(f"Original Summary: \"{summary1}\"\nNew Data: {data1}\nUpdated Summary: \"{updated_summary1}\"")

    summary2 = ""
    data2 = {"nombre_cliente": "Pedro Paramo", "estado_pedido": "En preparación"}
    updated_summary2 = update_summary_with_llm_extraction(summary2, data2)
    print(f"Original Summary: \"{summary2}\"\nNew Data: {data2}\nUpdated Summary: \"{updated_summary2}\"")

    summary3 = "Resumen existente:\n- Nombre: Juan"
    data3 = {"telefono_cliente": "12345", "Nombre": "Juan Perez"} # Test case sensitivity and overwrite logic (basic)
    updated_summary3 = update_summary_with_llm_extraction(summary3, data3)
    print(f"Original Summary: \"{summary3}\"\nNew Data: {data3}\nUpdated Summary: \"{updated_summary3}\"")

    # Test with mock robust_chat (if it's the one from this file)
    print("\n--- Testing with MOCK robust_chat (if active) ---")
    # This relies on the mock robust_chat defined at the top if services.cohere_ai is not found
    mock_contact_text = "My name is John Doe, I live at 123 Main St, call 555-1234, email john.doe@example.com"
    mock_contact_details = extract_multiple_contact_details_llm(mock_contact_text, ["nombre_cliente", "direccion_cliente", "telefono_cliente", "email_cliente"])
    print(f"Mock Input: \"{mock_contact_text}\"\nMock Extracted: {json.dumps(mock_contact_details, indent=2)}")

    mock_complaint_text = "There's a broken streetlight on Elm Street near pole 123. It's been out for 3 days."
    mock_complaint_details = extract_complaint_details_llm(mock_complaint_text)
    print(f"Mock Input: \"{mock_complaint_text}\"\nMock Extracted: {json.dumps(mock_complaint_details, indent=2)}")

    mock_summary = "Current summary: '''Initial problem reported.'''"
    mock_new_data = {"ubicacion_problema": "Calle Falsa 123", "urgencia": "Alta"}
    # Need to simulate the prompt structure for the mock robust_chat for summary
    is_mock_chat_active = hasattr(robust_chat, '__module__') and robust_chat.__module__ == __name__
    if is_mock_chat_active:
        print(f"Mock robust_chat is active: {is_mock_chat_active}")
        # The update_summary_with_llm_extraction function itself calls robust_chat,
        # so if the mock is active, it will be used.
        updated_mock_summary = update_summary_with_llm_extraction("Initial problem reported.", mock_new_data)
        print(f"Mock Summary Update:\nOriginal: Initial problem reported.\nData: {mock_new_data}\nUpdated: \"{updated_mock_summary}\"")
    else:
        print("Real robust_chat is active (or mock is not from this file). Summary test might differ.")

    # Test _clean_llm_json_output
    print("\n--- Testing _clean_llm_json_output ---")
    json_with_markdown = "```json\n{\"key\": \"value\", \"another_key\": 123,}\n```"
    cleaned_md = _clean_llm_json_output(json_with_markdown)
    print(f"Original: '{json_with_markdown}'\nCleaned: '{cleaned_md}' -> Parsed: {json.loads(cleaned_md)}")

    json_with_trailing_comma = "{\"name\": \"Test\", \"items\": [1, 2,], \"valid\": true,}"
    cleaned_tc = _clean_llm_json_output(json_with_trailing_comma)
    print(f"Original: '{json_with_trailing_comma}'\nCleaned: '{cleaned_tc}' -> Parsed: {json.loads(cleaned_tc)}")

    json_plain = "{\"key\": \"value\"}"
    cleaned_plain = _clean_llm_json_output(json_plain)
    print(f"Original: '{json_plain}'\nCleaned: '{cleaned_plain}' -> Parsed: {json.loads(cleaned_plain)}")

    empty_json = ""
    cleaned_empty = _clean_llm_json_output(empty_json)
    print(f"Original: '{empty_json}'\nCleaned: '{cleaned_empty}'")

    null_json = None
    # cleaned_null = _clean_llm_json_output(null_json) # This would cause error, handled by function
    # print(f"Original: '{null_json}'\nCleaned: '{cleaned_null}'")


    # Test case for extract_multiple_contact_details_llm where LLM might return non-requested fields
    text_with_extra_info = "My name is Alice Wonderland, my phone is 555-0000, and my favorite color is blue."
    fields_for_alice = ["nombre_cliente", "telefono_cliente"]
    alice_details = extract_multiple_contact_details_llm(text_with_extra_info, fields_for_alice)
    print(f"Input: \"{text_with_extra_info}\"\nFields: {fields_for_alice}\nExtracted: {json.dumps(alice_details, indent=2, ensure_ascii=False)}")
    assert "favorite_color" not in alice_details # Assuming LLM might include it if not instructed well

    # Test case for extract_complaint_details_llm with fewer details
    complaint_less_detail = "El agua no sale en mi casa."
    complaint_details_less = extract_complaint_details_llm(complaint_less_detail)
    print(f"Input: \"{complaint_less_detail}\"\nExtracted: {json.dumps(complaint_details_less, indent=2, ensure_ascii=False)}")

    # Test update_summary_with_llm_extraction with more complex existing summary
    summary_complex = "Cliente: Maria Soler\nTicket: #12345\nProblema: Internet lento."
    data_complex = {"ubicacion_problema": "Oficina central", "velocidad_reportada": "1 Mbps"}
    updated_summary_complex = update_summary_with_llm_extraction(summary_complex, data_complex)
    print(f"Original Summary: \"{summary_complex}\"\nNew Data: {data_complex}\nUpdated Summary: \"{updated_summary_complex}\"")

    # Test contact extraction where user provides only partial data matching potential_fields
    partial_contact_text = "Mi teléfono es 222-3333."
    partial_fields = ["nombre_cliente", "telefono_cliente", "direccion_cliente"]
    partial_details = extract_multiple_contact_details_llm(partial_contact_text, partial_fields)
    print(f"Input: \"{partial_contact_text}\"\nFields: {partial_fields}\nExtracted: {json.dumps(partial_details, indent=2, ensure_ascii=False)}")
    # Expected: {"telefono_cliente": "222-3333"}

    # Test complaint extraction where LLM might return extra fields
    complaint_with_extra = "La basura no se recoge en Calle Luna, esquina Sol. Además, el camión hace mucho ruido."
    # (Assuming LLM might try to add "nivel_ruido": "alto" if not well-instructed)
    complaint_details_extra = extract_complaint_details_llm(complaint_with_extra)
    print(f"Input: \"{complaint_with_extra}\"\nExtracted: {json.dumps(complaint_details_extra, indent=2, ensure_ascii=False)}")
    assert "nivel_ruido" not in complaint_details_extra # Check that only expected keys are present

    # Test summary update where new data is empty
    summary_no_new_data = "Existing info."
    data_empty = {}
    updated_summary_no_new = update_summary_with_llm_extraction(summary_no_new_data, data_empty)
    print(f"Original Summary: \"{summary_no_new_data}\"\nNew Data: {data_empty}\nUpdated Summary: \"{updated_summary_no_new}\"")
    assert updated_summary_no_new == summary_no_new_data

    # Test summary update where current summary is empty
    summary_empty_current = ""
    data_for_empty_summary = {"info_inicial": "Primer dato"}
    updated_summary_empty_curr = update_summary_with_llm_extraction(summary_empty_current, data_for_empty_summary)
    print(f"Original Summary: \"{summary_empty_current}\"\nNew Data: {data_for_empty_summary}\nUpdated Summary: \"{updated_summary_empty_curr}\"")
    # Expected (for basic append): "- Info_inicial: Primer dato" or similar

    # Test _clean_llm_json_output with problematic JSON string
    bad_json_str = "```json\n{\n  \"name\": \"Test Product\",\n  \"price\": 29.99 // This is a comment\n  \"available\": true,\n}\n```"
    # Note: _clean_llm_json_output does not remove comments. JSON.loads will fail.
    # This test is more about markdown and trailing commas.
    # For comments, a more sophisticated cleaning or a more robust JSON parser would be needed.
    # Current _clean_llm_json_output will produce:
    # {"name": "Test Product", "price": 29.99 // This is a comment "available": true}
    # which is invalid JSON.
    # A better LLM prompt should ask for strictly JSON, no comments.

    # Cleaned version for testing just markdown and trailing comma:
    json_for_cleaner_test = "```json\n{\n  \"name\": \"Test Product\",\n  \"price\": 29.99,\n  \"available\": true,\n}\n```"
    cleaned_for_test = _clean_llm_json_output(json_for_cleaner_test)
    print(f"Original for cleaner: '{json_for_cleaner_test}'\nCleaned: '{cleaned_for_test}'")
    try:
        parsed_cleaned = json.loads(cleaned_for_test)
        print(f"Parsed successfully: {parsed_cleaned}")
    except json.JSONDecodeError as e:
        print(f"Failed to parse cleaned JSON: {e}")

    # Test with a more complex trailing comma scenario
    complex_trailing_comma = "{\"a\":1, \"b\":[{\"c\":2,}, {\"d\":3,}], \"e\":{\"f\":4,},}"
    cleaned_complex_tc = _clean_llm_json_output(complex_trailing_comma)
    print(f"Original complex TC: '{complex_trailing_comma}'\nCleaned: '{cleaned_complex_tc}'")
    try:
        parsed_complex_tc = json.loads(cleaned_complex_tc)
        print(f"Parsed successfully: {parsed_complex_tc}")
    except json.JSONDecodeError as e:
        print(f"Failed to parse cleaned complex TC JSON: {e}")


print("Done with llm_utils.py basic execution tests.")

def extraer_lista_pedido_de_texto_con_llm(texto_ocr: str, pyme_id_context: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Utiliza un LLM (Gemini) para extraer una lista de productos y cantidades de un texto OCR.
    Intenta ser robusto a errores comunes de OCR y formatos de lista variados.

    Args:
        texto_ocr: El texto completo extraído por OCR de una imagen de pedido.
        pyme_id_context: Opcional, ID de la PYME para dar más contexto al LLM si es útil.

    Returns:
        Una lista de diccionarios, donde cada diccionario representa un item del pedido.
        Ej: [{"nombre_producto_ocr": "Coca Cola 2L", "cantidad_ocr": 2, "unidad_ocr": "botellas"}, ...]
        Retorna lista vacía si no se pueden extraer items o en caso de error.
    """
    logger_llm_utils = logging.getLogger(__name__)
    if not texto_ocr or not texto_ocr.strip():
        logger_llm_utils.warning("[LLM_PEDIDO_EXTRACT] texto_ocr vacío o solo espacios.")
        return []

    from services.gemini_bridge import llamar_gemini_para_generacion_texto # Local import

    # TODO: Refinar este prompt
    system_prompt_pedido = (
        "Eres un asistente experto en procesar listas de pedidos escritas a mano o en tickets. "
        "Dada la siguiente lista de productos extraída por OCR, identifica cada producto, su cantidad numérica y la unidad de medida si se especifica explícitamente (ej. kg, gr, lts, ml, caja, paquete, docena, etc.). "
        "Si la unidad es implícita o genérica como 'unidades' o 'ítems', puedes omitir el campo 'unidad_ocr'. "
        "Devuelve SOLAMENTE un array JSON de objetos. Cada objeto debe tener:\n"
        "- \"nombre_producto_ocr\": El nombre descriptivo y completo del producto tal como aparece (string).\n"
        "- \"cantidad_ocr\": La cantidad NUMÉRICA asociada al producto (integer o float).\n"
        "- \"unidad_ocr\" (opcional): La unidad de medida específica si se menciona (string).\n"
        "Si un ítem no tiene cantidad clara, intenta inferir 1. Si no puedes determinar un producto o cantidad para una línea, omítela de la lista.\n"
        "Ejemplo de salida: [{\"nombre_producto_ocr\": \"Coca Cola 2L\", \"cantidad_ocr\": 2, \"unidad_ocr\": \"botellas\"}, {\"nombre_producto_ocr\": \"Papas Fritas Grandes\", \"cantidad_ocr\": 1}]"
    )
    user_prompt_pedido = (
        "Por favor, procesa el siguiente texto OCR de un pedido y extrae los items en formato JSON array:\n"
        "Texto OCR:\n"
        "----------\n"
        f"{texto_ocr}\n"
        "----------\n"
        "Array JSON:"
    )

    logger_llm_utils.info(f"[LLM_PEDIDO_EXTRACT] Llamando a Gemini para extraer de: {texto_ocr[:200]}...")
    respuesta_gemini_texto = llamar_gemini_para_generacion_texto(
        system_prompt_especifico=system_prompt_pedido,
        user_prompt=user_prompt_pedido,
        temperature=0.1 # Más determinista para extracción
    )

    if not respuesta_gemini_texto:
        logger_llm_utils.warning(f"[LLM_PEDIDO_EXTRACT] Gemini no devolvió respuesta para el texto OCR.")
        return []

    cleaned_json_str = _clean_llm_json_output(respuesta_gemini_texto)
    try:
        items_extraidos = json.loads(cleaned_json_str)
        if isinstance(items_extraidos, list):
            # Validar estructura de cada item
            items_validos = []
            for item in items_extraidos:
                if isinstance(item, dict) and "nombre_producto_ocr" in item and "cantidad_ocr" in item:
                    try:
                        item["cantidad_ocr"] = int(float(str(item["cantidad_ocr"]).replace(',','.'))) # Asegurar que sea int o float y luego int
                        if item["cantidad_ocr"] < 0 : item["cantidad_ocr"] = 1 # No permitir cantidades negativas
                    except ValueError:
                        item["cantidad_ocr"] = 1 # Default si la cantidad no es numérica
                    items_validos.append(item)
                else:
                    logger_llm_utils.warning(f"[LLM_PEDIDO_EXTRACT] Item de LLM no tiene campos requeridos: {item}")
            logger_llm_utils.info(f"[LLM_PEDIDO_EXTRACT] Items válidos extraídos por LLM: {items_validos}")
            return items_validos
        else:
            logger_llm_utils.error(f"[LLM_PEDIDO_EXTRACT] LLM no devolvió una lista JSON. Respuesta: {cleaned_json_str}")
            return []
    except json.JSONDecodeError as e:
        logger_llm_utils.error(f"[LLM_PEDIDO_EXTRACT] Error decodificando JSON de LLM: {e}. Respuesta: {cleaned_json_str}")
        return []
    except Exception as e_gen:
        logger_llm_utils.error(f"[LLM_PEDIDO_EXTRACT] Error general procesando respuesta de LLM: {e_gen}", exc_info=True)
        return []

def resumir_descripcion_producto_llm(descripcion_larga: str, max_longitud: int = 200, min_longitud: int = 50) -> str:
    """
    Resume una descripción de producto utilizando un LLM.

    Args:
        descripcion_larga: La descripción original del producto.
        max_longitud: La longitud máxima deseada para el resumen (en caracteres).
        min_longitud: La longitud mínima deseada para el resumen (en caracteres).

    Returns:
        La descripción resumida, o la original si ya es corta o falla el resumen.
    """
    if not descripcion_larga or not isinstance(descripcion_larga, str):
        return ""

    len_original = len(descripcion_larga)

    if len_original <= max_longitud: # Si ya es suficientemente corta
        # Podríamos incluso devolverla si es un poco más larga que min_longitud, para no resumir innecesariamente
        if len_original >= min_longitud or len_original <= max_longitud * 0.75: # No resumir si ya está en un rango aceptable
             return descripcion_larga.strip()


    prompt = (
        f"Eres un experto en marketing. Resume la siguiente descripción de producto para que sea concisa, atractiva y no exceda los {max_longitud} caracteres, "
        f"pero intenta que tenga al menos {min_longitud} caracteres si es posible. "
        "Destaca los beneficios clave o características únicas. Evita jerga innecesaria. "
        "El resultado debe ser solo el texto resumido.\n\n"
        f"Descripción Original:\n\"\"\"\n{descripcion_larga}\n\"\"\"\n\n"
        "Resumen Optimizado:"
    )

    resumen = ""
    try:
        # Usar un modelo eficiente para resúmenes.
        # El modelo por defecto en robust_chat es command-r-plus.
        # Si se necesita un modelo específico aquí, robust_chat debería ser adaptado.
        resumen_candidato = robust_chat(message=prompt) # Removed model_override

        if resumen_candidato:
            resumen = resumen_candidato.strip()
            # Validar longitud del resumen y ajustar si es necesario (simple recorte)
            if len(resumen) > max_longitud:
                # Intentar cortar por la última frase completa dentro del límite
                last_period = resumen.rfind('.', 0, max_longitud)
                if last_period != -1:
                    resumen = resumen[:last_period+1]
                else: # Si no hay punto, cortar bruscamente
                    resumen = resumen[:max_longitud].rsplit(' ', 1)[0] + "..." if ' ' in resumen[:max_longitud] else resumen[:max_longitud]

            if len(resumen) < min_longitud and len_original > min_longitud : # Si el resumen es demasiado corto y el original no
                # Podríamos intentar re-prompting con "hazlo un poco más largo" o simplemente usar el original truncado
                logger.warning(f"[LLM_RESUMEN_PROD] Resumen LLM ('{resumen}') más corto ({len(resumen)}) que min_longitud ({min_longitud}). Original era {len_original}.")
                # Fallback a una porción del original si el resumen es insatisfactorio
                return descripcion_larga[:max_longitud].strip()


            logger.info(f"[LLM_RESUMEN_PROD] Descripción original (len {len_original}): '{descripcion_larga[:100]}...' -> Resumen (len {len(resumen)}): '{resumen[:100]}...'")
        else:
            logger.warning(f"[LLM_RESUMEN_PROD] LLM no devolvió resumen para: '{descripcion_larga[:100]}...'. Se usará original truncado si es necesario.")
            return descripcion_larga[:max_longitud].strip()

    except Exception as e:
        logger.error(f"[LLM_RESUMEN_PROD] Error al resumir descripción: {e}. Original: '{descripcion_larga[:100]}...'", exc_info=True)
        # Fallback a la descripción original (o una versión truncada si es muy larga)
        return descripcion_larga[:max_longitud].strip()

    return resumen


# Google Cloud AI Service Placeholders

try:
    from google.cloud import vision
    from google.cloud.documentai_v1 import Document
except ImportError:
    logger.warning("Google Cloud Vision or DocumentAI libraries not found. Related functionalities will not work.")
    # Define dummy classes or objects if needed for the code to not break entirely
    # For example, if other parts of the code expect `documentai.Document` to exist.
    class MockDocumentAI:
        class Document:
            def __init__(self, text="", mime_type=""):
                self.text = text
                self.mime_type = mime_type
                self.entities = []
                self.pages = []
        # Add any other types that might be needed from documentai
    Document = MockDocumentAI()
    vision = None # Or a similar mock if attributes from it are directly used


def analyze_image_with_google_vision_ocr(image_content: bytes) -> str:
    """
    Analyzes an image using Google Cloud Vision API's OCR capabilities.

    Args:
        image_content: Bytes of the image file.

    Returns:
        The extracted text as a string, or an empty string if an error occurs or no text is found.
    """
    if not vision:
        logger.error("Google Cloud Vision library not available. Cannot analyze image.")
        return ""
    logger.info("Placeholder: Analyzing image with Google Vision OCR.")
    # In a real implementation:
    # try:
    #     client = vision.ImageAnnotatorClient()
    #     image = vision.Image(content=image_content)
    #     response = client.text_detection(image=image)
    #     if response.error.message:
    #        logger.error(f"Vision API error: {response.error.message}")
    #        return ""
    #     if response.text_annotations:
    #         return response.text_annotations[0].description
    # except Exception as e:
    #     logger.error(f"Error in analyze_image_with_google_vision_ocr: {e}", exc_info=True)
    # return ""
    return "Placeholder OCR text from image."

def analyze_document_with_google_document_ai(
    project_id: str,
    location: str,
    processor_id: str,
    file_content: bytes,
    mime_type: str
) -> documentai.Document | None: # Return type includes None for error cases
    """
    Processes a document using Google Cloud Document AI.

    Args:
        project_id: Google Cloud project ID.
        location: Location of the Document AI processor.
        processor_id: ID of the Document AI processor.
        file_content: Bytes of the document file.
        mime_type: Mime type of the document (e.g., "application/pdf", "image/jpeg").

    Returns:
        A Document AI Document object, or None if an error occurs.
    """
    if not documentai or not hasattr(documentai, 'DocumentProcessorServiceClient'): # Check if real or mock
        logger.error("Google Cloud DocumentAI library not available or not fully mocked. Cannot analyze document.")
        return None

    logger.info(f"Placeholder: Analyzing document ({mime_type}) with Google Document AI for project {project_id}.")
    # In a real implementation:
    # try:
    #     opts = {"api_endpoint": f"{location}-documentai.googleapis.com"}
    #     client = documentai.DocumentProcessorServiceClient(client_options=opts)
    #     name = client.processor_path(project_id, location, processor_id)
    #     raw_document = documentai.RawDocument(content=file_content, mime_type=mime_type)
    #     request = documentai.ProcessRequest(name=name, raw_document=raw_document)
    #     result = client.process_document(request=request)
    #     return result.document
    # except Exception as e:
    #     logger.error(f"Error in analyze_document_with_google_document_ai: {e}", exc_info=True)
    #     return None

    # Example of returning a mock Document object for placeholder purposes:
    # Ensure the mock object is compatible with what the calling code might expect.

    # Actual Google Document AI client initialization and call
    try:
        # The opts dictionary should be defined using the location variable
        opts = {}
        if location: # Ensure location is not None or empty
            opts["api_endpoint"] = f"{location}-documentai.googleapis.com"

        # Initialize client with or without opts based on whether location was valid
        if opts:
            client = documentai.DocumentProcessorServiceClient(client_options=opts)
        else: # Fallback if location is not set, though this might lead to errors if endpoint isn't default
            logger.warning(f"Document AI location not set, using default endpoint for client. Project: {project_id}")
            client = documentai.DocumentProcessorServiceClient()

        name = client.processor_path(project_id, location, processor_id)

        # Construct the RawDocument
        raw_document = documentai.RawDocument(content=file_content, mime_type=mime_type)

        # Construct the request
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)

        logger.info(f"Processing document with Document AI. Processor: {name}")
        result = client.process_document(request=request)
        logger.info("Document AI processing complete.")
        return result.document

    except ImportError: # Should have been caught by the check at the top of the function
        logger.error("Google Cloud DocumentAI library not available during client instantiation.")
        return None
    except Exception as e:
        logger.error(f"Error in analyze_document_with_google_document_ai: {e}", exc_info=True)
        # Return a mock/empty document with error information if possible, or just None
        error_doc_text = f"Error processing document with Document AI: {str(e)}"
        if isinstance(documentai, type) and hasattr(documentai, 'Document'): # Check if it's the MockDocumentAI class
             # Create a mock document indicating error.
            mock_error_doc = documentai.Document(text=error_doc_text, mime_type=mime_type)
            # You could add custom fields/entities to this mock_error_doc if your calling code checks for them.
            # For example: mock_error_doc.entities = [{'type_': 'error', 'mention_text': str(e)}]
            return mock_error_doc
        # If using the real library and an error occurs, it might raise an exception
        # or return a response with an error field. Here we return None.
        return None
