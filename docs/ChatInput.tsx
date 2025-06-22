import React, { useState } from "react";
import AdjuntarArchivo from "./AdjuntarArchivo";

// Dummy handler if none is provided
const onFileUploaded = (file: File) => {
  console.log("Archivo subido:", file);
};

type Props = {
  onSend?: (message: string) => void;
  onFileUploaded?: (file: File) => void;
};

const ChatInput: React.FC<Props> = ({ onSend, onFileUploaded: onUploadProp }) => {
  const [text, setText] = useState("");

  const handleSend = () => {
    if (text.trim()) {
      onSend?.(text);
      setText("");
    }
  };

  // Use provided handler or fall back to dummy
  const handleFileUploaded = onUploadProp || onFileUploaded;

  return (
    <div>
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Escribe un mensaje..."
      />
      <button onClick={handleSend}>Enviar</button>
      <AdjuntarArchivo onUpload={handleFileUploaded} />
    </div>
  );
};

export default ChatInput;
