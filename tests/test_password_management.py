from models import User


def _login(client, email, password):
    response = client.post('/auth/login', json={'email': email, 'password': password})
    assert response.status_code == 200
    payload = response.get_json()
    assert 'token' in payload
    return payload['token']


def test_request_password_reset_generates_token(client, init_database):
    response = client.post('/auth/password/reset/request', json={'email': 'viewer@test.com'})
    assert response.status_code == 200
    payload = response.get_json()
    assert 'reset_token' in payload

    user = User.query.filter_by(email='viewer@test.com').first()
    assert user.password_reset_selector is not None
    assert user.password_reset_verifier_hash is not None
    assert user.password_reset_sent_at is not None


def test_password_reset_flow_updates_password(client, init_database):
    request_response = client.post('/auth/password/reset/request', json={'email': 'viewer@test.com'})
    token = request_response.get_json()['reset_token']

    reset_response = client.post(
        '/auth/password/reset/confirm',
        json={
            'token': token,
            'new_password': 'NuevaClave123',
            'confirm_password': 'NuevaClave123',
        },
    )
    assert reset_response.status_code == 200

    user = User.query.filter_by(email='viewer@test.com').first()
    assert user.check_password('NuevaClave123')
    assert user.password_reset_selector is None

    reuse_response = client.post(
        '/auth/password/reset/confirm',
        json={'token': token, 'new_password': 'OtraClave123'},
    )
    assert reuse_response.status_code == 400


def test_password_reset_request_unknown_email_is_generic(client, init_database):
    response = client.post('/auth/password/reset/request', json={'email': 'noexiste@test.com'})
    assert response.status_code == 200
    payload = response.get_json()
    assert 'reset_token' not in payload
    assert 'mensaje' in payload


def test_change_password_requires_current_password(client, init_database):
    token = _login(client, 'viewer@test.com', 'viewer')

    response = client.post(
        '/auth/password/change',
        headers={'Authorization': f'Bearer {token}'},
        json={
            'current_password': 'viewer',
            'new_password': 'NuevaClave456',
            'confirm_password': 'NuevaClave456',
        },
    )
    assert response.status_code == 200

    relog_response = client.post(
        '/auth/login',
        json={'email': 'viewer@test.com', 'password': 'NuevaClave456'},
    )
    assert relog_response.status_code == 200


def test_change_password_rejects_wrong_current_password(client, init_database):
    token = _login(client, 'viewer@test.com', 'viewer')

    response = client.post(
        '/auth/password/change',
        headers={'Authorization': f'Bearer {token}'},
        json={
            'current_password': 'incorrecta',
            'new_password': 'ClaveValida123',
        },
    )
    assert response.status_code == 400


def test_change_email_validates_password_and_uniqueness(client, init_database):
    token = _login(client, 'viewer@test.com', 'viewer')

    response = client.post(
        '/auth/email/change',
        headers={'Authorization': f'Bearer {token}'},
        json={
            'new_email': 'nuevo@test.com',
            'current_password': 'viewer',
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['email'] == 'nuevo@test.com'

    user = User.query.get(2)
    assert user.email == 'nuevo@test.com'

    duplicate_response = client.post(
        '/auth/email/change',
        headers={'Authorization': f'Bearer {token}'},
        json={
            'new_email': 'admin@test.com',
            'current_password': 'viewer',
        },
    )
    assert duplicate_response.status_code == 400


def test_update_personal_data_requires_password_for_email_change(client, init_database):
    token = _login(client, 'viewer@test.com', 'viewer')

    update_response = client.post(
        '/auth/update_personal_data',
        headers={'Authorization': f'Bearer {token}'},
        json={
            'email': 'sinpassword@test.com',
            'name': 'Nuevo Nombre',
        },
    )
    assert update_response.status_code == 400

    success_response = client.post(
        '/auth/update_personal_data',
        headers={'Authorization': f'Bearer {token}'},
        json={
            'email': 'cliente@test.com',
            'current_password': 'viewer',
            'name': 'Nuevo Nombre',
        },
    )
    assert success_response.status_code == 200
    user = User.query.get(2)
    assert user.email == 'cliente@test.com'
    assert user.name == 'Nuevo Nombre'
