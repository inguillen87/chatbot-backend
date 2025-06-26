import { useEffect, useRef, useState } from "react";

// Hook que encapsula la API de reconocimiento de voz del navegador
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
