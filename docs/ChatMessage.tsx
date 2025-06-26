import React from "react";

export interface ChatButton {
  text: string;
  action?: string;
}

export interface ChatMessageProps {
  message: {
    id?: string | number;
    text: string;
    isBot?: boolean;
    botones?: ChatButton[];
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
          rounded-2xl shadow-lg px-4 py-3 max-w-[80vw] md:max-w-md
        `}
      >
        <div className="flex items-center gap-2">
          {isBot && (
            <img src="/logo-icon-blue.png" alt="Bot" className="w-6 h-6" />
          )}
          <span className="font-medium text-base break-words">{message.text}</span>
        </div>
        {Array.isArray(message.botones) && message.botones.length > 0 && (
          <div className="flex flex-wrap gap-2 pt-1">
            {message.botones.map((btn, idx) => (
              <button
                key={btn.action || idx}
                onClick={() => { /* tu lógica */ }}
                className="rounded-xl px-4 py-1 text-sm font-semibold bg-white text-blue-900 border border-blue-300 hover:bg-blue-100 transition"
              >
                {btn.text}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

