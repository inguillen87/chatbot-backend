import React, { useEffect, useState } from "react";
import AdjuntarArchivo from "./AdjuntarArchivo";
import useSpeechRecognition from "./useSpeechRecognition";
import ListaArchivosAdjuntos from "./ListaArchivosAdjuntos"; // Para mostrar archivos seleccionados
import { Button } from "@/components/ui/button"; // Asumiendo que usas Button

// Definición de la interfaz ArchivoInfo para ListaArchivosAdjuntos
// Esto podría estar en un archivo de tipos compartido si se usa en más lugares.
interface ArchivoInfo {
  name: string;
  mimeType?: string;
  url: string; // Para ListaArchivosAdjuntos, pero para previsualización antes de subir, podría ser un ObjectURL
  size?: number;
  // Podríamos añadir 'fileObject' si necesitamos el objeto File completo para alguna operación
}


type Props = {
  onSend?: (message: string, files?: File[]) => void; // Modificado para incluir archivos
  // onFileUploaded ya no es tan relevante aquí si subimos con el mensaje.
  // Se podría tener una prop para notificar subidas individuales si ese fuera el flujo.
};

const ChatInput: React.FC<Props> = ({ onSend }) => {
  const [text, setText] = useState("");
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const { transcript, listening, start, stop, supported } = useSpeechRecognition();
  const [isRegistered, setIsRegistered] = useState(false);

  useEffect(() => {
    if (transcript) {
      setText(transcript);
    }
    const token = localStorage.getItem("token");
    setIsRegistered(!!token);
  }, [transcript]);

  const handleFilesSelected = async (files: File[]) => {
    if (files.length > 0) {
      const file = files[0];
      const formData = new FormData();
      formData.append("archivos", file);

      try {
        const response = await fetch("/archivos/subir", {
          method: "POST",
          body: formData,
          headers: {
            Authorization: `Bearer ${localStorage.getItem("token")}`,
          },
        });

        if (response.ok) {
          const result = await response.json();
          // Aquí puedes manejar la respuesta, por ejemplo, mostrando una vista previa de la imagen
          console.log("Image uploaded successfully:", result);
          // Opcional: podrías querer añadir la URL de la imagen a un estado para mostrarla
        } else {
          console.error("Error uploading image");
        }
      } catch (error) {
        console.error("Error uploading image:", error);
      }
    }
  };

  const handleRemoveFile = (fileNameToRemove: string) => {
    setSelectedFiles(prevFiles => prevFiles.filter(file => file.name !== fileNameToRemove));
  };

  const handleSend = () => {
    const msg = text.trim();
    if (msg || selectedFiles.length > 0) { // Enviar si hay mensaje o archivos
      onSend?.(msg, selectedFiles);
      setText("");
      setSelectedFiles([]); // Limpiar archivos después de enviar
    }
  };

  const toggleMic = () => {
    if (listening) {
      stop();
    } else {
      start();
    }
  };

  // Transformar Files a ArchivoInfo para ListaArchivosAdjuntos
  // Para la URL, podríamos usar URL.createObjectURL(file) si quisiéramos previsualizar imágenes,
  // pero para una lista simple, el nombre y tipo son suficientes.
  // ListaArchivosAdjuntos espera una 'url', podemos pasar un '#' o algo simbólico.
  const archivosParaLista: ArchivoInfo[] = selectedFiles.map(file => ({
    name: file.name,
    mimeType: file.type,
    size: file.size,
    url: "#", // Placeholder, ya que estos archivos aún no están subidos
  }));

  return (
    <div className="p-4 border-t">
      {selectedFiles.length > 0 && (
        <div className="mb-2">
          <ListaArchivosAdjuntos archivos={archivosParaLista} onEliminarArchivo={handleRemoveFile} />
        </div>
      )}
      <div className="flex items-center gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSend()}
          placeholder="Escribe un mensaje..."
          className="flex-grow p-2 border rounded" // Estilo básico
        />
        {supported && (
          <Button onClick={toggleMic} variant="outline" size="icon" aria-label={listening ? "Detener dictado" : "Iniciar dictado"}>
            {listening ? "🎙️ Detener" : "🎤"}
          </Button>
        )}
        {isRegistered && <AdjuntarArchivo onFilesSelected={handleFilesSelected} />}
        <Button onClick={handleSend} variant="default">Enviar</Button>
      </div>
    </div>
  );
};

export default ChatInput;
