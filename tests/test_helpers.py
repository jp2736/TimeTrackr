import main


def test_country_names_starts_with_uk():
    names = main.country_names()
    assert names[0] == "United Kingdom"
    assert "United States" in names


def test_dial_labels_format():
    labels = main.dial_labels()
    assert labels[0] == "United Kingdom (+44)"


def test_label_for_code_known():
    assert main.label_for_code("+44") == "United Kingdom (+44)"


def test_label_for_code_unknown_returns_code():
    assert main.label_for_code("+999") == "+999"


def test_code_from_label_roundtrip():
    assert main.code_from_label("United Kingdom (+44)") == "+44"
    assert main.code_from_label("United States (+1)") == "+1"


def test_code_from_label_plain():
    assert main.code_from_label("+44") == "+44"
