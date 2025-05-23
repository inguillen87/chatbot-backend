import os

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "7b10c2e04fbf48aeaa4580b1f79ad9c6fd2f9cfe3a747dd20f9432be5cb3ce43")

    if os.getenv("RENDER") == "true":
        SQLALCHEMY_DATABASE_URI = "sqlite:////data/database.db"
    else:
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(basedir, 'instance', 'database.db')}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False
