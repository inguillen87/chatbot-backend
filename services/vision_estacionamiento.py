import base64, io
from typing import List, Dict
from PIL import Image
from google.cloud import vision

def analizar_frame_y_contar_autos(frame_png_bytes: bytes) -> List[Dict]:
    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=frame_png_bytes)
    resp = client.object_localization(image=image)
    bbs=[]
    for obj in resp.localized_object_annotations:
        if obj.name.lower() in ("car","vehicle","truck","bus"):
            xs = [v.x for v in obj.bounding_poly.normalized_vertices]
            ys = [v.y for v in obj.bounding_poly.normalized_vertices]
            x1, x2 = min(xs), max(xs); y1, y2 = min(ys), max(ys)
            # Pasar a px suponiendo 1280x720; si querés, detectar dinámicamente con PIL
            W,H = 1280,720
            bbs.append({
                "x1": int(x1*W), "y1": int(y1*H), "x2": int(x2*W), "y2": int(y2*H)
            })
    return bbs
