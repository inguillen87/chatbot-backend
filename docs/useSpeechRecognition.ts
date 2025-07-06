import { useEffect, useRef, useState } from "react";

// Hook que encapsula la API de reconocimiento de voz del navegador
export default function useSpeechRecognition() {
  const SpeechRecognition =
    (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;

  const recognitionRef = useRef<any>(null);
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const supported = !!SpeechRecognition;

  const initializeRecognition = () => {
    if (!supported || !SpeechRecognition) return null;
    const recognition = new SpeechRecognition();
    recognition.lang = "es-ES";
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onresult = (event: any) => {
      let finalTranscript = "";
      for (let i = event.resultIndex; i < event.results.length; ++i) {
        finalTranscript += event.results[i][0].transcript;
      }
      setTranscript(finalTranscript); // Actualiza el transcript aquí
    };

    recognition.onstart = () => {
      setListening(true);
      setTranscript(""); // Limpiar transcripción anterior al iniciar
    };
    recognition.onend = () => {
      setListening(false);
      // No limpiar transcript aquí para que el usuario pueda verlo
    };
    recognition.onerror = (event: any) => {
      console.error("Speech recognition error", event.error);
      // Podrías querer manejar errores específicos aquí, como 'not-allowed'
      setListening(false);
    };
    return recognition;
  };

  useEffect(() => {
    // Este useEffect ahora solo sirve para limpiar al desmontar, si es necesario.
    // O para manejar cambios en 'supported' o 'SpeechRecognition' si fueran dinámicos.
    return () => {
      if (recognitionRef.current) {
        recognitionRef.current.stop(); // Asegura que se detenga si el componente se desmonta
      }
    };
  }, []); // Dependencias vacías, ya que la inicialización es ahora bajo demanda.

  const start = () => {
    if (!recognitionRef.current) {
      recognitionRef.current = initializeRecognition();
    }
    if (recognitionRef.current) {
      try {
        recognitionRef.current.start();
      } catch (e) {
        console.error("Error starting speech recognition:", e);
        // Si el error es por AudioContext no permitido, esto podría ser un lugar para notificar al usuario.
      }
    }
  };

  const stop = () => {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
    }
  };

  // Limpiar transcript cuando listening cambia de true a false (al detener)
  // Pero solo si queremos que el transcript se borre automáticamente.
  // Actualmente, el transcript persiste hasta la próxima vez que se inicia.
  // Esto parece razonable.

  return { listening, transcript, start, stop, supported };
}
