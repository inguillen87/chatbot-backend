from database import db
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_sock import Sock
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

migrate = Migrate()
login_manager = LoginManager()
sock = Sock()
limiter = Limiter(key_func=get_remote_address)
