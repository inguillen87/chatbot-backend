# Chatboc Meta/Twilio Partner Platform Roadmap 2026

Fecha: 2026-05-21

## Objetivo

Convertir Chatboc en una plataforma WhatsApp-first para LATAM: alta rapida de WhatsApp Business, inbox operativo, IA con herramientas, flujos verticales, sandbox, APIs para developers, auditoria y CRM conversacional.

La meta no es vender "un chatbot". La meta es vender infraestructura conversacional:

- Chatboc Connect: onboarding de WhatsApp Business para clientes.
- Chatboc Inbox: bandeja humana con IA, SLA, asignaciones, notas y templates.
- Chatboc Flows: turnos, pedidos, reclamos, consultas, encuestas y formularios.
- Chatboc Developer Platform: API, webhooks, logs, SDK, sandbox, CLI y MCP.
- Chatboc CRM: contactos enriquecidos automaticamente desde WhatsApp, widget, voz, formularios, tickets, pedidos y encuestas.
- Turnero: vertical fuerte para agenda por WhatsApp, voz y web.
- Intellitech: implementacion, integracion y soporte enterprise/gobierno.
- Inmovar: laboratorio, venture studio y demos, no nombre principal de venta enterprise.

## Veredicto de partnership

Ruta recomendada para la situacion actual:

1. Operar tecnicamente con Twilio y Meta sin prometer partner oficial.
2. Terminar base legal y de compliance: SAS, CUIT, dominio, politicas, soporte, seguridad y datos fiscales consistentes.
3. Construir MVP de Chatboc Connect + Inbox + CRM + Turnero + Developer Portal.
4. Completar Meta Business Verification y Meta App Review.
5. Onboardear como WhatsApp Tech Provider.
6. Pedir Twilio Partner Solution para conectar la Meta App con Twilio.
7. Integrar Embedded Signup y Senders API.
8. Correr pilotos reales.
9. Aplicar a Twilio Partner Program con evidencia.
10. Escalar a Meta Tech Partner cuando haya producto, volumen, soporte y casos.

No conviene empezar por "Solution Partner" de Meta. Para Chatboc el camino correcto es ISV/SaaS: Tech Provider primero, producto funcionando, luego partner superior.

## Benchmark Kapso sin copiar

Kapso es buen benchmark porque compite como WhatsApp API para developers, no como chatbot. Lo replicable para Chatboc es el patron de producto, no la marca, textos, UI, codigo ni activos.

Patrones a llevar a Chatboc:

- Setup links / Connect: el cliente conecta o provisiona WhatsApp desde la app.
- API-first: enviar mensajes, media, templates, interactivos y recibir eventos.
- Inbox: conversaciones, broadcasts y operacion humana.
- Workflows: automatizaciones visuales y pasos con IA.
- Webhooks: eventos de mensajes, delivery, conexiones y errores.
- Templates: creacion, estado, aprobacion y uso controlado.
- Numbers: alta, salud y configuracion de numeros.
- SDK: cliente TypeScript primero.
- CLI: diagnostico, templates, webhooks, mensajes y numeros desde terminal.
- MCP: permitir que agentes IA operen WhatsApp con permisos controlados.
- llms.txt: documentacion navegable por agentes y developers.

La diferencia defensible de Chatboc debe ser LATAM, soporte local, verticales listas, gobiernos, pymes, colegios, turnos, reclamos, pedidos, CRM y billing local.

## Producto objetivo

### Chatboc Connect

Permite activar WhatsApp Business desde Chatboc:

- crear tenant;
- crear cliente/organizacion;
- elegir numero propio o numero Twilio;
- comprar/asignar numero cuando aplique;
- abrir Embedded Signup;
- capturar WABA ID y phone number ID;
- crear Twilio subaccount por cliente;
- registrar WhatsApp Sender con Senders API;
- trackear estado: draft, pending, verifying, online, failed, rejected;
- configurar webhook inbound y status callback;
- bloquear produccion hasta que el sender este online;
- guardar auditoria completa del onboarding.

