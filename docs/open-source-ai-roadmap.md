# Open-Source AI Roadmap para Chatboc

## Principio

Open-source no reemplaza al agente principal por defecto. Suma capacidades
especializadas que hacen al producto más robusto, barato y auditable: extracción,
clasificación, embeddings, visión, evaluación y privacidad.

## Ya conectado detrás de flags

- Hugging Face Inference Providers: `services/huggingface_inference_service.py`.
- Clasificación zero-shot de reclamos y prioridad: `services/municipio_ai_classifier.py`.
- Fallback de embeddings para Qdrant con control de dimensión.
- Fallback de visión para fotos por WhatsApp.
- Docling opcional para documentos/catálogos antes de visión/LLM.

## Sprints recomendados

### 1. CRM Inteligente de Reclamos

Objetivo: que cada reclamo llegue al panel enriquecido.

- Categoría sugerida por HF zero-shot.
- Prioridad sugerida: normal, alta, urgente.
- Motivo operativo: seguridad, infraestructura, higiene, tránsito, agua.
- Riesgo: cable, incendio, árbol por caer, pérdida de agua, semáforo.
- Duplicados por ubicación + texto + ventana temporal.

Activación: segura por tenant y con umbral alto.

### 2. Document Intelligence

Objetivo: que colegios, gobiernos y empresas puedan subir documentos reales.

- Docling para PDF/DOCX/PPTX/XLSX/HTML a Markdown.
- Extracción de tablas de catálogos sin LLM cuando el documento tiene estructura.
- Extracción de trámites/ordenanzas/FAQs para base de conocimiento.
- Comprobantes escolares: detectar importe, fecha, alumno, cuota y referencia.

Activación: `INSTALL_OPEN_SOURCE_AI_EXTRAS=true` + `DOCLING_ENABLED=true`.

### 3. Semantic Search Pro

Objetivo: que el bot entienda búsquedas comerciales y trámites mal escritos.

- Sentence Transformers local o HF embeddings.
- Reranking de resultados antes de responder.
- Sinónimos por rubro: vinos, repuestos, colegios, tasas, trámites.
- Separar embeddings por colección/modelo para no mezclar dimensiones.

Requiere: evaluación A/B con consultas reales.

### 4. Visión Municipal Avanzada

Objetivo: fotos que ayuden de verdad a resolver.

- HF image classification/object detection como fallback.
- Meta Segment Anything 2 para segmentar baches, ramas, basura o luminarias.
- Comparar foto contra categorías esperadas del reclamo.
- Marcar “foto no relacionada” o “foto útil” para operadores.

Requiere: endpoint dedicado/GPU si se usa SAM 2 en producción.

### 5. Moderación y Confianza

Objetivo: proteger paneles públicos, encuestas y operadores.

- Clasificar insultos, amenazas, spam, datos sensibles y mensajes repetidos.
- Ocultar o mandar a revisión comentarios públicos.
- Redactar versiones seguras para timeline público.
- Alertar casos urgentes o violentos sin bloquear el reclamo.

Activación: primero modo observación, luego acciones automáticas.

### 6. Agentes por Vertical

Objetivo: mejores demos y tenants finales.

- Gobierno/municipio: reclamos, turnos, trámites, encuestas, agenda.
- Colegio: cuotas, comprobantes, horarios, autorizaciones, comunicados.
- Empresa: catálogo, stock, pedidos, presupuestos, seguimiento.
- Políticos/ONG: agenda territorial, encuestas, voluntarios, campañas.

Cada vertical debe tener dataset de evaluación propio.

### 7. Evaluación Offline

Objetivo: mejorar sin romper producción.

- Guardar conversaciones anonimizadas.
- Evaluar si el bot pidió los datos correctos.
- Evaluar si derivó a humano cuando debía.
- Evaluar si usó plantilla/flujo correcto.
- Crear benchmarks por demo y tenant.

Esto permite competir con SaaS maduras porque el producto aprende por métricas,
no por intuición.

## Librerías / ecosistema a evaluar

- Hugging Face `InferenceClient`: serverless, endpoints y modelos de terceros.
- Sentence Transformers: embeddings/reranking local o privado.
- Docling: parsing open-source de documentos complejos.
- Meta SAM 2: segmentación avanzada de imágenes/video.
- Llama: modelo abierto para RAG privado o fine-tuning futuro.
- Presidio: detección/redacción de PII antes de analytics o entrenamiento.
- RapidFuzz: matching robusto y barato para catálogos, trámites y nombres.
- spaCy/GLiNER: extracción de entidades en español para documentos y tickets.

## Reglas de activación

- Toda feature nueva entra detrás de flag.
- Nada de keys en frontend ni en repo.
- Medir latencia, costo, fallback y tasa de error por proveedor.
- Los modelos sugieren; Python valida acciones sensibles.
- No usar modelos pesados en Render starter sin endpoint dedicado.
