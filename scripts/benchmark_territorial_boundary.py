"""Mide contención y carga del límite oficial de una organización, con y sin envolventes/caché.

Uso: python3 scripts/benchmark_territorial_boundary.py [slug]
Sólo usa la biblioteca estándar y los datos versionados; no toca red ni base.
"""
import os
import random
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import services.territorial_evidence as evidence  # noqa: E402


def _points(count, inside, seed):
    generator = random.Random(seed)
    box = (-68.6, -54.9, -66.5, -53.0) if inside else (-73.9, -53.4, -69.5, -50.5)
    return [(generator.uniform(box[0], box[2]), generator.uniform(box[1], box[3])) for _ in range(count)]


def _timed(function, points):
    started = time.perf_counter()
    hits = sum(1 for lng, lat in points if function(lng, lat))
    return time.perf_counter() - started, hits


def main(slug):
    tenant = SimpleNamespace(slug=slug, municipio_id=None)
    evidence.clear_verified_boundary_cache()
    started = time.perf_counter()
    jurisdiction = evidence.resolve_tenant_jurisdiction(tenant)
    cold = time.perf_counter() - started
    started = time.perf_counter()
    for _ in range(20):
        evidence.resolve_tenant_jurisdiction(tenant)
    warm = (time.perf_counter() - started) / 20
    geometry = jurisdiction.get('boundary_geometry')
    if not isinstance(geometry, dict):
        raise SystemExit(f'{slug}: sin límite verificado')
    print(f'{slug}: {len(geometry["coordinates"])} componentes')
    print(f'resolver: primera {cold * 1000:.0f} ms; siguientes {warm * 1000:.2f} ms')

    def plain(lng, lat):
        return any(evidence._polygon_contains_position(polygon, lng, lat) for polygon in geometry['coordinates'])

    def indexed(lng, lat):
        return evidence._boundary_contains_position(geometry, lng, lat)

    for label, inside in (('zona de la isla', True), ('fuera (oeste)', False)):
        for count in (200, 2000):
            points = _points(count, inside, seed=count + inside)
            before, hits_before = _timed(plain, points)
            after, hits_after = _timed(indexed, points)
            assert hits_before == hits_after
            print(f'{label:16} {count:5} puntos ({hits_after:4} dentro): '
                  f'{before:6.2f} s -> {after:6.3f} s  (x{before / max(after, 1e-9):.0f})')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'tierra-del-fuego')
