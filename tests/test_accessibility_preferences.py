import json
import jwt
from datetime import datetime, timedelta
from app import db
from models import User


def _auth_headers(app, user_id):
    token = jwt.encode({'user_id': user_id, 'exp': datetime.utcnow() + timedelta(days=1)}, app.config['SECRET_KEY'], algorithm="HS256")
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_update_and_get_accessibility(client, app, init_database, viewer_user):
    headers = _auth_headers(app, viewer_user.id)
    payload = {'dislexia': True, 'tts': True}
    resp = client.put('/preferences/accessibility', headers=headers, data=json.dumps(payload))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['dislexia'] is True
    assert data['tts'] is True

    db.session.refresh(viewer_user)
    assert viewer_user.accesibilidad['dislexia'] is True

    resp = client.get('/preferences/accessibility', headers=headers)
    assert resp.status_code == 200
    data_get = resp.get_json()
    assert data_get['tts'] is True
