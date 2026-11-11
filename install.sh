#!/bin/sh
# Install upd for the current user, or remove that install.
# From a clone: ./install.sh
# From the internet:
#   curl -fsSL https://raw.githubusercontent.com/azmifarih/upd/main/install.sh | sh
# System-wide: sudo ./install.sh --prefix /usr/local
set -eu

REPO_URL="${UPD_REPO_URL:-https://github.com/azmifarih/upd}"
REPO_REF="${UPD_REPO_REF:-main}"
APP_ID=dev.azmi.upd
MARKER="installed by upd install.sh"

prefix="${PREFIX:-${HOME:?HOME is not set}/.local}"
action=install

usage() {
  cat <<'EOF'
Usage: install.sh [--prefix DIR] [--uninstall]

Installs upd into DIR (default: ~/.local). Files land in:
  DIR/bin/upd
  DIR/bin/upd-gui
  DIR/share/upd/
  DIR/share/applications/dev.azmi.upd.desktop
  DIR/share/icons/hicolor/scalable/apps/dev.azmi.upd.svg

The CLI needs python3. The window also needs PyGObject, GTK4, and libadwaita
from the distro, not from a virtualenv.
EOF
}

while [ $# -gt 0 ]; do
  case $1 in
    --prefix)
      [ $# -ge 2 ] || { echo "install.sh: --prefix needs a directory" >&2; exit 2; }
      prefix=$2
      shift 2
      ;;
    --prefix=*)
      prefix=${1#--prefix=}
      shift
      ;;
    --uninstall)
      action=uninstall
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "install.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

die() {
  echo "install.sh: $*" >&2
  exit 1
}

fetch() {
  url=$1
  dest=$2
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$dest" "$url"
  else
    die "curl or wget is required to download upd"
  fi
}

