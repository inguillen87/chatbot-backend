document.addEventListener("DOMContentLoaded", () => {
  //Protección: si no hay token, redirige a login
  const token = localStorage.getItem("token");
  if (!token) {
    window.location.href = "login.html";
     return;
    
  }

  // Mostrar nombre del usuario extraído del token
  const username = token.replace("token-", "");
  const userInfoDiv = document.getElementById("user-info");
  userInfoDiv.textContent = `👋 Hola, ${username.charAt(0).toUpperCase() + username.slice(1)}`;
  
  // ✅ Mensaje de bienvenida
  addToChatLog("¡Hola! Soy tu asistente virtual. ¿En qué puedo ayudarte?", "bot");

  // ✅ Botón ENVIAR
  document.querySelector("button[onclick='sendMessage()']").onclick = sendMessage;

  // ✅ Botón BORRAR CHAT
  document.getElementById("clear-btn").onclick = clearChat;

  // ✅ Botón LOGOUT (funcionando con alert)
  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) {
    logoutBtn.onclick = () => {
      localStorage.removeItem("token");
      window.location.href = "login.html";
    };
  }

  // ✅ Enter para enviar
  document.body.onkeydown = function (event) {
    if (event.key === "Enter") sendMessage();
  };
});

function sendMessage() {
  const input = document.getElementById("user-input");
  const message = input.value.trim();
  if (!message) return;

  addToChatLog(message, "user");
  input.value = "";

  addToChatLog("El bot está escribiendo...", "bot", true);

  const token = localStorage.getItem("token");

  fetch("http://127.0.0.1:5000/ask", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": token
    },
    body: JSON.stringify({ question: message })
  })
    .then((res) => {
      if (!res.ok) throw new Error("No autorizado");
      return res.json();
    })
    .then((data) => {
      removeTypingIndicator();
      addToChatLog(data.answer, "bot");
    })
    .catch((err) => {
      removeTypingIndicator();
      addToChatLog("Error de autorización o red.", "bot");
      console.error(err);
    });
}

function addToChatLog(message, sender, isTyping = false) {
  const chatLog = document.getElementById("chat-log");
  const container = document.createElement("div");
  container.classList.add("message", sender);
  container.dataset.typing = isTyping;

  const text = document.createElement("div");
  text.textContent = message;

  const time = document.createElement("div");
  time.classList.add("timestamp");
  time.textContent = getCurrentTime();

  container.appendChild(text);
  if (!isTyping) container.appendChild(time);

  chatLog.appendChild(container);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function removeTypingIndicator() {
  const typing = document.querySelector('.message[data-typing="true"]');
  if (typing) typing.remove();
}

function clearChat() {
  document.getElementById("chat-log").innerHTML = "";
  addToChatLog("¡Hola! Soy tu asistente virtual. ¿En qué puedo ayudarte?", "bot");
}

function getCurrentTime() {
  const now = new Date();
  return now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
