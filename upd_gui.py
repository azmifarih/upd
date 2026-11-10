#!/usr/bin/python3
"""upd — GUI GTK4/libadwaita untuk mengelola aplikasi di luar apt/snap.

Jalankan: upd-gui
Butuh /usr/bin/python3 (PyGObject + GTK4 + libadwaita dari distro).
"""

import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import upd  # noqa: E402

APP_ID = "dev.azmi.upd"

CSS = b"""
.chip {
  font-size: 10px; font-weight: 700; letter-spacing: .04em;
  padding: 3px 8px; border-radius: 999px;
  background: alpha(currentColor, .10);
  color: alpha(currentColor, .75);
}
.chip-npm      { background: alpha(#e01b24, .16); color: #e01b24; }
.chip-bun      { background: alpha(#f6d32d, .22); color: #b58900; }
.chip-pipx     { background: alpha(#1c71d8, .16); color: #1c71d8; }
.chip-uv       { background: alpha(#813d9c, .16); color: #813d9c; }
.chip-cargo    { background: alpha(#c64600, .16); color: #c64600; }
.chip-flatpak  { background: alpha(#26a269, .16); color: #26a269; }
.chip-asdf     { background: alpha(#613583, .16); color: #613583; }
.chip-script   { background: alpha(#3584e4, .16); color: #3584e4; }
.chip-appimage { background: alpha(#e66100, .16); color: #e66100; }
.chip-manual   { background: alpha(currentColor, .10); color: alpha(currentColor, .70); }
.chip-update   { background: alpha(#e5a50a, .20); color: #b07500; }
.statusbar { padding: 6px 14px; }
.hero { font-size: 13px; }
"""


def label(text, *classes):
    w = Gtk.Label(label=text)
    w.set_valign(Gtk.Align.CENTER)
    for c in classes:
        w.add_css_class(c)
    return w


class Row(Adw.ActionRow):
    def __init__(self, data, on_toggle=None, selected=False):
        name = data["name"]
        if len(name) > 46:
            name = name[:43] + "…"
        super().__init__(title=name)

        src = data["source"]
        chips = Gtk.Box(spacing=6)
        chips.set_valign(Gtk.Align.CENTER)

        self.check = None
        if on_toggle is not None and data["has_update"] and data["upgrade"]:
            self.check = Gtk.CheckButton()
            self.check.set_valign(Gtk.Align.CENTER)
            self.check.set_tooltip_text("Pilih untuk update")
            self.check.set_active(bool(selected))
            self.check.connect("toggled", lambda cb: on_toggle(data, cb.get_active()))
            chips.append(self.check)

        chips.append(label(src, "chip", f"chip-{src}"))
        self.add_prefix(chips)

        cur, latest = str(data["current"]), str(data["latest"])
        if data["has_update"] and latest not in ("?", cur):
            self.set_subtitle(f"{cur}  →  {latest}")
            if hasattr(self, "add_suffix") and not data["upgrade"]:
                # tanpa tombol Update, chip ini yang menjelaskan kenapa tidak ada aksi
                self.add_suffix(label("manual", "chip"))
        else:
            self.set_subtitle(cur)

        if data["has_update"] and data["upgrade"]:
            btn = Gtk.Button(label="Update")
            btn.add_css_class("pill")
            btn.add_css_class("suggested-action")
            btn.set_valign(Gtk.Align.CENTER)
            if hasattr(self, "add_suffix"):
                self.add_suffix(btn)
            self.update_button = btn


REMOVABLE = {"npm", "bun", "pipx", "uv", "cargo", "flatpak", "appimage", "manual"}


