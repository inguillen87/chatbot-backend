"""Utilities to format product dictionaries into human-friendly strings."""

from __future__ import annotations
from collections import defaultdict
from typing import Iterable, Dict, List, Tuple


RUBRO_ICON = {
    "vino": "🍷",
    "ropa": "👕",
    "ferreteria": "🔧",
    "generico": "🛒",
}


def _detectar_rubro(prod: Dict) -> str:
    if any(k in prod for k in ["bodega", "variedad"]):
        return "vino"
    if any(k in prod for k in ["talle", "color"]):
        return "ropa"
    if any(k in prod for k in ["herramienta", "modelo", "medida"]):
        return "ferreteria"
    return "generico"


def _group_products(productos: Iterable[Dict]) -> List[Dict]:
    grupos: Dict[Tuple, Dict] = {}
    for p in productos:
        rubro = _detectar_rubro(p)
        clave_base = (
            rubro,
            p.get("nombre"),
            p.get("bodega") or p.get("marca"),
        )
        grupo = grupos.setdefault(clave_base, {"base": {}, "variantes": [], "rubro": rubro})
        grupo["variantes"].append(p)
        for k, v in p.items():
            if v and k not in grupo["base"]:
                grupo["base"][k] = v
    return list(grupos.values())


def format_product_list(productos: Iterable[Dict], formato: str = "markdown") -> str:
    """Return a human friendly representation for a list of products.

    Parameters
    ----------
    productos:
        List of dictionaries with product data.
    formato:
        ``"markdown"`` or ``"html"`` or ``"jsx"``.
    """
    grupos = _group_products(productos)
    lineas: List[str] = []
    for g in grupos:
        prod = g["base"]
        rubro = g["rubro"]
        icon = RUBRO_ICON.get(rubro, RUBRO_ICON["generico"])
        nombre = prod.get("nombre", "Producto")
        detalles = []
        if rubro == "vino":
            if prod.get("bodega"):
                detalles.append(prod["bodega"])
            if prod.get("variedad"):
                detalles.append(f"Variedad: {prod['variedad']}")
            presentaciones = sorted({v.get("presentacion") for v in g["variantes"] if v.get("presentacion")})
            if presentaciones:
                detalles.append(" / ".join(presentaciones))
        elif rubro == "ropa":
            if prod.get("marca"):
                detalles.append(prod["marca"])
            talles = sorted({v.get("talle") for v in g["variantes"] if v.get("talle")})
            colores = sorted({v.get("color") for v in g["variantes"] if v.get("color")})
            if talles:
                detalles.append(f"Talle: {', '.join(talles)}")
            if colores:
                detalles.append(f"Color: {', '.join(colores)}")
        elif rubro == "ferreteria":
            if prod.get("marca"):
                detalles.append(prod["marca"])
            if prod.get("modelo"):
                detalles.append(f"Modelo: {prod['modelo']}")
            medidas = sorted({v.get("medida") for v in g["variantes"] if v.get("medida")})
            if medidas:
                detalles.append(f"Medida: {', '.join(medidas)}")
        else:
            for campo in ["marca", "bodega", "descripcion"]:
                if prod.get(campo):
                    detalles.append(str(prod[campo]))

        precio = prod.get("precio_str") or prod.get("precio")
        if precio:
            detalles.append(f"{prod.get('moneda', '')} {precio}")
        stock_total = sum(int(v.get("stock") or 0) for v in g["variantes"])
        if stock_total:
            detalles.append(f"Stock: {stock_total}")

        linea = f"{icon} {nombre}"
        if detalles:
            linea += " | " + " | ".join(detalles)
        lineas.append(linea)

    sep = "\n" if formato in {"markdown", "html", "jsx"} else "\n"
    return sep.join(lineas)

