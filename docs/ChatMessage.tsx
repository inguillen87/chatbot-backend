import React from "react";
import "./chat.css";

export interface ChatButton {
  text: string;
  action?: string;
  url?: string;
}

export interface ChatMessageProps {
  message: {
    id?: string | number;
    text: string;
    isBot?: boolean;
    botones?: ChatButton[];
    imageUrl?: string;
  };
}

export default function ChatMessage({ message }: ChatMessageProps) {
  const isBot = message.isBot;

  return (
    <div className={`flex ${isBot ? "justify-start" : "justify-end"} w-full mb-3`}>
      <div
        className={`
          flex flex-col gap-2
          ${isBot
            ? "bg-[#131c2b] text-white border border-[#195fa4]"
            : "bg-gradient-to-br from-blue-600 to-blue-800 text-white"
          }
          rounded-2xl shadow-lg px-4 py-3 max-w-[85vw] md:max-w-lg
        `}
      >
        <div className="flex items-center gap-2">
          {isBot ? (
            <img src="/logo-icon-blue.png" alt="Bot" className="w-6 h-6" />
          ) : (
            <img
              src="./images/user-avatar.svg"
              alt="Cliente"
              className="w-6 h-6 user-avatar"
            />
          )}
          <span
            className="font-medium text-base break-words"
            dangerouslySetInnerHTML={{ __html: message.text }}
          />
        </div>
        {message.imageUrl && (
          <div className="mt-2">
            <img src={message.imageUrl} alt="Uploaded" className="rounded-lg max-w-full h-auto" />
          </div>
        )}
        {Array.isArray(message.botones) && message.botones.length > 0 && (
          <div className="flex flex-wrap gap-2 pt-1">
            {message.botones.map((btn, idx) => {
              if (btn.url) {
                // Renderizar como un enlace <a> estilizado como botón
                return (
                  <a
                    key={btn.action || btn.url || idx}
                    href={btn.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="rounded-xl px-4 py-1 text-sm font-semibold bg-white text-blue-900 border border-blue-300 hover:bg-blue-100 transition no-underline"
                    //onClick={(e) => { e.preventDefault(); window.open(btn.url, '_blank'); }} // Alternativa si se prefiere manejar con JS
                  >
                    {btn.text}
                  </a>
                );
              } else {
                // Renderizar como botón normal con onClick
                return (
                  <button
                    key={btn.action || idx}
                    onClick={() => {
                      // Lógica existente para manejar acciones de botones que no son URL
                      // Esto usualmente implicaría enviar un mensaje de vuelta o activar una función.
                      // Por ahora, si no hay una función específica en el contexto para llamar,
                      // podríamos enviar un mensaje de texto con la acción.
                      // Esta parte debe ser consistente con cómo se manejan las acciones de botones en la app.
                      // Ejemplo: si se espera que envíe un mensaje:
                      // if (typeof window !== 'undefined' && window.sendChatMessage) {
                      //   window.sendChatMessage(btn.action || btn.text, {isUserMessage: true, action: btn.action});
                      // }
                      // O si hay un manejador de acciones específico:
                      // handleButtonAction(btn.action || btn.text);
                      console.log("Botón clickeado (sin URL):", btn.text, "Acción:", btn.action);
                    }}
                    className="rounded-xl px-4 py-1 text-sm font-semibold bg-white text-blue-900 border border-blue-300 hover:bg-blue-100 transition"
                  >
                    {btn.text}
                  </button>
                );
              }
            })}
          </div>
        )}
      </div>
    </div>
  );
}

