# widget.quick_menu.education.v1

Contrato funcional para menú rápido educativo en bootstrap de widget.

## Endpoint

- `GET /api/public/widget-config`

## Campo relevante

`quick_menu: Array<MenuItem>`

```json
{
  "quick_menu": [
    { "id": "menu_asistencia", "label": "Asistencia", "intent": "asistencia_alumno" },
    { "id": "menu_comunicados", "label": "Comunicados", "intent": "comunicados_familias" },
    { "id": "menu_agenda", "label": "Agenda académica", "intent": "agenda_academica" },
    {
      "id": "menu_tramites",
      "label": "Trámites secretaría",
      "intent": "tramites_secretaria",
      "institution_type": "public"
    }
  ]
}
```

## Reglas

- Aplica cuando `tenant.rubro_profile.education_profile.is_education = true`.
- `institution_type` en ítem `menu_tramites` puede ser:
  - `public`
  - `private`
  - `general`
