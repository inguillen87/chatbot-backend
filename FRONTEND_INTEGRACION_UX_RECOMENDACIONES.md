# Integraciones y Canales - Recomendaciones UX/UI

Estas sugerencias apuntan a que la vista de integraciones y la personalización del widget se sientan “SaaS pro” y más coherentes visualmente.

## Layout y estructura
- Usar una grilla de 2 columnas consistente en desktop: **panel de ajustes** (izquierda) + **vista previa** (derecha). En pantallas medianas, convertir a una sola columna con la vista previa fija al final.
- Mantener la **vista previa sticky** (position: sticky) para que el usuario vea el impacto de los cambios mientras scrollea.
- Alinear cards y evitar corrimientos: aplicar `min-height` y `gap` consistentes entre cards y secciones.

## Vista previa del widget
- Mostrar el widget con **estado real** (abierto/cerrado) y un conmutador “Preview abierto/cerrado”.
- Permitir **rotación de dispositivo** (desktop/tablet/mobile) con toggles, y mostrar el ancho real del widget.
- Agregar un “Modo embed” que muestre el **snippet** y un panel lateral con los atributos activos.
- Para el **preview real**, inyectar el `embed_snippet` desde `/api/public/widget-config` y renderizarlo dentro del iframe/contenedor de la vista previa (no usar mock estático).

## Controles de personalización
- Separar “Apariencia” (colores, tipografía, bordes) de “Contenido” (título, mensaje de bienvenida, CTA).
- Añadir presets visuales (cards con mini-preview).
- Exponer **canales** (WhatsApp/Telegram/Web) con estados claros (habilitado/deshabilitado) y CTA configurable.

## Detalles visuales y microinteracciones
- Agregar **tooltips** con ayuda contextual (por ejemplo, qué hace “borde redondeado”).
- Animaciones suaves en sliders y toggles; no usar saltos bruscos.
- Botón “Guardar cambios” sticky en el pie o en el header de la columna izquierda.

## Estados vacíos y errores
- Para canales sin datos, mostrar un **empty state** con ejemplos y CTA de configuración.
- Validaciones inline con color y texto breve (p. ej., “Ingresá un número válido de WhatsApp”).
