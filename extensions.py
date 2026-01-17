from database import db
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_sock import Sock

migrate = Migrate()
login_manager = LoginManager()
sock = Sock()
