import json
import logging
import unicodedata
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from .herramientas_municipio import TOOL_REGISTRY
from .poi_service import nearby as poi_nearby
from .estacionamiento_utils import _dist_m
from .estacionamiento_service import consultar_ocupacion
from .conversation_state import ConversationState

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

logger = logging.getLogger(__name__)

PARKING_FILE = Path(__file__).resolve().parents[1] / "data" / "estacionamiento" / "parking_spots.json"

class PointsOfInterestHandler:
    """Handle generic points-of-interest queries using the LLM tool system.

    For parking queries (containing the word 'estacionamiento'), a lightweight
    local dataset is used to emulate available spots in Junín, Mendoza. This
    avoids external API calls during development and provides a deterministic
    experience for users.
    """

    def __init__(self, context: dict):
        self.context = context
        try:
            if PARKING_FILE.exists():
                with open(PARKING_FILE, "r", encoding="utf-8") as fh:
                    self.parking_data = json.load(fh)
            else:
                self.parking_data = []
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Error loading parking dataset: %s", exc, exc_info=True)
            self.parking_data = []

    # ------------------------------------------------------------------
    # Parking helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_text(value: str | None) -> str:
        """Return a lowercase, accent-free string for fuzzy comparisons."""

        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", value)
        without_accents = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        return without_accents.lower().strip()

    @classmethod
    def _extract_street_and_number(cls, address: str | None) -> tuple[str | None, int | None]:
        """Extract street name and house number from an address string."""

        if not address:
            return None, None
        first_segment = str(address).split(",")[0].strip()
        if not first_segment:
            return None, None

        tokens = first_segment.split()
        number = None
        if tokens:
            last_token = tokens[-1]
            if last_token.isdigit():
                try:
                    number = int(last_token)
                    tokens = tokens[:-1]
                except ValueError:  # pragma: no cover - defensive
                    number = None
            elif last_token.replace("°", "").isdigit():
                try:
                    number = int(last_token.replace("°", ""))
                    tokens = tokens[:-1]
                except ValueError:  # pragma: no cover - defensive
                    number = None
        street = " ".join(tokens).strip() or first_segment
        return street, number

    def _spots_matching_address(self, address: str, limit: int = 5) -> list[dict]:
        """Return parking spots that closely match the provided address."""

        street, number = self._extract_street_and_number(address)
        if not street:
            return []

        normalized_street = self._normalize_text(street)
        if not normalized_street:
            return []

        ranked: list[tuple[tuple[float, float, float, float], dict]] = []
        for item in self.parking_data:
            spot_street, spot_number = self._extract_street_and_number(item.get("address"))
            if not spot_street:
                continue
            normalized_spot = self._normalize_text(spot_street)
            if not normalized_spot:
                continue

            match_priority = None
            if normalized_spot == normalized_street:
                match_priority = 0
            elif normalized_street in normalized_spot or normalized_spot in normalized_street:
                match_priority = 1
            elif normalized_street and normalized_street in self._normalize_text(item.get("address")):
                match_priority = 2

            if match_priority is None:
                continue

            diff = float("inf")
            if number is not None and spot_number is not None:
                diff = abs(number - spot_number)

            available = item.get("available_spots") or 0
            identifier = (
                float(item.get("id"))
                if isinstance(item.get("id"), (int, float))
                else float("inf")
            )
            ranked.append(((match_priority, diff, -available, identifier), item))

        ranked.sort(key=lambda element: element[0])
        unique = []
        seen_ids = set()
        for _, spot in ranked:
            key = (
                spot.get("id"),
                spot.get("lat"),
                spot.get("lon"),
            )
            if key in seen_ids:
                continue
            seen_ids.add(key)
            unique.append(spot)
            if len(unique) >= limit:
                break
        return unique

    @staticmethod
    def _merge_spot_lists(primary: list[dict], secondary: list[dict], limit: int) -> list[dict]:
        """Combine two lists of parking spots without duplicates."""

        combined: list[dict] = []
        seen = set()
        for sequence in (primary, secondary):
            for spot in sequence:
                key = (
                    spot.get("id"),
                    spot.get("lat"),
                    spot.get("lon"),
                )
                if key in seen:
                    continue
                seen.add(key)
                combined.append(spot)
                if len(combined) >= limit:
                    return combined
        return combined

    @staticmethod
    def _normalize_reference_value(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = value.replace("_", " ")
        cleaned = " ".join(cleaned.split())
        cleaned = cleaned.strip(",.;:- ")
        lowered = cleaned.lower()
        banned = {
            "buscar",
            "buscar estacionamiento",
            "buscar libre",
            "compartir ubicacion",
            "compartir ubicación",
            "ubicacion",
            "ubicación",
            "tu ubicacion",
            "libre",
        }
        if not cleaned or lowered in banned:
            return None
        return cleaned

    def _parking_response(self, location: dict | str, info: dict | None = None) -> dict:
        """Generate a parking response based on coordinates or an address."""
        if not self.parking_data:
            return self._build_simple_response(
                "No tengo datos de estacionamiento disponibles en este momento."
            )

        address = ""
        lat = lon = None
        if isinstance(location, dict):
            # Use explicit None checks to avoid discarding valid zero values
            lat = location.get("lat")
            if lat is None:
                lat = location.get("latitude")
            lon = location.get("lon")
            if lon is None:
                lon = location.get("lng")
            if lon is None:
                lon = location.get("longitude")
            address = location.get("address") or location.get("formatted_address") or ""
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                lat = lon = None
        else:
            address = str(location)

        consulta_payload = None
        if info is None:
            if lat is not None and lon is not None:
                consulta_payload = {"lat": lat, "lon": lon}
            else:
                consulta_payload = address or location
            try:
                if consulta_payload is not None:
                    info = consultar_ocupacion(consulta_payload)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Error consultando ocupación: %s", exc, exc_info=True)
                info = {}
        elif not isinstance(info, dict):
            info = {}

        lat = lat if lat is not None else info.get("resolved_lat")
        lon = lon if lon is not None else info.get("resolved_lon")
        if not address:
            address = (
                info.get("matched_reference")
                or info.get("reference_location")
                or (location.get("address") if isinstance(location, dict) else None)
                or ""
            )

        if info.get("geocode_source") == "invalid_query" and not info.get("segmentos"):
            from .municipio_responder import _message_with_menu  # local import
            payload = _message_with_menu(
                info.get("texto") or "Necesito una dirección para ayudarte.",
                self.context,
                include_greeting=False,
            )
            payload.update({
                "fuente": "points_of_interest_handler",
                "geocode_source": info.get("geocode_source"),
            })
            return payload

        if lat is None or lon is None:
            message = info.get("texto") or "No pude ubicar esa dirección. Probá con calle y altura (ej.: San Martín 1200)."
            from .municipio_responder import _message_with_menu
            payload = _message_with_menu(
                message,
                self.context,
                include_greeting=False,
            )
            payload.update({
                "fuente": "points_of_interest_handler",
                "geocode_source": info.get("geocode_source"),
            })
            return payload

        spots: list[dict] = []
        if not isinstance(info, dict):
            info = {}

        libres = info.get("libres") if isinstance(info, dict) else None
        cam_name = info.get("camera") if isinstance(info, dict) else None
        timestamp = info.get("timestamp") if isinstance(info, dict) else None

        matching_spots = self._spots_matching_address(address) if address else []

        if lat is not None and lon is not None:
            def dist(item: dict) -> float:
                return _dist_m(lat, lon, item["lat"], item["lon"])

            sorted_spots = sorted(self.parking_data, key=dist)
            prioritized = sorted(matching_spots, key=dist) if matching_spots else []
            nearest = self._merge_spot_lists(prioritized, sorted_spots, 3)
        else:
            if matching_spots:
                nearest = matching_spots[:3]
            else:
                nearest = sorted(
                    self.parking_data,
                    key=lambda item: (-(item.get("available_spots") or 0), item.get("id") or 0),
                )[:3]

        if not nearest:
            nearest = self.parking_data[:3]

        referencia = (
            self._normalize_reference_value(info.get("matched_reference") if isinstance(info, dict) else None)
            or self._normalize_reference_value(info.get("reference_location") if isinstance(info, dict) else None)
            or self._normalize_reference_value(address)
        )
        if referencia is None and isinstance(location, dict):
            referencia = self._normalize_reference_value(location.get("address") or location.get("formatted_address"))
        if referencia is None and isinstance(location, str):
            referencia = self._normalize_reference_value(location)
        if referencia is None:
            referencia = "tu ubicación"

        encabezado = info.get("texto") if isinstance(info, dict) else None
        if encabezado:
            lines = [encabezado.strip(), ""]
        else:
            lines = []

        lines.append(
            f"Datos de estacionamiento cerca de {referencia}:"
            + (f" (Fuente: {cam_name} {timestamp})" if cam_name and timestamp else "")
        )

        remaining_libres = libres if isinstance(libres, int) and libres >= 0 else None

        for item in nearest:
            distance_val = None
            distance_text = ""
            if lat is not None and lon is not None:
                try:
                    distance_val = int(_dist_m(lat, lon, item["lat"], item["lon"]))
                    if distance_val >= 1000:
                        distance_text = f" ({distance_val / 1000:.1f} km)"
                    elif distance_val > 0:
                        distance_text = f" ({distance_val} m)"
                except Exception:  # pragma: no cover
                    distance_val = None

            dataset_available = int(item.get("available_spots") or 0)
            available_now = dataset_available
            if remaining_libres is not None:
                if dataset_available > 0:
                    available_now = min(dataset_available, remaining_libres)
                else:
                    available_now = 0
                remaining_libres = max((remaining_libres or 0) - available_now, 0)

            if available_now > 0:
                if available_now == 1:
                    status = "libre (1 lugar disponible)"
                else:
                    status = f"libre ({available_now} lugares disponibles)"
            else:
                status = "ocupado"

            lines.append(f"- {item['address']}{distance_text}: {status}")
            spots.append({
                "address": item["address"],
                "lat": item["lat"],
                "lon": item["lon"],
                "distance_m": distance_val,
                "available_spots": available_now,
                "capacity": dataset_available,
            })

        message_text = "\n".join(lines)
        # Parking is a terminal lookup, so restore the accessible main menu
        # instead of leaving the user in an implicit POI state with no actions.
        from .municipio_responder import _message_with_menu

        base_payload = _message_with_menu(message_text, self.context)
        base_payload.update({
            "fuente": "points_of_interest_handler",
            "camera": cam_name,
            "timestamp": timestamp,
            "spots": spots,
            "geocode_source": info.get("geocode_source") if isinstance(info, dict) else None,
            "geocode_confidence": info.get("geocode_confidence") if isinstance(info, dict) else None,
            "matched_reference": info.get("matched_reference") if isinstance(info, dict) else None,
        })
        base_payload.update({
            "resolved_lat": lat,
            "resolved_lon": lon,
        })
        return base_payload

    @staticmethod
    def _requires_refinement(message: str | None) -> bool:
        if not message:
            return True
        lowered = message.lower()
        return any(
            phrase in lowered
            for phrase in (
                "no tengo información",
                "no tengo info",
                "no cuento con información",
                "no pude encontrar",
                "no pude encontrar información",
                "podrias especificar",
                "podrías especificar",
                "qué tipo de lugar",
                "que tipo de lugar",
                "qué tipo de lugares",
                "que tipo de lugares",
                "que tipo de lugares cercanos",
                "qué tipo de lugares cercanos",
            )
        )

    @staticmethod
    def _poi_refinement_options() -> list[dict[str, str]]:
        return [
            {"texto": "🏥 Hospitales o clínicas", "action_id": "hospitales"},
            {"texto": "🩺 Farmacias (incluye 24hs)", "action_id": "farmacias 24 horas"},
            {"texto": "🍽️ Restaurantes", "action_id": "restaurantes"},
            {"texto": "🚓 Comisarías", "action_id": "comisarias"},
            {"texto": "🚒 Bomberos", "action_id": "bomberos"},
            {"texto": "🏦 Cajeros/ATM", "action_id": "cajeros automáticos"},
            {"texto": "🏞️ Parques o plazas", "action_id": "parques"},
            {"texto": "🛒 Supermercados", "action_id": "supermercados"},
            {"texto": "🅿️ Estacionamiento (demo)", "action_id": "estacionamiento"},
            {"texto": "Otro tipo de lugar", "action_id": "otro lugar"},
        ]

    @staticmethod
    def _poi_keyword_from_query(query: str) -> tuple[str, bool] | tuple[None, bool]:
        normalized = PointsOfInterestHandler._normalize_text(query)
        keyword_map = {
            "hospitales": "hospital",
            "clinicas": "hospital",
            "clínicas": "hospital",
            "farmacias": "farmacia",
            "farmacias 24 horas": "farmacia",
            "farmacias 24 hs": "farmacia",
            "farmacias 24hs": "farmacia",
            "restaurantes": "restaurant",
            "comisarias": "police",
            "comisarías": "police",
            "bomberos": "fire station",
            "cajeros automaticos": "atm",
            "cajeros automáticos": "atm",
            "parques": "park",
            "plazas": "park",
            "supermercados": "supermarket",
        }
        open_now = any(token in normalized for token in ("24", "24hs", "24 horas", "de turno", "guardia"))
        for key, keyword in keyword_map.items():
            if key in normalized:
                return keyword, open_now
        return None, open_now

    @staticmethod
    def _format_poi_results(results: list[dict], label: str) -> str:
        if not results:
            return f"No pude encontrar {label} cerca de esa ubicación."
        lines = [f"{label.capitalize()} cercanos:"]
        for idx, item in enumerate(results[:3], 1):
            name = item.get("name") or "Lugar"
            address = item.get("vicinity") or item.get("formatted_address") or "Dirección no disponible"
            lines.append(f"{idx}. {name} — {address}")
        return "\n".join(lines)

    def _build_refinement_prompt(self, location: dict | None) -> dict:
        address = ""
        if isinstance(location, dict):
            address = location.get("address") or location.get("label") or ""
        address_text = f"cerca de *{address}*" if address else "cerca de tu ubicación"
        message_body = (
            f"Perfecto, puedo buscar lugares {address_text}. "
            "¿Qué tipo de lugar necesitás?"
        )
        return {
            "message_body": message_body,
            "options_list": self._poi_refinement_options(),
            "message_type": "interactive_buttons",
            "fuente": "points_of_interest_refinement",
            "generar_audio": True,
        }

    def _handle_direct_poi_lookup(self, rubro: str, location: dict | None) -> dict | None:
        """Attempt a direct POI tool lookup when the user already chose a category."""
        if not rubro or not location:
            return None

        herramienta = TOOL_REGISTRY.get("buscar_poi") or TOOL_REGISTRY.get("buscar_puntos_de_interes")
        if not herramienta:
            return None

        localidad = location.get("address") or location.get("label")
        if not localidad:
            return None

        try:
            resultado = herramienta["funcion"](rubro=rubro, localidad=localidad)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Error executing POI tool: %s", exc, exc_info=True)
            return self._build_simple_response(
                "Ocurrió un error al buscar lugares cercanos."
            )

        return self._build_simple_response(str(resultado))

    @staticmethod
    def _build_simple_response(message: str) -> dict:
        return {
            "message_body": message,
            "message_type": "text",
            "fuente": "points_of_interest_handler",
            "generar_audio": True,
        }
    def handle(self, payload: dict) -> dict | None:
        original_question = payload.get("pregunta") or ""
        pregunta = original_question.lower()
        location = payload.get("location")

        if not location:
            municipio_ctx = (
                self.context.get("chat_db_context_data", {})
                .get(CONTEXTO_MUNICIPIO, {})
            )
            location = (
                municipio_ctx.get("ubicacion_contextual")
                or self.context.get("ubicacion_usuario")
            )

        if isinstance(location, dict):
            normalized_location = {
                "address": location.get("address") or location.get("label"),
                "lat": location.get("lat") or location.get("latitude"),
                "lon": location.get("lon") or location.get("longitude"),
            }
            if location.get("formatted_address") and not normalized_location.get("address"):
                normalized_location["address"] = location.get("formatted_address")
            location = {k: v for k, v in normalized_location.items() if v}

        normalized_question = self._normalize_text(pregunta)
        if normalized_question in {"lugares cercanos", "lugares cerca", "lugares", "cerca"}:
            return self._build_refinement_prompt(location if isinstance(location, dict) else None)

        refinement_actions = {
            self._normalize_text(option.get("action_id"))
            for option in self._poi_refinement_options()
            if option.get("action_id")
        }
        if normalized_question in refinement_actions and "estacionamiento" not in normalized_question:
            if isinstance(location, dict) and location.get("lat") is not None and location.get("lon") is not None:
                keyword, open_now = self._poi_keyword_from_query(original_question)
                if keyword:
                    results = poi_nearby(
                        location.get("lat"),
                        location.get("lon"),
                        keyword=keyword,
                        radius=1500,
                        open_now=open_now,
                    )
                    if results is None:
                        return self._build_simple_response(
                            "No pude consultar lugares en tiempo real. Probá más tarde o consultá la web del municipio."
                        )
                    if open_now:
                        results = [
                            item
                            for item in (results or [])
                            if item.get("opening_hours", {}).get("open_now") is True
                        ]
                    label = original_question
                    message = self._format_poi_results(results or [], label)
                    return self._build_simple_response(message)

            direct_lookup = self._handle_direct_poi_lookup(original_question, location if isinstance(location, dict) else None)
            if direct_lookup:
                return direct_lookup

        keywords = ("estacionamiento", "estacionar", "lugar libre")
        if any(word in pregunta for word in keywords):
            municipio_ctx = (
                self.context.get("chat_db_context_data", {})
                .setdefault(CONTEXTO_MUNICIPIO, {})
            )
            chat_db_context = self.context.get("chat_db_context")

            if not location:
                # Try to geocode an address present in the question text
                address_candidate = pregunta
                for word in keywords:
                    address_candidate = address_candidate.replace(word, "")
                address_candidate = address_candidate.replace("_", " ")
                address_candidate = " ".join(address_candidate.split())
                address_candidate = address_candidate.strip(",.;:- ")
                coords = None
                info = None
                cleaned_candidate = self._normalize_reference_value(address_candidate)
                if cleaned_candidate and len(cleaned_candidate) > 3:
                    try:
                        info = consultar_ocupacion(cleaned_candidate)
                    except Exception:  # pragma: no cover - defensive
                        info = None
                    if isinstance(info, dict) and info.get("resolved_lat") is not None and info.get("resolved_lon") is not None:
                        coords = {
                            "lat": info.get("resolved_lat"),
                            "lon": info.get("resolved_lon"),
                            "address": info.get("matched_reference") or cleaned_candidate,
                        }
                    elif isinstance(info, dict) and info.get("texto") and not info.get("segmentos"):
                        # Forward the message returned by the occupancy service
                        municipio_ctx["estado_conversacion"] = ConversationState.ESPERANDO_UBICACION_GENERAL.name
                        municipio_ctx["consulta_pendiente_ubicacion"] = original_question
                        municipio_ctx["ultima_consulta_poi"] = original_question
                        if chat_db_context:
                            flag_modified(chat_db_context, "context_data")
                        return {
                            "message_body": info.get("texto"),
                            "options_list": [
                                {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                                {"texto": "Cancelar", "action": "cancelar"},
                            ],
                            "message_type": "interactive_buttons",
                            "fuente": "pedir_ubicacion_estacionamiento",
                        }
                if coords:
                    location = {
                        "address": coords.get("address") or cleaned_candidate,
                        "lat": coords.get("lat"),
                        "lon": coords.get("lon"),
                    }
                    municipio_ctx["ultima_consulta_poi"] = original_question
                    if chat_db_context:
                        flag_modified(chat_db_context, "context_data")
                    return self._parking_response(location, info=info if isinstance(info, dict) else None)

                # Geocoding failed; remember query and ask for location
                municipio_ctx["estado_conversacion"] = (
                    ConversationState.ESPERANDO_UBICACION_GENERAL.name
                )
                municipio_ctx["consulta_pendiente_ubicacion"] = original_question
                municipio_ctx["ultima_consulta_poi"] = original_question
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return {
                    "message_body": (
                        "Para encontrar estacionamiento libre, por favor compartí tu "
                        "ubicación o escribí una dirección."
                    ),
                    "options_list": [
                        {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                        {"texto": "Cancelar", "action": "cancelar"},
                    ],
                    "message_type": "interactive_buttons",
                    "fuente": "pedir_ubicacion_estacionamiento",
                }

            municipio_ctx["ultima_consulta_poi"] = original_question
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return self._parking_response(location)

        # --- Generic POI flow delegated to LLM + tools ---
        try:
            # The compatibility alias still routes through the provider
            # orchestrator and keeps older integrations patchable.
            from .municipio_responder import llamar_gemini as llamar_llm_con_fallback
            mensaje_usuario = json.dumps({"pregunta": pregunta, "ubicacion": location})
            llm_result = llamar_llm_con_fallback(
                None, mensaje_usuario, {"tipo_entidad": "municipio"}, [], None
            )
            if isinstance(llm_result, tuple):
                respuesta_llm = llm_result[0]
            else:  # pragma: no cover - unexpected but defensive
                respuesta_llm = llm_result
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Error invoking LLM for POI query: %s", exc, exc_info=True)

        accion = respuesta_llm.get("accion_backend")
        message_body = respuesta_llm.get("message_body", "")
        botones = respuesta_llm.get("botones", [])

        if accion == "responder_directamente" and self._requires_refinement(message_body):
            return self._build_refinement_prompt(location if isinstance(location, dict) else None)

        if accion == "ejecutar_herramienta":
            datos = respuesta_llm.get("datos_estructura", {})
            nombre = datos.get("nombre_herramienta")
            params = datos.get("parametros_herramienta", {})
            herramienta = TOOL_REGISTRY.get(nombre)
            if not herramienta:
                return self._build_simple_response(
                    message_body or "No tengo una herramienta para eso."
                )
            try:
                if (
                    nombre in {"buscar_poi", "buscar_puntos_de_interes"}
                    and isinstance(location, dict)
                    and location.get("lat") is not None
                    and location.get("lon") is not None
                ):
                    keyword, open_now = self._poi_keyword_from_query(params.get("rubro") or params.get("tipo_lugar") or "")
                    if keyword:
                        results = poi_nearby(
                            location.get("lat"),
                            location.get("lon"),
                            keyword=keyword,
                            radius=1500,
                            open_now=open_now,
                        )
                        if results is None:
                            return self._build_simple_response(
                                "No pude consultar lugares en tiempo real. Probá más tarde o consultá la web del municipio."
                            )
                        if open_now:
                            results = [
                                item
                                for item in (results or [])
                                if item.get("opening_hours", {}).get("open_now") is True
                            ]
                        label = params.get("rubro") or params.get("tipo_lugar") or "lugares"
                        result_text = self._format_poi_results(results or [], label)
                        return self._build_simple_response(result_text)

                resultado = herramienta["funcion"](**params)
                if isinstance(resultado, dict):
                    result_text = resultado.get("texto") or resultado.get("message_body") or str(resultado)
                else:
                    result_text = str(resultado)
                final_message = f"{message_body}\n{result_text}".strip() or result_text
                return self._build_simple_response(final_message)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Error executing tool %s: %s", nombre, exc, exc_info=True)
                return self._build_simple_response(
                    "Ocurrió un error al obtener la información solicitada."
                )

        return self._build_simple_response(message_body)
