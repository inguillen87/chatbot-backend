import os
import sys
from flask import Flask
from app import create_app, db
from models import User, Rubro
from services.rubro_classification import es_rubro_publico

def fix_tipo_chat():
    """
    Updates the tipo_chat field for existing users based on their rubro.
    """
    app = create_app()
    with app.app_context():
        users = User.query.all()
        for user in users:
            if user.rubro:
                if es_rubro_publico(user.rubro):
                    if user.tipo_chat != "municipio":
                        print(f"Updating user {user.email} to tipo_chat='municipio'")
                        user.tipo_chat = "municipio"
                else:
                    if user.tipo_chat != "pyme":
                        print(f"Updating user {user.email} to tipo_chat='pyme'")
                        user.tipo_chat = "pyme"
        db.session.commit()
    print("Done.")

if __name__ == "__main__":
    fix_tipo_chat()
