# Integración de Encuestas con Votación en Vivo y Comentarios

Este documento detalla cómo integrar las nuevas funcionalidades de **Votación en Vivo** (Live Voting) y **Panel de Comentarios** en el frontend.

## 1. Votación en Vivo (Quick Polls)

Las encuestas ahora soportan tres nuevas banderas de configuración que modifican la experiencia de usuario:
*   `es_votacion_envivo` (bool): Indica que la interfaz debe ser simplificada para una votación rápida (ej. Sí/No, Ratings).
*   `mostrar_resultados_envivo` (bool): Indica que el frontend debe conectarse vía Socket.IO para mostrar resultados en tiempo real.
*   `permitir_comentarios` (bool): Habilita el panel de comentarios y debate.

### Obtención de la Encuesta

Al consultar `GET /api/public/encuestas/<slug>`, el payload incluye estos campos.

**Ejemplo de respuesta:**
```json
{
  "slug": "apertura-ferrocarril-junin",
  "titulo": "Apertura del tramo ferroviario",
  "tipo": "opinion",
  "es_votacion_envivo": true,
  "mostrar_resultados_envivo": true,
  "permitir_comentarios": true,
  "puntos_recompensa": 50,
  "preguntas": [
    {
      "id": 101,
      "tipo": "opcion_unica",
      "texto": "¿Estás de acuerdo?",
      "opciones": [
        {"id": 1, "texto": "Sí", "valor": "si"},
        {"id": 2, "texto": "No", "valor": "no"}
      ]
    }
  ],
  "resultados_envivo": {
    "total_respuestas": 1540,
    "preguntas": {
      "101": {
        "tipo": "opcion_unica",
        "opciones": [
          {"id": 1, "texto": "Sí", "votos": 1200},
          {"id": 2, "texto": "No", "votos": 340}
        ]
      }
    }
  }
}
```

> **Nota:** El campo `resultados_envivo` viene precargado con el estado actual.

---

### Conexión WebSocket (Tiempo Real)

Para actualizar los gráficos o contadores en tiempo real sin recargar la página:

1.  **Conectar**: Usar la misma instancia de Socket.IO que el widget o crear una nueva conexión a `/api/socket.io`.
2.  **Unirse a la Sala**: Emitir el evento `join` con el nombre de la sala `encuesta_<slug>`.

**Código Cliente (Ejemplo JS):**
```javascript
const socket = io('https://api.chatboc.ar', { path: '/api/socket.io' });

socket.on('connect', () => {
    socket.emit('join', { room: 'encuesta_apertura-ferrocarril-junin' });
});

// Escuchar actualizaciones de votos
socket.on('survey_update', (data) => {
    console.log("Nuevos resultados:", data);
    // data tiene la misma estructura que "resultados_envivo" del JSON inicial
    updateCharts(data);
});

// Escuchar nuevos comentarios (si aplica)
socket.on('survey_comment', (comment) => {
    console.log("Nuevo comentario:", comment);
    // comment = { id, texto, nombre_autor, fecha, user_id }
    appendComment(comment);
});
```

---

### Tipos de Pregunta Visuales

Además de `opcion_unica` (Radio Buttons) y `opcion_multiple` (Checkboxes), el backend normaliza ciertos tipos para facilitar la UI:

*   **Rating con Emojis**: Si la pregunta tiene `tipo: "rating_emoji"`, el frontend debe renderizar las opciones como emojis grandes o estrellas. El valor del voto sigue siendo el `id` de la opción.

---

## 2. Panel de Comentarios

Si `permitir_comentarios` es `true`, se debe mostrar una sección de debate debajo de la votación.

### Listar Comentarios
`GET /api/public/encuestas/<slug>/comentarios?limit=50&offset=0`

**Respuesta:**
```json
[
  {
    "id": 45,
    "texto": "Me parece excelente iniciativa para el turismo.",
    "nombre_autor": "Juan Perez",
    "fecha": "2023-10-27T14:30:00+00:00",
    "user_id": 102,
    "anon_id": null
  },
  ...
]
```

### Publicar Comentario
`POST /api/public/encuestas/<slug>/comentarios`

**Body (JSON):**
```json
{
  "texto": "Opino que debería extenderse hasta la terminal.",
  "nombre": "Vecino Preocupado" // Opcional, si es anónimo
}
```
*   Si el usuario está logueado (tiene Token JWT), se envía en el header `Authorization` y el backend lo asocia automáticamente.
*   Si es anónimo, puede enviar un `nombre` opcional.

---

## 3. Puntos y Recompensas

El backend otorga puntos automáticamente al votar si la encuesta tiene `puntos_recompensa > 0` y el usuario está identificado.
*   **Feedback Visual:** Al recibir el `201 Created` tras votar, el frontend puede mostrar una animación de "+50 puntos" si el usuario estaba logueado.

---

## 4. Embed (Iframe / Widget)

Para permitir que municipios o pymes inserten la votación en sus sitios web:

*   **URL Standalone:** `https://portal.chatboc.ar/e/<slug>?mode=embed`
*   **Código Iframe:**
    ```html
    <iframe src="https://portal.chatboc.ar/e/apertura-ferrocarril-junin?mode=embed"
            width="100%" height="600" frameborder="0"></iframe>
    ```
*   El frontend debe detectar el parámetro `mode=embed` para ocultar headers/footers y mostrar solo la tarjeta de votación.

---

## 5. Moderación y Reportes

Para mantener un entorno seguro en encuestas públicas, se habilita la opción de reportar comentarios.

### Reportar Comentario
`POST /api/public/encuestas/<slug>/comentarios/<id>/reportar`

*   No requiere payload.
*   Si un comentario recibe múltiples reportes (umbral configurado en backend, por defecto 5), su estado cambia a `revision` y deja de ser visible públicamente hasta que un administrador lo apruebe.

### Panel de Administración (Frontend Admin)

El panel de administración de la encuesta debe incluir una pestaña "Comentarios" que consuma:
`GET /api/admin/encuestas/<id>/comentarios`

Esta vista muestra todos los comentarios (incluidos los ocultos o en revisión). El administrador puede moderarlos:

`PATCH /api/admin/encuestas/comentarios/<id>`
Body: `{ "accion": "aprobar" | "ocultar" | "eliminar" }`
