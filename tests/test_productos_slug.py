import pytest

from routes import productos


def test_normalize_slug_removes_accents_and_spaces():
    assert productos._normalize_slug("Municipalidad de Junín") == "municipalidad-de-junin"


def test_normalize_slug_collapses_hyphens():
    assert productos._normalize_slug("--Demo__Slug  --") == "demo-slug"
