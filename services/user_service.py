from models import db, User, Rol, ChatUser, TipoUsuario
from werkzeug.security import generate_password_hash
import uuid

def crear_usuario_invitado(nombre, email, telefono=None, pyme_id=None, municipio_id=None):
    """
    Crea un usuario "invitado" asociado a una pyme o municipio.
    """
    # Verificar si ya existe un usuario con ese email
    if User.query.filter_by(email=email).first():
        return None, "El correo electrónico ya está en uso."

    # Crear un usuario con un rol de "invitado"
    nuevo_usuario = User(
        email=email,
        nombre=nombre,
        telefono=telefono,
        password=generate_password_hash(str(uuid.uuid4())),  # Contraseña aleatoria
        activo=True,
        rol=Rol.INVITADO,
        pyme_id=pyme_id,
        municipio_id=municipio_id,
        tipo_usuario=TipoUsuario.INVITADO
    )

    db.session.add(nuevo_usuario)
    db.session.commit()

    return nuevo_usuario, "Usuario invitado creado exitosamente."

def crear_chat_user_invitado(nombre, telefono, pyme_id=None, municipio_id=None):
    """
    Crea un ChatUser para un usuario de WhatsApp.
    """
    # Verificar si ya existe un ChatUser con ese teléfono
    if ChatUser.query.filter_by(telefono=telefono).first():
        return None, "El número de teléfono ya está en uso."

    nuevo_chat_user = ChatUser(
        nombre=nombre,
        telefono=telefono,
        pyme_id=pyme_id,
        municipio_id=municipio_id
    )

    db.session.add(nuevo_chat_user)
    db.session.commit()

    return nuevo_chat_user, "ChatUser invitado creado exitosamente."
