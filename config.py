import os

class Config:

    SECRET_KEY = os.getenv("SECRET_KEY", "7b10c2e04fbf48aeaa4580b1f79ad9c6fd2f9cfe3a747dd20f9432be5cb3ce43")
    SQLALCHEMY_DATABASE_URI = "sqlite:////data/database.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
