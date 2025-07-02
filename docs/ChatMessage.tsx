import React from "react";
import "./chat.css";

export interface ChatButton {
  text: string;
  action?: string; // The action associated with the button (e.g., "hacer_reclamo", "login")
}

export interface ChatMessageProps {
  message: {
    id?: string | number;
    text: string;
    isBot?: boolean;
    botones?: ChatButton[];
  };
  onButtonClick?: (action: string, text: string) => void; // Callback when a button is clicked
}

export default function ChatMessage({ message, onButtonClick }: ChatMessageProps) {
  const isBot = message.isBot;

  const handleButtonClick = (button: ChatButton) => {
    if (onButtonClick) {
      // Prefer action if available, otherwise use button text as a fallback action
      onButtonClick(button.action || button.text, button.text);
    } else {
      // Fallback if no handler is provided (though it should be)
      console.warn("ChatMessage: onButtonClick handler not provided. Clicked:", button);
    }
  };

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
        {Array.isArray(message.botones) && message.botones.length > 0 && (
          <div className="flex flex-wrap gap-2 pt-1">
            {message.botones.map((btn, idx) => (
              <button
                key={btn.action || idx}
                onClick={() => handleButtonClick(btn)}
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

