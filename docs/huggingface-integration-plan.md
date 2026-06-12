# Hugging Face Integration Plan

## Objetivo

Sumar Hugging Face como capa especializada de IA sin cambiar el comportamiento
actual por defecto. OpenAI/Gemini siguen siendo el cerebro conversacional; Hugging
Face aporta tareas puntuales donde conviene tener modelos dedicados, proveedores
alternativos o modelos propios.

## Integrado en este sprint

- `services/huggingface_inference_service.py`: wrapper server-side para Inference
  Providers con imports lazy, timeout configurable y salida normalizada.
- Embeddings opcionales: `HUGGINGFACE_EMBEDDINGS_ENABLED=true` habilita fallback
  si OpenAI no está disponible. El backend rechaza vectores que no tengan la
  dimensión esperada por Qdrant.
- Visión opcional: `VISION_HUGGINGFACE_ENABLED=true` habilita clasificación de
  imagen y detección de objetos como fallback final.
- Zero-shot opcional: `HUGGINGFACE_ZERO_SHOT_ENABLED=true` deja listo el servicio
  para categorizar reclamos, prioridades y rubros sin hardcodear frases.
- Clasificación municipal auxiliar: cuando no hay match por menú/keyword, el
  flujo de reclamos puede usar zero-shot para sugerir categoría y prioridad con
  umbral configurable, guardando proveedor, confianza y candidatos.
- Enriquecimiento operativo de reclamos: el extractor municipal agrega señales
  de riesgo, necesidad de foto, ubicación exacta, atención humana y sentimiento
  del vecino como hints auxiliares.
- CRM AI enrichment: `POST /admin/tickets/:ticket_id/ai-enrichment` devuelve un
  contrato bajo demanda para paneles admin con hints de Hugging Face para
  municipios y pymes sin recalcularlo en cada listado.
- Variables declaradas en `.env.example`, `ENV_DOCUMENTATION.md` y `render.yaml`
  sin exponer secretos.
- `requirements-ai-oss.txt`: paquete opcional para extras open-source pesados,
  instalado solo si `INSTALL_OPEN_SOURCE_AI_EXTRAS=true`.
- Docling opcional: extracción open-source de tablas/documentos antes de caer a
  visión/LLM en el pipeline de catálogos.

## Casos de uso recomendados

1. Catálogos y búsqueda semántica

   Usar embeddings multilingües para productos, FAQs, trámites y documentos
   públicos. Prioridad alta para demos de empresas y municipios porque mejora la
   respuesta cuando el usuario escribe de forma natural.

2. Clasificación de reclamos

   Aplicar zero-shot con las categorías reales de cada municipio: luminaria,
   arbolado, limpieza, agua, calles, tránsito, sugerencias. Sirve como señal
   auxiliar para el LLM y como control cuando el usuario escribe mensajes cortos.

3. Priorización operativa

   Clasificar urgencia o impacto: normal, alta, crítica. Ejemplos: pérdida de
   agua, árbol caído, cable peligroso, semáforo roto, animal mordedor. Debe ser
   sugerencia, no decisión final automática.

4. Visión para fotos de WhatsApp

   Detectar objetos y etiquetas en fotos adjuntas para enriquecer el reclamo:
   pozo, rama, luminaria, basura, cartel, vehículo, calle, vereda. OpenAI sigue
   siendo proveedor principal; HF queda como fallback o comparación.

5. Moderación y calidad

   Detectar insultos, spam, datos sensibles, mensajes duplicados o contenido no
   apto antes de mostrarlo en paneles públicos, encuestas o timeline del reclamo.

6. Colegios y comprobantes

   Usar modelos OCR/documentos como complemento para interpretar comprobantes,
   cuotas, certificados y notas enviadas por familias. La validación contable debe
   seguir en backend.

7. Documentos complejos con Docling

   Convertir PDF, DOCX, PPTX, HTML y planillas a Markdown/texto estructurado para
   catálogos, trámites, ordenanzas, comprobantes y documentación escolar. Es el
   primer paso open-source antes de usar LLMs caros.

8. Evaluación de agentes

   Usar modelos más chicos para scoring offline: si la respuesta fue útil,
   consistente, segura y si pidió datos de más. Esto ayuda a mejorar plantillas y
   prompts sin depender de revisiones manuales.

## Reglas de producción

- Ninguna key de Hugging Face debe ir al frontend ni a variables `VITE_`.
- Activar por feature flag y, luego, por tenant.
- Registrar proveedor, modelo, latencia, fallback y errores.
- No mezclar embeddings de distintas dimensiones en la misma colección Qdrant.
- Usar HF como señal auxiliar en decisiones sensibles; Python valida antes de
  crear reclamos, pedidos, turnos o acciones administrativas.

## Próximos pasos

- Agregar toggles en Admin Tenant para activar HF por módulo.
- Guardar métricas por proveedor en analytics operativos.
- Usar la señal zero-shot ya conectada para dashboards de categoría/prioridad.
- Agregar evaluación offline de conversaciones reales anonimizadas.
- Probar modelos con datasets propios antes de habilitar por defecto.

Ver también `docs/open-source-ai-roadmap.md` para el plan completo con Docling,
Sentence Transformers, Meta SAM 2, Llama y moderación.

## Operacion segura de secretos

Los tokens de proveedores IA no se guardan en git ni se envian al frontend. Para
sincronizarlos con Render, el proceso local debe tener:

```bash
HUGGINGFACE_API_TOKEN=...
GEMINI_API_KEY=...
RENDER_ENV_SYNC_ENABLED=true
RENDER_API_KEY=...
RENDER_SERVICE_ID=... # o RENDER_ENV_GROUP_ID
```

Despues se puede ejecutar:

```bash
python scripts/sync_ai_provider_env.py --include-recommended-defaults --trigger-deploy
```

Para validar Hugging Face sin exponer el token:

```bash
python scripts/smoke_huggingface_provider.py
```

Ambos scripts imprimen nombres de variables y resultados operativos, nunca
valores secretos.
