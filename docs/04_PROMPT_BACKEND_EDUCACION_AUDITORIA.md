# Prompt · Backend educación · auditoría readonly

```md
Trabajá SOLO en el repo chatbot-backend.

Primero leé:
- AGENTS.md
- docs/ai/00_chatboc_codex_master.md si existe
- docs/ai/01_chatboc_backend_codex.md si existe
- el archivo 01_MASTER_VERTICAL_EDUCACION_SAAS.md
- el archivo 02_BACKEND_VERTICAL_EDUCACION_CODEX.md
- el archivo 08_BACKLOG_VERTICAL_EDUCACION.yaml

Fase actual: AUDITORÍA READONLY. No escribas código todavía.

Objetivo:
mapear cómo abrir la vertical educación sobre el backend actual sin romper municipios, pymes ni contratos públicos existentes.

Necesito que analices y documentes:
1. cómo reutilizar el motor actual de tickets para casos escolares
2. cómo reutilizar catálogo + embeddings + Qdrant como knowledge base escolar
3. cómo reutilizar auth actual para familiar verificado, staff y widget público
4. cómo resolver school/campus/section sin romper tenancy actual
5. cómo integrar campañas, documentos, asistencia e incidentes con el modelo actual
6. cómo integrar voz realtime como front desk escolar
7. qué puntos del backend actual son de alto riesgo para menores y datos sensibles
8. qué archivos concretos habría que tocar en una fase fundacional de educación
9. qué endpoints actuales deben preservarse y qué aliases nuevos conviene crear

Restricciones:
- no modificar código
- no proponer rewrite total
- no cambiar framework
- no romper contratos `/ask/*`, `/tickets/*`, `/catalogo/*`
- no crear otro motor de tickets
- no crear otro motor vectorial
- no convertir V1 en ERP académico completo
- tratar educación como una capa aditiva de dominios/capabilities

Entregables:
1. inventario de reutilización real del backend existente
2. mapa de módulos críticos y archivos concretos
3. propuesta de modelo de datos mínimo para educación
4. plan por slices
5. riesgos de seguridad y privacidad
6. tests requeridos
7. breaking changes esperados: ninguno / lista exacta

Formato de salida:
- Hallazgos
- Reutilización recomendada
- Gaps
- Archivos a tocar
- Riesgos
- Slice plan
- Tests
- Breaking changes
```
