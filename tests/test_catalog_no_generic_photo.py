from routes.catalogo import _fallback_image_for_item


def test_a_product_without_photo_gets_no_unrelated_stock_image():
    assert _fallback_image_for_item(None, {"nombre": "Yerba mate 1 kg", "sku": "ALM-001"}, "Almacén") is None
    assert _fallback_image_for_item("", {"nombre": "Taza"}, "Bazar") is None


def test_own_photos_and_curated_demo_images_are_kept():
    assert _fallback_image_for_item("https://example.test/taza.jpg", {}, "Bazar") == "https://example.test/taza.jpg"
    assert _fallback_image_for_item(None, {"sku": "kit-escolar-1"}, "") .startswith("https://images.unsplash.com/")
    assert _fallback_image_for_item(None, {"nombre": "Ítem"}, "Salud").startswith("https://images.unsplash.com/")
