#!/usr/bin/python3
"""upd — GTK4/libadwaita window for apps outside apt and snap.

Run: upd-gui
Needs /usr/bin/python3 (distro PyGObject, GTK4, and libadwaita).
"""

import sys
import threading
from html import escape
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import upd  # noqa: E402

APP_ID = "dev.azmi.upd"
ICON_ROOT = HERE / "icons"

# Ikon simbolis di icons/hicolor. Warnanya di CSS supaya ikut tema terang/gelap.
SOURCE_ICON = {
    "npm": "upd-npm-symbolic",
    "bun": "upd-bun-symbolic",
    "pipx": "upd-pipx-symbolic",
    "uv": "upd-uv-symbolic",
    "cargo": "upd-cargo-symbolic",
    "flatpak": "upd-flatpak-symbolic",
    "asdf": "upd-asdf-symbolic",
    "script": "upd-script-symbolic",
    "appimage": "upd-appimage-symbolic",
    "manual": "upd-manual-symbolic",
    "openbot": "upd-openbot-symbolic",
    "binary": "upd-binary-symbolic",
}

CSS = b"""
.src-npm      { color: #ff6d6d; }
.src-bun      { color: #f6d32d; }
.src-pipx     { color: #7db8ff; }
.src-uv       { color: #d9a0ea; }
.src-cargo    { color: #ffa573; }
.src-flatpak  { color: #57e389; }
.src-asdf     { color: #c8b0e8; }
.src-script   { color: #8cc8ff; }
.src-appimage { color: #ffb066; }
.src-openbot  { color: #ff8a4c; }
.src-manual, .src-binary { color: alpha(currentColor, .82); }
.ok-mark { color: #57e389; }
.warn-mark { color: #f6d32d; }
.statusbar { padding: 8px 16px; }
.menu-item { padding: 0; border-radius: 8px; }
"""


def label(text, *classes):
    w = Gtk.Label(label=text)
    w.set_valign(Gtk.Align.CENTER)
    for c in classes:
        w.add_css_class(c)
    return w


def source_image(src, size=16):
    icon = SOURCE_ICON.get(src, "package-x-generic-symbolic")
    img = Gtk.Image.new_from_icon_name(icon)
    img.set_pixel_size(size)
    img.set_valign(Gtk.Align.CENTER)
    img.add_css_class(f"src-{src}" if src in SOURCE_ICON else "src-manual")
    img.set_tooltip_text(src)
    return img


def icon_button(icon, text, *classes):
    btn = Gtk.Button()
    box = Gtk.Box(spacing=6)
    img = Gtk.Image.new_from_icon_name(icon)
    img.set_pixel_size(16)
    lab = Gtk.Label(label=text)
    box.append(img)
    box.append(lab)
    btn.set_child(box)
    btn._upd_label = lab
    for c in classes:
        btn.add_css_class(c)
    return btn


def install_icon_theme(display):
    theme = Gtk.IconTheme.get_for_display(display)
    path = str(ICON_ROOT)
    if path not in list(theme.get_search_path()):
        theme.add_search_path(path)