source_tree() {
  script=$0
  case $script in
    */*)
      script_dir=$(CDPATH= cd -- "$(dirname "$script")" && pwd)
      if [ -f "$script_dir/upd.py" ] && [ -f "$script_dir/upd_gui.py" ]; then
        printf '%s\n' "$script_dir"
        return 0
      fi
      ;;
  esac
  command -v tar >/dev/null 2>&1 || die "tar is required to unpack upd"
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/upd-install.XXXXXX")
  tarball=$tmp/upd.tar.gz
  fetch "$REPO_URL/archive/refs/heads/$REPO_REF.tar.gz" "$tarball"
  tar -xzf "$tarball" -C "$tmp"
  tree=$tmp/upd-$REPO_REF
  [ -f "$tree/upd.py" ] || die "download did not contain upd.py"
  printf '%s\n' "$tree"
}

gui_packages() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
  fi
  id=${ID:-}
  case $id in
    ubuntu|debian|linuxmint|pop)
      echo "sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1"
      ;;
    fedora)
      echo "sudo dnf install python3-gobject gtk4 libadwaita"
      ;;
    arch|endeavouros|manjaro)
      echo "sudo pacman -S python-gobject gtk4 libadwaita"
      ;;
    opensuse*|sles)
      echo "sudo zypper install python3-gobject gtk4 libadwaita typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1"
      ;;
    *)
      echo "Install PyGObject, GTK4, and libadwaita from your distro packages."
      ;;
  esac
}

find_python() {
  for py in /usr/bin/python3 python3; do
    command -v "$py" >/dev/null 2>&1 || continue
    if "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      command -v "$py"
      return 0
    fi
  done
  return 1
}

find_gui_python() {
  for py in /usr/bin/python3 python3; do
    command -v "$py" >/dev/null 2>&1 || continue
    if "$py" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' >/dev/null 2>&1; then
      command -v "$py"
      return 0
    fi
  done
  return 1
}

# POSIX quoting for a shell word.
shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

write_launcher() {
  name=$1
  python=$2
  target=$3
  dest=$prefix/bin/$name
  mkdir -p "$prefix/bin"
  # Remove a previous launcher. rm deletes a symlink itself, not the file it points at.
  rm -f "$dest"
  qpy=$(shell_quote "$python")
  qtarget=$(shell_quote "$target")
  cat > "$dest" <<EOF
#!/bin/sh
# $MARKER
exec $qpy $qtarget "\$@"
EOF
  chmod 755 "$dest"
}

remove_if_ours() {
  file=$1
  if [ -f "$file" ] && grep -q "$MARKER" "$file" 2>/dev/null; then
    rm -f "$file"
    echo "removed $file"
  fi
}

do_uninstall() {
  remove_if_ours "$prefix/bin/upd"
  remove_if_ours "$prefix/bin/upd-gui"
  if [ -d "$prefix/share/upd" ]; then
    rm -rf "$prefix/share/upd"
    echo "removed $prefix/share/upd"
  fi
  if [ -f "$prefix/share/applications/$APP_ID.desktop" ] && grep -q "$MARKER" "$prefix/share/applications/$APP_ID.desktop"; then
    rm -f "$prefix/share/applications/$APP_ID.desktop"
    echo "removed $prefix/share/applications/$APP_ID.desktop"
  fi
  icon=$prefix/share/icons/hicolor/scalable/apps/$APP_ID.svg
  if [ -f "$icon" ] && grep -q "upd-app-icon" "$icon"; then
    rm -f "$icon"
    echo "removed $icon"
  fi
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$prefix/share/applications" >/dev/null 2>&1 || true
  fi
  echo "upd uninstalled from $prefix"
}

if [ "$action" = uninstall ]; then
  do_uninstall
  exit 0
fi

[ -w "$(dirname "$prefix")" ] || [ -w "$prefix" ] || [ -d "$prefix" ] || die "$prefix is not writable. Try: sudo ./install.sh --prefix $prefix"
mkdir -p "$prefix" || die "cannot create $prefix (need permission to write there)"
[ -w "$prefix" ] || die "$prefix is not writable"

cli_python=$(find_python) || die "python3 3.9 or newer was not found"
gui_python=$(find_gui_python) || true

src=$(source_tree)
share=$prefix/share/upd
mkdir -p "$share"
cp -f "$src/upd.py" "$src/upd_gui.py" "$share/"
rm -rf "$share/icons"
mkdir -p "$share/icons"
cp -a "$src/icons/hicolor" "$share/icons/"
chmod 755 "$share/upd.py" "$share/upd_gui.py"

write_launcher upd "$cli_python" "$share/upd.py"
if [ -n "$gui_python" ]; then
  write_launcher upd-gui "$gui_python" "$share/upd_gui.py"
else
  write_launcher upd-gui "$cli_python" "$share/upd_gui.py"
fi

mkdir -p "$prefix/share/applications"
desktop=$prefix/share/applications/$APP_ID.desktop
exec_field=$prefix/bin/upd-gui
case $exec_field in
  *[[:space:]]*) exec_field="\"$exec_field\"" ;;
esac
cat > "$desktop" <<EOF
[Desktop Entry]
# $MARKER
Type=Application
Name=upd
GenericName=App Updater
Comment=Manage apps outside apt and snap
Exec=$exec_field
Icon=$APP_ID
Terminal=false
Categories=System;PackageManager;
Keywords=update;upgrade;appimage;npm;pipx;
StartupNotify=true
StartupWMClass=$APP_ID
EOF

icon_dir=$prefix/share/icons/hicolor/scalable/apps
mkdir -p "$icon_dir"
# Tag the icon so uninstall can tell it belongs to this installer.
sed 's/<svg /<svg data-upd="upd-app-icon" /' "$src/icons/hicolor/scalable/apps/$APP_ID.svg" > "$icon_dir/$APP_ID.svg"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$prefix/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t "$prefix/share/icons/hicolor" >/dev/null 2>&1 || true
fi
case $src in
  */upd-install.*) rm -rf "$(dirname "$src")" ;;
esac

echo "Installed upd into $prefix"
echo "  command: $prefix/bin/upd"
echo "  window:  $prefix/bin/upd-gui"
case ":$PATH:" in
  *":$prefix/bin:"*) ;;
  *)
    echo
    echo "This shell does not have $prefix/bin on PATH yet. Open a new terminal, or run:"
    echo "  export PATH=\"$prefix/bin:\$PATH\""
    ;;
esac

if [ -z "$gui_python" ]; then
  echo
  echo "The window is not available yet. Python found GTK4 and libadwaita nowhere."
  echo "Install them, then run this installer again:"
  echo "  $(gui_packages)"
else
  echo "  app list: upd"
fi
