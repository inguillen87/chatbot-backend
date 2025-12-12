import os
import sys

# Add the project root to the Python path
# This allows tests to import modules from the 'services', 'routes', etc. directories
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
    print(f"✅ tests/__init__.py executed. Project root '{project_root}' added to sys.path.")

# Provide a lightweight stub for the `webauthn` package during tests when the
# real dependency is not installed in the execution environment.
if "webauthn" not in sys.modules:
    import base64
    import json
    import types
    from dataclasses import dataclass
    from typing import Any, Dict

    webauthn_module = types.ModuleType("webauthn")
    helpers_module = types.ModuleType("webauthn.helpers")
    structs_module = types.ModuleType("webauthn.helpers.structs")

    def _bytes_to_base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def _base64url_to_bytes(value: str) -> bytes:
        padding = "=" * ((4 - len(value) % 4) % 4)
        return base64.urlsafe_b64decode(value + padding)

    @dataclass
    class _Options:
        challenge: bytes
        payload: Dict[str, Any]

    def _options_to_json(options: _Options) -> str:
        return json.dumps(options.payload)

    def _registration_payload(rp_id: str, rp_name: str, user_id: str, display_name: str, challenge: bytes) -> Dict[str, Any]:
        return {
            "challenge": _bytes_to_base64url(challenge),
            "rp": {"id": rp_id, "name": rp_name},
            "user": {
                "id": user_id,
                "name": user_id,
                "displayName": display_name,
            },
            "pubKeyCredParams": [],
        }

    def generate_registration_options(**kwargs) -> _Options:
        challenge = base64.urlsafe_b64encode(b"reg-challenge").rstrip(b"=")
        payload = _registration_payload(
            kwargs.get("rp_id", "localhost"),
            kwargs.get("rp_name", "Chatboc"),
            kwargs.get("user_id", "1"),
            kwargs.get("user_display_name", "Ciudadano"),
            challenge,
        )
        return _Options(challenge=challenge, payload=payload)

    def generate_authentication_options(**kwargs) -> _Options:
        challenge = base64.urlsafe_b64encode(b"auth-challenge").rstrip(b"=")
        payload = {
            "challenge": _bytes_to_base64url(challenge),
            "rpId": kwargs.get("rp_id", "localhost"),
        }
        return _Options(challenge=challenge, payload=payload)

    @dataclass
    class _RegistrationVerification:
        credential_id: bytes
        credential_public_key: bytes
        sign_count: int

    @dataclass
    class _AuthenticationVerification:
        new_sign_count: int

    @dataclass
    class RegistrationCredential:
        id: str
        raw_id: str
        response: Dict[str, Any]
        type: str

        @classmethod
        def parse_obj(cls, data: Dict[str, Any]) -> "RegistrationCredential":
            return cls(
                id=data.get("id", ""),
                raw_id=data.get("rawId", data.get("id", "")),
                response=data.get("response", {}),
                type=data.get("type", "public-key"),
            )

    @dataclass
    class AuthenticationCredential:
        id: str
        raw_id: str
        response: Dict[str, Any]
        type: str

        @classmethod
        def parse_obj(cls, data: Dict[str, Any]) -> "AuthenticationCredential":
            return cls(
                id=data.get("id", ""),
                raw_id=data.get("rawId", data.get("id", "")),
                response=data.get("response", {}),
                type=data.get("type", "public-key"),
            )

    class ResidentKeyRequirement:
        PREFERRED = "preferred"
        REQUIRED = "required"

    class UserVerificationRequirement:
        REQUIRED = "required"

    @dataclass
    class AuthenticatorSelectionCriteria:
        resident_key: str
        user_verification: str

    @dataclass
    class PublicKeyCredentialDescriptor:
        id: bytes

    def verify_registration_response(credential: RegistrationCredential, **_kwargs) -> _RegistrationVerification:
        credential_id = _base64url_to_bytes(credential.id or "") or b"credential"
        return _RegistrationVerification(
            credential_id=credential_id,
            credential_public_key=credential_id,
            sign_count=0,
        )

    def verify_authentication_response(credential: AuthenticationCredential, **_kwargs) -> _AuthenticationVerification:
        current_count = _kwargs.get("credential_current_sign_count", 0)
        return _AuthenticationVerification(new_sign_count=current_count + 1)

    webauthn_module.generate_registration_options = generate_registration_options
    webauthn_module.generate_authentication_options = generate_authentication_options
    webauthn_module.verify_registration_response = verify_registration_response
    webauthn_module.verify_authentication_response = verify_authentication_response

    helpers_module.bytes_to_base64url = _bytes_to_base64url
    helpers_module.base64url_to_bytes = _base64url_to_bytes
    helpers_module.options_to_json = _options_to_json

    structs_module.RegistrationCredential = RegistrationCredential
    structs_module.AuthenticationCredential = AuthenticationCredential
    structs_module.AuthenticatorSelectionCriteria = AuthenticatorSelectionCriteria
    structs_module.PublicKeyCredentialDescriptor = PublicKeyCredentialDescriptor
    structs_module.ResidentKeyRequirement = ResidentKeyRequirement
    structs_module.UserVerificationRequirement = UserVerificationRequirement

    sys.modules["webauthn"] = webauthn_module
    sys.modules["webauthn.helpers"] = helpers_module
    sys.modules["webauthn.helpers.structs"] = structs_module

    webauthn_module.helpers = helpers_module
    webauthn_module.helpers.structs = structs_module
