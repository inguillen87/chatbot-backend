# Selección asistida sin habilitaciones silenciosas

Continúa #2790 sobre 41614510 y frontend #1756 sobre c706b1d. No es otra aplicación
por cliente ni una variante paralela. El contrato de selección v1 recibe un campo
opcional selection_assistance con mensajes del backend. No cambia el registro
persistido, la revisión esperada, el plan Full, el permiso o los canales activos.

El editor coordinado permite solicitar una función y revisar sus prerrequisitos
como conjunto. Al retirar una función, muestra los dependientes que se retirarían.
Aceptar modifica sólo el borrador; el guardado conserva su confirmación separada.
Sin contrato de asistencia válido, el frontend mantiene el comportamiento anterior.
El backend sigue rechazando selections incompletas, desconocidas o no autorizadas.

Se agregan seis pruebas de contrato sin escrituras y dos escenarios de HTTP real
que se ejecutan junto a los 21 anteriores mediante el helper existente. La CI
existente de PostgreSQL conserva carreras/rollback y se amplía, no se duplica.
No hay esquemas/dependencias nuevas o llamadas a Meta/Twilio, Render o Neon.
Los resultados se registran en el PR al finalizar, sin inferirlos de este texto.
Publicación y aceptación de proveedor quedan separadas: este corte no las realiza.
