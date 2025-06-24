# Error "window.google.maps.Map is not a constructor"

Este mensaje aparece cuando el script de Google Maps aún no terminó de
cargar al momento de crear la instancia del mapa. Para solucionarlo
revisa lo siguiente:

1. Carga la librería con tu API key y utiliza el parámetro `callback`
   para ejecutar la inicialización solo cuando Maps esté listo:

   ```html
   <script async
           src="https://maps.googleapis.com/maps/api/js?key=TU_API_KEY&callback=initMap"></script>
   <script>
     function initMap() {
       new window.google.maps.Map(document.getElementById('map'), options);
     }
   </script>
   ```

2. Comprueba en la pestaña de red que el script se descarga sin errores
   (códigos 4xx/5xx o bloqueos de CORS impedirán su ejecución).
3. Evita incluir la librería más de una vez, ya que puede sobrescribir
   el objeto `window.google`.

Si sigues viendo el error, verifica que ninguna extensión del navegador o
política de contenido esté bloqueando la carga del script.
