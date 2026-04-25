# Prompt · Backend educación · implementación fundacional

```md
Trabajá SOLO en el repo chatbot-backend sobre una branch nueva:
feat/edu-foundation-domain

Primero leé:
- AGENTS.md
- 01_MASTER_VERTICAL_EDUCACION_SAAS.md
- 02_BACKEND_VERTICAL_EDUCACION_CODEX.md
- 08_BACKLOG_VERTICAL_EDUCACION.yaml

Objetivo:
implementar la fundación backend de la vertical educación sin romper la SaaS actual.

Alcance de esta tarea:
1. agregar capability/flags para vertical educación en tenant
2. agregar modelos base:
   - School
   - Campus
   - AcademicLevel
   - Shift
   - CourseSection
   - Student
   - Guardian
   - StudentGuardianRelation
3. definir taxonomía de school cases reutilizando el motor de tickets actual
4. crear base de verificación familiar (estado y contexto mínimo)
5. crear endpoints mínimos para:
   - listar schools/campuses/sections
   - lookup/verify guardian
   - obtener family context
   - crear/listar school cases por alias sobre tickets
6. preparar alias de knowledge escolar reutilizando catálogo/Qdrant
7. agregar tests y migraciones

Restricciones duras:
- no reescribir auth completo en esta tarea
- no tocar voz en esta tarea
- no implementar todavía billing/admissions completos
- no crear otro motor de tickets
- no crear otro motor RAG
- mantener compatibles `/ask/*`, `/tickets/*`, `/catalogo/*`
- no romper contratos de municipios/pymes
- no hacer refactor cosmético masivo

Requerimientos técnicos:
- additive migrations
- separación por tenant + school + campus donde aplique
- sensibilidad preparada para datos de menores
- claims/roles listos para familiar verificado vs staff
- nombres de endpoint claros bajo `/api/v1/education/*`
- reutilización explícita del ticket engine existente
- reutilización explícita del stack catálogo/Qdrant

Entregables obligatorios:
1. diff mínimo y enfocado
2. lista de archivos modificados
3. migraciones
4. variables de entorno nuevas si hacen falta
5. tests automáticos
6. checklist manual
7. rollback plan
8. breaking changes: ninguno / lista exacta

Criterio de done:
- el backend soporta tenant educación con estructura escolar básica
- existe lookup/verify family context mínimo
- school cases funcionan vía alias/reutilización del sistema actual
- knowledge escolar queda preparado sin duplicar motor
- tests pasan
- no se rompen contratos legacy
```
