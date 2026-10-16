from typing import Dict, Any, List
from ..base import CatalogProcessor, ProcessResult, CatalogItemData
from services.llm_utils import llamar_llm_para_json_estructurado
from services.common_utils import parse_precio_flexible
from utils.lazy_module import LazyModule

pdfplumber = LazyModule("pdfplumber")

class BodegaProcessor(CatalogProcessor):
    @property
    def slug(self) -> str:
        return "bodega"

    def process(self, file_path: str, mime_type: str, extracted_text: str = None, **kwargs) -> ProcessResult:
        if not extracted_text:
             return ProcessResult(items=[], confidence=0.0, warnings=["No text provided"], raw_text=None)

        if mime_type and "pdf" in mime_type:
            parsed_items = self._extract_items_from_pdf_table(file_path)
            if parsed_items:
                return ProcessResult(
                    items=parsed_items,
                    confidence=0.9,
                    warnings=[],
                    raw_text=extracted_text,
                )

        system_prompt = self.profile.get("prompt_template", "Analiza catálogo de Bodega.")
        user_prompt = f"""
        Texto extraído:
        \"\"\"{extracted_text[:15000]}\"\"\"

        Genera JSON con 'items'. Campos: nombre, marca, varietal, anada, presentacion,
        unidades_por_caja, pallet, precio_caja, precio_botella, sugerido_publico, moneda.
        Usa valores numéricos y respeta separadores de miles/decimales.
        Si hay símbolo $ sin aclarar, asumí ARS. Si aparece USD/U$S, asigná USD.
        """

        try:
            response = llamar_llm_para_json_estructurado(system_prompt, user_prompt)
            raw_items = response.get("items", [])

            canonical_items = []
            for raw in raw_items:
                # Industry specific logic
                presentacion = raw.get("presentacion", "")
                contenido_paquete = presentacion # e.g. "Caja x 6"
                marca = raw.get("marca")
                varietal = raw.get("varietal")
                nombre = raw.get("nombre")
                if not nombre:
                    nombre = " ".join(part for part in [marca, varietal] if part) or "Vino sin nombre"
                unidad_base = "botella"
                presentacion_lower = str(presentacion or "").lower()
                if "bag" in presentacion_lower or "box" in presentacion_lower:
                    unidad_base = "bag_in_box"
                elif "lata" in presentacion_lower:
                    unidad_base = "lata"

                # Parsing prices
                _, precio_unit, moneda_unit = parse_precio_flexible(raw.get("precio_botella"))
                _, precio_caja, moneda_caja = parse_precio_flexible(raw.get("precio_caja"))
                _, precio_publico, moneda_publico = parse_precio_flexible(raw.get("sugerido_publico"))
                moneda = raw.get("moneda")
                if not moneda:
                    moneda = moneda_unit or moneda_caja or moneda_publico
                if not moneda:
                    referencia_precio = precio_unit or precio_caja or precio_publico
                    if referencia_precio is not None and referencia_precio < 100:
                        moneda = "USD"
                    else:
                        moneda = "ARS"

                unidades_por_caja = raw.get("unidades_por_caja") or raw.get("unidades_caja")
                try:
                    unidades_por_caja = int(float(unidades_por_caja)) if unidades_por_caja else None
                except (TypeError, ValueError):
                    unidades_por_caja = None

                pallet = raw.get("pallet")
                try:
                    pallet = int(float(pallet)) if pallet else None
                except (TypeError, ValueError):
                    pallet = None

                # Attributes mapping
                attrs = {
                    "marca": marca,
                    "varietal": varietal,
                    "anada": raw.get("anada"),
                    "precio_caja": precio_caja,
                    "precio_botella": precio_unit,
                    "sugerido_publico": precio_publico,
                    "unidades_por_caja": unidades_por_caja,
                    "pallet": pallet,
                    "presentacion_original": presentacion,
                    "moneda": moneda,
                }

                item = CatalogItemData(
                    nombre=nombre,
                    precio=precio_unit or precio_publico or precio_caja, # Canonical price is usually unit price
                    moneda=moneda,
                    unidad_base=unidad_base, # Default for wineries usually
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

    def _extract_items_from_pdf_table(self, file_path: str) -> list[CatalogItemData]:
        items: list[CatalogItemData] = []
        try:
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages[:2]:
                    tables = page.extract_tables() or []
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        header = [str(cell or "").strip().lower() for cell in table[0]]
                        for row in table[1:]:
                            cells = [str(cell or "").strip() for cell in row]
                            if not any(cells):
                                continue
                            row_map = {header[i]: cells[i] if i < len(cells) else "" for i in range(len(header))}
                            marca = row_map.get("marca") or row_map.get("brand") or ""
                            varietal = row_map.get("varietal") or row_map.get("varietal ") or ""
                            unidades = row_map.get("unidades / caja") or row_map.get("unidades/caja") or ""
                            pallet = row_map.get("pallet") or ""
                            precio_caja_raw = row_map.get("precio caja") or ""
                            precio_botella_raw = row_map.get("precio botella") or ""
                            sugerido_raw = row_map.get("sugerido publico") or row_map.get("sugerido público") or ""

                            _, precio_caja, _ = parse_precio_flexible(precio_caja_raw)
                            _, precio_botella, _ = parse_precio_flexible(precio_botella_raw)
                            _, sugerido_publico, _ = parse_precio_flexible(sugerido_raw)

                            try:
                                unidades_val = int(float(unidades)) if unidades else None
                            except (TypeError, ValueError):
                                unidades_val = None
                            try:
                                pallet_val = int(float(pallet)) if pallet else None
                            except (TypeError, ValueError):
                                pallet_val = None

                            nombre = " ".join(part for part in [marca, varietal] if part) or "Vino sin nombre"
                            attrs = {
                                "marca": marca or None,
                                "varietal": varietal or None,
                                "unidades_por_caja": unidades_val,
                                "pallet": pallet_val,
                                "precio_caja": precio_caja,
                                "precio_botella": precio_botella,
                                "sugerido_publico": sugerido_publico,
                                "presentacion_original": f"Caja x{unidades_val}" if unidades_val else None,
                            }
                            items.append(
                                CatalogItemData(
                                    nombre=nombre,
                                    precio=precio_botella or sugerido_publico or precio_caja,
                                    moneda="ARS",
                                    unidad_base="botella",
                                    contenido_paquete=attrs.get("presentacion_original"),
                                    categoria="Vinos",
                                    atributos=attrs,
                                    original_text=str(row_map),
                                )
                            )
        except Exception:
            return []

        return items
