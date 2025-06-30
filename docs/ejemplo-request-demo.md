# Ejemplo de llamada en modo demo

Para probar el endpoint `/ask/municipio` de forma anónima debes generar un identificador único (por ejemplo con `crypto.randomUUID()` en el navegador) y enviarlo en el encabezado `Anon-Id`.  No se requiere token si el servicio está en *demo mode* pero el `Anon-Id` es obligatorio para que la sesión quede identificada.

```javascript
const anonId = localStorage.getItem('anon-id') || crypto.randomUUID();
localStorage.setItem('anon-id', anonId);

fetch('https://api.chatboc.ar/ask/municipio', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Anon-Id': anonId
  },
  body: JSON.stringify({
    pregunta: '¿Dónde puedo pagar mis impuestos?',
    tipo_chat: 'municipio'
  })
})
  .then(r => r.json())
  .then(console.log)
  .catch(console.error);
```

Si el widget está embebido en otro dominio, asegúrate de que ese dominio figure en `CORS_ALLOWED_ORIGINS` o define `CORS_ALLOWED_ORIGINS=*` durante las pruebas para aceptar cualquier origen.
