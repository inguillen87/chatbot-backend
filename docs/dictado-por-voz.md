# Dictado de mensajes por voz

Estos ejemplos muestran cómo integrar el reconocimiento de voz del navegador para dictar mensajes en el chat.

## Hook `useSpeechRecognition`

```ts
import { useEffect, useRef, useState } from "react";

export default function useSpeechRecognition() {
  const SpeechRecognition =
    (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;

  const recognitionRef = useRef<any>(null);
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const supported = !!SpeechRecognition;

  useEffect(() => {
    if (!supported) return;
    const recognition = new SpeechRecognition();
    recognition.lang = "es-ES";
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onresult = (event: any) => {
      let finalTranscript = "";
      for (let i = event.resultIndex; i < event.results.length; ++i) {
        finalTranscript += event.results[i][0].transcript;
      }
      setTranscript(finalTranscript);
    };

    recognition.onstart = () => setListening(true);
    recognition.onend = () => setListening(false);

    recognitionRef.current = recognition;
  }, [supported, SpeechRecognition]);

  const start = () => recognitionRef.current && recognitionRef.current.start();
  const stop = () => recognitionRef.current && recognitionRef.current.stop();

  return { listening, transcript, start, stop, supported };
}
```

El hook detecta si el navegador soporta `SpeechRecognition` y expone las funciones `start` y `stop` para controlar la escucha.

## `ChatInput` con botón de micrófono

```tsx
import React, { useEffect, useState } from "react";
import AdjuntarArchivo from "./AdjuntarArchivo";
import useSpeechRecognition from "./useSpeechRecognition";

const ChatInput: React.FC<{ onSend?: (msg: string) => void }> = ({ onSend }) => {
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

  const toggleMic = () => (listening ? stop() : start());

  return (
    <div>
      <input value={text} onChange={(e) => setText(e.target.value)} />
      {supported && (
        <button onClick={toggleMic}>{listening ? "Detener" : "Hablar"}</button>
      )}
      <button onClick={handleSend}>Enviar</button>
      <AdjuntarArchivo onUpload={() => {}} />
    </div>
  );
};
```

Cuando el navegador soporta la API, aparecerá un botón que permite activar o desactivar el dictado. El texto transcripto se coloca en el campo de entrada para enviarlo como cualquier mensaje.
