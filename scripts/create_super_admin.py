
import os
import sys
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import argparse

# Add the project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app
from extensions import db
from models import User, generate_token

def create_super_admin(email, password, name="Super Admin"):
    app = create_app()
    with app.app_context():
        # Check if user exists
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            print(f"User with email {email} already exists. Updating role to 'super_admin'.")
            existing_user.rol = "super_admin"
            existing_user.set_password(password) # Optional: Update password too
            db.session.commit()
            print("User updated successfully.")
            return

        # Create new user
        new_user = User(
            name=name,
            email=email,
            rol="super_admin",
            token=generate_token(),
            email_verified=True,
            fecha_aceptacion_terminos=datetime.now(timezone.utc),
            acepto_terminos=True
        )
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        print(f"Super Admin user created successfully: {email}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a Super Admin user.")
    parser.add_argument("--email", required=True, help="Email of the super admin")
    parser.add_argument("--password", required=True, help="Password of the super admin")
    parser.add_argument("--name", default="Super Admin", help="Name of the super admin")

    args = parser.parse_args()
    create_super_admin(args.email, args.password, args.name)
