# Verificación de WhatsApp y SMS

Esta guía resume los pasos para comprobar que el envío de notificaciones por WhatsApp y SMS funciona correctamente.

1. **Credenciales de Twilio**
   - Asegúrate de que las variables `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` y `TWILIO_WHATSAPP_NUMBER` estén definidas en el entorno donde corre el backend.
   - Si utilizas plantillas de WhatsApp, también es necesario `TWILIO_WHATSAPP_CONTENT_SID`.

2. **Probar el envío manualmente**
   - Desde un intérprete de Python puedes enviar un mensaje de prueba:
     ```python
     from twilio.rest import Client
     client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
     msg = client.messages.create(
         body="Prueba de SMS", from_=TWILIO_PHONE_NUMBER, to="+5491111111111"
     )
     print("SID:", msg.sid)
     ```
   - Para WhatsApp usa `from_=TWILIO_WHATSAPP_NUMBER` y `to="whatsapp:+5491111111111"`.

3. **Consultar el estado de un mensaje**
   - Con el `sid` devuelto puedes verificar si fue entregado:
     ```python
     mensaje = client.messages(msg.sid).fetch()
     print(mensaje.status)
     ```
   - También puedes revisar el historial desde la consola de Twilio.

4. **Revisar los logs del backend**
   - Los módulos `email_service.py` y `municipios.py` registran el `SID` cuando se envía cada notificación.
   - Revisa la carpeta `logs/` o la salida del servidor para confirmar que no haya errores.

Con estos pasos deberías poder confirmar que la integración con Twilio para SMS y WhatsApp funciona y que las plantillas aprobadas se están utilizando correctamente.
