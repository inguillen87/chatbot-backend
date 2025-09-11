from utils.address_parse import split_ubicacion_y_distrito


def test_no_split_without_comma_or_keyword():
    ubic, dist = split_ubicacion_y_distrito("sarmiento 125 esquina san martin")
    assert dist is None
    assert ubic == "sarmiento 125 esquina san martin"


def test_split_with_comma_and_keyword():
    ubic, dist = split_ubicacion_y_distrito("don bosco 55, barrio centro")
    assert ubic == "don bosco 55"
    assert dist == "centro"