class Row(Adw.ActionRow):
    def __init__(self, data, on_toggle=None, selected=False, gutter=False):
        super().__init__(title=escape(data["name"]))
        src = data["source"]
        cur, latest = str(data["current"]), str(data["latest"])
        if data["has_update"] and latest not in ("?", cur):
            subtitle = f"{cur}  →  {latest}    ·    {src}"
        else:
            subtitle = f"{cur}    ·    {src}"
        self.set_subtitle(escape(subtitle))

        prefix = Gtk.Box(spacing=10)
        prefix.set_valign(Gtk.Align.CENTER)
        self.check = None
        if on_toggle is not None and data["has_update"] and data["upgrade"]:
            self.check = Gtk.CheckButton()
            self.check.set_valign(Gtk.Align.CENTER)
            self.check.set_tooltip_text("Select for update")
            self.check.set_active(bool(selected))
            self.check.connect("toggled", lambda cb: on_toggle(data, cb.get_active()))
            prefix.append(self.check)
        elif gutter:
            gap = Gtk.CheckButton()
            gap.set_opacity(0)
            gap.set_sensitive(False)
            gap.set_can_target(False)
            prefix.append(gap)
        prefix.append(source_image(src, 20))
        self.add_prefix(prefix)

        self.update_button = None
        if data["has_update"] and data["upgrade"]:
            btn = icon_button("software-update-available-symbolic", "Update",
                              "pill", "suggested-action")
            btn.set_valign(Gtk.Align.CENTER)
            self.add_suffix(btn)
            self.update_button = btn
        elif data["has_update"]:
            hint = Gtk.Box(spacing=6)
            hint.set_valign(Gtk.Align.CENTER)
            mark = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            mark.set_pixel_size(16)
            mark.add_css_class("warn-mark")
            hint.append(mark)
            hint.append(label("Manual", "dim-label"))
            hint.set_tooltip_text("A newer version exists, but there is no automatic update")
            self.add_suffix(hint)


REMOVABLE = {"npm", "bun", "pipx", "uv", "cargo", "flatpak", "appimage", "manual"}


