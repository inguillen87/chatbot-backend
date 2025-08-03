from models import db, CatalogMapping
from typing import List, Dict, Any, Optional

class CatalogMappingService:
    def get_all_for_pyme(self, pyme_id: int) -> List[Dict[str, Any]]:
        """Returns all catalog mappings for a given PYME."""
        mappings = CatalogMapping.query.filter_by(pyme_id=pyme_id).all()
        return [mapping.to_dict() for mapping in mappings]

    def get_by_id(self, mapping_id: str) -> Optional[Dict[str, Any]]:
        """Returns a single catalog mapping by its ID."""
        mapping = db.session.get(CatalogMapping, mapping_id)
        return mapping.to_dict() if mapping else None

    def create(self, pyme_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a new catalog mapping."""
        new_mapping = CatalogMapping(
            pyme_id=pyme_id,
            name=data.get('name'),
            mapping=data.get('mapping')
        )
        db.session.add(new_mapping)
        db.session.commit()
        return new_mapping.to_dict()

    def update(self, mapping_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Updates an existing catalog mapping."""
        mapping = db.session.get(CatalogMapping, mapping_id)
        if not mapping:
            return None

        mapping.name = data.get('name', mapping.name)
        mapping.mapping = data.get('mapping', mapping.mapping)
        db.session.commit()
        return mapping.to_dict()

    def delete(self, mapping_id: str) -> bool:
        """Deletes a catalog mapping."""
        mapping = db.session.get(CatalogMapping, mapping_id)
        if not mapping:
            return False

        db.session.delete(mapping)
        db.session.commit()
        return True

catalog_mapping_service = CatalogMappingService()
