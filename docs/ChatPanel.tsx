import React from "react";
import ChatMessage, { ChatMessageProps } from "./ChatMessage";

export interface ChatPanelProps {
  messages: ChatMessageProps["message"][];
}

const ChatPanel: React.FC<ChatPanelProps> = ({ messages }) => {
  return (
    <div className="flex flex-col items-center w-full min-h-screen bg-[#111827]">
      <div className="w-full max-w-lg flex flex-col flex-1 py-6 px-2">
        {messages.map((msg) => (
          <ChatMessage key={msg.id} message={msg} />
        ))}
      </div>
    </div>
  );
};

export default ChatPanel;