Regla tecnica: un cliente/brand = un Twilio subaccount = un WABA mapeado. No mezclar negocios en el mismo WABA/subaccount.

### Chatboc Inbox

Bandeja para humanos y equipos administrativos:

- conversaciones por canal;
- asignacion a agente/equipo;
- notas internas;
- etiquetas y segmentos;
- historial completo;
- mensajes entrantes/salientes;
- templates aprobados;
- estado de entrega/lectura;
- handoff bot a humano;
- SLA;
- permisos por modulo;
- auditoria de acciones;
- resumen IA de conversacion.

### Chatboc CRM Conversacional

CRM automatico para usuarios de WhatsApp, web widget, voz, encuestas y formularios:

- contacto unico por telefono, email, anon_id, BSUID cuando exista, widget session y tenant;
- motivo de contacto;
- resumen corto de la conversacion;
- ultimo canal;
- ultimo interes detectado;
- rubro/vertical inferida;
- temperatura comercial: frio, tibio, hot;
- opt-in y opt-out;
- historial de tickets, pedidos, turnos, encuestas y conversaciones;
- eventos de campanas;
- notas internas;
- responsable;
- proxima accion recomendada;
- deduplicacion y merge asistido.

Para municipios/colegios/empresas debe mostrar "quien es y por que vino", no mails tipo `+549...@whatsapp.chatboc.com` como identidad primaria.

### Chatboc Flows

Constructor de flujos por vertical:

- crear reclamo;
- consultar estado de reclamo;
- pedir turno;
- cancelar/reprogramar turno;
- hacer pedido;
- consultar pedido;
- encuesta/votacion;
- contacto con ventas;
- solicitud de llamada;
- consulta escolar;
- autorizaciones/avisos a familias;
- derivacion a operador.

Los flujos deben poder correr por WhatsApp, web widget, voz y backoffice sin duplicar logica.

### Chatboc AI Agent

IA como orquestador, backend como validador/ejecutor:

- interpretar intencion;
- pedir faltantes;
- llamar herramientas;
- resumir conversacion;
- sugerir respuesta al operador;
- clasificar prioridad;
- extraer ubicacion, media, audio y documentos;
- derivar a humano;
- respetar ventanas 24h, opt-in, templates y permisos.

Debe seguir la arquitectura actual del repo: inteligencia en LLM/prompt/herramientas; Python valida, persiste y ejecuta.

### Developer Platform

Para equipos IT, software factories y partners:

- API REST multi-tenant;
- API keys por tenant, ambiente y scope;
- webhooks configurables;
- replay de webhooks;
- event ledger;
- request IDs;
- status page por integracion;
- sandbox;
- SDK TypeScript;
- ejemplos Node/Python;
- llms.txt;
- CLI futura;
- MCP futuro.

## Compliance y limites que no se pueden negociar

- No vender "verificar si cualquier numero tiene WhatsApp" como lookup libre. Lo correcto es validar formato, propiedad, registro de sender, opt-in, estado de sender y delivery/error real.
- Todo telefono debe ir en E.164 para WhatsApp/SMS.
- El numero debe poder recibir OTP por SMS o llamada para registrarse en WhatsApp.
- Short codes no aplican para WhatsApp sender.
- Fuera de la ventana de 24 horas hay que usar templates aprobados.
- Dentro de la ventana de 24 horas puede haber texto/media libre, con restricciones de formato.
- Marketing requiere opt-in trazable.
- Opt-out debe bloquear outbound no transaccional.
- Nunca exponer Twilio AuthToken ni credenciales Meta en frontend.
- Cada WABA se mapea a un solo Twilio account/subaccount.
- Logs deben guardar request_id, tenant, actor, canal, payload seguro, estado y error.
- Gobierno: vender atencion ciudadana, turnos y tramites; evitar uso politico/propaganda.

## Backlog P0

### Backend

