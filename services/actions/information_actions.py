import logging
from .base_action_handler import BaseActionHandler
from typing import Dict, Any

logger = logging.getLogger(__name__)

import json
import os

class ConsultarPuntosDeInteresActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarPuntosDeInteresActionHandler with data: {action_data}")

        # Cargar el mapeo de palabras clave a categorías
        try:
            with open("data/municipios/default/puntos_de_interes.json", "r", encoding="utf-8") as f:
                puntos_de_interes = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            puntos_de_interes = {}

        # Determinar la categoría de la consulta
        categoria = "puntos de interes"
        for cat, keywords in puntos_de_interes.items():
            if any(keyword in action_data.get("descripcion", "").lower() for keyword in keywords):
                categoria = cat
                break

        ubicacion = self.context.get("ubicacion_usuario") or "Junín, Mendoza"

        # Por ahora, devolvemos una respuesta predefinida con un link a Google Maps.
        # Más adelante, se puede mejorar para que busque en la base de datos.
        return {
            "success": True,
            "message_to_user": f"Aquí tienes una lista de {categoria} cerca de {ubicacion}: [Ver en Google Maps](https://www.google.com/maps/search/{categoria}+{ubicacion})",
            "data": {"categoria": categoria, "ubicacion": ubicacion}
        }
