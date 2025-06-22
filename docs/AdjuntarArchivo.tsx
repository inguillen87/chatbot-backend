import React, { useRef, useState } from "react";
import { Button } from "@/components/ui/button";

const MAX_FILE_SIZE_MB = 10;
const ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'pdf', 'xlsx', 'xls', 'csv', 'docx', 'txt'];

const AdjuntarArchivo = ({ onUpload }) => {
  const inputRef = useRef(null);
  const [error, setError] = useState("");

  const handleFileChange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const ext = file.name.split('.').pop().toLowerCase();
    if (!ALLOWED_EXTENSIONS.includes(ext)) {
      setError("Formato no permitido");
      return;
    }
    if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      setError("Archivo demasiado grande (máx 10MB)");
      return;
    }
    setError("");
    const formData = new FormData();
    formData.append('archivo', file);
    try {
      const resp = await fetch('/api/archivos/subir', {
        method: 'POST',
        body: formData,
        credentials: 'include',
      });
      const data = await resp.json();
      if (resp.ok) {
        onUpload && onUpload(data);
      } else {
        setError(data.error || "Error al subir archivo");
      }
    } catch (e) {
      setError("Error de conexión");
    }
  };

  return (
    <div>
      <Button onClick={() => inputRef.current?.click()} variant="outline">
        📎 Adjuntar archivo
      </Button>
      <input
        ref={inputRef}
        type="file"
        accept={ALLOWED_EXTENSIONS.map(e => '.' + e).join(',')}
        style={{ display: "none" }}
        onChange={handleFileChange}
      />
      {error && <div style={{ color: "red" }}>{error}</div>}
    </div>
  );
};

export default AdjuntarArchivo;
