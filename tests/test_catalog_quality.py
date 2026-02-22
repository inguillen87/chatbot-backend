from services.catalog_quality import evaluate_catalog_quality


def test_evaluate_catalog_quality_flags_low_confidence_and_duplicates():
    items = [
        {"nombre": "A", "categoria": "", "precio": "0"},
        {"nombre": "Malbec Reserva", "categoria": "Vinos", "precio": "15000"},
        {"nombre": "Malbec Reserva", "categoria": "Vinos", "precio": "15100"},
    ]

    result = evaluate_catalog_quality(items)

    assert len(result) == 3
    assert result[0]["review_required"] is True
    assert "nombre_demasiado_corto" in result[0]["quality_issues"]
    assert "precio_invalido" in result[0]["quality_issues"]
    assert result[2]["review_required"] is True
    assert "duplicado_aproximado" in result[2]["quality_issues"]
