import React from "react";
import ChatMessage, { ChatMessageProps } from "./ChatMessage";
import TypingIndicator from "./TypingIndicator";

export interface ChatPanelProps {
  messages: ChatMessageProps["message"][];
  isTyping?: boolean;
}

const ChatPanel: React.FC<ChatPanelProps> = ({ messages, isTyping }) => {
  return (
    <div className="flex flex-col items-center w-full h-full md:h-screen bg-[#111827]">
      <div
        className="w-full max-w-md md:max-w-lg mx-auto flex flex-col flex-1 py-4 px-2 md:px-4 space-y-3 overflow-y-auto"
      >
        {messages.map((msg) => (
          <ChatMessage key={msg.id} message={msg} />
        ))}
        {isTyping && <TypingIndicator isUser />}
      </div>
    </div>
  );
};

export default ChatPanel;
