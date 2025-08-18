import tempfile, subprocess, os

def obtener_frame_png(hls_url: str, fps_interval=10) -> bytes|None:
    """
    Usa ffmpeg para tomar un frame actual del HLS. En Render, instala ffmpeg en build.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        out = tmp.name
    try:
        cmd = [
            "ffmpeg","-y","-i", hls_url,
            "-vframes","1", "-q:v","2", out
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        with open(out, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        try: os.remove(out)
        except: pass
