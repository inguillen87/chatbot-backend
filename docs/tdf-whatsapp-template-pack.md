# Pack TDF de orientación institucional

El catálogo existente de Chatboc incorpora `tdf_orientacion_institucional_es_ar`, versión `1.0.0`, con cinco borradores en español argentino. Se preparan en el mismo registro local de plantillas y con los mismos permisos, auditoría e idempotencia que Municipio, Colegio y Empresa. Es implementación de catálogo y creación local; no constituye aceptación de una cuenta de WhatsApp, aprobación del proveedor, activación ni entrega.

## Mensajes y contenido

| Intento | Nombre versionado | Uso previsto, pendiente de validar para envío |
|---|---|---|
| Bienvenida/orientación | `chatboc_tdf_orientacion_v1` | Responder a una consulta iniciada por la persona y orientar hacia el canal institucional |
| Continuidad | `chatboc_tdf_continuidad_v1` | Retomar una consulta iniciada y autorizada, sin insistencia comercial |
| Turno | `chatboc_tdf_turno_aviso_v1` | Avisar de una novedad de turno realmente registrada por el área responsable |
| Novedad | `chatboc_tdf_consulta_aviso_v1` | Avisar de una actualización real de esa consulta, sin afirmar una aprobación |
| Derivación humana | `chatboc_tdf_derivacion_v1` | Informar una derivación registrada y aceptada por el equipo |

Los textos no incluyen identidad, documentos, diagnósticos, datos de salud, número de expediente, beneficios, promesas de empleo ni plazos de atención. No llevan variables, adjuntos, números de teléfono ni botones. Los contactos de Ushuaia/Río Grande y las listas documentales que necesitan reconciliación institucional se gestionan como fuentes del agente; este pack no convierte esos aportes en requisitos vigentes ni los inserta en un aviso automático.

`UTILITY` es la categoría propuesta por la relación con una consulta o turno solicitado por la persona. Meta conserva la decisión final y puede rechazar o reclasificar el contenido. La bienvenida genérica nunca debe utilizarse para una campaña de captación. La relación con la consulta original debe acreditarse antes de cualquier solicitud de aprobación o activación. [Categorías de WhatsApp](https://whatsappbusiness.com/products/conversation-categories/utility/), [criterios de categorías del proveedor actual](https://www.twilio.com/docs/whatsapp/tutorial/message-template-approvals-statuses).

## Acceso y creación local

El pack sólo está disponible cuando el controlador pasa el slug `tierra-del-fuego` del `TenantProfile` autorizado. El nombre de un pack, un parámetro de URL, texto del contexto o un número registrado no determina la organización. El GET y el POST conservan `token_requerido`, `require_tenant`, la comprobación de pertenencia y las capacidades existentes `whatsapp.templates.read`/`whatsapp.templates.manage`.

1. Consultar `GET /api/admin/whatsapp/template-packs` dentro de la sesión y tenant autorizados. El resultado contiene el pack TDF, sus previsualizaciones y los bloqueos de envío.
2. Para crear borradores locales, usar `POST /api/admin/whatsapp/template-packs/tdf/drafts`, con `Idempotency-Key` estable y `{"pack_version":"1.0.0"}`. Este documento no autoriza ejecutar esa escritura en una base de cliente.
3. El controlador bloquea la fila del tenant, vuelve a verificar permisos y crea cinco filas `provider=chatboc`, `status=local_draft`, sin referencia externa. El commit incluye el recibo de auditoría.
4. Repetir la misma operación devuelve el recibo y comprueba que las definiciones siguen intactas. Otro contenido/versionado con la misma clave devuelve conflicto. Una versión incorrecta o tenant ajeno no crea filas.

El autoservicio de WhatsApp expone una referencia al catálogo y al endpoint de borradores; sigue indicando `provider_status=not_checked`, sin activación ni envío. No presume propiedad de un WABA o sender. Los tres packs existentes y sus definiciones permanecen iguales. Se conserva la versión global del catálogo para no invalidar fingerprints/recibos previos; el nuevo pack tiene identidad, versión y hashes propios.

## Separación del futuro envío

`dispatch_policy.mode=draft_only` mantiene `production_send_allowed=false` en el catálogo incluso si se presenta una fila con aprobación del proveedor. Esta es una restricción de disponibilidad del pack en el catálogo; no reemplaza el control del transporte ni verifica un evento.

`intended_triggers` y `required_before_activation` son requisitos declarativos. `source_event_validation_implemented=false` y `runtime_dispatch_enabled=false` explicitan que todavía no existe un validador/dispatcher conectado a estos cinco borradores. No hay evento de turno, derivación ni aprobación de gestión certificado por materializar una plantilla. Un futuro cambio debe conectar evidencia persistida real y comprobar la organización, destinatario y estado del evento antes de construir el aviso.

Antes de habilitarlo se requieren propiedad real del WABA/sender y la conexión del tenant, credenciales seguras por organización, aprobación actual de la definición exacta en esa cuenta, opt-in/opt-out, control de ventana y permisos, evento validado y transporte con recibos/deduplicación. La creación local no realiza llamadas a Twilio/Meta ni activa el aprovisionamiento; su preflight sin almacén cifrado continúa bloqueado.

La política primaria de WhatsApp exige plantilla aprobada para iniciar conversaciones o enviar fuera de las 24 horas desde el último mensaje de la persona, además de opt-in y una vía clara de atención humana. También limita la solicitud de identificadores y el tratamiento de información sensible. [Política WhatsApp vigente consultada el 01/10/2026](https://whatsappbusiness.com/policy/).

El contrato actual valida nombres, categoría, idioma, variables correlativas con ejemplos y CTA HTTPS/E.164. Para futuras variables/botones deben revisarse las reglas del proveedor, sus ejemplos y enlaces institucionales autorizados. Este pack evita introducir un enlace de Drive, un dominio no confirmado o un botón sin flujo real. [Variables Twilio](https://www.twilio.com/docs/content/using-variables-with-content-api), [botones Twilio](https://www.twilio.com/docs/content/twilio-call-to-action).

Las páginas técnicas directas de `developers.facebook.com` devolvieron HTTP 429 en esta revisión. Se leyeron las páginas primarias de WhatsApp sobre política/categorías y las reglas primarias de Twilio, que es el proveedor existente; no se declara una validación completa de un payload enviado a Meta.

## Validación y límites de evidencia

Las pruebas locales usan Flask y una base desechable con identidades/tokens sintéticos. Comprueban el GET/POST real del código, permisos y scope, creación de cinco borradores, replay/alias, ausencia de referencias externas y ausencia de llamadas HTTP/proveedor. La suite de transacciones existente comprueba rollback/auditoría e idempotencia; los casos PostgreSQL se ejecutan sólo con su configuración explícita de base desechable.

Estos fixtures prueban el comportamiento y la ausencia de efectos externos. No son login nominal, propiedad del WABA, revisión de Meta ni aceptación del envío. El cambio permanece en código local hasta la publicación controlada que autorice el responsable de la release; no crea un Preview ni modifica Producción.

Resultado local del 01/10/2026: **96 pruebas y 19 subcasos aprobaron**, con cuatro casos PostgreSQL omitidos por no habilitar su base desechable. Incluye packs, autoservicio, transacciones, seguridad del transporte de notificaciones, plataforma Meta y reglas empresariales. Las pruebas HTTP usan SQLite en memoria; las de transacciones usan SQLite temporal en esta ejecución. Los avisos de SQLAlchemy/fechas y el skip PostgreSQL no se presentan como certificación de producción.
