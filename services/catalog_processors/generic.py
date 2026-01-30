from typing import List, Dict, Any
from .base import BaseCatalogProcessor

class GenericCatalogProcessor(BaseCatalogProcessor):
    """
    Default processor for any industry that doesn't have a specific implementation.
    """

    def get_extraction_prompt(self, filename: str) -> tuple[str, str]:
        system_prompt = (
            "Eres un asistente experto en interpretar documentos comerciales de pymes. "
            "Debes producir JSON estricto que describa pedidos o catálogos."
        )
        user_prompt = (
            "Analiza el siguiente texto (extraído de un documento) y genera un JSON con esta estructura exacta:\n"
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
            "      \"subtotal_estimado\": string\n"
            "    }\n"
            "  ],\n"
            "  \"totales\": {\"moneda\": string, \"total_estimado\": string},\n"
            "  \"contacto\": {\"nombre\": string, \"telefono\": string, \"email\": string}\n"
            "}\n"
            "Usa cadenas vacías si un dato no aparece. No inventes información.\n"
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
            normalized.append(
                {
                    "nombre": nombre,
                    "descripcion": str(item.get("descripcion") or "").strip(),
                    "unidad": str(item.get("unidad") or "").strip(),
                    "stock": str(item.get("cantidad") or item.get("stock") or "").strip(),
                    "precio_str": str(item.get("precio_unitario") or item.get("precio") or "").strip(),
                    "moneda": str(item.get("moneda") or "").strip(),
                    "categoria": str(item.get("categoria") or self.rubro_nombre).strip(),
                    # Generic fields only
                    "unidades_por_caja": "",
                    "unidades_por_pallet": "",
                    "precio_sugerido": "",
                    "precio_caja": "",
                }
            )
        return normalized
