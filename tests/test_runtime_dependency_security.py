import io
import os
import warnings
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


REQUIREMENTS_PATH = Path(__file__).resolve().parents[1] / "requirements.txt"
SECURITY_FLOORS = {
    "gunicorn": Version("22.0.0"),
    "pdfminer-six": Version("20251230"),
    "pillow": Version("12.3.0"),
    "python-engineio": Version("4.13.2"),
    "python-socketio": Version("5.16.2"),
}


def _production_requirements() -> dict[str, Requirement]:
    requirements: dict[str, Requirement] = {}
    for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        requirements[canonicalize_name(requirement.name)] = requirement
    return requirements


def _exact_pin(requirement: Requirement) -> Version:
    exact_versions = [
        Version(specifier.version)
        for specifier in requirement.specifier
        if specifier.operator == "==" and "*" not in specifier.version
    ]
    assert len(exact_versions) == 1, (
        f"{requirement.name} must have one exact production pin, got "
        f"{requirement.specifier or 'no version constraint'}"
    )
    return exact_versions[0]


def test_production_manifest_meets_runtime_security_floors():
    requirements = _production_requirements()

    for package_name, minimum in SECURITY_FLOORS.items():
        assert package_name in requirements
        assert _exact_pin(requirements[package_name]) >= minimum

    assert _exact_pin(requirements["pdfplumber"]) == Version("0.11.9")
    assert _exact_pin(requirements["pdfminer-six"]) == Version("20251230")


def test_installed_runtime_resolves_the_security_floor_set():
    for package_name, minimum in SECURITY_FLOORS.items():
        assert Version(metadata.version(package_name)) >= minimum

    installed_pdfminer = Version(metadata.version("pdfminer.six"))
    pdfplumber_requirements = [
        Requirement(raw_requirement)
        for raw_requirement in metadata.requires("pdfplumber") or []
    ]
    pdfminer_constraint = next(
        requirement
        for requirement in pdfplumber_requirements
        if canonicalize_name(requirement.name) == "pdfminer-six"
    )
    assert pdfminer_constraint.specifier.contains(installed_pdfminer)


@pytest.mark.skipif(
    os.name == "nt",
    reason="Gunicorn's production HTTP parser is supported on Unix runtimes only",
)
def test_gunicorn_rejects_ambiguous_transfer_encoding_framing():
    from gunicorn.config import Config
    from gunicorn.http.errors import InvalidHeader
    from gunicorn.http.parser import RequestParser

    request_bytes = (
        b"POST /api/security-probe HTTP/1.1\r\n"
        b"Host: example.test\r\n"
        b"Content-Length: 4\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"0\r\n\r\n"
    )

    with pytest.raises(InvalidHeader):
        list(RequestParser(Config(), iter([request_bytes]), None))


@pytest.mark.skipif(
    os.name == "nt",
    reason="Gunicorn's production HTTP parser is supported on Unix runtimes only",
)
def test_gunicorn_accepts_unambiguous_chunked_framing():
    from gunicorn.config import Config
    from gunicorn.http.parser import RequestParser

    request_bytes = (
        b"POST /api/security-probe HTTP/1.1\r\n"
        b"Host: example.test\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"4\r\nping\r\n"
        b"0\r\n\r\n"
    )

    parsed_request = next(RequestParser(Config(), iter([request_bytes]), None))

    assert parsed_request.method == "POST"
    assert parsed_request.path == "/api/security-probe"
    assert parsed_request.body.read() == b"ping"


def _minimal_bdf(*, width: int, height: int) -> bytes:
    return f"""STARTFONT 2.1
SIZE 16 75 75
FONTBOUNDINGBOX 1 1 0 0
STARTPROPERTIES 1
COMMENT safe-in-memory-regression
ENDPROPERTIES
CHARS 1
STARTCHAR A
ENCODING 65
SWIDTH 500 0
DWIDTH 1 0
BBX {width} {height} 0 0
BITMAP
ENDCHAR
ENDFONT
""".encode("ascii")


def test_pillow_bdf_loader_enforces_decompression_bomb_limit(monkeypatch):
    from PIL import Image
    from PIL.BdfFontFile import BdfFontFile

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with pytest.raises(Image.DecompressionBombError):
            BdfFontFile(io.BytesIO(_minimal_bdf(width=3, height=1)))


def test_pillow_bdf_loader_accepts_small_font_control(monkeypatch):
    from PIL import Image
    from PIL.BdfFontFile import BdfFontFile

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        font = BdfFontFile(io.BytesIO(_minimal_bdf(width=1, height=1)))

    assert font is not None
