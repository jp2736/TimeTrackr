import pytest
import tkinter as tk
import main


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    r.withdraw()
    yield r
    r.destroy()


def test_placeholder_shown_initially(root):
    e = main.PlaceholderEntry(root, placeholder="123 Example St")
    assert e.get() == "123 Example St"
    assert e.get_value() == ""


def test_focus_in_clears_placeholder(root):
    e = main.PlaceholderEntry(root, placeholder="hint")
    e._on_focus_in()
    e.insert(0, "real text")
    assert e.get_value() == "real text"


def test_focus_out_restores_placeholder_when_empty(root):
    e = main.PlaceholderEntry(root, placeholder="hint")
    e._on_focus_in()
    e._on_focus_out()
    assert e.get_value() == ""
    assert e.get() == "hint"


def test_set_value_sets_real_text(root):
    e = main.PlaceholderEntry(root, placeholder="hint")
    e.set_value("actual")
    assert e.get_value() == "actual"


def test_set_value_blank_restores_placeholder(root):
    e = main.PlaceholderEntry(root, placeholder="hint")
    e.set_value("actual")
    e.set_value("")
    assert e.get_value() == ""
    assert e.get() == "hint"
