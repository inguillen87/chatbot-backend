# Especificaciones de Integración con el Backend

Este documento detalla los endpoints y estructuras de datos requeridos para alimentar el Portal del Usuario (Noticias, Eventos, Catálogo, Encuestas) y cómo funciona la lógica multi-tenant.

## 1. Autenticación y Multi-Tenancy

El sistema utiliza un modelo **Multi-Tenant**. Para que el backend identifique a qué municipio o PyME pertenece una operación, se debe cumplir una de las siguientes condiciones:
*   **Usuario Admin/Empleado**: El token de usuario (`Authorization: Bearer <token>`) ya contiene la información del tenant (`municipio_id` o `empresa_id`).
*   **Usuarios Públicos**: El portal envía el `tenant_slug` en la URL (ej. `/market/<slug>`) o el `widget_token` en los headers.

## 2. Gestión de Contenido (Admin)

Los administradores (Municipios/PyMEs) utilizan estos endpoints para subir contenido que se reflejará automáticamente en el Portal del Usuario.

### A. Noticias y Eventos (`POST /municipal/posts`)

Utilizado para crear noticias ("novedades") o eventos en la agenda.

**Endpoint:** `POST /municipal/posts`
**Headers:** `Authorization: Bearer <TOKEN_ADMIN>`
**Form-Data (Multipart):**

| Campo | Tipo | Requerido | Descripción |
| :--- | :--- | :--- | :--- |
| `titulo` | Texto | Sí | Título de la noticia o evento. |
| `contenido` | Texto | Sí | Descripción detallada. |
| `tipo_post` | Texto | Sí | `noticia` o `evento`. |
| `fecha_evento_inicio` | ISO8601 | Sí (Eventos) | Ej: `2023-12-25T18:00:00`. |
| `fecha_evento_fin` | ISO8601 | No | Fin del evento. |
| `ubicacion` | Texto | No | Lugar del evento. |
| `flyer_image` | Archivo | No | Imagen de portada (JPG/PNG). |
| `tags` | Lista | No | Etiquetas para filtrado (ej. `cultura`, `deporte`). |

**Ejemplo de Respuesta:**
```json
{
  "id": 123,
  "titulo": "Festival de Verano",
  "tipo_post": "evento",
  "imagen_url": "https://...",
  "fecha_publicacion": "2023-11-20T10:00:00Z"
}
```

### B. Catálogo de Productos/Servicios

#### 1. Carga Individual (`POST /api/admin/market/catalog`)

**Endpoint:** `POST /api/admin/market/catalog`
**Headers:** `Authorization: Bearer <TOKEN_ADMIN>`
**JSON Body:**

```json
{
  "nombre": "Kit Escolar Primaria",
  "descripcion": "Cuadernos, lápices y mochila.",
  "precio": 5000,
  "moneda": "ARS", // O "PTS" para canjes
  "categoria": "Educación",
  "image_url": "https://...",
  "stock": 100,
  "disponible": true
}
```

#### 2. Carga Masiva (Archivo) (`POST /catalogo/upload`)

**Endpoint:** `POST /catalogo/upload`
**Form-Data:** `file` (PDF, Excel, Word). El sistema procesa inteligentemente el archivo y extrae los productos.

### C. Encuestas (`POST /api/encuestas/admin/surveys`)

(Verificar endpoint exacto en `routes/encuestas_admin.py`, estructura típica)

```json
{
  "titulo": "Satisfacción de Espacios Públicos",
  "preguntas": [
    { "texto": "¿Cómo califica la plaza central?", "tipo": "stars" }
  ],
  "puntos_recompensa": 50, // Puntos por completar
  "estado": "publicada"
}
```

## 3. Consumo desde el Portal (Frontend)

El portal (`/market/<slug>`) ya está configurado para consumir esta información automáticamente.

*   **Catálogo**: Se agrupa por categorías. Los items con moneda `PTS` se muestran en la sección "Canjes".
*   **Novedades**: Se listan los posts con `tipo_post='noticia'`.
*   **Eventos**: Se listan los posts con `tipo_post='evento'` ordenados por fecha.
*   **Encuestas**: Se muestran las encuestas activas.

## 4. Integración con WhatsApp / Widget

Todo el contenido subido es accesible via API pública para integrarse con bots:
*   **API Pública de Productos**: `GET /api/public/market/<slug>/productos`
*   **API Pública de Posts**: `GET /municipal/posts` (con token de sistema o wrapper público si se requiere).

## 5. Próximos Pasos para Conexión Real

1.  **Backend**: Asegurarse de que el `tenant_slug` sea único y corresponda a un registro en `tenant_profile`.
2.  **Frontend**: El portal usa `/api/market/<slug>/cart` para gestionar el carrito. Asegurar que los usuarios se registren o tengan una sesión anónima (manejado por `pwa_misc.py`).
3.  **Notificaciones**: Para notificaciones push reales, integrar con Firebase o WebSockets en el futuro. Por ahora, el panel de notificaciones es visual.