- [ ] Modelo `ProviderConnection` para Meta/Twilio por tenant.
- [ ] Modelo `ProviderSender` para WhatsApp sender y estado.
- [ ] Modelo `ProviderSubaccount` para subcuenta Twilio por cliente.
- [ ] Modelo `MessagingEventLedger` para inbound/outbound/status/error.
- [ ] Modelo `ConsentLedger` para opt-in/opt-out por contacto/canal.
- [ ] Modelo `TemplateRegistry` para templates, categoria, estado y Content SID.
- [ ] Endpoint `GET /api/v2/integrations/whatsapp/status`.
- [ ] Endpoint `POST /api/v2/integrations/whatsapp/test-message`.
- [ ] Endpoint `GET /api/v2/backoffice/contacts/summary`.
- [ ] Endpoint `GET /api/v2/backoffice/contacts`.
- [ ] Endpoint `GET /api/v2/backoffice/contacts/{id}`.
- [ ] Endpoint `PATCH /api/v2/backoffice/contacts/{id}` para tags, responsable y estado comercial.
- [ ] Normalizador de contacto: telefono, email, anon_id, ExternalUserId/BSUID, session_id.
- [ ] Resumen IA corto de conversaciones entrantes.
- [ ] Deduplicacion segura de contactos.
- [ ] Bloqueo backend para outbound fuera de 24h sin template aprobado.

### Frontend/Admin

- [ ] CRM de contactos legible: nombre, telefono, canal, motivo, resumen, temperatura, ultimo contacto.
- [ ] Vista contacto 360: conversaciones, tickets, pedidos, turnos, campanas, notas.
- [ ] Inbox con asignacion clara y sin duplicados.
- [ ] Campanita realtime con eventos centralizados.
- [ ] Pantalla WhatsApp Connect con checklist de estado.
- [ ] Vista de templates con estado y uso.
- [ ] Vista de logs/webhooks para IT.

### Demo comercial

- [ ] WhatsApp hub de Chatboc con menus cortos, iconos y links compactos.
- [ ] Demo municipios: reclamo completo, encuesta participativa, estado de reclamo con mapa.
- [ ] Demo empresas: pedido completo, catalogo, envio, factura, ventas.
- [ ] Demo colegios: consulta familiar, aviso, autorizacion, turno/reunion, derivacion.
- [ ] Demo voz: saludo por nombre, menu por vertical y fallback correcto.
- [ ] Widget web con el mismo CRM/contacto que WhatsApp.

## Backlog P1

- [ ] Embedded Signup integrado en admin.
- [ ] Flujo para numero propio vs numero Twilio.
- [ ] Compra/asignacion de numeros Twilio desde backend.
- [ ] Registro de sender con Twilio Senders API.
- [ ] Persistencia de WABA ID, phone number ID, Sender SID y Partner Solution ID.
- [ ] Estado asincronico de sender y errores de registro.
- [ ] Subaccount por cliente.
- [ ] API keys por tenant.
- [ ] Webhook replay.
- [ ] SDK TypeScript inicial.
- [ ] Documentacion developer publica.
- [ ] `llms.txt` para agentes IA.
- [ ] Sandbox de WhatsApp/Widget/Voz por vertical.
- [ ] Ledger de campanas WhatsApp/email con limite 24h.
- [ ] Export CSV/XLSX/PDF de CRM, tickets y campanas.

## Backlog P2

- [ ] CLI `chatboc`.
- [ ] MCP server para operar Chatboc desde agentes IA.
- [ ] WhatsApp Flows builder.
- [ ] Marketplace de plantillas por vertical.
- [ ] White-label para consultoras/software factories.
- [ ] SSO y permisos enterprise.
- [ ] Retencion de datos configurable.
- [ ] Evaluacion IA y QA de conversaciones.
- [ ] Partner/admin console para Intellitech.
- [ ] Reportes de gobierno, educacion, soporte, ventas y operaciones.

## Roadmap 30/60/90

### 0 a 30 dias: base vendible

Entregables:

