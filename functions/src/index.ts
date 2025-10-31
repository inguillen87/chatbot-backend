import { onCall } from "firebase-functions/v2/https";
import { setGlobalOptions, logger } from "firebase-functions/v2";
import * as admin from "firebase-admin";
import { v4 as uuidv4 } from "uuid";
import { getStorage as getGcs } from "@google-cloud/storage";

setGlobalOptions({ region: "southamerica-east1", enforceAppCheck: true });

admin.initializeApp();
const db = admin.firestore();
const gcs = getGcs();

function periodNowYYYYMM(): string {
  const d = new Date();
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  return `${y}${m}`;
}

function makeQuotaId(orgId: string, uid: string, period?: string): string {
  return `${orgId}__${uid}__${period ?? periodNowYYYYMM()}`;
}

const DEFAULT_CHAT = 5;   // créditos de texto por período
const DEFAULT_MEDIA = 5;  // créditos de imagen por período

// ---------- consumeChatCredit ----------
// Descuenta 1 crédito de chat; devuelve remaining
export const consumeChatCredit = onCall(async (req) => {
  if (!req.auth) throw new Error("UNAUTHENTICATED");
  const uid = req.auth.uid;
  const orgId = (req.auth.token as any).orgId || (req.data as any)?.orgId;
  if (!orgId) throw new Error("MISSING_ORG");

  const qid = makeQuotaId(orgId, uid);
  const ref = db.collection("quota").doc(qid);

  const remaining = await db.runTransaction(async (tx) => {
    const snap = await tx.get(ref);
    const base = snap.exists ? (snap.data() as admin.firestore.DocumentData) : {
      orgId,
      uid,
      period: periodNowYYYYMM(),
      chatRemaining: DEFAULT_CHAT,
      mediaRemaining: DEFAULT_MEDIA,
      resetAt: admin.firestore.Timestamp.fromDate(
        new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth() + 1, 1))
      ),
      _byFn: true
    };

    if (!snap.exists) tx.set(ref, base, { merge: true });

    const current = (snap.exists ? snap.data()!.chatRemaining : base.chatRemaining) as number;
    if (current <= 0) throw new Error("NO_CREDIT");

    tx.update(ref, { chatRemaining: admin.firestore.FieldValue.increment(-1), _byFn: true });
    return current - 1;
  });

  logger.info("consumeChatCredit", { uid, orgId, remaining });
  return { remaining };
});

// ---------- getSignedUploadUrl ----------
// Valida crédito media, descuenta y devuelve URL firmada para subir a Storage
export const getSignedUploadUrl = onCall(async (req) => {
  if (!req.auth) throw new Error("UNAUTHENTICATED");
  const uid = req.auth.uid;
  const orgId = (req.auth.token as any).orgId || (req.data as any)?.orgId;
  if (!orgId) throw new Error("MISSING_ORG");

  const contentType: string = (req.data as any)?.contentType || "image/webp";
  const projectId = process.env.GCLOUD_PROJECT!;
  const bucketName = `${projectId}.appspot.com`
    .replace(".appspot.com", ".appspot.com"); // cambia si tu bucket es distinto

  // 1) Descontar crédito
  const qid = makeQuotaId(orgId, uid);
  const ref = db.collection("quota").doc(qid);

  await db.runTransaction(async (tx) => {
    const snap = await tx.get(ref);
    const base = snap.exists ? (snap.data() as admin.firestore.DocumentData) : {
      orgId,
      uid,
      period: periodNowYYYYMM(),
      chatRemaining: DEFAULT_CHAT,
      mediaRemaining: DEFAULT_MEDIA,
      resetAt: admin.firestore.Timestamp.fromDate(
        new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth() + 1, 1))
      ),
      _byFn: true
    };
    if (!snap.exists) tx.set(ref, base, { merge: true });

    const current = (snap.exists ? snap.data()!.mediaRemaining : base.mediaRemaining) as number;
    if (current <= 0) throw new Error("NO_MEDIA_CREDIT");

    tx.update(ref, { mediaRemaining: admin.firestore.FieldValue.increment(-1), _byFn: true });
  });

  // 2) Generar URL firmada corta
  const fileName = `uploads/${uid}/${uuidv4()}`;
  const bucket = gcs.bucket(bucketName);
  const file = bucket.file(fileName);

  const [url] = await file.getSignedUrl({
    version: "v4",
    action: "write",
    expires: Date.now() + 5 * 60 * 1000,
    contentType
  });

  return { uploadUrl: url, path: fileName, contentType };
});
