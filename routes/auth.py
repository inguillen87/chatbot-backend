from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import check_password_hash, generate_password_hash
from models import User, Rubro, CatalogoItem  # 👈 importamos también CatalogoItem
from extensions import db
from functools import wraps
import uuid
import logging
import traceback

auth_bp = Blueprint('auth', __name__)

# Decorador para verificar token en rutas protegidas
def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "Token faltante"}), 401

        user = User.query.filter_by(token=token).first()
        if not user:
            return jsonify({"error": "Token inválido"}), 403

        return f(user, *args, **kwargs)
    return decorated

# Obtener información del usuario actual
@auth_bp.route('/me', methods=['GET'])
def get_current_user():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

    if not token:
        return jsonify({"error": "Token faltante"}), 401

    user = User.query.filter_by(token=token).first()
    if not user:
        return jsonify({"error": "Token inválido"}), 401

    try:
        rubro_nombre = "General"
        rubro = db.session.get(Rubro, user.rubro_id) if user.rubro_id else None
        if rubro and hasattr(rubro, "nombre"):
            rubro_nombre = rubro.nombre

        return jsonify({
            "token": user.token,
            "id": user.id,
            "name": getattr(user, "name", "") or "",
            "email": getattr(user, "email", "") or "",
            "plan": getattr(user, "plan", "gratis") or "gratis",
            "preguntas_usadas": getattr(user, "preguntas_usadas", 0) or 0,
            "limite_preguntas": getattr(user, "limite_preguntas", 0) or 0,
            "nombre_empresa": getattr(user, "nombre_empresa", "") or "",
            "direccion": getattr(user, "direccion", "") or "",
            "telefono": getattr(user, "telefono", "") or "",
            "link_web": getattr(user, "link_web", "") or "",
            "horario": getattr(user, "horario", "") or "",
            "ubicacion": getattr(user, "ubicacion", "") or "",
            "logo_url": getattr(user, "logo_url", "") or "",
            "rubro": rubro_nombre
        })

    except Exception:
        current_app.logger.error("❌ Error crítico en /me:\n" + traceback.format_exc())
        return jsonify({"error": "Error interno al obtener perfil"}), 500

# Endpoint debug: ver todos los usuarios y su catálogo
@auth_bp.route('/debug/users', methods=['GET'])
def debug_usuarios():
    try:
        usuarios = User.query.all()
        resultado = []
        for u in usuarios:
            rubro = db.session.get(Rubro, u.rubro_id) if u.rubro_id else None
            items = CatalogoItem.query.filter_by(user_id=u.id).all()

            catalogo = []
            for item in items:
                catalogo.append({
                    "nombre": item.nombre,
                    "descripcion": item.descripcion,
                    "precio": item.precio,
                    "stock": item.stock,
                    "categoria": item.categoria or "",
                    "unidad": item.unidad or ""
                })

            resultado.append({
                "id": u.id,
                "token": u.token,
                "name": u.name,
                "email": u.email,
                "empresa": u.nombre_empresa,
                "rubro": rubro.nombre if rubro else "General",
                "telefono": u.telefono,
                "direccion": u.direccion,
                "link_web": u.link_web,
                "horario": u.horario,
                "catalogo": catalogo  # ✅ lo que importa
            })
        return jsonify(resultado)
    except Exception:
        current_app.logger.error("❌ Error crítico en /debug/users:\n" + traceback.format_exc())
        return jsonify({"error": "Error interno en debug"}), 500

# Registro de usuario
@auth_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.get_json()
        name = data.get("name", "").strip()
        email = data.get("email", "").strip()
        password = data.get("password", "").strip()
        nombre_empresa = data.get("nombre_empresa", "").strip()
        rubro_nombre = data.get("rubro", "").strip()

        if not all([name, email, password, nombre_empresa, rubro_nombre]):
            return jsonify({"error": "Todos los campos son obligatorios"}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({"error": "Ya existe un usuario con ese email"}), 400

        rubro = Rubro.query.filter_by(nombre=rubro_nombre).first()
        if not rubro:
            return jsonify({"error": "Rubro no válido"}), 400

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            token=str(uuid.uuid4()),
            nombre_empresa=nombre_empresa,
            rubro_id=rubro.id,
            plan="gratis",
            preguntas_usadas=0,
            limite_preguntas=50
        )

        db.session.add(user)
        db.session.commit()
        logging.info(f"✅ Usuario registrado: {email}")

        return jsonify({
            "token": user.token,
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "plan": user.plan,
            "nombre_empresa": user.nombre_empresa,
            "rubro_id": user.rubro_id,
            "rubro": rubro.nombre,
            "preguntas_usadas": user.preguntas_usadas,
            "limite_preguntas": user.limite_preguntas
        })
    except Exception:
        logging.error("❌ Error en /register:\n" + traceback.format_exc())
        return jsonify({"error": "Error interno al registrar usuario"}), 500

# Login de usuario
@auth_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        email = data.get("email", "").strip()
        password = data.get("password", "").strip()

        if not email or not password:
            return jsonify({"error": "Email y contraseña requeridos"}), 400

        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            logging.warning(f"❌ Intento fallido de login: {email}")
            return jsonify({"error": "Credenciales inválidas"}), 401

        return jsonify({
            "token": user.token,
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "plan": user.plan,
            "preguntas_usadas": user.preguntas_usadas or 0,
            "limite_preguntas": user.limite_preguntas or 0
        })
    except Exception:
        logging.error("❌ Error en /login:\n" + traceback.format_exc())
        return jsonify({"error": "Error interno al iniciar sesión"}), 500

# Actualizar perfil del usuario
@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def update_profile(user):
    try:
        data = request.get_json()
        print("📥 RECIBIDO EN /perfil:", data)
        print("🔑 USER TOKEN:", user.token)

        if not data:
            print("❌ No se recibió JSON válido")
            return jsonify({"error": "No se recibió ningún dato"}), 400

        campos_actualizables = [
            "nombre_empresa",
            "telefono",
            "direccion",
            "ubicacion",
            "horario",
            "link_web",
            "logo_url"
        ]

        for campo in campos_actualizables:
            if campo in data:
                valor = data[campo]
                if isinstance(valor, str):
                    valor = valor.strip()
                setattr(user, campo, valor)
                print(f"✅ Campo actualizado: {campo} → {valor}")

        db.session.commit()
        print(f"✅ PERFIL ACTUALIZADO PARA: {user.email}")
        return jsonify({"mensaje": "Perfil actualizado correctamente"})
    except Exception as e:
        print("❌ ERROR EN /perfil:", e)
        traceback.print_exc()
        return jsonify({"error": "Error interno"}), 500

# CLI opcional para crear base sin migraciones
@auth_bp.cli.command("crear_base")
def crear_base():
    db.create_all()
    print("✅ Base creada directamente desde los modelos.")
