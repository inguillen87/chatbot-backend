# Menús de evaluación con fuente trazable

Fuente: plan de IA accesible/Mesa Única de 14 páginas, SHA-256
`1f6de63d4f70ede4e967077cc9eae768c64574b022d66c5ebb8868aac3d4a6ee`.
No se publica el PDF; se mantiene un catálogo de orientación de evaluación con
referencias de páginas y sin datos personales, credenciales o secretos.

`data/conversation_guides/accessible-support.evaluation.v1.json` es el único artefacto
canónico. Contiene 29 nodos: rol de consulta, cinco ejes, submenús, información,
derivación de ejemplo y dos preguntas separadas de cierre. Las selecciones son
códigos explícitos; no se presenta como comprensión libre de lenguaje por un LLM.

`services/accessible_support_guide.py` valida el grafo y genera respuestas de menú,
texto WhatsApp y TwiML escapado. No conecta números, no envía mensajes, no crea casos,
no procesa información clínica y no consulta registros oficiales. El adaptador
concreto de proveedor deberá validar firma, número autorizado y contexto del tenant
antes de invocar el servicio. No se modificó el webhook de clientes existentes.

La evaluación web consume un artefacto fijado por commit y SHA-256 desde este repo.
No hay una copia editable de los contenidos de negocio en React. Actualizar el
contenido requiere actualizar ese pin; el empaquetador rechaza cambios no esperados.

Pruebas: 12 casos de navegación, alcance, fuente, independencia de respuestas y
round-trip XML para todos los nodos. No sustituyen pruebas de mensajes reales.
Los requisitos y datos de fuentes oficiales siguen pendientes de validación; la
prueba no certifica vigencia CUD, elegibilidad RUPE, coberturas, turnos o fe de vida.
