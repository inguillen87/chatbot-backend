"""Minimal WSGI application for the Linux Gunicorn/WebSocket CI gate."""

from flask import Flask, jsonify
from flask_socketio import SocketIO


app = Flask(__name__)
socketio = SocketIO(
    app,
    async_mode="threading",
    path="/api/socket.io",
)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "async_mode": socketio.async_mode})


@socketio.on("runtime_ping")
def runtime_ping(payload):
    return {
        "ok": True,
        "probe": str((payload or {}).get("probe") or ""),
        "async_mode": socketio.async_mode,
    }
