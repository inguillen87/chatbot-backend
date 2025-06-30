import React from "react";

export interface TypingIndicatorProps {
  isUser?: boolean;
}

const TypingIndicator: React.FC<TypingIndicatorProps> = ({ isUser }) => {
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} w-full mb-3`}>
      <div className="flex items-center gap-2">
        {isUser ? (
          <img src="./images/user-avatar.svg" alt="Cliente" className="w-6 h-6 user-avatar" />
        ) : (
          <img src="/logo-icon-blue.png" alt="Bot" className="w-6 h-6" />
        )}
        <div className="loading-dots flex gap-1 text-xl">
          <span>.</span>
          <span>.</span>
          <span>.</span>
        </div>
      </div>
    </div>
  );
};

export default TypingIndicator;
