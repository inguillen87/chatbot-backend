from flask import Blueprint, request, jsonify
from models import User
import logging
from werkzeug.security import check_password_hash, generate_password_hash
import uuid  # para generar token único
from extensions import db

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos"}), 400

    user = User.query.filter_by(email=email).first()

    if not user or not check_password_hash(user.password_hash, password):
        logging.warning(f"Intento fallido de login con usuario: {email}")
        return jsonify({"error": "Credenciales inválidas"}), 401

    # ✅ Si todo bien, devolvemos el user
    return jsonify({
        "token": user.token,
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas
    })


@auth_bp.route('/me', methods=['GET'])
def get_current_user():
    token = request.headers.get("Authorization", "")
    user = User.query.filter_by(token=token).first()

    if not user:
        return jsonify({"error": "Token inválido"}), 401
    
    return jsonify({
        "token": user.token,
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas
    })
@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not name or not email or not password:
        return jsonify({"error": "Todos los campos son obligatorios"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Ya existe un usuario con ese email"}), 400

    hashed_password = generate_password_hash(password)
    token = str(uuid.uuid4())

    user = User(
        name=name,
        email=email,
        password_hash=hashed_password,
        token=token
    )
    db.session.add(user)
    print("📝 Usuario agregado al session")
    db.session.commit()
    print("✅ Usuario guardado en DB")

    return jsonify({
        "token": user.token,
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas
    })
@app.route('/demo-chat', methods=['POST'])
def demo_chat():
    data = request.json
    messages = data.get('messages', [])

    # Acá podés limitar o simular una respuesta fija si querés
    response = openai_chat_response(messages)  # Tu función habitual
    return jsonify({ "content": response })

 @auth_bp.route('/debug/users', methods=['GET']) 
 def list_users():
     try:
         users = User.query.all()
         return jsonify([
             {
                 "id": user.id,
                 "name": user.name,
                 "email": user.email,
                 "plan": user.plan,
                 "preguntas_usadas": user.preguntas_usadas,
                 "token": user.token
             } for user in users
         ])
     except Exception as e:
         return jsonify({"error": f"Error al listar usuarios: {str(e)}"}), 500
