from typing import Dict, Any, List
from ..base import CatalogProcessor, ProcessResult, CatalogItemData
from services.llm_utils import llamar_llm_para_json_estructurado
from services.common_utils import parse_precio_flexible

class CorralonProcessor(CatalogProcessor):
    @property
    def slug(self) -> str:
        return "corralon"

    def process(self, file_path: str, mime_type: str, extracted_text: str = None, **kwargs) -> ProcessResult:
        if not extracted_text:
             return ProcessResult(items=[], confidence=0.0, warnings=["No text provided"], raw_text=None)

        system_prompt = self.profile.get("prompt_template", "Analiza catálogo de Corralón.")
        user_prompt = f"""
        Texto extraído:
        \"\"\"{extracted_text[:15000]}\"\"\"

        Genera JSON con 'items'. Campos: nombre, precio, unidad, marca.
        """

        try:
            response = llamar_llm_para_json_estructurado(system_prompt, user_prompt)
            raw_items = response.get("items", [])

            canonical_items = []
            for raw in raw_items:
                _, precio, _ = parse_precio_flexible(raw.get("precio"))

                attrs = {
                    "marca": raw.get("marca"),
                    "dimensiones": raw.get("dimensiones")
                }

                item = CatalogItemData(
                    nombre=raw.get("nombre", "Material sin nombre"),
                    precio=precio,
                    unidad_base=raw.get("unidad"),
                    categoria="Materiales",
                    atributos=attrs,
                    original_text=str(raw)
                )
                canonical_items.append(item)

            return ProcessResult(
                items=canonical_items,
                confidence=0.85 if canonical_items else 0.0,
                warnings=[],
                raw_text=extracted_text
            )
        except Exception as e:
            return ProcessResult(items=[], confidence=0.0, warnings=[str(e)], raw_text=extracted_text)
