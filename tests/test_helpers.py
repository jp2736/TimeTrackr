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


def test_compose_address_skips_blanks():
    parts = ["123 Example St", "", "  ", "London", "", "SW1A 1AA", "United Kingdom"]
    assert main.compose_address(parts) == "123 Example St, London, SW1A 1AA, United Kingdom"


def test_compose_address_block_separator():
    parts = ["123 Example St", "Suite 4", "London"]
    assert main.compose_address(parts, sep="\n") == "123 Example St\nSuite 4\nLondon"


def test_compose_address_all_blank():
    assert main.compose_address(["", "  ", ""]) == ""


def test_compose_phone_with_number():
    assert main.compose_phone("+44", "7700 900123") == "+44 7700 900123"


def test_compose_phone_blank_number():
    assert main.compose_phone("+44", "  ") == ""


def test_resolve_project_job_selected_wins():
    assert main.resolve_project_job(5, [1, 2, 3]) == ("use", 5)


def test_resolve_project_job_single():
    assert main.resolve_project_job(None, [7]) == ("use", 7)


def test_resolve_project_job_many():
    assert main.resolve_project_job(None, [1, 2]) == ("choose", None)


def test_resolve_project_job_none():
    assert main.resolve_project_job(None, []) == ("empty", None)
