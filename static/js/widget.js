document.addEventListener("DOMContentLoaded", function() {
    const chatBubble = document.getElementById("chat-bubble");
    const chatWidget = document.getElementById("chat-widget");
    const closeChat = document.getElementById("close-chat");
    const chatMessages = document.getElementById("chat-messages");

    chatBubble.addEventListener("click", function() {
        chatWidget.style.display = "flex";
        chatBubble.style.display = "none";
        addMessage("Hello! How can I help you today?", "bot");
    });

    closeChat.addEventListener("click", function() {
        chatWidget.style.display = "none";
        chatBubble.style.display = "flex";
    });

    function addMessage(message, sender) {
        const messageElement = document.createElement("div");
        messageElement.classList.add("message", sender);
        messageElement.textContent = message;
        chatMessages.appendChild(messageElement);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }
});
