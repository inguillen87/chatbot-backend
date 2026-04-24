# Guía rápida de actualización para la PWA

## Flujo normal de actualización en producción
1. Ejecutar el build y el deploy del frontend. El plugin de PWA genera un nuevo Service Worker (SW) versionado por hashes.
2. El navegador descarga el SW nuevo en segundo plano.
3. Cuando finaliza, la UI muestra el aviso de "Nueva versión disponible". El usuario toca **Actualizar** y la aplicación se recarga en caliente con la versión nueva.

### Configuración recomendada (Vite)
```ts
// vite.config.ts
VitePWA({
  registerType: 'autoUpdate',          // baja el SW nuevo automáticamente
  workbox: { /* tus cachés como ya dejamos */ }
})
```

```ts
// src/pwa.ts
import { registerSW } from 'virtual:pwa-register'

export const setupPWA = () => {
  const updateSW = registerSW({
    onNeedRefresh() {
      // Mostrá un toast/modal con botón "Actualizar"
      // Al hacer click:
      updateSW() // -> manda skipWaiting y recarga
    },
    onOfflineReady() {
      // opcional: “Listo para usar offline”
    }
  })
}
```

```ts
// sw.ts (o sw-extra)
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()))
```

## Cómo forzar una actualización (debug o emergencia)
- **Chrome desktop/móvil**: abrir la PWA → menú ⋮ → *Información de la app* → *Borrar caché* (no borrar almacenamiento si querés preservar el login). Alternativa: DevTools > Application > Service Workers → `Update` y `Skip waiting`.
- **iOS (Safari PWA)**: cerrar la app desde el selector de apps y reabrir. Para un *hard refresh*, abrir el sitio en Safari, tocar ↻ y luego volver a la PWA.

## Buenas prácticas para evitar pérdida de datos
- **Nunca pedir borrar datos salvo emergencia.**
- Exponer `/api/version` y mostrar la versión visible en la UI (por ejemplo en el footer de Admin) para confirmar despliegues. El backend ya publica:

```json
GET /api/version -> {
  "frontend": "2025.10.22-abc123",
  "backend": "2025.10.22-api456"
}
```

- `/api/config` también devuelve `frontendVersion`/`backendVersion` para reutilizarlos en paneles o health-checks.
- Si cambiás esquemas en IndexedDB/localStorage, guardar `APP_SCHEMA_VERSION` y migrar en el arranque.
- Mantener la estrategia Workbox: `NetworkFirst` para `/api/public/*` y `StaleWhileRevalidate` para assets.
- Background Sync garantiza que las colas de envíos pendientes sobreviven a las actualizaciones.

## Checklist de release
1. `pnpm build` (o el comando equivalente) y verificar que se generan `dist/manifest.webmanifest` y `dist/sw.js`.
2. Deploy del frontend (Vercel/Render/Nginx).
3. Abrir la PWA instalada, esperar 10–20 s y confirmar el banner de "Nueva versión disponible".
4. Pulsar **Actualizar** y validar `/api/version` actualizado en la UI.

## Cuándo sí borrar datos
- Cambios drásticos en IndexedDB sin migraciones disponibles.
- Service Worker corrupto imposible de reemplazar.
  - En ese caso: Ajustes del sitio → *Borrar almacenamiento* (avisa que cierra sesión y elimina colas offline).

## Extras de UX
- Botón **Buscar actualizaciones** en `/perfil` que llame a `updateSW()` si hay versión nueva.
- En la sección *About* mostrar: `App 2025.10.22-abc123 · Backend 1.14.7`.

Con esta configuración los despliegues publican versiones nuevas sin pedir a los usuarios borrar cachés ni reinstalar la aplicación.
