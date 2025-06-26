# Configurar globito de atención del widget

El backend expone el endpoint `/widget/attention` que devuelve un mensaje breve para mostrar en la burbuja del chat. Para que aparezcan frases rotativas debes definir la variable de entorno `ATTENTION_BUBBLE_CHOICES` con las opciones deseadas separadas por el caracter `|`.

```bash
ATTENTION_BUBBLE_CHOICES="Hola|¿Necesitas ayuda?|¿Querés hacer un reclamo?"
```

Si no configuras esa variable se tomará `ATTENTION_BUBBLE_TEXT` (o su valor por defecto `"¡Hola! ¿Necesitas ayuda?"`).

Desde el frontend basta con hacer un `GET /widget/attention` periódicamente y actualizar el texto de la burbuja con el campo `mensaje` recibido en la respuesta.
