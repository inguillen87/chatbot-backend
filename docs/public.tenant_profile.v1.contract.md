# public.tenant_profile.v1

Contrato de bootstrap público para widget/portal.

## Endpoint canónico

- `GET /api/public/tenant-profile?tenant=<slug>`

## Response 200

```json
{
  "contract_version": "public.tenant_profile.v1",
  "tenant": {
    "slug": "colegio-san-martin",
    "tipo": "pyme",
    "rubro_profile": {
      "tenant_type": "pyme",
      "rubro_label": "Colegio Privado San Martín",
      "rubro_slug": "colegio-privado-san-martín",
      "education_profile": {
        "is_education": true,
        "institution_type": "private",
        "modules": ["asistencia", "comunicados", "agenda_academica", "tramites_secretaria"]
      }
    }
  }
}
```

## Response 404

```json
{
  "contract_version": "public.tenant_profile.v1",
  "error": {
    "code": 404,
    "message": "Tenant no encontrado"
  }
}
```

## Notas

- `contract_version` se devuelve tanto en éxito como en error para parsing consistente en frontend.
- `education_profile` es opcional y solo aparece para rubros educativos detectados.
