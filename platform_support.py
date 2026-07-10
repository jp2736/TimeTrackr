"""Platform abstraction seam for TimeTrackr.

All OS-specific behaviour lives here so the rest of the app stays platform-neutral.
Windows/Linux keep the original model (pystray on a daemon thread + Tk mainloop).
macOS also runs Tk's mainloop (Tk owns the single Cocoa loop) and attaches a native
NSStatusItem to the NSApplication Tk already created — no second run loop.

pystray and AppKit/Foundation are imported lazily inside the tray classes, so importing
this module never requires either package to be installed.
"""
import os
import sys
import subprocess
import tempfile
import threading

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")


def open_file(path):
    """Open a file with the OS default handler. Raises on failure."""
    path = str(path)
    if IS_WINDOWS:
        os.startfile(path)  # noqa: exists on Windows only
    elif IS_MAC:
        subprocess.run(["open", path], check=True)
    else:
        subprocess.run(["xdg-open", path], check=True)


class MenuItem:
    """Backend-neutral tray menu item.

    label:        text shown in the menu (None for a separator)
    action:       zero-arg callable invoked on click (marshalled onto the Tk thread)
    enabled_when: zero-arg callable -> bool; re-evaluated to grey the item (None = always on)
    default:      True marks the primary (left-click / double-click) action
    """
    def __init__(self, label, action=None, enabled_when=None, default=False,
                 separator=False, visible_when=None):
        self.label = label
        self.action = action
        self.enabled_when = enabled_when
        self.default = default
        self.separator = separator
        self.visible_when = visible_when


SEPARATOR = MenuItem(None, separator=True)


def make_tray(app_name, items, icon_provider):
    """Return a TrayController for the current platform."""
    if IS_MAC:
        return _MacTray(app_name, items, icon_provider)
    return _PystrayTray(app_name, items, icon_provider)


class _PystrayTray:
    """Windows/Linux: pystray on a daemon thread, Tk owns the main thread."""

    def __init__(self, app_name, items, icon_provider):
        self._app_name = app_name
        self._items = items
        self._icon_provider = icon_provider
        self._icon = None
        self._tk_root = None

    def _wrap(self, action):
        return lambda *_: self._tk_root.after(0, action)

    def _enabled(self, item):
        if item.enabled_when is None:
            return True
        return lambda _i: item.enabled_when()

    def _visible(self, item):
        if item.visible_when is None:
            return True
        return lambda _i: item.visible_when()

    def _build_menu(self):
        import pystray
        entries = []
        for it in self._items:
            if it.separator:
                entries.append(pystray.Menu.SEPARATOR)
            else:
                entries.append(pystray.MenuItem(
                    it.label, self._wrap(it.action),
                    default=it.default, enabled=self._enabled(it),
                    visible=self._visible(it)))
        return pystray.Menu(*entries)

    def update_icon(self):
        if self._icon is not None:
            self._icon.icon = self._icon_provider()

    def run(self, tk_root):
        import pystray
        self._tk_root = tk_root
        self._icon = pystray.Icon(
            self._app_name, self._icon_provider(), self._app_name, self._build_menu())
        threading.Thread(target=self._icon.run, daemon=True).start()
        tk_root.mainloop()

    def stop(self):
        if self._icon is not None:
            self._icon.stop()
        if self._tk_root is not None:
            self._tk_root.quit()


def _mac_menu_handler_class():
    """Build the PyObjC NSObject subclass lazily (needs Foundation at call time)."""
    from Foundation import NSObject

    class _MacMenuHandler(NSObject):
        # `items` (list[MenuItem]) and `root` (Tk root) are assigned after alloc/init.
        def menuAction_(self, sender):
            # CRITICAL: do NOT touch Tk here. menuAction_ fires inside Cocoa's menu-
            # tracking run loop; calling into Tcl/Tk from that context (even root.after)
            # corrupts the interpreter thread state and aborts the process with
            # "PyEval_RestoreThread ... thread state is NULL". Instead defer via the
            # Cocoa run loop: performSelector:afterDelay:0 runs runAction_ on the main
            # thread once the menu has closed, where Tk calls are safe.
            self.performSelector_withObject_afterDelay_("runAction:", sender, 0.0)

        def runAction_(self, sender):
            it = self.items[sender.tag()]
            if it.action is not None:
                it.action()

        def validateMenuItem_(self, menu_item):
            it = self.items[menu_item.tag()]
            if it.enabled_when is None:
                return True
            return bool(it.enabled_when())

        def menuNeedsUpdate_(self, menu):
            for idx, it in enumerate(self.items):
                if it.separator or it.visible_when is None:
                    continue
                mi = self.menu_items.get(idx)
                if mi is not None:
                    mi.setHidden_(not it.visible_when())

    return _MacMenuHandler


class _MacTray:
    """macOS: native NSStatusItem sharing Tk's Cocoa loop. Tk owns mainloop()."""

    def __init__(self, app_name, items, icon_provider):
        self._app_name = app_name
        self._items = items
        self._icon_provider = icon_provider
        self._tk_root = None
        self._status_item = None
        self._handler = None

    def _apply_icon(self):
        from AppKit import NSImage
        fd, path = tempfile.mkstemp(suffix=".png", prefix="timetrackr_icon_")
        os.close(fd)
        self._icon_provider().save(path, "PNG")
        image = NSImage.alloc().initWithContentsOfFile_(path)
        image.setSize_((18, 18))
        self._status_item.button().setImage_(image)
        try:
            os.remove(path)
        except OSError:
            pass

    def run(self, tk_root):
        from AppKit import (NSStatusBar, NSMenu, NSMenuItem,
                            NSVariableStatusItemLength, NSApplication,
                            NSApplicationActivationPolicyAccessory)
        self._tk_root = tk_root
        # The caller creates the Tk root before calling run(), so Tk has already
        # installed its own NSApplication subclass; only now is it safe to touch
        # NSApplication. Accessory policy makes this a proper menu-bar app: no Dock
        # icon, and its lifetime is not tied to a visible window.
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
        self._status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)

        self._handler = _mac_menu_handler_class().alloc().init()
        self._handler.items = self._items
        self._handler.root = tk_root
        self._handler.menu_items = {}

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(True)  # consult validateMenuItem_ for enabled state
        menu.setDelegate_(self._handler)  # consult menuNeedsUpdate_ for hidden state
        # note: MenuItem.default is intentionally unused on macOS — clicking the status item opens the menu
        for idx, it in enumerate(self._items):
            if it.separator:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                it.label, "menuAction:", "")
            mi.setTarget_(self._handler)
            mi.setTag_(idx)
            menu.addItem_(mi)
            self._handler.menu_items[idx] = mi
        self._status_item.setMenu_(menu)

        self._apply_icon()
        tk_root.mainloop()

    def update_icon(self):
        if self._status_item is not None:
            self._apply_icon()

    def stop(self):
        if self._status_item is not None:
            from AppKit import NSStatusBar
            NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
            self._status_item = None
        if self._tk_root is not None:
            self._tk_root.quit()
