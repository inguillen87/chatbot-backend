import logging

from models import CatalogoItem

logger = logging.getLogger(__name__)


class EducationKnowledgeService:
    def query_knowledge_base(self, tenant_id: int, query: str, top_k: int = 5, school_id: int | None = None) -> list:
        """
        Reuses the existing CatalogoItem / Qdrant stack with an education alias.
        Results include lightweight citations and scope metadata.
        """
        scope_metadata = {
            "alias": "education_knowledge",
            "tenant_id": tenant_id,
            "school_id": school_id,
        }

        try:
            from services.qdrant_search import buscar_catalogo_qdrant

            results = buscar_catalogo_qdrant(tenant_id, query) if buscar_catalogo_qdrant else None
            if not results:
                raise ImportError("Fallback to DB")

            citations = []
            for res in results[:top_k]:
                citations.append(
                    {
                        "source_id": res.get("id"),
                        "title": res.get("nombre", "Documento Institucional"),
                        "content_snippet": res.get("descripcion", ""),
                        "relevance_score": res.get("score", 0.0),
                        "metadata": {**scope_metadata, "engine": "qdrant"},
                    }
                )
            return citations

        except ImportError:
            items = (
                CatalogoItem.query.filter(
                    CatalogoItem.tenant_id == tenant_id,
                    CatalogoItem.descripcion.ilike(f"%{query}%"),
                )
                .order_by(CatalogoItem.id.desc())
                .limit(top_k)
                .all()
            )

            return [
                {
                    "source_id": str(item.id),
                    "title": item.nombre,
                    "content_snippet": item.descripcion,
                    "relevance_score": 1.0,
                    "metadata": {**scope_metadata, "engine": "catalog_db"},
                }
                for item in items
            ]


education_kb_service = EducationKnowledgeService()
