from typing import Dict, Any, List
from ..base import CatalogProcessor, ProcessResult, CatalogItemData
from services.llm_utils import llamar_llm_para_json_estructurado
from services.common_utils import parse_precio_flexible

class GenericProcessor(CatalogProcessor):
    @property
    def slug(self) -> str:
        return "generic"

    def process(self, file_path: str, mime_type: str, extracted_text: str = None, **kwargs) -> ProcessResult:
        # Use extracted text if available (from base logic), otherwise extracted_text must be provided.
        if not extracted_text:
             return ProcessResult(items=[], confidence=0.0, warnings=["No text provided"], raw_text=None)

        system_prompt = self.profile.get("prompt_template", "Analiza este catálogo genérico.")
        user_prompt = f"""
        Texto extraído del archivo:
        \"\"\"{extracted_text[:15000]}\"\"\"

        Genera un JSON con una lista de items bajo la clave 'items'.
        Cada item debe tener: nombre, precio, moneda, sku, stock, descripcion, marca,
        categoria, unidad, presentacion.
        """

        try:
            response = llamar_llm_para_json_estructurado(system_prompt, user_prompt)
            raw_items = response.get("items", [])

            canonical_items = []
            for raw in raw_items:
                precio, moneda = self._parse_price(raw.get("precio"))
                atributos = {
                    "marca": raw.get("marca"),
                    "categoria": raw.get("categoria"),
                    "unidad": raw.get("unidad"),
                    "presentacion": raw.get("presentacion"),
                }
                item = CatalogItemData(
                    nombre=raw.get("nombre", "Sin nombre"),
                    precio=precio,
                    moneda=moneda or "ARS",
                    sku=raw.get("sku"),
                    stock=self._parse_float(raw.get("stock")),
                    unidad_base=raw.get("unidad"),
                    contenido_paquete=raw.get("presentacion"),
                    categoria=raw.get("categoria"),
                    atributos=atributos,
                    original_text=str(raw)
                )
                canonical_items.append(item)

            return ProcessResult(
                items=canonical_items,
                confidence=0.8 if canonical_items else 0.0, # Placeholder logic
                warnings=[],
                raw_text=extracted_text
            )
        except Exception as e:
            return ProcessResult(items=[], confidence=0.0, warnings=[str(e)], raw_text=extracted_text)

    def _parse_price(self, val):
        if not val:
            return None, None
        _, precio_float, moneda = parse_precio_flexible(str(val))
        return precio_float, moneda

    def _parse_float(self, val):
        try:
            return float(val)
        except:
            return None
