from typing import List, Dict, Any
from .base import BaseCatalogProcessor

class BodegaCatalogProcessor(BaseCatalogProcessor):
    """
    Processor tailored for Wineries (Bodegas).
    Handles cases, pallets, suggested prices, and specific wine terminology.
    """

    def get_extraction_prompt(self, filename: str) -> tuple[str, str]:
        system_prompt = (
            "Eres un asistente experto en interpretar listas de precios y catálogos de BODEGAS de vino. "
            "Debes producir JSON estricto."
        )
        user_prompt = (
            "Analiza el siguiente texto (extraído de un documento de una bodega) y genera un JSON con esta estructura exacta:\n"
            "{\n"
            "  \"resumen\": string,\n"
            "  \"items\": [\n"
            "    {\n"
            "      \"nombre\": string,\n"
            "      \"descripcion\": string,\n"
            "      \"unidad\": string,\n"
            "      \"cantidad\": string,\n"
            "      \"precio_unitario\": string,\n"
            "      \"moneda\": string,\n"
            "      \"subtotal_estimado\": string,\n"
            "      \"unidades_por_caja\": string,\n"
            "      \"unidades_por_pallet\": string,\n"
            "      \"precio_sugerido\": string,\n"
            "      \"precio_caja\": string\n"
            "    }\n"
            "  ],\n"
            "  \"totales\": {\"moneda\": string, \"total_estimado\": string},\n"
            "  \"contacto\": {\"nombre\": string, \"telefono\": string, \"email\": string}\n"
            "}\n"
            "Usa cadenas vacías si un dato no aparece. No inventes información.\n"
            "Si detectas columnas como 'PALLET' o 'Sugerido Público', asignalas a 'unidades_por_pallet' y 'precio_sugerido' respectivamente.\n"
            "Si detectas 'Unidades / Caja' o 'Botellas x Caja', asigna a 'unidades_por_caja'.\n"
            "Si detectas 'Precio Caja', asignalo a 'precio_caja'.\n"
            f"Nombre del archivo (si disponible): {filename or 'desconocido'}.\n"
            "Devuelve solamente el JSON final."
        )
        return system_prompt, user_prompt

    def normalizar_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            nombre = str(item.get("nombre") or item.get("producto") or "").strip()
            if not nombre:
                continue

            # Winery specific parsing
            unidades_caja = str(item.get("unidades_por_caja") or "").strip()
            if not unidades_caja:
                # Try to infer from description if not explicit
                inferred = self.infer_unidad_por_caja(str(item.get("descripcion") or ""))
                if inferred:
                    unidades_caja = str(inferred)

            normalized.append(
                {
                    "nombre": nombre,
                    "descripcion": str(item.get("descripcion") or "").strip(),
                    "unidad": str(item.get("unidad") or "").strip(),
                    "stock": str(item.get("cantidad") or item.get("stock") or "").strip(),
                    "precio_str": str(item.get("precio_unitario") or item.get("precio") or "").strip(),
                    "moneda": str(item.get("moneda") or "").strip(),
                    "categoria": str(item.get("categoria") or self.rubro_nombre).strip(),
                    # Specific fields for Bodegas
                    "unidades_por_caja": unidades_caja,
                    "unidades_por_pallet": str(item.get("unidades_por_pallet") or "").strip(),
                    "precio_sugerido": str(item.get("precio_sugerido") or "").strip(),
                    "precio_caja": str(item.get("precio_caja") or "").strip(),
                }
            )
        return normalized
