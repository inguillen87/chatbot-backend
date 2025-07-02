import React from "react";
import ChatMessage, { ChatMessageProps as SingleChatMessageProps, ChatButton } from "./ChatMessage"; // Renamed to avoid conflict & import ChatButton
import TypingIndicator from "./TypingIndicator";

export interface ChatPanelProps {
  messages: SingleChatMessageProps["message"][];
  isTyping?: boolean;
  onButtonClick?: (action: string, text: string) => void; // Propagate from parent
}

const ChatPanel: React.FC<ChatPanelProps> = ({ messages, isTyping, onButtonClick }) => {
  return (
    <div className="flex flex-col items-center w-full h-full md:h-screen bg-[#111827]">
      <div
        className="w-full max-w-md md:max-w-lg mx-auto flex flex-col flex-1 py-4 px-2 md:px-4 space-y-3 overflow-y-auto"
      >
        {messages.map((msg) => (
          <ChatMessage
            key={msg.id}
            message={msg}
            onButtonClick={onButtonClick} // Pass the handler to each ChatMessage
          />
        ))}
        {isTyping && <TypingIndicator isUser />}
      </div>
    </div>
  );
};

export default ChatPanel;
