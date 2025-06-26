import React, { useEffect, useState } from "react";
import AdjuntarArchivo from "./AdjuntarArchivo";
import useSpeechRecognition from "./useSpeechRecognition";

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
  const { transcript, listening, start, stop, supported } = useSpeechRecognition();

  useEffect(() => {
    if (transcript) {
      setText(transcript);
    }
  }, [transcript]);

  const handleSend = () => {
    if (text.trim()) {
      onSend?.(text);
      setText("");
    }
  };

  // Use provided handler or fall back to dummy
  const handleFileUploaded = onUploadProp || onFileUploaded;

  const toggleMic = () => {
    if (listening) {
      stop();
    } else {
      start();
    }
  };

  return (
    <div>
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Escribe un mensaje..."
      />
      {supported && (
        <button onClick={toggleMic}>
          {listening ? "Detener" : "Hablar"}
        </button>
      )}
      <button onClick={handleSend}>Enviar</button>
      <AdjuntarArchivo onUpload={handleFileUploaded} />
    </div>
  );
};

export default ChatInput;
