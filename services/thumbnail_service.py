import io
import fitz  # PyMuPDF
from PIL import Image, ImageOps

def _thumb_image(file_stream, max_px=512):
    """
    Generates a thumbnail for an image file.
    - Strips EXIF data for privacy and to correct orientation.
    - Resizes to a max dimension of 512px.
    - Converts to WEBP for efficiency.
    """
    try:
        img = Image.open(file_stream)

        # Corrects orientation based on EXIF data and removes other EXIF info
        img = ImageOps.exif_transpose(img)

        # Convert to RGB if it has an alpha channel (e.g., PNG) to avoid issues with WEBP saving
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        img.thumbnail((max_px, max_px))

        output = io.BytesIO()
        img.save(output, format="WEBP", quality=80)
        output.seek(0)

        meta = {"width": img.width, "height": img.height}
        return output.getvalue(), meta
    except Exception as e:
        print(f"Error generating image thumbnail: {e}")
        return None, None

def _thumb_pdf(file_stream, max_px=512):
    """
    Generates a thumbnail for the first page of a PDF.
    - Renders the first page as a pixmap.
    - Converts the pixmap to a PNG bytestream.
    """
    try:
        # PyMuPDF needs bytes, so we read the stream
        pdf_bytes = file_stream.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        if doc.page_count == 0:
            return None, None

        page = doc.load_page(0)

        # Set zoom factor to achieve desired pixel width (approx)
        # Assuming standard 72 DPI, a page is ~595x842 pts. We scale for a width of ~800px before thumbnailing.
        zoom = max_px / 800.0
        mat = fitz.Matrix(zoom, zoom)

        pix = page.get_pixmap(matrix=mat, alpha=False)

        output = io.BytesIO(pix.tobytes("png"))
        output.seek(0)

        meta = {"pages": doc.page_count, "width": pix.width, "height": pix.height}
        doc.close()
        return output.getvalue(), meta
    except Exception as e:
        import traceback
        print(f"Error generating PDF thumbnail: {e}")
        traceback.print_exc()
        return None, None

def generar_thumbnail(file_stream, mime_type: str):
    """
    Generates a thumbnail for a given file stream based on its MIME type.

    Args:
        file_stream: A file-like object (e.g., io.BytesIO).
        mime_type: The MIME type of the file.

    Returns:
        A tuple containing (thumbnail_bytes, metadata_dict), or (None, None) on failure.
    """
    mime_type = mime_type.lower()

    if mime_type in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return _thumb_image(file_stream)
    elif mime_type == "application/pdf":
        return _thumb_pdf(file_stream)
    else:
        # Unsupported type
        return None, None