class DetailDialog(Adw.Dialog):
    """Halaman detail: update sebagai aksi utama, hapus dijauhkan dari situ."""

    def __init__(self, data, win):
        super().__init__(title=data["name"])
        self.set_content_width(560)
        self.data = data
        self.win = win

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(18)

        info = Adw.PreferencesGroup()
        info.add(Adw.ActionRow(title="Sumber", subtitle=data["source"]))
        info.add(Adw.ActionRow(title="Terpasang", subtitle=str(data["current"])))
        info.add(Adw.ActionRow(title="Terbaru", subtitle=str(data["latest"])))
        if data.get("target"):
            info.add(Adw.ActionRow(title="Lokasi", subtitle=str(data["target"])))
        if data.get("note"):
            info.add(Adw.ActionRow(title="Catatan", subtitle=str(data["note"])))
        if data.get("upgrade"):
            info.add(Adw.ActionRow(title="Cara update",
                                   subtitle=upd.describe_upgrade(data["upgrade"])))
        if data.get("url"):
            info.add(Adw.ActionRow(title="Halaman rilis", subtitle=str(data["url"])))
        box.append(info)

        actions = Gtk.Box(spacing=8)
        actions.set_halign(Gtk.Align.END)

        if data["has_update"] and data["upgrade"]:
            btn = Gtk.Button(label="Update")
            btn.add_css_class("pill")
            btn.add_css_class("suggested-action")
            btn.connect("clicked", self._on_update)
            actions.append(btn)
        elif data["has_update"]:
            hint = label("Update perlu manual", "chip")
            actions.append(hint)

        if data["source"] in REMOVABLE:
            rm = Gtk.Button(label="Hapus…")
            rm.add_css_class("pill")
            rm.add_css_class("destructive-action")
            rm.connect("clicked", self._confirm_remove)
            actions.append(rm)

        close = Gtk.Button(label="Tutup")
        close.add_css_class("pill")
        close.connect("clicked", lambda *_: self.close())
        actions.append(close)

        box.append(actions)
        view.set_content(box)
        self.set_child(view)

    def _on_update(self, *_):
        self.win.start_update(self.data)
        self.close()

    def _confirm_remove(self, *_):
        src, name = self.data["source"], self.data["name"]
        cara = "Dipindah ke Trash, masih bisa dikembalikan." if src in ("appimage", "manual") \
            else "Dihapus lewat perintah resmi paketnya."
        dlg = Adw.AlertDialog(heading=f"Hapus {name}?", body=cara)
        dlg.add_response("cancel", "Batal")
        dlg.add_response("ok", "Hapus")
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

    def _make_page(self):
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.set_margin_top(12)
        lb.set_margin_bottom(18)
        lb.set_margin_start(18)
        lb.set_margin_end(18)
        lb.set_valign(Gtk.Align.START)

        clamp = Adw.Clamp(maximum_size=920, child=lb)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=clamp)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        st = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        st.add_named(scroll, "list")
        empty = Adw.StatusPage(
            icon_name="emblem-ok-symbolic",
            title="Tidak ada update",
            description="Semua aplikasi sudah versi terbaru.",
        )
        st.add_named(empty, "empty")
        return lb, st, empty

    def _build(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self.list_installed, self.stack_installed, self.empty_installed = self._make_page()
        self.list_updates, self.stack_updates, self.empty_updates = self._make_page()

        self.viewstack = Adw.ViewStack()
        self.viewstack.add_titled_with_icon(
            self.stack_installed, "installed", "Terpasang", "view-list-symbolic")
        self.viewstack.add_titled_with_icon(
            self.stack_updates, "updates", "Update", "software-update-available-symbolic")
        self.viewstack.connect("notify::visible-child-name", lambda *_: (self._rebuild(), self._sync_selection()))

        self.switcher = Adw.ViewSwitcher(
            stack=self.viewstack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.switcher)

        scan_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        scan_btn.set_tooltip_text("Periksa update (butuh jaringan)")
        scan_btn.connect("clicked", lambda *_: self.reload(network=True))
        header.pack_start(scan_btn)

        self.sel_btn = Gtk.Button(label="Update terpilih")
        self.sel_btn.add_css_class("suggested-action")
        self.sel_btn.set_valign(Gtk.Align.CENTER)
        self.sel_btn.set_visible(False)
        self.sel_btn.connect("clicked", self._confirm_update_selected)
        header.pack_end(self.sel_btn)

        menu = Gio.Menu()
        menu.append("Periksa update", "win.scan")
        menu.append("Pilih semua yang tampil", "win.select-all")
        menu.append("Batal pilih", "win.select-none")
        menu.append("Update semua", "win.update-all")
        menu.append("Bersihkan launcher yatim", "win.clean")
        menu.append("Bersihkan sisa versi lama", "win.clean-stale")
        menu.append("Tentang", "win.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)

        self.search = Gtk.SearchEntry(placeholder_text="Cari aplikasi…")
        self.search.set_margin_top(6)
        self.search.set_margin_bottom(6)
        self.search.set_margin_start(12)
        self.search.set_margin_end(12)
        self.search.connect("search-changed", lambda *_: self._rebuild())

        self.spinner = Gtk.Spinner()
        self.status = label("", "statusbar")
        self.status.set_xalign(0)
        self.status.set_hexpand(True)

        bar = Gtk.Box(spacing=8)
        bar.add_css_class("statusbar")
        bar.append(self.spinner)
        bar.append(self.status)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.search)
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

    def reload(self, network=False):
        if self.busy:
            self.toast("Tunggu, masih memuat…")
            return
        self.busy = True
        self.spinner.start()
        self.status.set_text("Memeriksa versi terbaru…" if network else "Membaca daftar aplikasi…")
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
            self.status.set_text(f"Gagal: {error}")
            self.toast(f"Gagal memuat: {error}")
            return
        self.rows = rows
        keys = {self._key(r) for r in rows if r["has_update"] and r["upgrade"]}
        self.selected = {k: v for k, v in self.selected.items() if k in keys}
        self._sync_selection()
        self._rebuild(both=True)
        if network:
            n = sum(1 for r in rows if r["has_update"])
            self.status.set_text(
                f"{len(rows)} aplikasi · {n} punya update" if n else f"{len(rows)} aplikasi · semua terbaru"
            )
        else:
            self.status.set_text(f"{len(rows)} aplikasi terpasang · klik ⟳ untuk memeriksa update")

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
        for data in shown:
            key = self._key(data)
            row = Row(data, on_toggle=self._toggle_select, selected=key in self.selected)
            row.set_activatable(True)
            row.connect("activated", self._open_detail, data)
            if getattr(row, "update_button", None):
                row.update_button.connect("clicked", self._update_one, data, row.update_button)
            box.append(row)

        if shown:
            stack.set_visible_child_name("list")
            return
        if page == "updates":
            empty.set_title("Tidak ada update")
            empty.set_description("Semua aplikasi sudah versi terbaru.")
        else:
            empty.set_title("Tidak ada hasil")
            empty.set_description("Coba kata kunci lain.")
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
        btn.set_label(f"Update {n} terpilih" if n != 1 else "Update 1 terpilih")
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
            self.toast("Belum ada yang dipilih")
            return
        dlg = Adw.AlertDialog(
            heading=f"Update {len(todo)} aplikasi terpilih?",
            body=", ".join(r["name"] for r in todo[:6])
                 + ("…" if len(todo) > 6 else "")
                 + "\n\nPerintah dijalankan satu per satu. Binary sistem butuh sudo tanpa password.",
        )
        dlg.add_response("cancel", "Batal")
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
            self.toast("Tunggu, masih ada proses lain")
            return
        self.busy = True
        if button is not None:
            button.set_sensitive(False)
            button.set_label("…")
        self.status.set_text(f"Memperbarui {data['name']}…")
        threading.Thread(target=self._run_one, args=(data,), daemon=True).start()

    def after_remove(self, name, ok, msg):
        self.toast(f"{name}: {'dihapus' if ok else 'gagal — ' + msg}")
        self.reload(network=True)

    def _run_one(self, data):
        cmd = data.get("upgrade") or []
        try:
            ok, msg = upd.execute_upgrade(cmd)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)
        GLib.idle_add(self._after_one, data, ok, msg)

    def _after_one(self, data, ok, msg):
        self.toast(f"{data['name']}: {'berhasil' if ok else 'gagal — ' + msg}")
        self.busy = False
        self.reload(network=True)

    def _confirm_update_all(self, *_):
        todo = [r for r in self.rows if r["has_update"] and r["upgrade"]]
        if not todo:
            self.toast("Tidak ada update otomatis")
            return
        dlg = Adw.AlertDialog(
            heading=f"Update {len(todo)} aplikasi?",
            body="Perintah dijalankan satu per satu. Binary sistem butuh sudo tanpa password.",
        )
        dlg.add_response("cancel", "Batal")
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
            GLib.idle_add(self.status.set_text, f"({i}/{len(todo)}) {data['name']}…")
            if self._run_one_sync(data):
                ok_count += 1
        GLib.idle_add(self._after_all, ok_count, len(todo))

    def _run_one_sync(self, data):
        ok, _ = upd.execute_upgrade(data.get("upgrade") or [])
        return ok

    def _after_all(self, ok_count, total):
        self.busy = False
        self.spinner.stop()
        self.toast(f"{ok_count}/{total} update dijalankan")
        self.reload(network=True)

    def _clean(self, *_):
        def work():
            removed = upd.orphan_desktop_entries(dry_run=False)
            GLib.idle_add(self._after_clean, removed)
        threading.Thread(target=work, daemon=True).start()

    def _after_clean(self, removed):
        self.toast(f"{len(removed)} entri launcher dibersihkan" if removed else "Tidak ada launcher yatim")

    def _clean_stale(self, *_):
        items = upd.stale_items()
        if not items:
            self.toast("Tidak ada sisa versi lama")
            return
        dlg = Adw.AlertDialog(
            heading=f"Bersihkan {len(items)} sisa versi lama?",
            body="Semua dipindah ke Trash, jadi masih bisa dikembalikan.",
        )
        dlg.add_response("cancel", "Batal")
        dlg.add_response("ok", "Bersihkan")
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
        self.toast(f"{count} sisa versi lama dipindah ke Trash")
        self.reload(network=True)

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name="upd",
            application_icon=APP_ID,
            developer_name="Azmi Farih",
            version="1.0",
            comments="Kelola aplikasi yang tidak diinstall lewat apt atau snap.",
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
