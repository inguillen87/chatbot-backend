# Tarea Frontend: Plantilla "Votación" tipo YouTube + Comentarios + Analytics

## Objetivo
Diseñar e implementar una nueva plantilla de participación llamada **"Votación"**, inspirada en las encuestas de YouTube (estilo profesional), con resultados en tiempo real y un panel de comentarios (anónimos y con login social). Además, habilitar un **seed de 100 respuestas** (aplicable a encuestas, sondeos y votaciones) para poblar datos realistas y coherentes que alimenten analytics, mapas de calor y métricas sin tener que cargar respuestas manuales.

> Este documento es el handoff para el equipo frontend, con los requisitos y tareas concretas para integrar la nueva experiencia sin errores.

---

## 1) Requisitos UX/UI (nivel mundial)

### Pantalla principal de Votación
- **Hero card** con:
  - Título de la votación.
  - Descripción breve.
  - Sello “En tiempo real”.
- **Opciones de voto**:
  - Modo binario (Sí / No).
  - Modo 4 opciones (estilo YouTube: barras con porcentajes).
  - Cada opción con **barra de progreso dinámica**, porcentaje y conteo.
  - Estados: sin votar, votado, cerrado.
- **Resumen superior**:
  - Total de votos.
  - Tiempo restante (si aplica).
  - Compartir (QR, link, WhatsApp).

### Comentarios (debajo de la votación)
- **Caja de texto** con placeholder: “Dejá tu comentario (anónimo o con Facebook)”.
- Dos modos:
  - **Anónimo** (sin autenticación).
  - **Registrado** (login social: Facebook — *UI debe prepararse para SDK*).
- Orden: “Más recientes / Más votados”.
- Reacciones simples opcionales (like).

### Resultado en tiempo real
- Actualización automática cada X segundos (polling) o por WebSocket/SSE cuando esté listo.
- Animación suave al cambiar porcentajes.

### Estado “cerrado”
- Mostrar resultados finales + mensaje institucional.

---

## 2) Componentes Frontend requeridos

1. **VoteTemplateCard**
   - Props: `title`, `description`, `options`, `totalVotes`, `status`, `timeRemaining`.
   - Responsivo para mobile/tablet/desktop.

2. **VoteOptionsList**
   - Render de opciones con barras progresivas.
   - Estado de selección y envío.

3. **VoteResultsLive**
   - Transiciones animadas (CSS/Framer).
   - Actualización incremental.

4. **CommentsBox**
   - Input + botón enviar.
   - Toggle “Anónimo / Facebook”.
   - Lista de comentarios.

5. **CommentsList**
   - Paginación o “cargar más”.

---

## 3) Data Contract (frontend-friendly)

### Modelo de votación esperado
```
{
  "id": "vote_123",
  "title": "¿Dónde construimos el próximo parque?",
  "description": "Elegí tu opción preferida.",
  "status": "open | closed",
  "options": [
    { "id": "opt_1", "label": "Zona Norte", "votes": 320 },
    { "id": "opt_2", "label": "Zona Centro", "votes": 280 },
    { "id": "opt_3", "label": "Zona Sur", "votes": 180 },
    { "id": "opt_4", "label": "Zona Este", "votes": 90 }
  ],
  "totalVotes": 870,
  "updatedAt": "2025-01-01T12:00:00Z",
  "allowAnonymousComments": true,
  "allowFacebookLogin": true
}
```

### Comentarios
```
{
  "id": "c_01",
  "author": "Anónimo" | "Facebook User",
  "avatar": "url|null",
  "text": "Me parece mejor la Zona Norte por accesibilidad.",
  "createdAt": "2025-01-01T12:05:00Z"
}
```

---

## 4) Integración (front + back)

### Fuente de datos (API ya existente)
La “Votación” se implementa **como encuesta pública** con flags:
`es_votacion_envivo`, `mostrar_resultados_envivo`, `permitir_comentarios`.
La plantilla sugerida es **`votacion-envivo-youtube`**.

### Endpoints reales (backend)
- **GET** `/api/public/encuestas/{slug}` → obtener data y estado (incluye flags y, si aplica, `resultados_envivo`).
- **POST** `/api/public/encuestas/{slug}/responder` → enviar voto (payload con `respuestas`).
- **GET** `/api/public/encuestas/{slug}/live-results` → resultados live (polling).
- **GET** `/api/public/encuestas/{slug}/comentarios` → listar comentarios.
- **POST** `/api/public/encuestas/{slug}/comentarios` → crear comentario `{ texto, nombre }`.
- **POST** `/api/public/encuestas/{slug}/comentarios/{id}/reportar` → reportar comentario.

### Modo tiempo real
Mientras no exista socket:
- **Polling cada 5–10s** a `/live-results`.
Cuando esté socket:
- **Socket.IO** `survey_update` y `survey_comment` en la sala `encuesta_<slug>`.

---

## 5) Seed de 100 respuestas (producción y showcases)

### Requisitos
- Botón admin (solo staff): “**Reset y generar 100 seeds**”.
- Al regenerar seeds:
  - Reset votos + comentarios.
  - Generar 100 respuestas aleatorias **coherentes con el tipo de encuesta** (edad, género, barrio, rango etario, etc. si aplica).
  - Actualizar analytics, mapas de calor, métricas.

### Estado visual recomendado
Badge “Datos simulados”.

### Endpoint seed (backend)
- **POST** `/api/admin/encuestas/{id}/seed-demo`
  - Body: `{ cantidad: 100, reset: true }`
  - Soporta `geo_profile_key` y `municipality_label` para mapas de calor.

---

## 6) Checklist de entrega frontend

- [ ] Plantilla “Votación” integrada en catálogo de plantillas.
- [ ] UI votación con resultados live.
- [ ] Comentarios anónimos + social login (UI preparada).
- [ ] Seed de 100 respuestas (UI + botón admin).
- [ ] Estado “cerrado” con resultados.
- [ ] Diseño consistente con marca (tipografía, colores, botones).

---

## 7) Notas
- Esto debe verse **nivel consultora top mundial**, con UI/UX premium.
- Animaciones suaves, textos claros y layout moderno.
- Preparar componentes para reutilizar en encuestas y sondeos existentes.
