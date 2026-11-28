from routes.catalog_import import _guess_catalog_column_type, _normalize_rows_for_images, _product_images_from_row


def test_a_plain_imagen_column_is_the_product_photo():
    url = "https://images.example.test/yerba.jpg"
    assert _product_images_from_row({"nombre": "Yerba", "imagen": url}) == (url, [url])
    assert _product_images_from_row({"nombre": "Yerba", "image": url})[0] == url
    rows, summary = _normalize_rows_for_images([{"nombre": "Yerba", "imagen": url}, {"nombre": "Arroz"}])
    assert rows[0]["imagen_url"] == url and "imagen_url" not in rows[1]
    assert summary["with_images"] == 1
    assert _guess_catalog_column_type("Imagen", [url]) == "image"


def test_the_explicit_image_url_column_still_wins():
    first, other = "https://a.test/1.jpg", "https://a.test/2.jpg"
    assert _product_images_from_row({"imagen_url": first, "imagen": other})[0] == first
