# Notificaciones de tickets (email, SMS y WhatsApp)

Este backend dispara notificaciones en eventos clave de los tickets. A
continuación se detallan los endpoints involucrados, los servicios de envío y
la configuración requerida para cada canal.

## Endpoints y flujos

### Cambio de estado
- **Endpoint:** `PUT /tickets/<tipo>/<id>/estado`
- **Payload:** `{ "estado": "en_proceso" | "cerrado" | "resuelto" | ... }`
- **Canales:** Email, SMS y WhatsApp (solo municipios)
- **Lógica:** la ruta utiliza `services.notification_dispatcher.dispatch_ticket_state_change`
  para construir el mensaje y llamar a `enviar_email_ticket_novedad`,
  `enviar_sms_ticket_novedad` y `enviar_whatsapp_ticket_novedad`. El resultado de
  cada canal se deja en logs para diagnóstico.

### Responder a un ticket
- **Endpoint:** `POST /tickets/<tipo>/<id>/respuesta`
- **Payload:** puede incluir `comentario` y archivos adjuntos
- **Canales:** Email, SMS y WhatsApp (adjuntos solo si el canal devuelve éxito)
- **Lógica:** tras guardar el comentario/archivos, la ruta llama a
  `dispatch_ticket_update` con el comentario reciente. Si el envío por WhatsApp
  fue exitoso y hay adjuntos, se reintenta con `archivos_adjuntos` habilitados.

### Creación de ticket
- **Servicio:** `services.ticket_service._notificar_ticket_por_email`
- **Canales:** Email al admin (`enviar_email_ticket_admin`) y al ciudadano
  (`enviar_email_ticket_cliente`).

### Comentarios de ciudadanos
- **Servicio:** `services.ticket_service.crear_comentario`
- **Canales:**
  - Comentario de admin → notifica al ciudadano (email/SMS/WhatsApp).
  - Comentario de ciudadano → notifica al admin (email).

## Configuración requerida

### Email (SMTP)
Variables en `.env` o configuración de la app:
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
- `MAIL_FROM_ADDRESS`, `MAIL_FROM_NAME`
- Opcionales: `SMTP_USE_TLS` (true/false), `SMTP_USE_SSL` (true/false)

Si falta host/puerto/remitente, el envío se omite y se registra un error.

### SMS (Twilio)
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`

Si falta alguna credencial se registra un error y no se envía el SMS.

### WhatsApp (Twilio)
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_NUMBER` (ej. `whatsapp:+14155238886`)

El canal solo se intenta para tickets de tipo `municipio`.

## Ejemplos de uso

### Cambio de estado
```bash
curl -X PUT "https://api.chatboc.ar/tickets/municipio/123/estado" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"estado": "en_proceso"}'
```

### Responder con comentario
```bash
curl -X POST "https://api.chatboc.ar/tickets/municipio/123/respuesta" \
  -H "Authorization: Bearer <token>" \
  -F "comentario=Seguimos trabajando" \
  -F "archivos_adjuntos=@foto.jpg"
```

## Monitoreo

Cada llamada al dispatcher deja trazas con el resultado por canal (email/SMS/
WhatsApp). En caso de fallas de configuración o errores de red, se registran en
los logs de la aplicación para su diagnóstico.

