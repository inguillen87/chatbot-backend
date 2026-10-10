# Reconciliación segura de ownership de sender WhatsApp

`scripts/reconcile_tenant_whatsapp_sender.py` prepara o repara un único
`ProviderSender` oficial, asociado a un tenant y a su conexión Twilio de
producción. No llama a Twilio, no envía mensajes, no crea conexiones, no borra
filas y no modifica otros tenants.

El comando es **dry-run por defecto**. Valida antes de proponer cambios:

- slug exacto y tenant activo con un único owner;
- teléfono oficial E.164 y un único `WhatsappNumero` activo del mismo owner;
- una única `ProviderConnection` `twilio/whatsapp/production` con
  `credentials_ref`;
- ausencia de reclamos del mismo teléfono, sender SID o messaging service SID
  en otro tenant;
- ausencia de otro sender productivo listo que vuelva ambigua la resolución;
- metadata Twilio opcional leída solamente desde nombres de variables de
  entorno explícitos.

La salida JSON no contiene URL de base, credenciales, teléfono ni SID
completos. Solo publica fingerprints SHA-256 y `last4`. El `plan_sha256` enlaza
el estado revisado, la base destino, el tenant y la metadata externa.

## 1. Dry-run obligatorio

PowerShell:

```powershell
$env:MIGRATIONS_DATABASE_URL = '<postgresql TLS URL>'
$env:JUNIN_TWILIO_SENDER_SID = '<XE...>' # opcional, solo con evidencia Twilio
$env:JUNIN_TWILIO_MESSAGING_SERVICE_SID = '<MG...>' # opcional

& '<python.exe>' scripts/reconcile_tenant_whatsapp_sender.py `
  --tenant-slug 'junin' `
  --official-phone '<E.164 verificado>' `
  --provider 'twilio' `
  --environment 'production' `
  --database-environment-variable 'MIGRATIONS_DATABASE_URL' `
  --sender-sid-environment-variable 'JUNIN_TWILIO_SENDER_SID' `
  --messaging-service-sid-environment-variable 'JUNIN_TWILIO_MESSAGING_SERVICE_SID'
```

Omitir ambos parámetros `*-sid-environment-variable` si esos identificadores
todavía no fueron certificados. Nunca pasar sus valores por la línea de
comandos.

Archivar la salida privada y revisar `conflicts: 0`, las acciones propuestas y
el `plan_sha256`. Un dry-run exitoso **no certifica Twilio live**.

## 2. Apply controlado

Solo durante una ventana aprobada, con evidencia read-only reciente de Twilio
y el digest exacto del dry-run:

```powershell
& '<python.exe>' scripts/reconcile_tenant_whatsapp_sender.py `
  --tenant-slug 'junin' `
  --official-phone '<E.164 verificado>' `
  --provider 'twilio' `
  --environment 'production' `
  --database-environment-variable 'MIGRATIONS_DATABASE_URL' `
  --sender-sid-environment-variable 'JUNIN_TWILIO_SENDER_SID' `
  --messaging-service-sid-environment-variable 'JUNIN_TWILIO_MESSAGING_SERVICE_SID' `
  --twilio-read-only-evidence-id '<evidence-id>' `
  --cutover-window-evidence-id '<window-id>' `
  --approved-plan-sha256 '<dry-run-plan-sha256>' `
  --apply
```

`--apply` exige PostgreSQL con TLS, una transacción `SERIALIZABLE`, advisory
lock transaccional y locks únicamente sobre las filas objetivo. Relee y valida
todo bajo el lock; cualquier drift del plan bloquea antes de mutar. Crea o
actualiza solamente el `ProviderSender` oficial y alinea
`TenantProfile.whatsapp_sender_id`. Una segunda ejecución aprobada sobre el
nuevo plan devuelve `already_reconciled` sin escrituras.

Un sender nuevo queda `draft` o `registered`; el comando nunca lo declara
`active`, `approved`, `connected` u `online` sin la certificación posterior del
proveedor.

Después del apply siguen pendientes los gates externos: canario entrante y
saliente, callback de estado, worker/outbox, observabilidad y rollback. Este
comando no reemplaza esas pruebas ni autoriza apagar Render.
