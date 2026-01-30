from typing import Dict, Any, List
from ..base import CatalogProcessor, ProcessResult, CatalogItemData
from services.llm_utils import llamar_llm_para_json_estructurado
from services.common_utils import parse_precio_flexible

class BodegaProcessor(CatalogProcessor):
    @property
    def slug(self) -> str:
        return "bodega"

    def process(self, file_path: str, mime_type: str, extracted_text: str = None, **kwargs) -> ProcessResult:
        if not extracted_text:
             return ProcessResult(items=[], confidence=0.0, warnings=["No text provided"], raw_text=None)

        system_prompt = self.profile.get("prompt_template", "Analiza catálogo de Bodega.")
        user_prompt = f"""
        Texto extraído:
        \"\"\"{extracted_text[:15000]}\"\"\"

        Genera JSON con 'items'. Campos: nombre, precio_unitario, precio_caja, varietal, anada, presentacion.
        """

        try:
            response = llamar_llm_para_json_estructurado(system_prompt, user_prompt)
            raw_items = response.get("items", [])

            canonical_items = []
            for raw in raw_items:
                # Industry specific logic
                presentacion = raw.get("presentacion", "")
                contenido_paquete = presentacion # e.g. "Caja x 6"

                # Parsing prices
                _, precio_unit, _ = parse_precio_flexible(raw.get("precio_unitario"))
                _, precio_caja, _ = parse_precio_flexible(raw.get("precio_caja"))

                # Attributes mapping
                attrs = {
                    "varietal": raw.get("varietal"),
                    "anada": raw.get("anada"),
                    "precio_caja": precio_caja,
                    "presentacion_original": presentacion
                }

                item = CatalogItemData(
                    nombre=raw.get("nombre", "Vino sin nombre"),
                    precio=precio_unit, # Canonical price is usually unit price
                    unidad_base="botella", # Default for wineries usually
                    contenido_paquete=contenido_paquete,
                    categoria="Vinos",
                    atributos=attrs,
                    original_text=str(raw)
                )
                canonical_items.append(item)

            return ProcessResult(
                items=canonical_items,
                confidence=0.9 if canonical_items else 0.0,
                warnings=[],
                raw_text=extracted_text
            )
        except Exception as e:
            return ProcessResult(items=[], confidence=0.0, warnings=[str(e)], raw_text=extracted_text)
