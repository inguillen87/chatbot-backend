# Conocimiento institucional dentro de Chatboc

Este corte conecta un corpus compuesto por documentos y menús al producto, no a un visor externo. El frontend emparejado utiliza el Centro de Implementación existente y el espacio público de la organización. El corpus real de TDF permanece fuera del repositorio; los fixtures son sintéticos.

## Implementación

- Importación con identidad exacta en TenantConfig, canal knowledge y clave reservada institutional_assistant. No requiere una tabla nueva. El JSON normalizado conserva texto canónico, opciones, versiones y evidencia por documento/página, sin rutas privadas del material original.
- La importación queda privada. Publicar/retirar son operaciones explícitas, autorizadas, con revisión previa, bloqueo de la organización, nueva revisión y AuditEvent en el mismo commit. No se altera otra configuración ni la propiedad del tenant.
- GET administrativo para probar la versión privada y GET público sólo cuando se publica. La respuesta entrega nombre institucional y todos los textos de interfaz; ninguna identidad del cliente se hardcodea en React.
- La pregunta natural invoca el selector JSON del modelo existente. El modelo elige cero a tres nodos; el servidor materializa sólo respuestas y citas de la versión vigente. No acepta texto inventado, URLs ni órdenes del modelo. Menús no llaman al proveedor. Se revalida la publicación después de interpretar la pregunta.
- responder_chatboc llama a este servicio para el propietario institucional resuelto. El widget y el canal de WhatsApp que usa ese handler reutilizan el mismo contenido. Adjuntos, acciones operativas explícitas y conversaciones con un flujo operacional vigente conservan sus handlers anteriores. No implica que WhatsApp esté activado.
- Enlaces sólo HTTPS, explícitamente suministrados como referencia revisada y vigentes en su fecha de revisión; no se inventan formularios ni se usa el login como enlace de trámite.

## Autorización y alcance

Las lecturas y escrituras administrativas requieren autenticación real y autorización existente sobre el tenant; no dependen de correo, nombre ni slug aportado en el cuerpo. La publicación es una decisión de administración institucional. Se rechazan orígenes no confiables, ámbitos contradictorios, JSON ambiguo, revisión antigua y estados no íntegros. El modo público es informativo y sólo muestra una versión publicada. No crea trámites, recibe documentación personal, decide prestaciones o envía mensajes proactivos.

La información se persiste como versión compuesta, no como un embedding ni un entrenamiento. Las referencias de hash provienen del corpus preparado; no se certifica autenticidad criptográfica del PDF ni corrección semántica del LLM por validar el formato. La elección del nodo sigue requiriendo evaluación del modelo. La configuración general no expone el corpus privado reservado.

## Validación

Sintaxis Python y diez pruebas de normalización aprobadas localmente. La aceptación HTTP usa la aplicación Flask completa y base descartable; sus resultados deben comprobarse en CI. Incluye la integración con responder_chatboc, importación, pregunta, publicación, revocación, ámbito y recuperación. El proveedor externo se sustituye en las pruebas; no se afirma una conversación real con el modelo.

No se ha ejecutado importación ni publicación en TDF. Vercel devolvió 403 en este turno para el equipo autorizado; no se elude por otra vía. La cuenta de Analía no se cambia ni se repite su prueba bloqueada. MuniControl queda fuera de alcance. Este código no sustituye la necesidad de publicar las revisiones frontend/backend compatibles y verificar el acceso nominal.

## Correcciones de revisión antes de la entrega

Una consulta sin nodos relevantes debe continuar al handler operativo existente, no terminar en el desconocimiento del corpus. Se corrigió esa salida y se conservó la prioridad del catálogo. El guard de contexto usa ahora la constante real del flujo municipal; las acciones explícitas y los procesos activos no llaman al selector. Las respuestas institucionales siguen el postprocesamiento original, incluida la salida de audio cuando el canal ya la solicita. Sus nuevas pruebas sustituyen el servicio externo, no afirman un audio productivo enviado.

Las citas con fragmento ahora necesitan una página incluida en el rango validado. Sólo se deriva el número cuando existe una única página inequívoca; no se acepta página 999 ni una referencia ambigua. Las dos regresiones de ese fallo reprobaron sobre el código anterior. Se mantienen la validación de fuente y el alcance de la organización.
