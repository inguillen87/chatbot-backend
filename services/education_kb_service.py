import logging
from app import db
from models import CatalogoItem

logger = logging.getLogger(__name__)

class EducationKnowledgeService:
    def query_knowledge_base(self, tenant_id: int, query: str, top_k: int = 5) -> list:
        """
        Reuses the existing CatalogoItem / Qdrant setup to serve as an Educational KB.
        This provides a facade over standard catalog search to return answers formatted
        for manuals, rules, and school policies with citations.
        """
        # In a real scenario we would call `buscar_catalogo_qdrant(tenant_id, query)`
        # Here we mock the DB fallback / alias layer to demonstrate the semantic reuse
        # without duplicating the vector engine.
        try:
            from services.qdrant_search import buscar_catalogo_qdrant
            results = None
            if buscar_catalogo_qdrant:
                results = buscar_catalogo_qdrant(tenant_id, query)
            if not results:
                raise ImportError("Fallback to DB")

            # Reformat them into citation objects
            citations = []
            for res in results:
                citations.append({
                    "source_id": res.get("id"),
                    "title": res.get("nombre", "Documento Institucional"),
                    "content_snippet": res.get("descripcion", ""),
                    "relevance_score": res.get("score", 0.0)
                })
            return citations

        except ImportError:
            # Fallback to local DB if Qdrant module isn't loaded or available
            items = CatalogoItem.query.filter(
                CatalogoItem.tenant_id == tenant_id,
                CatalogoItem.descripcion.ilike(f"%{query}%")
            ).limit(top_k).all()

            return [
                {
                    "source_id": str(item.id),
                    "title": item.nombre,
                    "content_snippet": item.descripcion,
                    "relevance_score": 1.0 # Exact match mock
                } for item in items
            ]

education_kb_service = EducationKnowledgeService()
