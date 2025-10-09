"""Helper utilities to generate QR codes for public surveys."""
from __future__ import annotations

import io

import qrcode


def build_qr_png(url: str, size: int = 512) -> bytes:
    if not url:
        raise ValueError("URL requerida para generar QR")
    if size < 128 or size > 1024:
        raise ValueError("El tamaño del QR debe estar entre 128 y 1024")

    qr = qrcode.QRCode(border=1, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img = img.resize((size, size))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()