- WhatsApp sender propio de Chatboc funcionando.
- Hub demo Chatboc por WhatsApp mejorado.
- CRM conversacional automatico.
- Inbox sin duplicados.
- Estado de reclamos/pedidos/turnos con mapa/timeline.
- Demo de voz con menu por vertical.
- Privacy, Terms, DPA, Acceptable Use y Security page.
- Videos demo: WhatsApp, widget, voz, admin, CRM.

Aceptacion:

- Un prospecto puede probar municipio, empresa y colegio sin ayuda.
- Todo contacto queda guardado con resumen y motivo.
- Un admin entiende que atender primero y por que.
- No hay refresh manual para eventos criticos.

### 31 a 60 dias: plataforma ISV

Entregables:

- ProviderConnection + ProviderSender + EventLedger en backend.
- Subaccount Twilio por cliente en entorno controlado.
- Sender status y template status en panel.
- API keys, webhooks y logs.
- Developer docs y ejemplos.
- Sandbox por vertical.
- Primeros 2 o 3 pilotos.

Aceptacion:

- Un cliente puede iniciar alta de WhatsApp desde Chatboc.
- IT puede ver logs, webhooks, request IDs y errores.
- Comercial puede ver leads hot/tibios/frios.

### 61 a 90 dias: partner readiness

Entregables:

- Meta Business Verification encaminada/completa.
- Meta App preparada para App Review.
- Screencasts de review.
- Twilio Partner Solution solicitada cuando Meta App este lista.
- Embedded Signup en staging.
- Registro sender con Senders API.
- 3 a 5 pilotos con metricas.
- Pitch deck tecnico/comercial.

Aceptacion:

- Se puede demostrar onboarding, envio, recepcion, template, CRM, inbox, voz y analytics.
- Hay evidencia de uso real.
- La aplicacion esta lista para Twilio Partner Program y Meta Tech Provider.

## Evidencia minima para aplicar

- SAS o entidad lista y datos fiscales consistentes.
- Dominio y emails corporativos: support, legal, security.
- Politicas publicas fetchables: privacidad, terminos, eliminacion de datos, DPA, Acceptable Use.
- Meta Business Portfolio con 2FA.
- Sender propio registrado.
- Templates creados/aprobados.
- Videos de flujo completo.
- CRM y logs auditables.
- Pilotos o casos de uso.
- Pricing con margen real: licencia + uso + soporte + IA + numero + onboarding.

## Primer corte tecnico recomendado

Implementar `ProviderConnection` y `MessagingEventLedger` antes de seguir puliendo UI. Sin esa base no hay Partner Platform, solo pantallas. El primer PR deberia incluir:

- modelos/migracion;
- servicios para normalizar provider state;
- endpoint de estado WhatsApp por tenant;
- event ledger para webhooks/status;
- contrato de frontend;
- tests de tenant isolation, 24h policy y contacto CRM.

## Fuentes revisadas

- Twilio Tech Provider Program Overview: https://www.twilio.com/docs/whatsapp/isv/tech-provider-program
- Twilio Tech Provider Integration Guide: https://www.twilio.com/docs/whatsapp/isv/tech-provider-program/integration-guide
- Twilio WhatsApp Senders API: https://www.twilio.com/docs/whatsapp/api/senders
- Twilio WhatsApp key concepts, 24h window, templates, WABA/subaccount: https://www.twilio.com/docs/whatsapp/key-concepts
- Twilio WhatsApp best practices and number requirements: https://www.twilio.com/docs/whatsapp/best-practices-and-faqs
- Twilio self sign-up and OTP ownership verification: https://www.twilio.com/docs/whatsapp/self-sign-up
- Twilio Partner Program: https://www.twilio.com/en-us/partners/become-a-partner
- Twilio Partner Program Policies: https://www.twilio.com/en-us/legal/partner-program-policies
- Kapso Documentation: https://docs.kapso.ai/
- Kapso CLI Documentation: https://docs.kapso.ai/docs/whatsapp/cli

