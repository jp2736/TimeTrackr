import sys
import platform_support as plat


def test_flags_match_sys_platform():
    assert plat.IS_MAC == (sys.platform == "darwin")
    assert plat.IS_WINDOWS == sys.platform.startswith("win")


def test_open_file_mac(monkeypatch):
    seen = {}
    monkeypatch.setattr(plat, "IS_MAC", True)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat.subprocess, "run", lambda *a, **k: seen.setdefault("cmd", a[0]))
    plat.open_file("/tmp/x.pdf")
    assert seen["cmd"] == ["open", "/tmp/x.pdf"]


def test_open_file_windows(monkeypatch):
    seen = {}
    monkeypatch.setattr(plat, "IS_MAC", False)
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    # os.startfile only exists on Windows; inject a stub.
    monkeypatch.setattr(plat.os, "startfile", lambda p: seen.setdefault("p", p), raising=False)
    plat.open_file("C:/x.pdf")
    assert seen["p"] == "C:/x.pdf"


def test_open_file_linux(monkeypatch):
    seen = {}
    monkeypatch.setattr(plat, "IS_MAC", False)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat.subprocess, "run", lambda *a, **k: seen.setdefault("cmd", a[0]))
    plat.open_file("/tmp/x.pdf")
    assert seen["cmd"] == ["xdg-open", "/tmp/x.pdf"]


def test_make_tray_returns_controller_with_api():
    tray = plat.make_tray("App", [plat.MenuItem("Quit", lambda: None)], lambda: None)
    assert hasattr(tray, "run") and hasattr(tray, "update_icon") and hasattr(tray, "stop")
    expected = plat._MacTray if plat.IS_MAC else plat._PystrayTray
    assert isinstance(tray, expected)
