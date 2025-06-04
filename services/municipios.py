# services/municipios.py

import logging
import random
import json
import re
from typing import Any, Optional, Dict
from flask import session
from models import MunicipioTicket, Sugerencia, db

logger = logging.getLogger(__name__)

def responder_municipio(pregunta: str, user_obj: Any, rubro_obj: Any, session, otros_args=None) -> Dict[str, Any]:
    logger.info(f"[MUNI] Respondiendo pregunta municipio: '{pregunta}'")

    # 1. Si la pregunta es de reclamo, crea un ticket y devuelve nro único
    if contiene_reclamo(pregunta):
        nro_ticket = generar_ticket_db(pregunta, user_obj)
        respuesta = f"Tu reclamo fue registrado con el número #{nro_ticket}. Nuestro equipo municipal lo revisará a la brevedad. ¿Querés seguir con otro trámite?"
        return {"respuesta": respuesta, "fuente": "municipio_ticket"}

    # 2. Busca FAQ (igual que pymes, pero usando rubro_id propio de municipios)
    try:
        from services.faq_matcher_spacy import buscar_en_faq_spacy
        faq = buscar_en_faq_spacy(pregunta, rubro_obj.id)
        if faq and faq.answer:
            from services.logic import reemplazar_placeholders
            respuesta = reemplazar_placeholders(faq.answer, user_obj)
            return {"respuesta": respuesta, "fuente": "faq"}
    except Exception as e:
        logger.warning(f"[MUNI] Error FAQ: {e}")

    # 3. Busca Intent (si tenés intenciones específicas para municipios)
    try:
        from services.intent_matcher import buscar_en_intents
        intent_resp = buscar_en_intents(pregunta, rubro_obj.nombre)
        if intent_resp:
            from services.logic import reemplazar_placeholders
            respuesta = reemplazar_placeholders(intent_resp, user_obj)
            return {"respuesta": respuesta, "fuente": "intent"}
    except Exception as e:
        logger.warning(f"[MUNI] Error Intents: {e}")

    # 4. Fallback con sugerencias
    sugs = sugerencias_municipio(rubro_obj.id)
    resp_sug = "No encontré la información. ¿Querés intentar con: " + " · ".join(f"“{s}”" for s in sugs)
    return {"respuesta": resp_sug, "fuente": "sugerencia_municipios"}

def contiene_reclamo(pregunta: str) -> bool:
    palabras_reclamo = ["reclamo", "denuncia", "bache", "luminaria", "ruido", "reparar", "arreglo", "problema", "queja"]
    return any(p in pregunta.lower() for p in palabras_reclamo)

def generar_ticket_db(pregunta: str, user_obj: Any) -> int:
    # Simulación: crea un ticket en tabla propia de municipio
    nro_ticket = random.randint(10000, 99999)
    try:
        ticket = MunicipioTicket(
            pregunta=pregunta,
            user_id=getattr(user_obj, 'id', None),
            estado="nuevo",
            nro_ticket=nro_ticket,
            # Podés agregar más campos: fecha, contacto, rubro, etc.
        )
        db.session.add(ticket)
        db.session.commit()
        logger.info(f"[MUNI] Ticket guardado OK: {nro_ticket}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"[MUNI] Error guardando ticket: {e}")
    return nro_ticket

def sugerencias_municipio(rubro_id: int) -> list:
    # Sugerencias propias del rubro municipio
    try:
        sugs = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        textos = [s.texto for s in sugs if s.texto and s.texto.strip()]
        if textos:
            return random.sample(textos, min(3, len(textos)))
    except Exception as e:
        logger.warning(f"[MUNI] Error buscando sugerencias: {e}")
    return [
        "¿Dónde tramito el carnet de conducir?",
        "¿Cómo hago un reclamo por luminaria?",
        "¿Dónde pago las tasas municipales?"
    ]

# Ejemplo: integración con Qdrant usando colección exclusiva de municipio
def buscar_catalogo_municipio_qdrant(municipio_id: int, consulta: str):
    from services.qdrant_search import buscar_catalogo_qdrant
    # Colección especial, nunca la de pymes
    collection = f"municipio_{municipio_id}_catalogo"
    return buscar_catalogo_qdrant(municipio_id, consulta, limite=3, score_min=0.60, collection=collection)
