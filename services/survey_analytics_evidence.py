"""Describe the basis of an authorized analytics read, not population inference.

Pure and query-free. Attach ONLY in authenticated admin routes, after their
existing tenant/capability checks. Do not add this to public/live responses.
"""
from __future__ import annotations
import hashlib
import json
from math import isfinite
from typing import Any

CONTRACT = 'surveys.analytics_evidence.v1'
MAX_COUNT = 9007199254740991
FILTER_LABELS = {
    'desde': 'Inicio del intervalo', 'hasta': 'Fin del intervalo', 'canal': 'Canal',
    'barrio': 'Barrio', 'ciudad': 'Ciudad', 'provincia': 'Provincia', 'pais': 'Pa\u00eds',
    'genero': 'G\u00e9nero', 'rango_etario': 'Rango etario', 'bbox': '\u00c1rea del mapa',
    'utm_source': 'Origen de campa\u00f1a', 'utm_campaign': 'Campa\u00f1a',
}


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNT


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def build_analytics_evidence(survey: Any, summary: Any, filters: Any = None) -> dict | None:
    """Never invent zeroes or promote missing metadata to verified evidence."""
    sid, tid = getattr(survey, 'id', None), getattr(survey, 'tenant_id', None)
    if not _count(sid) or not sid or not _count(tid) or not tid or not isinstance(summary, dict):
        return None
    if type(summary.get('encuesta_id')) is not int or summary['encuesta_id'] != sid:
        return None
    p = summary.get('data_provenance')
    if (not isinstance(p, dict) or p.get('contract_version') != 'surveys.response_provenance.v1'
            or p.get('server_trusted_classification') is not True or p.get('mode') not in ('real', 'synthetic')
            or p.get('exact_aggregates') is not True):
        return None
    count_keys = ['population_size', 'sample_size', 'sample_limit', 'real_responses_included',
                  'synthetic_responses_included', 'synthetic_responses_excluded',
                  'unverified_responses_included', 'unverified_responses_excluded']
    if any(not _count(p.get(key)) for key in count_keys):
        return None
    total, sample, limit = p['population_size'], p['sample_size'], p['sample_limit']
    unique, complete = summary.get('participantes_unicos'), summary.get('respuestas_completas')
    partial = sample < total
    mode = p['mode']
    if (not _count(summary.get('total_respuestas')) or summary['total_respuestas'] != total
            or sample > total or sample > limit or p.get('sample_order') != 'latest'
            or p.get('partial') is not partial or p.get('sampled') is not partial
            or p.get('completion_estimated_from_sample') is not partial
            or p.get('eligibility_estimated_from_sample') is not partial
            or p['unverified_responses_included'] != 0
            or p['real_responses_included'] != (total if mode == 'real' else 0)
            or p['synthetic_responses_included'] != (total if mode == 'synthetic' else 0)
            or p.get('contains_synthetic') is not (mode == 'synthetic' and total > 0)
            or not _count(unique) or unique > total or not _count(complete) or complete > total):
        return None
    rate = summary.get('tasa_completitud')
    if not _number(rate) or not 0 <= rate <= 100:
        return None
    expected_rate = round(complete / total * 100, 2) if total else 0
    if abs(rate - expected_rate) > 0.011:
        return None
    coverage = round(sample / total * 100, 2) if total else None
    completion = rate if total and sample else None
    filtered = isinstance(filters, dict) and any(
        filters.get(key) is not None and filters.get(key) != '' for key in FILTER_LABELS)
    filter_labels = [label for key, label in FILTER_LABELS.items()
                     if isinstance(filters, dict) and filters.get(key) is not None and filters.get(key) != '']
    facts = [
        {'id': 'detail_base', 'label': 'Registros revisados en detalle',
         'value': f'{sample} / {total}',
         'note': ('Subconjunto de respuestas m\u00e1s recientes, no muestra aleatoria.' if partial
                  else 'Se revisaron en detalle todos los registros seleccionados.')},
        {'id': 'detail_limit', 'label': 'L\u00edmite t\u00e9cnico de lectura', 'value': str(limit),
         'note': 'Este l\u00edmite de procesamiento no define el dise\u00f1o muestral del estudio.'},
        {'id': 'synthetic_excluded', 'label': 'Respuestas sint\u00e9ticas excluidas',
         'value': str(p['synthetic_responses_excluded']), 'note': 'Excluidas de esta selecci\u00f3n; no eliminadas.'},
        {'id': 'unverified_excluded', 'label': 'Origen no verificado excluido',
         'value': str(p['unverified_responses_excluded']), 'note': 'No se cuentan como participaci\u00f3n real verificada.'},
        {'id': 'scope', 'label': 'Alcance de esta lectura',
         'value': 'Filtros activos' if filtered else 'Sin filtros de segmentaci\u00f3n',
         'note': ' / '.join(filter_labels) or 'Registros seleccionados por encuesta y origen.'},
    ]
    cards = [
        {'id': 'responses', 'label': 'Respuestas analizadas', 'value': total, 'unit': 'count',
         'basis': 'observed', 'detail': 'Conteo de registros del origen y filtros seleccionados, completos o incompletos.'},
        {'id': 'identities', 'label': 'Identidades diferenciadas', 'value': unique, 'unit': 'count',
         'basis': 'observed', 'detail': 'Identificadores distintos seg\u00fan la regla del sistema; no acredita personas \u00fanicas.'},
        {'id': 'completion', 'label': 'Completitud estimada' if partial else 'Completitud observada',
         'value': completion, 'unit': 'percent', 'basis': 'unavailable' if completion is None else 'estimated' if partial else 'observed',
         'detail': ('Estimaci\u00f3n desde el subconjunto reciente. No es tasa de respuesta poblacional.' if partial
                    else 'Formularios completos / registros seleccionados. No es tasa de respuesta poblacional.')},
    ]
    notes = [
        {'id': 'inference', 'title': 'Conclusiones descriptivas',
         'detail': 'Este resumen describe registros recibidos. No valida un dise\u00f1o muestral ni la representatividad de una poblaci\u00f3n.'},
        {'id': 'precision', 'title': 'Sin margen de error poblacional',
         'detail': 'No se publica un margen de error ni un intervalo de confianza: faltan un dise\u00f1o o modelo inferencial verificados.'},
        {'id': 'rates', 'title': 'Denominadores distintos',
         'detail': 'Completitud, tasa de respuesta, abstenci\u00f3n y cobertura poblacional no son intercambiables. Estas tres \u00faltimas no se calculan en esta ficha.'},
        {'id': 'weighting', 'title': 'Conteos sin ponderaci\u00f3n estad\u00edstica',
         'detail': 'Los conteos del resumen no aplican pesos de dise\u00f1o, ajuste por no respuesta ni calibraci\u00f3n poblacional.'},
    ]
    if partial:
        notes.insert(0, {'id': 'recent_subset', 'title': 'Hay indicadores estimados',
            'detail': 'La completitud y la elegibilidad por pregunta se estiman a partir de las respuestas m\u00e1s recientes; los totales agregados se consultan sobre todos los registros seleccionados.'})
    if mode == 'synthetic':
        notes.insert(0, {'id': 'synthetic', 'title': 'Modo de demostraci\u00f3n',
            'detail': 'Esta vista usa respuestas sint\u00e9ticas. No debe comunicarse como participaci\u00f3n real ni como opini\u00f3n de personas.'})
    result = {
        'contract_version': CONTRACT, 'scope': {'survey_id': sid, 'tenant_id': tid, 'mode': mode, 'filtered': filtered},
        'basis': {'selected_records': total, 'detail_records': sample, 'detail_limit': limit,
                  'detail_coverage_percent': coverage, 'partial': partial, 'complete_records': complete},
        'cards': cards, 'facts': facts, 'limitations': notes,
        'ui': {'heading': 'Base y alcance de los resultados', 'eyebrow': 'Evidencia anal\u00edtica',
               'description': 'Separ\u00e1 lo observado, lo estimado y lo que estos datos no permiten concluir.',
               'details': 'Ver ficha de datos y l\u00edmites', 'mode': 'Datos sint\u00e9ticos' if mode == 'synthetic' else 'Origen real',
               'observed': 'Observado', 'estimated': 'Estimado', 'unavailable': 'Sin base suficiente',
               'coverage': 'Lectura detallada'},
        'inference_authorized': False, 'margin_of_error': None,
    }
    # Identifier for the disclosed payload, not a digital signature or certification.
    canonical = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    result['evidence_revision'] = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return result
