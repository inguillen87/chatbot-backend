import React, { useRef, useState } from "react";
import { Button } from "@/components/ui/button";

const MAX_FILES = 10;
const MAX_FILE_SIZE_MB = 10; // Per file
const ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'pdf', 'xlsx', 'xls', 'csv', 'docx', 'txt', 'json'];

interface AdjuntarArchivoProps {
  onFilesSelected: (files: File[]) => void;
  disabled?: boolean;
}

const AdjuntarArchivo: React.FC<AdjuntarArchivoProps> = ({
  onFilesSelected,
  disabled,
}) => {
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string>("");

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setError(""); // Reset error on new selection
    const selectedFiles = e.target.files;
    if (!selectedFiles || selectedFiles.length === 0) {
      return;
    }

    if (selectedFiles.length > MAX_FILES) {
      setError(`Puedes seleccionar un máximo de ${MAX_FILES} archivos.`);
      // Limpiar el valor del input para permitir nueva selección
      if (inputRef.current) {
        inputRef.current.value = "";
      }
      return;
    }

    const validFiles: File[] = [];
    const currentErrors: string[] = [];

    for (let i = 0; i < selectedFiles.length; i++) {
      const file = selectedFiles[i];
      const ext = file.name.split('.').pop()?.toLowerCase();

      if (!ext || !ALLOWED_EXTENSIONS.includes(ext)) {
        currentErrors.push(`Archivo "${file.name}": formato no permitido. Permitidos: ${ALLOWED_EXTENSIONS.join(', ')}`);
        continue;
      }
      if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
        currentErrors.push(`Archivo "${file.name}" demasiado grande (máx ${MAX_FILE_SIZE_MB}MB)`);
        continue;
      }
      validFiles.push(file);
    }

    if (currentErrors.length > 0) {
      setError(currentErrors.join("\n"));
      // Limpiar el valor del input para permitir nueva selección si hay errores
      // y no pasar archivos parcialmente válidos si alguno falló, para simplificar.
      // Opcionalmente, se podría llamar a onFilesSelected solo con validFiles.
      if (inputRef.current) {
        inputRef.current.value = "";
      }
      onFilesSelected([]); // Notifica que no hay archivos válidos o la selección fue mala
      return;
    }

    setError("");
    onFilesSelected(validFiles);

    // Limpiar el valor del input para permitir subir los mismos archivos de nuevo si es necesario
    // o si el usuario quiere cambiar su selección después de un error previo.
    if (inputRef.current) {
      inputRef.current.value = "";
    }
  };

  return (
    <div>
      <Button
        onClick={() => inputRef.current?.click()}
        variant="outline"
        disabled={disabled}
      >
        📎 Adjuntar archivos
      </Button>
      <input
        ref={inputRef}
        type="file"
        multiple // Permitir selección múltiple
        accept={ALLOWED_EXTENSIONS.map(e => '.' + e).join(',')}
        style={{ display: "none" }}
        onChange={handleFileChange}
        disabled={disabled}
      />
      {error && (
        <div style={{ color: "red", marginTop: "8px", whiteSpace: "pre-line" }}>
          {error}
        </div>
      )}
    </div>
  );
};

export default AdjuntarArchivo;
