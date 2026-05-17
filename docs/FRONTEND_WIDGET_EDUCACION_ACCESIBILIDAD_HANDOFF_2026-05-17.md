# Frontend Handoff - Widget Educacion, Live Handoff y Accesibilidad

Fecha: 2026-05-17

## Objetivo

Evitar mezcla de rubros y hacer que la demo escolar se sienta como una conversacion real de WhatsApp, tanto en widget web como en WhatsApp sandbox. El frontend no debe inventar textos ni rutas: debe consumir los contratos publicados por backend y enviar acciones estructuradas.

## Problemas corregidos en backend

- Los recursos demo ya no deben abrirse como rutas SPA `/media/...`; el backend publica URLs bajo `/api/v2/demo/catalog-assets/...`.
- En demo publica, si cambia `tenant_slug`, el backend puede seguir el tenant demo seleccionado aunque haya quedado un entity token viejo de otro rubro.
- Para educacion, el backend acepta acciones estructuradas del widget: `create_school_case`, `justify_absence`, `talk_secretary`.
- Las imagenes/PDFs en contexto escolar se tratan como adjuntos de un caso escolar, no como catalogo comercial.

## 1. Menu de tres puntos en widget

Cuando `experience_blueprint.experience_type === "education"` o `workspace.education_profile.is_education === true`, el menu rapido del widget debe mostrar estas acciones principales:

```json
[
  { "label": "Crear caso escolar", "action_id": "create_school_case" },
  { "label": "Justificar inasistencia", "action_id": "justify_absence" },
  { "label": "Hablar con secretaria", "action_id": "talk_secretary" }
]
```

El click no debe navegar. Debe enviar un mensaje al endpoint del `chat_bootstrap` actual.

Payload recomendado:

```json
{
  "pregunta": "",
  "tenant_slug": "qa-colegio-sandbox",
  "tipo_chat": "pyme",
  "demo_mode": true,
  "action_id": "create_school_case",
  "education_context": { "is_education": true }
}
```

Headers requeridos:

```json
{
  "X-Chat-Session-Id": "sid_...",
  "X-Demo-Session-Id": "eyJ...",
  "X-Tenant-Slug": "qa-colegio-sandbox",
  "X-Anon-Id": "anon..."
}
```

Si el usuario cambia de rubro o tenant, el frontend debe reiniciar `chat_bootstrap` y no reutilizar un entity token de otro tenant.

## 2. Flujos esperados

### Crear caso escolar

1. Usuario toca `Crear caso escolar`.
2. Backend responde con prompt para pedir detalle.
3. Usuario manda texto, audio, imagen, PDF o ubicacion.
4. Backend crea ticket `pyme` + alias `school_case`.
5. Frontend muestra ticket/caso y deja visible `Ver estado`, `Menu colegio`, `Hablar con secretaria`.

### Justificar inasistencia

1. Usuario toca `Justificar inasistencia`.
2. Backend pide alumno, curso, fecha y motivo.
3. Usuario puede adjuntar foto/PDF del certificado.
4. Backend crea caso escolar con adjunto.

### Hablar con secretaria

1. Usuario toca `Hablar con secretaria`.
2. Backend crea ticket con estado `esperando_agente_en_vivo`.
3. Backend publica `data.live_chat`.
4. Frontend muestra:
   - disponible: `Secretaria esta disponible. Te dejamos en espera.`
   - fuera de horario: descripcion de horario desde backend.
5. Admin panel debe escuchar el evento de nuevo ticket y mostrar campanita: `Familia en espera de secretaria`.

## 3. WhatsApp parity

WhatsApp y widget deben compartir etiquetas e intentos. Backend publica:

- `workspace.education.quick_menu`
- `workspace.education.whatsapp_playbook`
- `experience_blueprint.conversion_ctas.actions`

No hardcodear menus distintos por canal. El frontend puede priorizar tres acciones principales, pero debe conservar el menu completo si backend lo manda.

## 4. Catalogos y PDFs

Abrir `resource.url` exactamente como viene de backend. Para demo debe verse asi:

```txt
/api/v2/demo/catalog-assets/colegios/catalogo-demo-colegios.pdf
/api/v2/demo/catalog-assets/colegios/lista-precios-demo.pdf
```

No convertir esas URLs en rutas internas de React. Usar `<a href target="_blank" rel="noopener">` o descarga.

## 5. Accesibilidad e inclusion

Base recomendada: WCAG 2.2 AA. El widget debe tratar chat, voz y avatar como superficies accesibles, no como decoracion.

Requisitos minimos:

- El widget abierto debe tener `role="dialog"` o region equivalente, titulo accesible y cierre con `Escape`.
- Foco atrapado dentro del widget mientras esta abierto; al cerrar, vuelve al boton que lo abrio.
- Todos los botones iconicos tienen `aria-label`.
- Mensajes nuevos anuncian cambios con `aria-live="polite"`.
- Estados de carga usan `aria-busy`; botones bloqueados usan `aria-disabled`.
- Navegacion 100% por teclado: abrir, cerrar, escribir, enviar, adjuntar, menu tres puntos, elegir accion.
- Avatar/realtime con controles visibles: pausar animacion, silenciar, activar subtitulos/transcripcion, repetir ultimo mensaje.
- Respetar `prefers-reduced-motion`; animaciones no deben ser obligatorias.
- Contraste suficiente en modo claro y oscuro; foco visible y no solo por color.
- Inputs con etiquetas reales, no solo placeholders.
- Adjuntos y ubicacion tienen descripciones accesibles.

Tooltip/popup sugerido:

```txt
Chatboc tambien esta pensado para personas que no pueden o no quieren escribir. Podes usar voz, subtitulos, lectura, adjuntos y derivacion humana para comunicarte con una institucion o empresa sin quedar afuera.
```

No mostrarlo como modal invasivo. Usar tooltip/ayuda contextual cerca del boton de voz/avatar y una entrada clara en el menu de accesibilidad.

## 6. Copy por rubro

Si `experience_type === "education"` no usar textos municipales como:

- reclamos urbanos
- turnos municipales
- vecino
- tramites express municipales

Usar lenguaje escolar:

- caso escolar
- secretaria
- familia
- alumno/curso
- inasistencia
- certificado
- comunicado

## 7. QA frontend

- Cambiar de Empresas a Colegios no conserva token/owner anterior.
- El primer saludo de Colegios no menciona municipio.
- Los tres botones del menu rapido envian `action_id` al backend y reciben respuesta real.
- Adjuntar imagen/PDF despues de `Justificar inasistencia` crea caso escolar.
- `Hablar con secretaria` crea un ticket visible para admin con estado `esperando_agente_en_vivo`.
- PDFs demo abren desde `/api/v2/demo/catalog-assets/...` sin 404 SPA.
- Widget se usa completo con teclado y lector de pantalla.
- Avatar/realtime tiene transcripcion, pausa y modo reducido.
