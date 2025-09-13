from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

@dataclass
class EvidenceBundle:
    """Standard structure returned by media processors."""
    kind: str
    raw_text: Optional[str] = None
    summary: Optional[str] = None
    categoria_sugerida: Optional[str] = None
    descripcion_sugerida: Optional[str] = None
    personales_detectados: Optional[List[str]] = None
    ubicacion_detectada: Optional[str] = None
    adjunto_id: Optional[int] = None
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "kind": self.kind,
            "raw_text": self.raw_text,
            "summary": self.summary,
            "categoria_sugerida": self.categoria_sugerida,
            "descripcion_sugerida": self.descripcion_sugerida,
            "personales_detectados": self.personales_detectados,
            "ubicacion_detectada": self.ubicacion_detectada,
            "adjunto_id": self.adjunto_id,
            "error": self.error,
        }
        if self.extra:
            data.update(self.extra)
        return data