class DetailDialog(Adw.Dialog):
    """Detail page: update is the main action, delete sits on the other side."""

    def __init__(self, data, win):
        super().__init__(title="")
        self.set_content_width(640)
        self.data = data
        self.win = win

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(18)

        hero = Gtk.Box(spacing=14)
        hero.append(source_image(data["source"], 40))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        titles.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label=data["name"], xalign=0)
        name.add_css_class("title-2")
        name.set_wrap(True)
        name.set_selectable(True)
        cur, latest = str(data["current"]), str(data["latest"])
        if data["has_update"] and latest not in ("?", cur):
            version = f"{cur}  →  {latest}"
        else:
            version = cur
        sub = Gtk.Label(label=f"{data['source']}   ·   {version}", xalign=0)
        sub.add_css_class("dim-label")
        sub.set_selectable(True)
        titles.append(name)
        titles.append(sub)
        hero.append(titles)
        box.append(hero)

        facts = []
        if data.get("target"):
            facts.append(self._fact("Location", str(data["target"]), "folder-symbolic"))
        if data.get("note"):
            facts.append(self._fact("Note", str(data["note"]), "dialog-information-symbolic"))
        if data.get("upgrade"):
            facts.append(self._fact("How to update", upd.describe_upgrade(data["upgrade"]),
                                   "software-update-available-symbolic"))
        elif data["has_update"]:
            facts.append(self._fact("How to update", "No automatic path yet", "dialog-warning-symbolic"))
        if data.get("url"):
            link = self._fact("Release page", str(data["url"]), "web-browser-symbolic")
            link.set_activatable(True)
            link.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
            link.connect("activated", lambda *_: self._open_url(str(data["url"])))
            facts.append(link)
        if facts:
            info = Adw.PreferencesGroup()
            for fact in facts:
                info.add(fact)
            box.append(info)

        actions = Gtk.Box(spacing=8)
        if data["source"] in REMOVABLE:
            rm = icon_button("user-trash-full-symbolic", "Delete…", "pill", "destructive-action")
            rm.connect("clicked", self._confirm_remove)
            actions.append(rm)
        actions.append(Gtk.Box(hexpand=True))
        if data["has_update"] and data["upgrade"]:
            btn = icon_button("software-update-available-symbolic", "Update",
                              "pill", "suggested-action")
            btn.connect("clicked", self._on_update)
            actions.append(btn)
        elif data["has_update"]:
            actions.append(label("Update is manual", "dim-label"))
        box.append(actions)

        view.set_content(box)
        self.set_child(view)

    @staticmethod
    def _fact(title, subtitle, icon):
        row = Adw.ActionRow(title=escape(title), subtitle=escape(subtitle or "—"))
        row.set_subtitle_selectable(True)
        row.set_subtitle_lines(3)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(16)
        row.add_prefix(img)
        return row

    def _open_url(self, url):
        Gtk.UriLauncher.new(url).launch(self.win, None, None)

    def _on_update(self, *_):
        self.win.start_update(self.data)
        self.close()

    def _confirm_remove(self, *_):
        src, name = self.data["source"], self.data["name"]
        cara = "Moved to Trash. You can restore it." if src in ("appimage", "manual") \
            else "Removed with that package manager's own command."
        dlg = Adw.AlertDialog(heading=f"Delete {name}?", body=cara)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Delete")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda d, r: self._remove(r))
        dlg.present(self)

    def _remove(self, response):
        if response != "ok":
            return
        src, name = self.data["source"], self.data["name"]
        self.close()

        def work():
            import contextlib
            import io as _io
            buf = _io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = upd.main(["uninstall", f"{src}:{name}", "-y"])
            except SystemExit as exc:
                rc = exc.code
            except Exception as exc:  # noqa: BLE001
                rc, buf = 1, _io.StringIO(str(exc))
            lines = [l for l in buf.getvalue().strip().splitlines() if l.strip()]
            GLib.idle_add(self.win.after_remove, name, rc == 0, lines[-1] if lines else "")

        threading.Thread(target=work, daemon=True).start()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="upd")
        self.set_default_size(1000, 720)
        self.rows = []
        self.view = "installed"
        self.busy = False
        self.selected = {}
        install_icon_theme(self.get_display())
        self.set_icon_name(APP_ID)
        self._load_css()
        self._build()
        self.viewstack.set_visible_child_name("updates")
        self.reload(network=True)

    # ---------------------------------------------------------------- ui

    def _load_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _make_page(self, empty_icon, empty_title, empty_desc):
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.set_margin_top(6)
        lb.set_margin_bottom(18)
        lb.set_margin_start(18)
        lb.set_margin_end(18)
        lb.set_valign(Gtk.Align.START)

        clamp = Adw.Clamp(maximum_size=920, child=lb)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=clamp)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        st = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        st.add_named(scroll, "list")
        empty = Adw.StatusPage(icon_name=empty_icon, title=empty_title, description=empty_desc)
        st.add_named(empty, "empty")
        return lb, st, empty

    def _menu_button_row(self, icon, text, action):
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("menu-item")
        box = Gtk.Box(spacing=10)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(12)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(16)
        lab = Gtk.Label(label=text, xalign=0, hexpand=True)
        box.append(img)
        box.append(lab)
        btn.set_child(box)
        btn.connect("clicked", lambda *_: self._fire_menu(action))
        return btn

    def _fire_menu(self, action):
        pop = self.menu_btn.get_popover()
        if pop is not None:
            pop.popdown()
        self.activate_action(action, None)

    def _build(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self.list_installed, self.stack_installed, self.empty_installed = self._make_page(
            "system-search-symbolic", "No results", "Try another search.")
        self.list_updates, self.stack_updates, self.empty_updates = self._make_page(
            "emblem-ok-symbolic", "No updates", "Every app is already current.")

        self.viewstack = Adw.ViewStack()
        self.viewstack.add_titled_with_icon(
            self.stack_installed, "installed", "Installed", "view-list-symbolic")
        self.page_updates = self.viewstack.add_titled_with_icon(
            self.stack_updates, "updates", "Update", "software-update-available-symbolic")
        self.viewstack.connect("notify::visible-child-name", lambda *_: (self._rebuild(), self._sync_selection()))

        self.switcher = Adw.ViewSwitcher(
            stack=self.viewstack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.switcher)

        scan_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        scan_btn.set_tooltip_text("Check for updates (needs the network)")
        scan_btn.connect("clicked", lambda *_: self.reload(network=True))
        header.pack_start(scan_btn)

        self.sel_btn = icon_button("software-update-available-symbolic", "Update selected",
                                   "suggested-action")
        self.sel_btn.set_valign(Gtk.Align.CENTER)
        self.sel_btn.set_visible(False)
        self.sel_btn.connect("clicked", self._confirm_update_selected)
        header.pack_end(self.sel_btn)

        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        menu_box.set_margin_top(4)
        menu_box.set_margin_bottom(4)
        menu_box.set_margin_start(4)
        menu_box.set_margin_end(4)
        entries = (
            ("view-refresh-symbolic", "Check for updates", "win.scan"),
            ("edit-select-all-symbolic", "Select all shown", "win.select-all"),
            ("edit-clear-symbolic", "Clear selection", "win.select-none"),
            ("software-update-available-symbolic", "Update all", "win.update-all"),
        )
        for icon, text, action in entries:
            menu_box.append(self._menu_button_row(icon, text, action))
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        menu_box.append(sep)
        for icon, text, action in (
            ("edit-delete-symbolic", "Clean orphaned launchers", "win.clean"),
            ("user-trash-full-symbolic", "Clean old versions", "win.clean-stale"),
            ("help-about-symbolic", "About", "win.about"),
        ):
            menu_box.append(self._menu_button_row(icon, text, action))
        pop = Gtk.Popover()
        pop.set_child(menu_box)
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", popover=pop)
        self.menu_btn.set_tooltip_text("Menu")
        header.pack_end(self.menu_btn)

        self.search = Gtk.SearchEntry(placeholder_text="Search apps or sources…")
        self.search.set_margin_top(10)
        self.search.set_margin_bottom(4)
        self.search.set_margin_start(18)
        self.search.set_margin_end(18)
        self.search.connect("search-changed", lambda *_: self._rebuild())
        search_clamp = Adw.Clamp(maximum_size=920, child=self.search)

        self.spinner = Gtk.Spinner()
        self.status_icon = Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic")
        self.status_icon.set_pixel_size(16)
        self.status = label("", "statusbar")
        self.status.set_xalign(0)
        self.status.set_hexpand(True)

        bar = Gtk.Box(spacing=8)
        bar.add_css_class("statusbar")
        bar.append(self.spinner)
        bar.append(self.status_icon)
        bar.append(self.status)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(search_clamp)
        body.append(self.viewstack)
        body.append(bar)

        view.add_top_bar(header)
        view.set_content(body)

        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)

        for name, cb in (
            ("scan", lambda *_: self.reload(network=True)),
            ("select-all", self._select_all),
            ("select-none", self._select_none),
            ("update-all", self._confirm_update_all),
            ("clean", self._clean),
            ("clean-stale", self._clean_stale),
            ("about", self._about),
        ):
            act = Gio.SimpleAction(name=name)
            act.connect("activate", cb)
            self.add_action(act)

    # ---------------------------------------------------------------- data

    def _set_status(self, text, kind):
        self.status.set_text(text)
        icons = {
            "busy": "emblem-synchronizing-symbolic",
            "ok": "emblem-ok-symbolic",
            "warn": "software-update-available-symbolic",
            "err": "dialog-warning-symbolic",
        }
        self.status_icon.set_from_icon_name(icons[kind])
        for css in ("ok-mark", "warn-mark"):
            self.status_icon.remove_css_class(css)
        if kind == "ok":
            self.status_icon.add_css_class("ok-mark")
        elif kind in ("warn", "err"):
            self.status_icon.add_css_class("warn-mark")

    def reload(self, network=False):
        if self.busy:
            self.toast("Wait, still loading…")
            return
        self.busy = True
        self.spinner.start()
        self._set_status("Checking latest versions…" if network else "Reading installed apps…", "busy")
        threading.Thread(target=self._load, args=(network,), daemon=True).start()

    def _load(self, network):
        try:
            rows = upd.collect(None, with_latest=network)
            tracked = {r["name"] for r in rows if r["source"] in ("script", "asdf")}
            rows += upd.manual_rows(tracked)
            GLib.idle_add(self._apply, rows, network, None)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._apply, [], network, exc)

    def _apply(self, rows, network, error):
        self.busy = False
        self.spinner.stop()
        if error:
            self._set_status(f"Failed: {error}", "err")
            self.toast(f"Could not load: {error}")
            return
        self.rows = rows
        keys = {self._key(r) for r in rows if r["has_update"] and r["upgrade"]}
        self.selected = {k: v for k, v in self.selected.items() if k in keys}
        n = sum(1 for r in rows if r["has_update"])
        self.page_updates.set_badge_number(n)
        self._sync_selection()
        self._rebuild(both=True)
        if network:
            self._set_status(
                f"{len(rows)} apps · {n} with an update" if n else f"{len(rows)} apps · all current",
                "warn" if n else "ok",
            )
        else:
            self._set_status(f"{len(rows)} apps installed · check to compare versions", "busy")

    def _matches(self, page, r):
        if page == "updates" and not r["has_update"]:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in r["name"].lower() or q in r["source"]

    def _page_widgets(self, page):
        if page == "updates":
            return self.list_updates, self.stack_updates, self.empty_updates
        return self.list_installed, self.stack_installed, self.empty_installed

    def _fill(self, page):
        box, stack, empty = self._page_widgets(page)
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

        shown = [r for r in self.rows if self._matches(page, r)]
        shown.sort(key=lambda r: (not r["has_update"], r["source"], r["name"].lower()))
        gutter = any(r["has_update"] and r["upgrade"] for r in shown)
        for data in shown:
            key = self._key(data)
            row = Row(data, on_toggle=self._toggle_select, selected=key in self.selected, gutter=gutter)
            row.set_activatable(True)
            row.connect("activated", self._open_detail, data)
            if getattr(row, "update_button", None):
                row.update_button.connect("clicked", self._update_one, data, row.update_button)
            box.append(row)

        if shown:
            stack.set_visible_child_name("list")
            return
        if page == "updates":
            empty.set_icon_name("emblem-ok-symbolic")
            empty.set_title("No updates")
            empty.set_description("Every app is already current." if not self.search.get_text().strip()
                                  else "No updates match this search.")
        else:
            empty.set_icon_name("system-search-symbolic")
            empty.set_title("No results")
            empty.set_description("Try another search.")
        stack.set_visible_child_name("empty")

    # ---------------------------------------------------------------- pilihan

    @staticmethod
    def _key(data):
        return f"{data['source']}:{data['name']}"

    def _toggle_select(self, data, active):
        key = self._key(data)
        if active:
            self.selected[key] = data
        else:
            self.selected.pop(key, None)
        self._sync_selection()

    def _sync_selection(self):
        btn = getattr(self, "sel_btn", None)
        if btn is None:
            return
        n = len(self.selected)
        btn.set_visible(bool(n))
        word = "Update 1 selected" if n == 1 else f"Update {n} selected"
        btn._upd_label.set_label(word)
        btn.set_sensitive(bool(n) and not self.busy)

    def _select_all(self, *_):
        page = self.viewstack.get_visible_child_name()
        for r in self.rows:
            if r["has_update"] and r["upgrade"] and self._matches(page, r):
                self.selected[self._key(r)] = r
        self._fill(page)
        self._sync_selection()

    def _select_none(self, *_):
        self.selected.clear()
        self._fill(self.viewstack.get_visible_child_name())
        self._sync_selection()

    def _confirm_update_selected(self, *_):
        todo = list(self.selected.values())
        if not todo:
            self.toast("Nothing selected yet")
            return
        dlg = Adw.AlertDialog(
            heading=f"Update {len(todo)} selected apps?",
            body=", ".join(r["name"] for r in todo[:6])
                 + ("…" if len(todo) > 6 else "")
                 + "\n\nCommands run one at a time. System binaries need passwordless sudo.",
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Update")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda d, resp: self._update_selected(todo) if resp == "ok" else None)
        dlg.present(self)

    def _update_selected(self, todo):
        self.selected.clear()
        self._sync_selection()
        self._update_all(todo)

    def _rebuild(self, both=False):
        page = self.viewstack.get_visible_child_name() or "installed"
        self.view = page
        for p in (["installed", "updates"] if both else [page]):
            self._fill(p)

    # ---------------------------------------------------------------- aksi

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def _open_detail(self, _row, data):
        DetailDialog(data, self).present(self)

    def _update_one(self, _btn, data, button):
        self.start_update(data, button)

    def start_update(self, data, button=None):
        if self.busy:
            self.toast("Wait, another job is still running")
            return
        self.busy = True
        if button is not None:
            button.set_sensitive(False)
            if getattr(button, "_upd_label", None):
                button._upd_label.set_label("…")
        self._set_status(f"Updating {data['name']}…", "busy")
        self.spinner.start()
        threading.Thread(target=self._run_one, args=(data,), daemon=True).start()

    def after_remove(self, name, ok, msg):
        self.toast(f"{name}: {'removed' if ok else 'failed — ' + msg}")
        self.reload(network=True)

    def _run_one(self, data):
        cmd = data.get("upgrade") or []
        try:
            ok, msg = upd.execute_upgrade(cmd)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)
        GLib.idle_add(self._after_one, data, ok, msg)

    def _after_one(self, data, ok, msg):
        self.toast(f"{data['name']}: {'done' if ok else 'failed — ' + msg}")
        self.busy = False
        self.spinner.stop()
        self.reload(network=True)

    def _confirm_update_all(self, *_):
        todo = [r for r in self.rows if r["has_update"] and r["upgrade"]]
        if not todo:
            self.toast("No automatic updates")
            return
        dlg = Adw.AlertDialog(
            heading=f"Update {len(todo)} apps?",
            body="Commands run one at a time. System binaries need passwordless sudo.",
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Update")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda d, resp: self._update_all(todo) if resp == "ok" else None)
        dlg.present(self)

    def _update_all(self, todo):
        if self.busy:
            return
        self.busy = True
        self.spinner.start()
        threading.Thread(target=self._run_all, args=(todo,), daemon=True).start()

    def _run_all(self, todo):
        ok_count = 0
        for i, data in enumerate(todo, 1):
            GLib.idle_add(self._set_status, f"({i}/{len(todo)}) {data['name']}…", "busy")
            if self._run_one_sync(data):
                ok_count += 1
        GLib.idle_add(self._after_all, ok_count, len(todo))

    def _run_one_sync(self, data):
        ok, _ = upd.execute_upgrade(data.get("upgrade") or [])
        return ok

    def _after_all(self, ok_count, total):
        self.busy = False
        self.spinner.stop()
        self.toast(f"{ok_count}/{total} updates ran")
        self.reload(network=True)

    def _clean(self, *_):
        def work():
            removed = upd.orphan_desktop_entries(dry_run=False)
            GLib.idle_add(self._after_clean, removed)
        threading.Thread(target=work, daemon=True).start()

    def _after_clean(self, removed):
        self.toast(f"{len(removed)} launcher entries cleaned" if removed else "No orphaned launchers")

    def _clean_stale(self, *_):
        items = upd.stale_items()
        if not items:
            self.toast("No old versions left")
            return
        dlg = Adw.AlertDialog(
            heading=f"Clean {len(items)} old versions?",
            body="Everything moves to Trash, so you can restore it.",
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Clean")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def run(_dlg, resp):
            if resp != "ok":
                return
            def work():
                for it in items:
                    upd.trash_file(it["path"])
                GLib.idle_add(self._after_stale, len(items))
            threading.Thread(target=work, daemon=True).start()

        dlg.connect("response", run)
        dlg.present(self)

    def _after_stale(self, count):
        self.toast(f"{count} old versions moved to Trash")
        self.reload(network=True)

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name="upd",
            application_icon=APP_ID,
            developer_name="Azmi Farih",
            version="1.0",
            comments="Manage apps that were not installed with apt or snap.",
        )
        about.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main():
    app = App()
    if "--self-test" in sys.argv:
        GLib.timeout_add_seconds(3, lambda: app.quit() or False)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
