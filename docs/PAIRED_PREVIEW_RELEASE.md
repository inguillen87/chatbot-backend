# SS-RUNTIME-PAIR: verificación de una release coordinada

Base: df6c321d (aceptación institucional). No modifica el runtime de Render,
cuentas, esquema, planes, emisores WhatsApp ni el bloqueo de escritores.

## Diagnóstico sustentado en el código

bootstrap_wsgi ya precalienta en segundo plano; gunicorn.conf.py lo inicia al
preparar el worker y Dockerfile.vercel ya compila bytecode. No se vuelve a
presentar esas medidas existentes como mejoras nuevas. El 503 explícito protege
el presupuesto de arranque y evita despachar escrituras antes de estar listo.
No se aumenta esa espera ni se cambia 503 por un falso 200.

El nuevo verificador detecta un frontend de revisión correcta que aún dirige
su API a otro backend. Antes se comprobaban candidatos individuales; eso no
acredita que conformen un par. Comprueba la meta chatboc-build-revision, versión
directa del backend, disponibilidad requerida de DB/Redis, versión A TRAVÉS del
frontend, rechazo de perfil anónimo y versiones nuevamente al finalizar.

## Ejecución

```sh
python -m scripts.verify_paired_preview \
  --frontend-url https://chatboc-r2-preview.vercel.app \
  --backend-url https://api-preview.chatboc.ar \
  --frontend-revision <SHA_EXACTO_40> \
  --backend-revision <SHA_EXACTO_40> \
  --output paired-preview-proof.json
```

Acepta sólo esos hosts QA o candidatos inmutables del proyecto y equipo
conocidos. Rechaza producción, credenciales, puertos, paths, query y fragmentos.
No sigue redirecciones ni lee curlrc; no envía cookies o tokens. Respuestas
acotadas a 64 KiB, intentos y presupuesto global limitados. Sólo reintenta el
contrato explícito application_initializing; no errores genéricos ni terminales.
El reporte contiene estados y tiempos, nunca cuerpos privados o excepciones.

Salida 0: par de versiones comprobado. Salida 1: falla de verificación. Salida 2:
argumentos inválidos. --require-first-attempt produce salida 3 si necesitó
recuperarse del arranque. promotion_authorized y authenticated_acceptance
permanecen falsos: esto no prueba aislamiento de DB, identidad de despliegue,
acceso de un cliente, MFA o entrega de WhatsApp. No mueve aliases automáticamente.

## Evidencia y límites de este corte

18 pruebas focales: candidato mixto, cambio de alias durante verificación,
bootstrap recuperable/terminal, rechazos de orígenes, lectura anónima, respuesta
no JSON/HTML, tamaño, deadline y redacción. Transporte sintético en tests.
La CI importa el helper original verify_migration_runtime sin modificarlo.
El frontend coordinado agrega leases de disponibilidad de 30 segundos y un pin
opcional VITE_EXPECTED_BACKEND_REVISION; la disponibilidad no se cachea de por vida.

La PC del propietario no estuvo conectada y el conector Vercel respondió 403
para el equipo. No se realizó un despliegue ni se retiró el writer fence. Antes
de promover: configurar el par correcto, ejecutar este guard y luego completar
la aceptación institucional autorizada. El 503 real no se declara eliminado.

Referencia pública consultada: documentación Vercel de Python y Fluid compute.
Las mejoras automáticas del runtime Python administrado no se extrapolan al
contenedor Docker de esta aplicación sin medición del despliegue real.
