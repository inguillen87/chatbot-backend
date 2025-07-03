import React, { useRef, useState } from "react";
import { Button } from "@/components/ui/button";

const MAX_FILE_SIZE_MB = 10;
const ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'pdf', 'xlsx', 'xls', 'csv', 'docx', 'txt'];

interface AdjuntarArchivoProps {
  onUpload: (fileData: any) => void;
  pymeTicketId?: string | number;
  municipioTicketId?: string | number;
  sessionId?: string; // Para asociar a una sesión de chat si es necesario
}

const AdjuntarArchivo: React.FC<AdjuntarArchivoProps> = ({
  onUpload,
  pymeTicketId,
  municipioTicketId,
  sessionId
}) => {
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string>("");
  const [uploading, setUploading] = useState<boolean>(false);

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    const ext = file.name.split('.').pop()?.toLowerCase();
    if (!ext || !ALLOWED_EXTENSIONS.includes(ext)) {
      setError("Formato no permitido. Permitidos: " + ALLOWED_EXTENSIONS.join(', '));
      return;
    }
    if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      setError(`Archivo demasiado grande (máx ${MAX_FILE_SIZE_MB}MB)`);
      return;
    }
    setError("");
    setUploading(true);

    const formData = new FormData();
    formData.append('archivo', file);

    // Añadir IDs si están presentes
    if (pymeTicketId) {
      formData.append('pyme_ticket_id', String(pymeTicketId));
    }
    if (municipioTicketId) {
      formData.append('municipio_ticket_id', String(municipioTicketId));
    }
    if (sessionId) {
      formData.append('session_id', sessionId);
    }
    // El backend también espera 'tipo' (ej. "chat", "ticket_adjunto"),
    // podría ser otra prop o inferirse. Por ahora, el backend le da un default.

    try {
      const resp = await fetch('/archivos/subir', { // Corregido: /api/archivos/subir -> /archivos/subir
        method: 'POST',
        body: formData,
        credentials: 'include',
        // Es importante que el backend maneje la autenticación (ej. vía cookie HttpOnly)
        // y que las cabeceras CORS estén bien configuradas en el backend.
      });
      const data = await resp.json();
      if (resp.ok) {
        onUpload && onUpload(data); // data debería incluir: { mensaje, filename, name, mimeType, size, url }
      } else {
        setError(data.error || `Error del servidor: ${resp.status}`);
      }
    } catch (err) {
      console.error("Error al subir archivo:", err);
      setError("Error de conexión o al procesar la subida.");
    } finally {
      setUploading(false);
      // Limpiar el valor del input para permitir subir el mismo archivo de nuevo si hay error
      if (inputRef.current) {
        inputRef.current.value = "";
      }
    }
  };

  return (
    <div>
      <Button
        onClick={() => inputRef.current?.click()}
        variant="outline"
        disabled={uploading}
      >
        {uploading ? "Subiendo..." : "📎 Adjuntar archivo"}
      </Button>
      <input
        ref={inputRef}
        type="file"
        accept={ALLOWED_EXTENSIONS.map(e => '.' + e).join(',')}
        style={{ display: "none" }}
        onChange={handleFileChange}
        disabled={uploading}
      />
      {error && <div style={{ color: "red", marginTop: "8px" }}>{error}</div>}
    </div>
  );
};

export default AdjuntarArchivo;
