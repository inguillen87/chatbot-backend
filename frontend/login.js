document.getElementById("login-form").addEventListener("submit", function (e) {
    e.preventDefault();  // Evita recargar la página
  
    const username = document.getElementById("username").value.trim();
    const password = document.getElementById("password").value.trim();
    const errorDiv = document.getElementById("login-error");
  
    fetch("http://127.0.0.1:5000/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    })
      .then((res) => {
        if (!res.ok) throw new Error("Login inválido");
        return res.json();
      })
      .then((data) => {
        localStorage.setItem("token", data.token);
        window.location.href = "index.html";
      })
      .catch(() => {
        errorDiv.textContent = "Usuario o contraseña incorrectos";
      });
  });
  