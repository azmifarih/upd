#!/usr/bin/env python3
"""
upd - lihat dan update aplikasi yang TIDAK dikelola apt/snap.

Sumber yang didukung:
  npm       paket global npm
  bun       paket global bun
  pipx      aplikasi Python lewat pipx
  uv        tool Python lewat `uv tool`
  cargo     binary hasil `cargo install`
  flatpak   aplikasi flatpak
  appimage  file .AppImage di folder umum (butuh pemetaan repo GitHub)

Perintah:
  upd scan                 tampilkan semua yang terpasang + versi terbaru
  upd scan --outdated      hanya yang ada update
  upd scan --json          keluaran mesin (untuk rutinitas/cron)
  upd update [sumber...]   jalankan update (minta konfirmasi)
  upd update --dry-run     tampilkan perintahnya saja
  upd appimage map FILE OWNER/REPO
  upd sources              status tiap sumber

Semua perintah `scan` bersifat baca-saja; hanya `update` yang mengubah sistem.
"""

import argparse
import io
import pwd
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SUDO_USER = os.environ.get("SUDO_USER") or ""


def real_home():
    """Lewat sudo, tetap pakai home user asli (SUDO_USER), bukan /root."""
    if os.geteuid() == 0 and SUDO_USER:
        try:
            return Path(pwd.getpwnam(SUDO_USER).pw_dir)
        except KeyError:
            pass
    return Path.home()


HOME = real_home()
CONFIG_DIR = Path(os.environ.get("UPD_CONFIG_DIR", HOME / ".config" / "upd"))
CACHE_DIR = Path(os.environ.get("UPD_CACHE_DIR", HOME / ".cache" / "upd"))
APPIMAGE_MAP = CONFIG_DIR / "appimages.json"
BINARIES_CFG = CONFIG_DIR / "binaries.json"
APPIMAGE_CACHE = CACHE_DIR / "appimage_versions.json"
INSTALLED_CACHE = CACHE_DIR / "installed.json"
SCRIPTS_CFG = CONFIG_DIR / "scripts.json"
TIMEOUT = float(os.environ.get("UPD_HTTP_TIMEOUT", "10"))

SOURCES = ["npm", "bun", "pipx", "uv", "cargo", "flatpak", "appimage", "script", "asdf", "openbot", "binary"]

# runtime yang dibundel OpenBot sendiri (dikelola OpenBot, bukan upd)
OPENBOT_RUNTIME_NPM = {
    "pi": "@earendil-works/pi-coding-agent",
    "codex": "@openai/codex",
    "opencode": "@opencode/cli",
    "bun": "bun",
}

ASDF_REPOS = {
    "kubectl": ("kubernetes/kubernetes", ["v"]),
    "terraform": ("hashicorp/terraform", ["v"]),
    "helm": ("helm/helm", ["v"]),
    "k9s": ("derailed/k9s", ["v"]),
}

# Tool yang biasanya dipasang lewat `curl ... | sh` atau installer resmi.
# kind "tool": bandingkan versi dengan GitHub. kind "git": bandingkan commit.
SCRIPT_REGISTRY = [
    {"name": "bun", "kind": "tool", "path": "~/.bun/bin/bun",
     "version_cmd": ["bun", "--version"], "repo": "oven-sh/bun",
     "tag_strip": ["bun-v", "v"], "update": ["bun", "upgrade"]},
    {"name": "uv", "kind": "tool", "path": "~/.local/bin/uv",
     "version_cmd": ["uv", "--version"], "repo": "astral-sh/uv",
     "tag_strip": ["v"], "update": ["uv", "self", "update"]},
    {"name": "rustup", "kind": "tool", "path": "~/.cargo/bin/rustup",
     "version_cmd": ["rustup", "--version"], "repo": "rust-lang/rustup",
     "tag_strip": ["v"], "update": ["rustup", "self", "update"],
     "manual": "toolchain: rustup update"},
    {"name": "deno", "kind": "tool", "path": "~/.deno/bin/deno",
     "version_cmd": ["deno", "--version"], "repo": "denoland/deno",
     "tag_strip": ["v"], "update": ["deno", "upgrade"]},
    {"name": "ollama", "kind": "tool", "path": "/usr/local/bin/ollama",
     "version_cmd": ["ollama", "--version"], "repo": "ollama/ollama",
     "tag_strip": ["v"], "update": ["__ollama__"]},
    {"name": "starship", "kind": "tool", "path": "~/.local/bin/starship",
     "version_cmd": ["starship", "--version"], "repo": "starship/starship",
     "tag_strip": ["v"], "update": [],
     "manual": "curl -sS https://starship.rs/install.sh | sh -s -- -y"},
    {"name": "zoxide", "kind": "tool", "path": "~/.local/bin/zoxide",
     "version_cmd": ["zoxide", "--version"], "repo": "ajeetdsouza/zoxide",
     "tag_strip": ["v"], "update": [],
     "manual": "curl -sS https://raw.githubusercontent.com/ajeetdsouza/zoxide/main/install.sh | sh"},
    {"name": "oh-my-zsh", "kind": "git", "path": "~/.oh-my-zsh",
     "repo": "robbyrussell/oh-my-zsh",
     "update": ["git", "-C", "~/.oh-my-zsh", "pull", "--ff-only"]},
]


# --------------------------------------------------------------------------- util

def expand_path(value):
    """Expand ~ memakai HOME asli, bukan $HOME milik root saat sudo."""
    v = str(value)
    if v == "~":
        return HOME
    if v.startswith("~/"):
        return Path(HOME) / v[2:]
    return Path(v)


def sh(cmd, timeout=120, cwd=None):
    """Jalankan perintah, kembalikan (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


RATE_LIMIT = "__rate_limited__"
_GH_TOKEN = None


def github_token():
    """Token dari env, atau dari `gh auth token` bila ada. Tidak pernah dicetak."""
    global _GH_TOKEN
    if _GH_TOKEN is not None:
        return _GH_TOKEN
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token and shutil.which("gh"):
        cmd = ["gh", "auth", "token"]
        if os.geteuid() == 0 and SUDO_USER:
            cmd = ["sudo", "-u", SUDO_USER] + cmd  # token milik user, bukan root
        rc, out, _ = sh(cmd, timeout=10)
        if rc == 0:
            token = out.strip()
    _GH_TOKEN = token
    return token


def http_json(url, timeout=None):
    headers = {"User-Agent": "upd/1.0", "Accept": "application/json"}
    token = github_token()
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse_version(v):
    """Ubah versi jadi tuple yang bisa dibandingkan (semver, date, rc-safe)."""
    v = (v or "").strip()
    v = re.sub(r"^[vV]", "", v)
    v = v.split("+")[0]
    core, _, pre = v.partition("-")
    out = []
    for tok in re.split(r"[._]", core):
        if tok.isdigit():
            out.append((1, int(tok), ""))
        elif tok:
            m = re.match(r"(\d+)(.*)", tok)
            if m:
                out.append((1, int(m.group(1)), m.group(2).lower()))
            else:
                out.append((0, 0, tok.lower()))
    if pre:
        for tok in re.split(r"[.\-]", pre):
            if tok.isdigit():
                out.append((-1, int(tok), ""))
            elif tok:
                out.append((-1, 0, tok.lower()))
    return tuple(out)


def is_newer(candidate, current):
    if not candidate or not current:
        return False
    try:
        return parse_version(candidate) > parse_version(current)
    except TypeError:
        return False


VERSION_CMDS = {
    "helm": ["helm", "version", "--short"],
    "kubectl": ["kubectl", "version", "--client"],
    "k0sctl": ["k0sctl", "version"],
    "changie": ["changie", "--version"],
    "docker-compose": ["docker-compose", "version"],
    "flux": ["flux", "--version"],
}


def detect_version(cmd, name, path=None):
    """Jalankan perintah versi, utamakan baris yang menyebut nama tool (k3d vs k3s)."""
    if name in VERSION_CMDS:
        override = VERSION_CMDS[name]
        if path or shutil.which(override[0]) or (isinstance(cmd, list) and cmd and os.path.basename(cmd[0]) == name):
            cmd = list(override)
    if path:
        cmd = [str(path)] + list(cmd[1:])
    rc, out, err = sh(cmd, timeout=15)
    lines = [l for l in (out or err).splitlines() if l.strip()]
    for line in lines:
        if name and name.lower() in line.lower():
            v = find_version(line)
            if v:
                return v
    for line in lines:
        v = find_version(line)
        if v:
            return v
    if re.search(r"\bdev\b|dev build|vdev", " ".join(lines), re.I):
        return "dev"
    return "?"


def find_version(text):
    for m in re.finditer(r"(?<![\d.])(\d+\.\d+(?:\.\d+){0,2})(?![\d.])", text or ""):
        cand = m.group(1)
        parts = cand.split(".")
        if len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts):
            continue  # alamat IP, bukan versi
        return cand
    return None


# --------------------------------------------------------------------------- npm / bun

def npm_installed():
    rc, out, _ = sh(["npm", "ls", "-g", "--depth=0", "--json"], timeout=60)
    if rc != 0 and not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    items = []
    for name, meta in (data.get("dependencies") or {}).items():
        items.append({"name": name, "current": (meta or {}).get("version") or "?"})
    return items


def npm_default_prefix():
    rc, out, _ = sh(["npm", "prefix", "-g"], timeout=30)
    if rc != 0 or not out.strip():
        return None
    return Path(out.strip().splitlines()[-1])


def npm_other_installed():
    """Paket npm global di prefix lain (mis. /usr/local) yang tidak terlihat `npm ls -g`."""
    default = npm_default_prefix()
    items, seen = [], set()
    for prefix in (Path("/usr/local"), Path("/usr")):
        root = prefix / "lib" / "node_modules"
        if not root.is_dir():
            continue
        if default and prefix.resolve() == default.resolve():
            continue
        for pj in sorted(root.glob("*/package.json")) + sorted(root.glob("@*/*/package.json")):
            data = read_json_file(pj) or {}
            name = data.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            bins = data.get("bin")
            if isinstance(bins, str):
                bins = [name.split("/")[-1]]
            elif isinstance(bins, dict):
                bins = list(bins.keys())
            else:
                bins = []
            items.append({
                "name": name, "current": data.get("version") or "?",
                "prefix": str(prefix), "bins": bins,
            })
    return items


def bun_installed():
    rc, out, _ = sh(["bun", "pm", "ls", "-g"], timeout=60)
    if rc != 0:
        return []
    items = []
    for line in out.splitlines():
        line = line.strip().lstrip("├└─ ").strip()
        m = re.match(r"(@?[\w./-]+)@([\w.\-+]+)$", line)
        if m:
            items.append({"name": m.group(1), "current": m.group(2)})
    return items


def npm_latest(name):
    data = http_json(f"https://registry.npmjs.org/{name}/latest")
    return data.get("version")


def pypi_latest(name):
    data = http_json(f"https://pypi.org/pypi/{name}/json")
    return (data.get("info") or {}).get("version")


def crates_latest(name):
    data = http_json(f"https://crates.io/api/v1/crates/{name}")
    return ((data.get("crate") or {}).get("max_version"))


# --------------------------------------------------------------------------- python

def pipx_installed():
    rc, out, _ = sh(["pipx", "list", "--json"], timeout=60)
    items = []
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            for name, meta in (data.get("venvs") or {}).items():
                main = ((meta or {}).get("metadata") or {}).get("main_package") or {}
                items.append({
                    "name": main.get("package") or name,
                    "current": main.get("package_version") or "?",
                })
        except json.JSONDecodeError:
            pass
    if items:
        return items
    rc, out, _ = sh(["pipx", "list", "--short"], timeout=60)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            items.append({"name": parts[0], "current": parts[1]})
    return items


def uv_installed():
    rc, out, _ = sh(["uv", "tool", "list"], timeout=60)
    if rc != 0:
        return []
    items = []
    for line in out.splitlines():
        if not line or line.startswith(" ") or line.startswith("-"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            items.append({"name": parts[0], "current": parts[1].lstrip("v")})
    return items


def cargo_installed():
    rc, out, _ = sh(["cargo", "install", "--list"], timeout=60)
    if rc != 0:
        return []
    items = []
    for line in out.splitlines():
        if not line or line.startswith(" ") or line.startswith("-"):
            continue
        m = re.match(r"(\S+)\s+v([\w.\-+]+):", line)
        if m:
            items.append({"name": m.group(1), "current": m.group(2)})
    return items


# --------------------------------------------------------------------------- flatpak

def flatpak_current():
    rc, out, _ = sh(["flatpak", "list", "--app", "--columns=application,version"], timeout=60)
    cur = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                cur[parts[0]] = parts[1].strip() or "?"
    return cur


def flatpak_updates():
    rc, out, _ = sh(["flatpak", "remote-ls", "--updates", "--columns=application,version"], timeout=120)
    pend = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 1 and parts[0].strip():
                pend[parts[0].strip()] = parts[1].strip() if len(parts) > 1 else "?"
    cur = flatpak_current()
    items = []
    for app in sorted(set(cur) | set(pend)):
        items.append({
            "name": app,
            "current": cur.get(app, "?"),
            "latest": pend.get(app) or cur.get(app),
            "has_update": app in pend,
        })
    return items


# --------------------------------------------------------------------------- appimage

def appimage_dirs():
    return [HOME / "Applications", HOME / "Downloads", HOME / "bin",
            HOME / ".local/bin", HOME / "opt", HOME / "AppImages"]


def appimage_map():
    try:
        return json.loads(APPIMAGE_MAP.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_binaries_cfg():
    try:
        data = json.loads(BINARIES_CFG.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _yaml_scalar(text, key):
    for line in text.splitlines():
        if line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        m = re.match(rf"^{re.escape(key)}\s*:\s*(.+)$", line)
        if m:
            return m.group(1).strip().strip("'\"")
    return ""


def appimage_meta(path):
    """Metadata AppImage: nama, versi, dan sumber updater (di-cache per mtime+size).

    Sumber updater diambil dari yang ditanam aplikasinya sendiri:
      - resources/app-update.yml  (electron-updater: provider github/generic)
      - X-AppImage-UpdateInformation (zsync ke rilis GitHub)
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return {"name": path.stem, "version": find_version(path.name),
                "repo": None, "feed": None}
    sig = f"{int(st.st_mtime)}:{st.st_size}"
    cache = {}
    try:
        cache = json.loads(APPIMAGE_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    if (cache.get(key) or {}).get("sig") == sig and (cache.get(key) or {}).get("v") == 2:
        return cache[key]["meta"]

    meta = {"name": path.stem, "version": find_version(path.name),
            "repo": None, "feed": None}
    try:
        with tempfile.TemporaryDirectory() as td:
            for pattern in ("*.desktop", "resources/app-update.yml"):
                sh([str(path), "--appimage-extract", pattern], timeout=90, cwd=td)
            version = name = update_info = ""
            for desk in Path(td).glob("squashfs-root/*.desktop"):
                try:
                    text = desk.read_text(errors="replace")
                except OSError:
                    continue
                for line in text.splitlines():
                    if line.startswith("Name=") and not name:
                        name = line.split("=", 1)[1].strip()
                    elif line.startswith("X-AppImage-Version=") and not version:
                        version = line.split("=", 1)[1].strip()
                    elif line.startswith("X-AppImage-UpdateInformation=") and not update_info:
                        update_info = line.split("=", 1)[1].strip()
            if name:
                meta["name"] = name
            if version:
                meta["version"] = version

            yml = Path(td) / "squashfs-root/resources/app-update.yml"
            if yml.is_file():
                text = yml.read_text(errors="replace")
                provider = _yaml_scalar(text, "provider")
                url = _yaml_scalar(text, "url")
                owner = _yaml_scalar(text, "owner")
                repo = _yaml_scalar(text, "repo")
                if provider == "github" and owner and repo:
                    meta["repo"] = f"{owner}/{repo}"
                m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", url or "")
                if m:
                    meta["repo"] = m.group(1)
                    if "/releases/" in (url or ""):
                        # feed electron-updater: versi asli aplikasi ada di latest-linux.yml
                        meta["feed"] = url.rstrip("/")
                elif url and provider == "generic":
                    meta["feed"] = url

            if not meta["repo"]:
                m = re.search(r"github\.com/([\w.-]+/[\w.-]+)/releases/", update_info)
                if m:
                    meta["repo"] = m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass

    cache[key] = {"sig": sig, "v": 2, "meta": meta}
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        APPIMAGE_CACHE.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass
    return meta


def appimage_embedded_version(path):
    return appimage_meta(path).get("version")


def appimage_asset(assets):
    best = None
    for a in assets:
        name = (a.get("name") or "").lower()
        if not name.endswith(".appimage"):
            continue
        score = 1
        if any(k in name for k in ("x86_64", "amd64", "x64")):
            score += 2
        if any(k in name for k in ("aarch64", "arm64")):
            score -= 2
        if best is None or score > best[0]:
            best = (score, a)
    return best[1] if best else None


def _electron_feed_item(base, filename):
    try:
        text = download(f"{base.rstrip('/')}/{filename}", timeout=30).decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return None
    version = _yaml_scalar(text, "version")
    if not version:
        return None
    url = sha = ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s*-\s*url:\s*(\S+)", line)
        if m and m.group(1).lower().endswith(".appimage"):
            url = m.group(1)
            for nxt in lines[i + 1:i + 5]:
                mm = re.match(r"\s*sha512:\s*(\S+)", nxt)
                if mm:
                    sha = mm.group(1)
                    break
            break
    if url and not url.startswith(("http://", "https://")):
        url = f"{base.rstrip('/')}/{url}"
    return version, url or None, sha or None


def appimage_latest(item):
    """(versi, url unduhan, sha512) terbaru untuk satu AppImage."""
    feed = item.get("feed")
    if feed:
        for name in ("latest-linux.yml", "latest.yml"):
            got = _electron_feed_item(feed, name)
            if got:
                return got
    if item.get("repo"):
        try:
            tag, assets = github_latest(item["repo"])
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError,
                json.JSONDecodeError):
            return None
        asset = appimage_asset(assets)
        return (tag, asset.get("browser_download_url") if asset else None, None)
    feed = item.get("feed")
    if feed:
        for name in ("latest-linux.yml", "latest.yml"):
            got = _electron_feed_item(feed, name)
            if got:
                return got
    return None


def appimage_scan():
    mapping = appimage_map()
    seen, items = set(), []
    for d in appimage_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.AppImage")):
            real = f.resolve()
            if real in seen:
                continue
            seen.add(real)
            meta = appimage_meta(f)
            current = (remembered_version(str(f)) or meta.get("version")
                       or find_version(f.name) or "?")
            items.append({
                "name": f.name,
                "display": meta.get("name") or f.stem,
                "path": str(f),
                "current": current,
                "repo": mapping.get(str(f)) or mapping.get(f.name) or meta.get("repo"),
                "feed": meta.get("feed"),
                "stale": False,
            })

    # aplikasi yang sama dengan beberapa versi: hanya versi tertinggi yang dianggap terpasang
    groups = {}
    for it in items:
        groups.setdefault(it["display"], []).append(it)
    for group in groups.values():
        if len(group) < 2:
            continue
        newest = max(group, key=lambda i: parse_version(i["current"]) if i["current"] != "?" else ())
        for it in group:
            it["stale"] = it is not newest
    return items


def github_latest(repo):
    data = http_json(f"https://api.github.com/repos/{repo}/releases/latest")
    return (data.get("tag_name") or "").lstrip("vV"), data.get("assets") or []


def strip_tag(tag, prefixes=("v",)):
    t = tag or ""
    for p in prefixes:
        if p and t.lower().startswith(p.lower()):
            return t[len(p):]
    return t


def github_latest_tag(repo, prefixes=("v",)):
    try:
        data = http_json(f"https://api.github.com/repos/{repo}/releases/latest")
        tag = data.get("tag_name")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        data = http_json(f"https://api.github.com/repos/{repo}/tags?per_page=1")
        tag = (data[0].get("name") if isinstance(data, list) and data else None)
    return strip_tag(tag or "", tuple(prefixes)) or None


def github_head_sha(repo):
    data = http_json(f"https://api.github.com/repos/{repo}/commits?per_page=1")
    if isinstance(data, list) and data:
        return (data[0].get("sha") or "")[:7] or None
    return None


# --------------------------------------------------------------------------- script (installer curl)

PY_SHEBANG = ("python", "python3", "uv", "pipx")
NODE_SHEBANG = ("node", "bun", "deno")


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def package_name_from_bin(rel, name):
    """Nama paket npm untuk launcher: ikuti node_modules/.bin/<name>."""
    b = rel / "node_modules" / ".bin" / name
    if b.exists():
        real = b.resolve()
        for parent in [real.parent, *list(real.parents)[:4]]:
            pj = parent / "package.json"
            data = read_json_file(pj) or {}
            if data.get("name"):
                return data["name"]
    root = read_json_file(rel / "package.json") or {}
    for dep in (root.get("dependencies") or {}):
        if name in dep.split("/")[-1]:
            return dep
    return ""


def launcher_metadata(target, name=""):
    """Cari metadata di sekitar launcher: managed install atau package.json."""
    d = target.parent
    for _ in range(3):
        cver = d / "install" / "current-version"
        if cver.is_file():
            try:
                ver = cver.read_text().strip()
            except OSError:
                ver = ""
            rel = d / "install" / "releases" / ver
            pkg = package_name_from_bin(rel, name) if name else ""
            if not pkg:
                for pj in sorted(rel.glob("node_modules/*/package.json")):
                    data = read_json_file(pj) or {}
                    if data.get("name"):
                        pkg = data["name"]
                        break
            return {"kind": "managed", "root": str(d), "version": ver or None,
                    "npm": pkg or None, "repo": ""}
        pj = d / "package.json"
        data = read_json_file(pj) or {}
        if data.get("name"):
            repo = data.get("repository") or {}
            url = repo.get("url") if isinstance(repo, dict) else str(repo or "")
            repo_slug = ""
            if "github.com" in url:
                repo_slug = url.split("github.com/")[-1].removesuffix(".git").strip("/")
            return {"kind": "package", "root": str(d), "version": data.get("version"),
                    "npm": data.get("name"), "repo": repo_slug}
        d = d.parent
    return None


def launcher_shebang(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(400).decode("utf-8", "replace")
    except OSError:
        return ""
    if not head.startswith("#!"):
        return ""
    return head.splitlines()[0][2:].strip()


def auto_launchers(known_names, exclude_npm=()):
    """Launcher shell/node yang punya metadata, jadi bisa dipantau tanpa daftar manual."""
    found = {}
    exclude_npm = set(exclude_npm)
    for d in rc_path_dirs():
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for f in entries:
            if f.name in found or f.name in known_names:
                continue
            try:
                target = f.resolve(strict=True) if f.is_symlink() else f
                if not target.is_file() or not os.access(target, os.X_OK):
                    continue
            except (OSError, RuntimeError):
                continue
            shebang = launcher_shebang(target)
            if not shebang:
                continue
            low = shebang.lower()
            if any(k in low for k in PY_SHEBANG):
                continue  # console script pip/pipx/uv, sudah ditangani sumbernya
            meta = launcher_metadata(target, f.name)
            if not meta:
                continue
            npm = meta.get("npm") or ""
            if npm and npm in exclude_npm:
                continue  # sudah dicatat sebagai paket npm global di prefix lain
            current = meta.get("version") or detect_version([str(target), "--version"], f.name)
            npm = meta.get("npm") or ""
            if not npm and meta.get("repo"):
                npm = ""
            update = []
            if meta["kind"] == "managed":
                rc, _, _ = sh([str(target), "update", "--help"], timeout=6)
                if rc == 0:
                    update = [f.name, "update"]
            found[f.name] = {
                "name": f.name, "kind": "tool", "repo": meta.get("repo") or "",
                "npm": npm, "current": current, "upgrade": update,
                "tag_strip": ["v"], "path": str(f), "note": meta["kind"],
            }
    return list(found.values())


def script_entries():
    extras = []
    try:
        data = json.loads(SCRIPTS_CFG.read_text())
        if isinstance(data, list):
            extras = data
    except (OSError, json.JSONDecodeError):
        pass
    return SCRIPT_REGISTRY + extras


# --- kepemilikan paket & folder PATH dari shell rc -------------------------

_DPKG_OWNED = None


def dpkg_owned_paths():
    """Set semua path yang dimiliki paket dpkg (dibaca dari /var/lib/dpkg/info)."""
    global _DPKG_OWNED
    if _DPKG_OWNED is not None:
        return _DPKG_OWNED
    owned = set()
    info = Path("/var/lib/dpkg/info")
    if info.is_dir():
        for f in info.glob("*.list"):
            try:
                for line in f.read_text(errors="replace").splitlines():
                    if line:
                        owned.add(line)
            except OSError:
                continue
    _DPKG_OWNED = owned
    return owned


def package_owner(path):
    """Nama paket dpkg pemilik path, atau None kalau tidak dikelola paket."""
    owned = dpkg_owned_paths()
    if str(path) in owned:
        return "dpkg"
    return None


def rc_files():
    names = [".zshrc", ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".profile"]
    files = [HOME / n for n in names]
    extra = Path("/etc/profile.d")
    if extra.is_dir():
        files += sorted(extra.glob("*.sh"))
    return [f for f in files if f.is_file()]


SKIP_DIR_HINTS = (".asdf", "/snap/", "virtualenv", "versions/node", "/.nvm/",
                  "site-packages", "node_modules", "/.cache/", "/opt/cni/",
                  "/opt/containerd/")


def rc_path_dirs():
    """Folder yang ditambahkan ke PATH oleh shell rc, plus folder umum manual."""
    dirs, seen = [], set()

    def push(p):
        try:
            p = expand_path(str(p))
        except (TypeError, ValueError):
            return
        key = str(p)
        if key in seen or not p.is_dir():
            return
        if any(h in key for h in SKIP_DIR_HINTS):
            return
        seen.add(key)
        dirs.append(p)

    for d in (HOME / ".local/bin", Path("/usr/local/bin"), HOME / "go/bin",
              HOME / "bin", HOME / "opt/bin", HOME / ".pulumi/bin",
              HOME / ".opencode/bin", HOME / ".capy/bin"):
        push(d)

    pat = re.compile(r"PATH\s*=\s*[\"']?([^\"'\n]+)")
    for f in rc_files():
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            for m in pat.finditer(line):
                for part in m.group(1).split(":"):
                    part = part.strip().strip("\"'")
                    if not part or "$" in part or "`" in part:
                        continue
                    push(part)

    for opt in Path("/opt").glob("*/bin") if Path("/opt").is_dir() else []:
        push(opt)
    return dirs


def is_script(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return True


def repo_for(name):
    """Repo GitHub untuk sebuah binary: dari daftar manual atau entri script terdaftar."""
    if name in MANUAL_REPOS:
        repo, strip = MANUAL_REPOS[name]
        return repo, list(strip)
    for e in script_entries():
        if e.get("name") == name and e.get("repo"):
            return e["repo"], list(e.get("tag_strip") or ["v"])
    return None


def openbot_runtimes():
    """Runtime OpenBot, satu baris per app+target: hanya versi tertinggi (folder lama = sisa)."""
    base = HOME / ".config/OpenBot/provider-runtimes"
    if not base.is_dir():
        return []

    best = {}
    for name_dir in sorted(base.iterdir()):
        if not name_dir.is_dir():
            continue
        for target in name_dir.iterdir():
            if not target.is_dir():
                continue
            versions = [v for v in target.iterdir()
                        if v.is_dir() and re.match(r"^\d", v.name)]
            if not versions:
                continue
            try:
                newest = max(versions, key=lambda v: parse_version(v.name))
            except TypeError:
                newest = max(versions, key=lambda v: v.name)
            best[(name_dir.name, str(target))] = {
                "name": name_dir.name, "current": newest.name,
                "path": str(newest), "stale": len(versions) - 1,
                "stale_paths": [str(v) for v in versions if v != newest],
                "npm": OPENBOT_RUNTIME_NPM.get(name_dir.name, ""),
            }
    return list(best.values())


def asdf_installed():
    plugins = []
    rc, out, _ = sh(["asdf", "plugin-list"], timeout=30)
    if rc != 0 or "MANAGE PLUGINS" in out:
        rc, out, _ = sh(["asdf", "plugin", "list"], timeout=30)
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("version:") or " " in line or line.isupper():
            continue
        plugins.append(line)
    rc, cur, _ = sh(["asdf", "current"], timeout=30)
    versions = {}
    for line in cur.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^\d", parts[1]):
            versions[parts[0]] = parts[1]
    # plugin tanpa versi aktif bukan aplikasi yang dipakai: jangan tampilkan sebagai "?"
    return [{"name": p, "current": versions[p]} for p in plugins if p in versions]


ASDF_UNSTABLE = re.compile(r"(alpha|beta|rc\d*|pre|dev|nightly|preview)", re.I)
_ASDF_ALL = {}


def asdf_versions(plugin):
    if plugin in _ASDF_ALL:
        return _ASDF_ALL[plugin]
    rc, out, _ = sh(["asdf", "list-all", plugin], timeout=120)
    vers = []
    if rc == 0:
        for line in out.splitlines():
            v = line.strip()
            if not v or ASDF_UNSTABLE.search(v):
                continue
            vers.append(v)
    _ASDF_ALL[plugin] = vers
    return vers


def asdf_latest(plugin):
    """Versi stabil tertinggi: dari plugin asdf sendiri, cadangan dari GitHub."""
    vers = asdf_versions(plugin)
    if vers:
        try:
            return max(vers, key=parse_version)
        except TypeError:
            return vers[-1]
    info = ASDF_REPOS.get(plugin)
    if info:
        try:
            return github_latest_tag(info[0], tuple(info[1]))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError,
                json.JSONDecodeError):
            return None
    return None


_ASDF_PLUGINS = None


def asdf_plugins():
    global _ASDF_PLUGINS
    if _ASDF_PLUGINS is not None:
        return _ASDF_PLUGINS
    rc, out, _ = sh(["asdf", "plugin-list"], timeout=30)
    if rc != 0 or "MANAGE PLUGINS" in out:
        rc, out, _ = sh(["asdf", "plugin", "list"], timeout=30)
    names = set()
    for line in out.splitlines():
        line = line.strip()
        if line and " " not in line and not line.startswith("version:") and not line.isupper():
            names.add(line)
    _ASDF_PLUGINS = names
    return names


OFFPATH_ROOTS = ["~/.local/opt", "~/opt", "~/tools", "/opt"]
OFFPATH_SKIP_DIRS = {"node_modules", ".git", "lib", "lib64", "share", "doc",
                     "docs", "scripts", "mocks", "__pycache__", "locales"}
OFFPATH_SKIP_EXT = (".so", ".pak", ".desktop", ".json", ".mjs", ".js", ".py",
                    ".sh", ".png", ".svg", ".css", ".html", ".map", ".bin")


def offpath_candidates():
    """Binary di folder aplikasi yang tidak pernah masuk PATH."""
    found = {}
    for root in OFFPATH_ROOTS:
        base = expand_path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            depth = str(dirpath).count(os.sep) - str(base).count(os.sep)
            dirnames[:] = [d for d in dirnames
                           if d not in OFFPATH_SKIP_DIRS and not d.startswith(".")]
            if depth >= 3:
                dirnames[:] = []
            for fn in filenames:
                if fn.lower().endswith(OFFPATH_SKIP_EXT) or fn.lower().endswith(".appimage") or ".so." in fn:
                    continue
                fp = Path(dirpath) / fn
                if fn in found:
                    continue
                try:
                    if fp.is_symlink() or not fp.is_file() or not os.access(fp, os.X_OK):
                        continue
                except OSError:
                    continue
                if package_owner(fp) or is_script(fp):
                    continue
                found[fn] = fp
    return found


def manual_candidates():
    """Binary executable di PATH yang tidak dimiliki dpkg dan bukan script."""
    found = {}
    plugins = asdf_plugins()
    dirs = rc_path_dirs() + [Path("/usr/bin")]
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for f in entries:
            if f.name in found:
                continue
            if f.name.lower().endswith(".appimage"):
                continue
            target = f
            if f.is_symlink():
                try:
                    target = f.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if "/.asdf/shims/" in str(target) and re.sub(r"\d+$", "", f.name) in plugins:
                    continue
            try:
                if not target.is_file() or not os.access(target, os.X_OK):
                    continue
            except OSError:
                continue
            if str(target).lower().endswith(".appimage"):
                continue
            if package_owner(f) or package_owner(target):
                continue
            if is_script(target):
                continue
            found[f.name] = target
    return found


MANUAL_REPOS = {
    "helm": ("helm/helm", ["v"]),
    "kind": ("kubernetes-sigs/kind", ["v"]),
    "flux": ("fluxcd/flux2", ["v"]),
    "sops": ("getsops/sops", ["v"]),
    "kubectl": ("kubernetes/kubernetes", ["v"]),
    "k9s": ("derailed/k9s", ["v"]),
    "kustomize": ("kubernetes-sigs/kustomize", ["kustomize/v"]),
    "yq": ("mikefarah/yq", ["v"]),
    "terraform": ("hashicorp/terraform", ["v"]),
    "vault": ("hashicorp/vault", ["v"]),
    "jq": ("jqlang/jq", ["jq-"]),
    "starship": ("starship/starship", ["v"]),
    "zoxide": ("ajeetdsouza/zoxide", ["v"]),
    "fzf": ("junegunn/fzf", ["v"]),
    "bat": ("sharkdp/bat", ["v"]),
    "fd": ("sharkdp/fd", ["v"]),
    "lazygit": ("jesseduffield/lazygit", ["v"]),
    "eza": ("eza-community/eza", ["v"]),
    "cloudflared": ("cloudflare/cloudflared", [""]),
    "docker-compose": ("docker/compose", ["v"]),
    "k3d": ("k3d-io/k3d", ["v"]),
    "kubie": ("sbstp/kubie", ["v"]),
    "k0sctl": ("k0sproject/k0sctl", ["v"]),
    "golangci-lint": ("golangci/golangci-lint", ["v"]),
    "grpcurl": ("fullstorydev/grpcurl", ["v"]),
    "changie": ("miniscruff/changie", ["v"]),
    "go-pmtiles": ("protomaps/go-pmtiles", ["v"]),
    "lan-mouse": ("feschber/lan-mouse", ["v"]),
}


def script_scan(exclude_npm=()):
    items = []
    for e in script_entries():
        raw_path = str(expand_path(e.get("path") or ""))
        if not raw_path or not Path(raw_path).exists():
            continue
        kind = e.get("kind", "tool")
        if kind == "git":
            rc, out, _ = sh(["git", "-C", raw_path, "rev-parse", "--short=7", "HEAD"], timeout=20)
            current = out.strip() if rc == 0 else "?"
            upgrade = [c.replace("~", str(HOME), 1) if c.startswith("~/") else c
                       for c in (e.get("update") or [])]
            items.append({
                "name": e["name"], "kind": "git", "repo": e["repo"],
                "current": current, "upgrade": upgrade,
                "note": "commit", "path": raw_path,
            })
            continue

        cmd = e.get("version_cmd") or [e["name"], "--version"]
        current = detect_version(cmd, e["name"], raw_path)
        if current in ("?", "dev"):
            current = remembered_version(raw_path) or current
        items.append({
            "name": e["name"], "kind": "tool", "repo": e["repo"],
            "current": current, "upgrade": list(e.get("update") or []),
            "tag_strip": e.get("tag_strip") or ["v"], "npm": e.get("npm") or "",
            "note": e.get("manual") or "", "path": raw_path,
        })

    # launcher script yang punya metadata (managed install / package.json)
    known_names = {i["name"] for i in items}
    for it in auto_launchers(known_names, exclude_npm):
        items.append(it)

    # binary manual yang sumbernya bisa ditebak dari nama (helm, kind, flux, ...)
    known = {i["name"] for i in items}
    for name, path in sorted(all_manual().items()):
        info = repo_for(name)
        if name in known or not info:
            continue
        repo, strip = info
        items.append({
            "name": name, "kind": "tool", "repo": repo,
            "current": _manual_version(name, path),
            "upgrade": [], "tag_strip": list(strip),
            "note": f"manual: {path}", "path": str(path),
        })
    return items


# --------------------------------------------------------------------------- unduh binary GitHub

BACKUP_DIR = CACHE_DIR / "backup"
JUNK_ASSET = (".sha256", ".sha512", ".sig", ".asc", ".sbom", ".pem", ".txt",
              ".json", ".yaml", ".yml", ".deb", ".rpm", ".apk", ".pkg", ".msi")
OTHER_OS = ("darwin", "windows", ".exe", ".dmg", "freebsd", "arm64", "aarch64",
            "386", "ppc64le", "s390x", "riscv64", "loong64", "armv7")
# URL rilis resmi untuk repo yang tidak menyediakan binary di GitHub
# repo yang tidak bisa diganti binary-nya begitu saja (butuh service, user, dll)
NO_BINARY_REPOS = {"ollama/ollama"}

ASSET_URL_TEMPLATES = {
    "helm/helm": "https://get.helm.sh/helm-v{ver}-linux-amd64.tar.gz",
    "hashicorp/terraform": "https://releases.hashicorp.com/terraform/{ver}/terraform_{ver}_linux_amd64.zip",
}


def gh_release(repo):
    data = http_json(f"https://api.github.com/repos/{repo}/releases/latest")
    return (data.get("tag_name") or "").lstrip("vV"), data.get("assets") or []


def tool_names_for(repo, target):
    base = re.sub(r"\d+$", "", Path(target).name).lower()
    names = [base]
    rname = repo.split("/")[-1].lower()
    if rname not in names:
        names.append(rname)
    return names


def pick_asset(assets, tools):
    """Pilih asset rilis untuk linux/amd64 yang paling mungkin berisi binary."""
    if isinstance(tools, str):
        tools = [tools]
    best, best_score = None, -99
    for a in assets:
        n = (a.get("name") or "").lower()
        if not n:
            continue
        score = 0
        if "linux" in n:
            score += 3
        if any(k in n for k in ("amd64", "x86_64", "x64")):
            score += 2
        if any(k in n for k in OTHER_OS):
            score -= 8
        if any(k in n for k in JUNK_ASSET):
            score -= 20
        if "fips" in n:
            score -= 3
        if ".zst" in n:
            score -= 5
        if any(t in n for t in tools):
            score += 1
        if n.endswith((".tar.gz", ".tgz", ".zip")):
            score += 1
        if score > best_score:
            best, best_score = a, score
    if best and any(k in (best.get("name") or "").lower() for k in JUNK_ASSET):
        return None
    return best


def extract_binary(data, filename, tools):
    """Ambil binary dari tar.gz/zip; kalau file mentah, pakai apa adanya."""
    if isinstance(tools, str):
        tools = [tools]
    low = (filename or "").lower()
    if low.endswith((".tar.gz", ".tgz")):
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            pick = [m for m in members if os.path.basename(m.name).lower() in tools]
            if not pick:
                pick = [m for m in members
                        if any(t in os.path.basename(m.name).lower() for t in tools)]
            if not pick:
                pick = [m for m in members
                        if "." not in os.path.basename(m.name)
                        and not os.path.basename(m.name).isupper()]
            if not pick:
                return None
            fh = tf.extractfile(pick[0])
            return fh.read() if fh else None
    if low.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            pick = [n for n in names if os.path.basename(n).lower() in tools]
            if not pick:
                pick = [n for n in names
                        if any(t in os.path.basename(n).lower() for t in tools)]
            if not pick:
                return None
            return zf.read(pick[0])
    return data


def download(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "upd/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _write_cache(path, text):
    """Tulis cache; kalau file milik root (pernah dijalankan via sudo), ambil alih lewat sudo -n."""
    try:
        path.write_text(text)
        return True
    except PermissionError:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(text)
        except OSError:
            return False
        rc, _, _ = sh(["sudo", "-n", "install", "-m", "0644", "-o", str(os.getuid()),
                       "-g", str(os.getgid()), str(tmp), str(path)], timeout=30)
        tmp.unlink(missing_ok=True)
        return rc == 0
    except OSError:
        return False


def remembered_version(path):
    try:
        data = json.loads(INSTALLED_CACHE.read_text())
        return data.get(str(path))
    except (OSError, json.JSONDecodeError):
        return None


def remember_version(path, version):
    data = {}
    try:
        data = json.loads(INSTALLED_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    data[str(path)] = version
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _write_cache(INSTALLED_CACHE, json.dumps(data, indent=2))


def binary_runs(path):
    """Cek binary baru benar-benar bisa dijalankan (--version, version, atau --help)."""
    for args in (["--version"], ["version"], ["--help"]):
        try:
            rc, out, err = sh([str(path)] + args, timeout=20)
        except OSError:
            return False
        text = (out or "") + (err or "")
        if rc == 0 and text.strip():
            return True
    return False


_SUDO_OK = None


def sudo_available():
    """Sudo tanpa password tersedia? (dipakai hanya untuk file root)."""
    global _SUDO_OK
    if _SUDO_OK is not None:
        return _SUDO_OK
    if os.geteuid() == 0:
        _SUDO_OK = True
        return True
    if not shutil.which("sudo"):
        _SUDO_OK = False
        return False
    rc, _, _ = sh(["sudo", "-n", "true"], timeout=10)
    _SUDO_OK = rc == 0
    return _SUDO_OK


def sudo_install(payload, target):
    """Tulis binary ke lokasi root lewat sudo -n (tanpa prompt)."""
    tmpdir = CACHE_DIR / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / f"{target.name}.new"
    tmp.write_bytes(payload)
    tmp.chmod(0o755)
    rc, out, err = sh(["sudo", "-n", "install", "-m", "0755", "-o", "root",
                       "-g", "root", str(tmp), str(target)], timeout=120)
    tmp.unlink(missing_ok=True)
    return rc == 0, (err or out).strip()


def sudo_install_file(src, target):
    """Sama seperti sudo_install, tapi dari file di disk (untuk AppImage/ollama besar)."""
    rc, out, err = sh(["sudo", "-n", "install", "-m", "0755", "-o", "root",
                       "-g", "root", str(src), str(target)], timeout=600)
    return rc == 0, (err or out).strip()


def download_to(url, dest, timeout=900):
    """Unduh ke file sambil jalan (tidak menahan ratusan MB di RAM)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "upd/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 20)
    return dest


def verify_sha512(path, expected):
    """Cocokkan sha512 base64 dari feed updater. Kalau tidak ada hash, lewati."""
    if not expected:
        return True
    import base64
    import hashlib
    h = hashlib.sha512()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        want = base64.b64decode(expected)
    except (OSError, ValueError):
        return True
    return h.digest() == want


def install_github_binary(repo, latest, target, dry_run=False):
    """Unduh, verifikasi ELF, backup, lalu timpa target. Kembalikan (ok, pesan)."""
    target = expand_path(target)
    tools = tool_names_for(repo, target)
    tag, assets = gh_release(repo)

    template = ASSET_URL_TEMPLATES.get(repo)
    if template:
        url = template.format(ver=latest)
        name = url.rsplit("/", 1)[-1]
        asset = {"name": name, "browser_download_url": url}
    else:
        asset = pick_asset(assets, tools)
        if not asset:
            return False, "tidak ada asset linux/amd64 di rilis ini"
        name = asset.get("name") or ""

    if dry_run:
        if os.access(target.parent, os.W_OK):
            note = ""
        else:
            note = "  [via sudo]" if sudo_available() else "  [butuh sudo]"
        return True, f"{name} -> {target}{note}"

    try:
        data = download(asset.get("browser_download_url") or "")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return False, f"unduh gagal: {exc}"

    payload = extract_binary(data, name, tools)
    if not payload:
        return False, f"binary {'/'.join(tools)} tidak ditemukan di dalam {name}"
    if not payload.startswith(b"\x7fELF"):
        return False, f"{name} bukan binary ELF linux"
    if len(payload) < 100_000:
        return False, f"ukuran mencurigakan ({len(payload)} byte)"

    backup = None
    if target.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{target.name}.{int(time.time())}"
        try:
            shutil.copy2(target, backup)
        except OSError as exc:
            return False, f"gagal backup: {exc}"

    tmp = target.with_name(f".{target.name}.upd-tmp")
    try:
        tmp.write_bytes(payload)
        tmp.chmod(0o755)
        os.replace(tmp, target)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        if not sudo_available():
            return False, f"tidak punya izin menulis {target} (jalankan dengan sudo penuh: sudo {sys.argv[0]})"
        ok_sudo, msg = sudo_install(payload, target)
        if not ok_sudo:
            return False, f"sudo gagal menulis {target}: {msg}"
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return False, f"gagal menulis: {exc}"

    if not binary_runs(target):
        if backup and backup.exists():
            shutil.copy2(backup, target)
            return False, "binary baru gagal dijalankan, versi lama dikembalikan"
        return False, "binary baru gagal dijalankan"
    remember_version(target, latest or tag)
    return True, f"{target} -> {tag}"


# --------------------------------------------------------------------------- update khusus

def install_appimage(target, url, sha512, latest, dry_run=False):
    """Unduh AppImage baru, verifikasi, lalu timpa file lama (nama file dipertahankan)."""
    target = expand_path(target)
    if not url:
        return False, "rilis ini tidak punya asset .AppImage"
    if dry_run:
        return True, f"{latest} -> {target} (nama file dipertahankan)"

    tmpdir = CACHE_DIR / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / f"{target.name}.download"
    try:
        download_to(url, tmp)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        return False, f"unduh gagal: {exc}"

    try:
        if not verify_sha512(tmp, sha512):
            return False, "sha512 tidak cocok dengan feed — dibatalkan"
        with open(tmp, "rb") as fh:
            if fh.read(4) != b"\x7fELF":
                return False, "file unduhan bukan ELF/AppImage"
        if tmp.stat().st_size < 1_000_000:
            return False, f"ukuran mencurigakan ({tmp.stat().st_size} byte)"

        backup = None
        if target.exists():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup = BACKUP_DIR / f"{target.name}.{int(time.time())}"
            shutil.copy2(target, backup)

        tmp.chmod(0o755)
        try:
            os.replace(tmp, target)
        except PermissionError:
            if not sudo_available():
                return False, f"tidak punya izin menulis {target}"
            ok_sudo, msg = sudo_install_file(tmp, target)
            if not ok_sudo:
                return False, f"sudo gagal menulis {target}: {msg}"
        except OSError as exc:
            return False, f"gagal menulis: {exc}"

        got = (appimage_meta(target) or {}).get("version")
        if not sha512 and latest and got and parse_version(got) != parse_version(latest):
            if backup and backup.exists():
                shutil.copy2(backup, target)
                return False, f"isi AppImage {got} bukan {latest}, versi lama dikembalikan"
        remember_version(target, latest or got or "")
        return True, f"{target} -> {latest or got}"
    except OSError as exc:
        return False, f"gagal memasang: {exc}"
    finally:
        tmp.unlink(missing_ok=True)


def install_asdf(plugin, version, dry_run=False):
    """asdf: pasang versi baru lalu jadikan global (menggantikan versi lama)."""
    if dry_run:
        return True, f"asdf install {plugin} {version} && asdf global {plugin} {version}"
    rc, out, err = sh(["asdf", "install", plugin, str(version)], timeout=1800)
    if rc != 0:
        tail = (err or out).strip().splitlines()
        return False, tail[-1][:200] if tail else f"asdf install gagal ({rc})"
    sh(["asdf", "global", plugin, str(version)], timeout=60)
    sh(["asdf", "reshim", plugin, str(version)], timeout=60)

    rc, cur, _ = sh(["asdf", "current", plugin], timeout=30)
    now = ""
    for line in cur.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == plugin and len(parts) >= 2 and re.match(r"^\d", parts[1]):
            now = parts[1]
            break
        if re.match(r"^\d", parts[0]):
            now = parts[0]
            break
    if now and parse_version(now) == parse_version(str(version)):
        return True, f"{plugin} {version}"
    return False, f"{plugin} terpasang tapi versi aktif masih {now or '?'}"


OLLAMA_ARCHIVE = ("https://github.com/ollama/ollama/releases/latest/download/"
                  "ollama-linux-amd64.tar.zst")


def install_ollama(latest, dry_run=False):
    """Pasang ulang binary + library ollama dari arsip rilis resmi (tanpa skrip installer)."""
    if dry_run:
        return True, "unduh ollama-linux-amd64.tar.zst (~1,5 GB), pasang bin/ollama + lib/ollama"
    if not sudo_available():
        return False, "butuh sudo tanpa password"

    tmpdir = CACHE_DIR / "tmp" / "ollama"
    tmpdir.mkdir(parents=True, exist_ok=True)
    arc = tmpdir / "ollama.tar.zst"
    try:
        download_to(OLLAMA_ARCHIVE, arc, timeout=1800)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        return False, f"unduh gagal: {exc}"

    try:
        rc, out, err = sh(["tar", "--zstd", "-xf", str(arc), "-C", str(tmpdir)], timeout=900)
        if rc != 0:
            return False, f"ekstrak gagal: {(err or out).strip()[:160]}"

        src_bin = tmpdir / "bin" / "ollama"
        if not src_bin.is_file():
            return False, "arsip tidak berisi bin/ollama"
        ok, msg = sudo_install_file(src_bin, Path("/usr/local/bin/ollama"))
        if not ok:
            return False, f"sudo gagal menulis /usr/local/bin/ollama: {msg}"

        src_lib = tmpdir / "lib" / "ollama"
        if src_lib.is_dir():
            rc, out, err = sh(["sudo", "-n", "cp", "-a", f"{src_lib}/.",
                               "/usr/local/lib/ollama/"], timeout=600)
            if rc != 0:
                return False, f"gagal menyalin lib: {(err or out).strip()[:160]}"

        rc, state, _ = sh(["systemctl", "is-active", "ollama"], timeout=15)
        if state.strip() == "active":
            sh(["sudo", "-n", "systemctl", "restart", "ollama"], timeout=120)

        now = detect_version(["ollama", "--version"], "ollama")
        if latest and parse_version(now):
            if parse_version(now) != parse_version(str(latest)):
                return False, f"versi terpasang masih {now}"
        return True, f"ollama {now}"
    finally:
        arc.unlink(missing_ok=True)
        shutil.rmtree(tmpdir / "bin", ignore_errors=True)
        shutil.rmtree(tmpdir / "lib", ignore_errors=True)


def describe_upgrade(cmd):
    if not cmd:
        return "manual"
    kind = cmd[0]
    if kind == "__gh__":
        return f"unduh rilis GitHub {cmd[1]} -> {cmd[3]}"
    if kind == "__appimage__":
        return f"unduh AppImage {cmd[4]} -> {cmd[1]}"
    if kind == "__asdf__":
        return f"asdf install {cmd[1]} {cmd[2]} && asdf global {cmd[1]} {cmd[2]}"
    if kind == "__ollama__":
        return "unduh arsip rilis ollama, pasang bin + lib"
    if kind == "__sudo__":
        return "sudo " + " ".join(cmd[1:])
    return " ".join(cmd)


def execute_upgrade(cmd, dry_run=False):
    """Jalankan satu rencana update; satu tempat untuk CLI dan GUI."""
    if not cmd:
        return False, "tidak ada jalur otomatis"
    kind = cmd[0]
    try:
        if kind == "__gh__":
            return install_github_binary(cmd[1], cmd[2], cmd[3], dry_run=dry_run)
        if kind == "__appimage__":
            return install_appimage(cmd[1], cmd[2], cmd[3], cmd[4], dry_run=dry_run)
        if kind == "__asdf__":
            return install_asdf(cmd[1], cmd[2], dry_run=dry_run)
        if kind == "__ollama__":
            return install_ollama(cmd[1], dry_run=dry_run)
        if dry_run:
            return True, describe_upgrade(cmd)
        if kind == "__sudo__":
            rc, out, err = sh(["sudo", "-n"] + list(cmd[1:]), timeout=1800)
        else:
            rc, out, err = sh(cmd, timeout=1800)
        if rc == 0:
            return True, "ok"
        tail = (err or out).strip().splitlines()
        return False, (tail[-1][:200] if tail else f"kode {rc}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        return False, str(exc)


# --------------------------------------------------------------------------- launcher yatim

def user_app_dirs():
    return [HOME / ".local/share/applications"]


def desktop_exec_path(text):
    for key in ("Exec=", "TryExec="):
        for line in text.splitlines():
            if line.startswith(key):
                val = line[len(key):].strip()
                tok = val.split()[0] if val.split() else ""
                tok = tok.strip('"').strip("'")
                if tok in ("env", "sh", "bash", "/usr/bin/env"):
                    parts = [x for x in val.split() if not x.startswith("-") and "=" not in x]
                    tok = parts[1] if len(parts) > 1 else ""
                return tok
    return ""


def desktop_icon(text):
    for line in text.splitlines():
        if line.startswith("Icon="):
            return line[5:].strip()
    return ""


def orphan_desktop_entries(existing_targets=None, dry_run=False, filters=None):
    """Entri .desktop yang menunjuk file yang sudah tidak ada."""
    targets = {str(Path(t).resolve()) for t in (existing_targets or []) if t}
    removed = []
    for d in user_app_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.desktop")):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            exe = desktop_exec_path(text)
            if not exe:
                continue
            if filters and not any(k.lower() in (str(f) + " " + exe).lower() for k in filters):
                continue
            broken = not Path(exe).exists()
            if targets and not broken:
                broken = str(Path(exe).resolve()) in targets
            if not broken:
                continue
            if not dry_run:
                removed.append(trash_file(f))
            else:
                removed.append(f)
            icon = desktop_icon(text)
            if icon and not Path(icon).is_absolute() and not dry_run:
                for icondir in (HOME / ".local/share/icons").glob("hicolor/*/apps"):
                    for ext in (".png", ".svg", ".xpm"):
                        cand = icondir / f"{icon}{ext}"
                        if cand.exists():
                            trash_file(cand)
    if not dry_run:
        sh(["update-desktop-database", str(HOME / ".local/share/applications")], timeout=30)
    return removed


def stale_items():
    """Sisa versi lama: AppImage duplikat dan folder runtime OpenBot yang tak terpakai."""
    items = []
    for it in appimage_scan():
        if it.get("stale"):
            items.append({"kind": "appimage", "path": Path(it["path"]),
                          "label": f"{it['display']} {it['current']} (versi lama)"})
    for r in openbot_runtimes():
        for p in r.get("stale_paths") or []:
            items.append({"kind": "runtime", "path": Path(p),
                          "label": f"runtime OpenBot {r['name']} {Path(p).name} (versi lama)"})
    return items


def cmd_clean(args):
    if getattr(args, "stale", False):
        items = stale_items()
        if not items:
            print("Tidak ada versi lama yang tersisa.")
            return 0
        print(f"{len(items)} sisa versi lama:")
        for it in items:
            size = sh(["du", "-sh", str(it["path"])], timeout=60)[1].split("\t")[0]
            print(f"  {size:>6}  {it['label']}\n          {it['path']}")
        if args.dry_run:
            return 0
        if not args.yes:
            try:
                ans = input("\nPindahkan ke Trash? [y/N] ").strip().lower()
            except EOFError:
                ans = "n"
            if ans not in ("y", "ya", "yes"):
                print("Dibatalkan.")
                return 1
        for it in items:
            dest = trash_file(it["path"])
            print(f"  -> Trash: {dest.name}")
        return 0

    orphans = orphan_desktop_entries(dry_run=True, filters=args.filter or None)
    if not orphans:
        print("Tidak ada entri launcher yatim.")
        return 0
    print(f"{len(orphans)} entri launcher menunjuk file yang sudah hilang:")
    for f in orphans:
        print(f"  {f}")
    if args.dry_run:
        return 0
    if not args.yes:
        try:
            ans = input("\nPindahkan ke Trash? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "ya", "yes"):
            print("Dibatalkan.")
            return 1
    moved = orphan_desktop_entries(dry_run=False, filters=args.filter or None)
    print(f"{len(moved)} file dipindah ke Trash, cache menu diperbarui.")
    for m in moved:
        print(f"  {m}")
    return 0


# --------------------------------------------------------------------------- uninstall

REMOVE_CMDS = {
    "npm": lambda n: ["npm", "uninstall", "-g", n],
    "bun": lambda n: ["bun", "remove", "-g", n],
    "pipx": lambda n: ["pipx", "uninstall", n],
    "uv": lambda n: ["uv", "tool", "uninstall", n],
    "cargo": lambda n: ["cargo", "uninstall", n],
    "flatpak": lambda n: ["flatpak", "uninstall", "-y", n],
}

PROTECTED = {"npm", "pnpm", "corepack", "bun", "uv", "rustup", "cargo",
             "pipx", "asdf", "node", "git", "docker", "python3", "upd"}


def installed_index():
    """Daftar nama terpasang per sumber, tanpa memanggil jaringan."""
    idx = {}
    if source_available("npm"):
        idx["npm"] = [i["name"] for i in npm_installed()]
    if source_available("bun"):
        idx["bun"] = [i["name"] for i in bun_installed()]
    if source_available("pipx"):
        idx["pipx"] = [i["name"] for i in pipx_installed()]
    if source_available("uv"):
        idx["uv"] = [i["name"] for i in uv_installed()]
    if source_available("cargo"):
        idx["cargo"] = [i["name"] for i in cargo_installed()]
    if source_available("flatpak"):
        idx["flatpak"] = list(flatpak_current())
    idx["appimage"] = [i["name"] for i in appimage_scan()]
    idx["manual"] = list(all_manual())
    return idx


def trash_file(path):
    """Pindahkan file ke Trash freedesktop, bukan hapus permanen."""
    files_dir = HOME / ".local/share/Trash/files"
    info_dir = HOME / ".local/share/Trash/info"
    files_dir.mkdir(parents=True, exist_ok=True)
    info_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = files_dir / f"{path.name}.{stamp}"
    shutil.move(str(path), str(dest))
    (info_dir / f"{dest.name}.trashinfo").write_text(
        "[Trash Info]\n"
        f"Path={path}\n"
        f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
    )
    return dest


def resolve_target(spec, idx):
    """Kembalikan list (sumber, nama) untuk 'nama' atau 'sumber:nama'."""
    if ":" in spec:
        src, name = spec.split(":", 1)
        src = src.strip().lower()
        if name not in idx.get(src, []):
            return []
        return [(src, name)]
    hits = [(src, spec) for src, names in idx.items() if spec in names]
    return hits


def cmd_uninstall(args):
    spec = args.target
    idx = installed_index()
    hits = resolve_target(spec, idx)

    if not hits:
        import difflib
        print(f"Tidak ditemukan: {spec}")
        semua = [f"{s}:{n}" for s, names in idx.items() for n in names]
        dekat = difflib.get_close_matches(spec, [n for n in semua], n=5, cutoff=0.6)
        if not dekat:
            dekat = [x for x in semua if spec.lower() in x.lower()][:5]
        if dekat:
            print("Mungkin maksudmu: " + ", ".join(dekat))
        return 1

    if len(hits) > 1:
        print(f"'{spec}' ada di beberapa sumber: " + ", ".join(f"{s}:{n}" for s, n in hits))
        print("Ulangi dengan format sumber:nama, contoh: " + f"{hits[0][0]}:{hits[0][1]}")
        return 2

    src, name = hits[0]
    if name in PROTECTED and not args.force:
        print(f"{name} dipakai upd sendiri. Pakai --force kalau benar-benar mau menghapus.")
        return 2

    plan, note = [], ""
    if src in REMOVE_CMDS:
        plan = [REMOVE_CMDS[src](name)]
    elif src == "appimage":
        path = next((p for p in appimage_scan() if p["name"] == name), None)
        if not path:
            print("File AppImage tidak ditemukan.")
            return 1
        note = f"file akan dipindah ke Trash: {path['path']}"
        plan = [["trash", path["path"]]]
    else:
        target = all_manual().get(name)
        if not target:
            print(f"{src}:{name} tidak ditemukan di disk.")
            return 1
        note = f"file dipindah ke Trash (bisa dikembalikan): {target}"
        plan = [["trash", str(target)]]

    print(f"Akan menghapus {src}:{name}")
    for cmd in plan:
        print("  $ " + " ".join(cmd) if cmd[0] != "trash" else f"  (pindah ke Trash) {cmd[1]}")
    if note:
        print(f"  {note}")

    if args.dry_run:
        return 0
    if not args.yes:
        try:
            ans = input("\nLanjut? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "ya", "yes"):
            print("Dibatalkan.")
            return 1

    cmd = plan[0]
    if cmd[0] == "trash":
        gone = Path(cmd[1])
        dest = trash_file(gone)
        print(f"Dipindah ke Trash: {dest}")
        sisa = orphan_desktop_entries(existing_targets=[gone], dry_run=False)
        for f in sisa:
            print(f"  launcher ikut dibersihkan: {f}")
        return 0

    rc, out, err = sh(cmd, timeout=600)
    if rc == 0:
        print(f"{GREEN}ok{RESET} — {src}:{name} dihapus")
        return 0
    print(f"{RED}gagal{RESET} ({rc})")
    for line in (err or out).strip().splitlines()[-3:]:
        print("  " + line[:160])
    return 2


# --------------------------------------------------------------------------- collect

def source_available(src):
    if src == "npm":
        return shutil.which("npm") is not None
    if src == "bun":
        return shutil.which("bun") is not None
    if src == "pipx":
        return shutil.which("pipx") is not None
    if src == "uv":
        return shutil.which("uv") is not None
    if src == "cargo":
        return shutil.which("cargo") is not None
    if src == "flatpak":
        return shutil.which("flatpak") is not None
    if src == "appimage":
        return True
    if src == "script":
        return True
    if src == "asdf":
        return shutil.which("asdf") is not None
    if src == "openbot":
        return (HOME / ".config/OpenBot/provider-runtimes").is_dir()
    if src == "binary":
        return True
    return False


def collect(only=None, with_latest=True):
    only = only or SOURCES
    rows = []

    def add(src, name, current, latest=None, has_update=None, upgrade=None, note="", url="", target="", repo=""):
        if has_update is None:
            has_update = is_newer(latest, current) if latest else False
        rows.append({
            "source": src, "name": name, "current": current,
            "latest": latest or "?", "has_update": bool(has_update),
            "upgrade": upgrade or [], "note": note, "url": url,
            "target": target, "repo": repo,
        })

    tasks = {}

    if "npm" in only and source_available("npm"):
        items = npm_installed()
        tasks["npm"] = (items, npm_latest)
    if "bun" in only and source_available("bun"):
        items = bun_installed()
        tasks["bun"] = (items, npm_latest)
    if "pipx" in only and source_available("pipx"):
        items = pipx_installed()
        tasks["pipx"] = (items, pypi_latest)
    if "uv" in only and source_available("uv"):
        items = uv_installed()
        tasks["uv"] = (items, pypi_latest)
    if "cargo" in only and source_available("cargo"):
        items = cargo_installed()
        tasks["cargo"] = (items, crates_latest)

    # cek versi terbaru secara paralel
    futures = {}
    latest_map = {}
    if with_latest:
        with ThreadPoolExecutor(max_workers=12) as pool:
            for src, (items, fn) in tasks.items():
                for it in items:
                    futures[(src, it["name"])] = pool.submit(fn, it["name"])

            for key, fut in futures.items():
                try:
                    latest_map[key] = fut.result()
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
                    latest_map[key] = None

    upg = {
        "npm": lambda n: ["npm", "install", "-g", f"{n}@latest"],
        "bun": lambda n: ["bun", "add", "-g", n],
        "pipx": lambda n: ["pipx", "upgrade", n],
        "uv": lambda n: ["uv", "tool", "upgrade", n],
        "cargo": lambda n: ["cargo", "install", "--force", n],
    }
    for src, (items, _) in tasks.items():
        for it in items:
            latest = latest_map.get((src, it["name"]))
            add(src, it["name"], it["current"], latest, upgrade=upg[src](it["name"]))

    # npm global di prefix lain (/usr/local, /usr): tidak terlihat `npm ls -g` user
    other_npm = npm_other_installed() if source_available("npm") else []
    if other_npm and "npm" in only:
        npm_exe = shutil.which("npm") or "npm"
        if with_latest:
            with ThreadPoolExecutor(max_workers=6) as pool:
                latest_other = dict(zip([i["name"] for i in other_npm],
                                        pool.map(lambda i: npm_latest(i["name"]), other_npm)))
        else:
            latest_other = {}
        for it in other_npm:
            root = Path(it["prefix"]) / "lib" / "node_modules"
            cmd = [npm_exe, "install", "-g", "--prefix", it["prefix"], f"{it['name']}@latest"]
            if not os.access(root, os.W_OK):
                cmd = ["__sudo__"] + cmd
            bins = f" · bin: {', '.join(it['bins'])}" if it["bins"] else ""
            add("npm", it["name"], it["current"], latest_other.get(it["name"]),
                upgrade=cmd, note=f"global {it['prefix']}{bins}")

    if "flatpak" in only and source_available("flatpak"):
        for it in (flatpak_updates() if with_latest else
                   [{"name": k, "current": v, "latest": v, "has_update": False}
                    for k, v in flatpak_current().items()]):
            add("flatpak", it["name"], it["current"], it["latest"], it["has_update"],
                upgrade=["flatpak", "update", "-y", it["name"]])

    if "appimage" in only:
        apps = appimage_scan()
        if with_latest:
            with ThreadPoolExecutor(max_workers=6) as pool:
                remotes = list(pool.map(appimage_latest, apps))
        else:
            remotes = [None] * len(apps)
        for it, remote in zip(apps, remotes):
            latest = remote[0] if remote else None
            url = remote[1] if remote else None
            sha = remote[2] if remote else None
            upgrade = ["__appimage__", it["path"], url, sha or "", latest] if (url and latest) else []
            if it.get("repo"):
                origin = f"github:{it['repo']}"
            elif it.get("feed"):
                origin = f"feed:{it['feed']}"
            else:
                origin = "tanpa metadata updater"
            if it.get("stale"):
                note = f"{origin} · versi lama, aman dibersihkan: upd clean --stale"
            else:
                note = origin
            add("appimage", it["name"], it["current"],
                "-" if it.get("stale") else latest,
                False if it.get("stale") else None,
                upgrade=[] if it.get("stale") else upgrade,
                note=note, target=it["path"], repo=it.get("repo") or "")

    if "asdf" in only and source_available("asdf"):
        items = asdf_installed()
        if with_latest:
            with ThreadPoolExecutor(max_workers=6) as pool:
                latest_map_a = dict(zip([i["name"] for i in items],
                                        pool.map(lambda i: asdf_latest(i["name"]), items)))
        else:
            latest_map_a = {}
        for it in items:
            latest = latest_map_a.get(it["name"])
            upgrade = ["__asdf__", it["name"], latest] if latest else []
            add("asdf", it["name"], it["current"], latest, upgrade=upgrade,
                note=f"asdf: versi aktif {it['current']}")

    if "openbot" in only and source_available("openbot"):
        runs = openbot_runtimes()
        if runs:
            with ThreadPoolExecutor(max_workers=4) as pool:
                latest_ob = {}
                for r in runs:
                    key = r["npm"] or r["name"]
                    if r["npm"]:
                        latest_ob[key] = pool.submit(npm_latest, r["npm"])
            for r in runs:
                latest = None
                fut = latest_ob.get(r["npm"] or r["name"])
                if fut is not None:
                    try:
                        latest = fut.result()
                    except Exception:
                        latest = None
                sisa = f" · {r['stale']} folder versi lama" if r.get("stale") else ""
                if r["npm"]:
                    add("openbot", r["name"], r["current"], latest,
                        note=f"runtime OpenBot{sisa}")
                else:
                    add("openbot", r["name"], r["current"], "-", False,
                        note=f"runtime OpenBot (dikelola OpenBot){sisa}")

    if "script" in only:
        exclude_npm = {i["name"] for i in other_npm}
        for i in other_npm:
            exclude_npm.update(i.get("bins") or [])
        entries = script_scan(exclude_npm)
        remote = {}
        if with_latest:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = {}
                for it in entries:
                    if it["kind"] == "git":
                        futs[it["name"]] = pool.submit(github_head_sha, it["repo"])
                    elif it.get("npm"):
                        futs[it["name"]] = pool.submit(npm_latest, it["npm"])
                    else:
                        futs[it["name"]] = pool.submit(github_latest_tag, it["repo"], tuple(it["tag_strip"]))
                for key, fut in futs.items():
                    try:
                        remote[key] = fut.result()
                    except urllib.error.HTTPError as exc:
                        remote[key] = RATE_LIMIT if exc.code in (403, 429) else None
                    except Exception:
                        remote[key] = None
        for it in entries:
            latest = remote.get(it["name"])
            if latest == RATE_LIMIT:
                latest = None
                it["note"] = "GitHub API rate limit — set GITHUB_TOKEN"
            if it["kind"] == "git":
                has = bool(latest) and latest != it["current"]
                add("script", it["name"], it["current"], latest or "?", has,
                    upgrade=it["upgrade"], note="git")
            else:
                url = ""
                if latest and it.get("repo"):
                    url = f"https://github.com/{it['repo']}/releases/tag/v{latest}"
                up = list(it["upgrade"])
                if up[:1] == ["__ollama__"]:
                    up = ["__ollama__", latest] if latest else []
                add("script", it["name"], it["current"], latest,
                    upgrade=up, note=it["note"], url=url,
                    target=it.get("path") or "", repo=it.get("repo") or "")

    if "binary" in only:
        for b in load_binaries_cfg():
            cur = "?"
            if b.get("version_cmd"):
                rc, out, _ = sh(b["version_cmd"], timeout=30) if isinstance(b["version_cmd"], list) else (0, "", "")
                cur = find_version(out) or "?"
            latest = None
            if b.get("latest_url"):
                try:
                    data = http_json(b["latest_url"])
                    latest = find_version(json.dumps(data)) if isinstance(data, dict) else None
                except Exception:
                    latest = None
            add("binary", b.get("name", "?"), cur, latest, note=b.get("path", ""))

    return rows


# --------------------------------------------------------------------------- output

RESET = "\033[0m" if sys.stdout.isatty() else ""
DIM = "\033[2m" if RESET else ""
BOLD = "\033[1m" if RESET else ""
GREEN = "\033[32m" if RESET else ""
YELLOW = "\033[33m" if RESET else ""
RED = "\033[31m" if RESET else ""


def print_table(rows):
    if not rows:
        print("Tidak ada yang terdeteksi.")
        return
    cols = [("SUMBER", "source"), ("PAKET", "name"), ("KINI", "current"), ("TERBARU", "latest"), ("STATUS", "status")]
    data = []
    for r in rows:
        status = "update" if r["has_update"] else ("ok" if r["latest"] != "?" else "?")
        data.append({
            "source": r["source"], "name": r["name"],
            "current": str(r["current"]), "latest": str(r["latest"]),
            "status": status,
        })
    widths = [len(c[0]) for c in cols]
    for d in data:
        for i, (_, key) in enumerate(cols):
            widths[i] = max(widths[i], len(d[key]))

    head = "  ".join(BOLD + c[0].ljust(widths[i]) + RESET for i, c in enumerate(cols))
    print(head)
    print(DIM + "-" * (sum(widths) + 2 * (len(cols) - 1)) + RESET)
    for d in data:
        line = []
        for i, (_, key) in enumerate(cols):
            val = d[key]
            if key == "status":
                color = YELLOW if val == "update" else (GREEN if val == "ok" else DIM)
                val = color + val.ljust(widths[i]) + RESET
            else:
                val = val.ljust(widths[i])
            line.append(val)
        print("  ".join(line))


def summarize(rows):
    out = [r for r in rows if r["has_update"]]
    unknown = [r for r in rows if r["latest"] == "?"]
    print()
    print(f"{len(rows)} aplikasi terpantau, {BOLD}{len(out)}{RESET} punya update" +
          (f", {len(unknown)} versi terbaru tidak diketahui" if unknown else ""))
    if out:
        print(f"Jalankan: {BOLD}upd update{RESET} untuk memasang semuanya")


# --------------------------------------------------------------------------- update

def run_updates(rows, assume_yes=False, dry_run=False):
    todo = [r for r in rows if r["has_update"] and r["upgrade"]]
    skipped = [r for r in rows if r["has_update"] and not r["upgrade"]]
    if not todo:
        print("Tidak ada update yang bisa dijalankan otomatis.")
        if skipped:
            print(f"{len(skipped)} item perlu tindakan manual (lihat kolom catatan).")
        return 0

    print(f"{len(todo)} update akan dijalankan:")
    for r in todo:
        print(f"  - {r['source']}:{r['name']}  {r['current']} -> {r['latest']}")
    if dry_run:
        print()
        for r in todo:
            print(f"  $ {describe_upgrade(r['upgrade'])}")
        if skipped:
            print(f"\n{DIM}Tidak bisa otomatis ({len(skipped)}):{RESET}")
            for r in skipped:
                hint = r["note"] or "update manual"
                print(f"  - {r['source']}:{r['name']}  {r['current']} -> {r['latest']}  {DIM}{hint}{RESET}")
                if r.get("url"):
                    print(f"    {DIM}{r['url']}{RESET}")
        return 0

    if not assume_yes:
        try:
            ans = input("\nLanjut? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "ya", "yes"):
            print("Dibatalkan.")
            return 1

    ok_count = fail = 0
    for r in todo:
        print(f"\n{BOLD}>>> {r['source']}:{r['name']}{RESET} ({describe_upgrade(r['upgrade'])})")
        ok, msg = execute_upgrade(r["upgrade"])
        if ok:
            ok_count += 1
            print(f"    {GREEN}ok{RESET} — {msg}")
        else:
            fail += 1
            print(f"    {RED}gagal{RESET} — {msg}")
    print(f"\nSelesai: {ok_count} berhasil, {fail} gagal.")
    if skipped:
        print(f"\n{DIM}Tidak bisa otomatis ({len(skipped)}):{RESET}")
        for r in skipped:
            print(f"  - {r['source']}:{r['name']}  {r['current']} -> {r['latest']}  {DIM}{r['note']}{RESET}")
            if r.get("url"):
                print(f"    {DIM}{r['url']}{RESET}")
    return 0 if fail == 0 else 2


# --------------------------------------------------------------------------- cli

def cmd_sources(_args):
    print(f"{BOLD}Status sumber{RESET}")
    for src in SOURCES:
        avail = source_available(src)
        mark = GREEN + "ada" + RESET if avail else DIM + "-" + RESET
        print(f"  {src.ljust(10)} {mark}")
    print(f"\nKonfigurasi: {CONFIG_DIR}")
    return 0


def manual_rows(exclude):
    """Baris untuk binary yang tidak dikelola paket apa pun (paralel, hemat waktu)."""
    items = [(n, p) for n, p in sorted(all_manual().items()) if n not in exclude]

    def ver(item):
        name, path = item
        if repo_for(name) or remembered_version(str(path)):
            return _manual_version(name, path)
        return "-"  # tidak dicek: tidak ada sumber versi

    with ThreadPoolExecutor(max_workers=12) as pool:
        versions = list(pool.map(ver, items))

    return [{
        "source": "manual", "name": name,
        "current": versions[i], "latest": "?", "has_update": False,
        "upgrade": [], "note": str(path), "url": "",
        "target": str(path), "repo": (repo_for(name) or ("",))[0] if repo_for(name) else "",
    } for i, (name, path) in enumerate(items)]


def cmd_list(args):
    """Daftar semua yang terpasang, tanpa cek jaringan."""
    only = args.only.split(",") if args.only else None
    rows = collect(only, with_latest=False)
    if not only or "manual" in only:
        tracked = {r["name"] for r in rows if r["source"] in ("script", "asdf")}
        rows += manual_rows(tracked)
    if args.json:
        print(json.dumps({"count": len(rows), "rows": rows}, indent=2))
        return 0
    if not rows:
        print("Tidak ada yang terdeteksi.")
        return 0

    widths = [len("SUMBER"), len("PAKET"), len("VERSI")]
    for r in rows:
        widths[0] = max(widths[0], len(r["source"]))
        widths[1] = max(widths[1], len(r["name"]))
        widths[2] = max(widths[2], len(str(r["current"])))

    print(f"{BOLD}{'SUMBER'.ljust(widths[0])}  {'PAKET'.ljust(widths[1])}  {'VERSI'.ljust(widths[2])}{RESET}")
    print(DIM + "-" * (sum(widths) + 4) + RESET)
    for r in rows:
        print(f"{r['source'].ljust(widths[0])}  {r['name'].ljust(widths[1])}  {str(r['current']).ljust(widths[2])}")

    per = {}
    for r in rows:
        per[r["source"]] = per.get(r["source"], 0) + 1
    rincian = ", ".join(f"{k} {v}" for k, v in sorted(per.items()))
    print(f"\n{BOLD}{len(rows)}{RESET} aplikasi terpasang ({rincian})")
    print(f"{DIM}Untuk cek versi terbaru: upd scan{RESET}")
    return 0


def cmd_scan(args):
    only = args.only.split(",") if args.only else None
    rows = collect(only)
    if args.outdated:
        rows = [r for r in rows if r["has_update"]]
    if args.json:
        print(json.dumps({"count": len(rows), "rows": rows}, indent=2))
        return 0
    print_table(rows)
    summarize(rows)
    manual = [r for r in rows if r["has_update"] and not r["upgrade"] and r["note"]]
    if manual:
        print(f"\n{DIM}Perlu tindakan manual:{RESET}")
        for r in manual:
            print(f"  - {r['source']}:{r['name']}  {r['current']} -> {r['latest']}  {DIM}{r['note']}{RESET}")
            if r.get("url"):
                print(f"    {DIM}{r['url']}{RESET}")
    if any(r["source"] == "appimage" and r["note"].startswith("tanpa metadata") for r in rows):
        print(f"{DIM}AppImage tanpa metadata updater: `upd appimage map FILE OWNER/REPO`{RESET}")
    return 0


def cmd_update(args):
    raw = args.sources or getattr(args, "only", None)
    only = raw.split(",") if raw else None
    rows = collect(only)
    if getattr(args, "pick", None):
        import fnmatch
        wanted = {sp if ":" in sp else f"*:{sp}" for sp in args.pick}
        rows = [r for r in rows
                if any(fnmatch.fnmatch(f"{r['source']}:{r['name']}", w) for w in wanted)]
    if os.geteuid() == 0 and SUDO_USER:
        keep = {"script"} if (getattr(args, "binaries", False) or getattr(args, "only_binaries", False)) else set()
        skipped_sudo = [r for r in rows if r["has_update"] and r["source"] not in keep]
        rows = [r for r in rows if r["source"] in keep]
        if skipped_sudo:
            print(f"{len(skipped_sudo)} item dari paket manager dilewati saat sudo "
                  f"(jalankan tanpa sudo untuk itu)")

    if getattr(args, "binaries", False):
        for r in rows:
            if (r["source"] == "script" and r["has_update"] and not r["upgrade"]
                    and r.get("repo") and r.get("target")
                    and r["repo"] not in NO_BINARY_REPOS):
                if not os.access(Path(r["target"]).parent, os.W_OK):
                    r["note"] = (r["note"] + " [butuh sudo]").strip()
                    continue
                r["upgrade"] = ["__gh__", r["repo"], str(r["latest"]), r["target"]]
                r["is_binary"] = True
        if getattr(args, "only_binaries", False):
            rows = [r for r in rows if r.get("is_binary")]
    return run_updates(rows, assume_yes=args.yes, dry_run=args.dry_run)


def cmd_script(args):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        entries = json.loads(SCRIPTS_CFG.read_text())
        if not isinstance(entries, list):
            entries = []
    except (OSError, json.JSONDecodeError):
        entries = []

    if args.action == "list":
        for e in entries:
            print(f"{e.get('name')}  {e.get('path')}  github:{e.get('repo')}")
        if not entries:
            print("Belum ada tool tambahan. Pakai: upd script add NAME PATH OWNER/REPO")
        return 0

    if args.action == "add":
        name, path, repo = args.name, args.path, args.value
        if not (name and path and repo):
            print("Pakai: upd script add NAME PATH OWNER/REPO [--cmd 'tool --version']")
            return 2
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            print("Repo harus OWNER/REPO, contoh: junegunn/fzf")
            return 2
        entries = [e for e in entries if e.get("name") != name]
        entries.append({
            "name": name,
            "path": str(expand_path(path)),
            "repo": repo,
            "version_cmd": (args.cmd.split() if args.cmd else [name, "--version"]),
            "tag_strip": ["v"],
            "update": args.update.split() if args.update else [],
        })
        SCRIPTS_CFG.write_text(json.dumps(entries, indent=2))
        print(f"Ditambahkan: {name} -> github:{repo}")
        return 0

    print("Pakai: upd script add NAME PATH OWNER/REPO | upd script list")
    return 2


def all_manual():
    """PATH + folder aplikasi di luar PATH, tanpa duplikat."""
    cands = dict(manual_candidates())
    for n, p in offpath_candidates().items():
        cands.setdefault(n, p)
    return cands


def _manual_version(name, path):
    ver = detect_version([str(path), "--version"], name, str(path))
    if ver in ("?", "dev"):
        ver = remembered_version(str(path)) or ver
    return ver


def cmd_manual(args):
    """Semua binary yang tidak dimiliki paket dpkg, termasuk di luar PATH."""
    cands = all_manual()
    names = sorted(cands)

    def ver(n):
        return _manual_version(n, cands[n])

    with ThreadPoolExecutor(max_workers=12) as pool:
        versions = dict(zip(names, pool.map(ver, names)))

    if args.json:
        rows = [{
            "name": n, "path": str(cands[n]),
            "repo": (repo_for(n) or (None,))[0],
            "version": versions.get(n),
        } for n in names]
        print(json.dumps(rows, indent=2))
        return 0

    with_repo = [n for n in names if repo_for(n)]
    print(f"{BOLD}Binary tanpa manajer paket{RESET} ({len(names)} ditemukan, {len(with_repo)} ada repo GitHub)\n")
    for n in names:
        repo = repo_for(n)
        tag = f"github:{repo[0]}" if repo else DIM + "belum ada repo" + RESET
        print(f"  {n.ljust(18)} {versions.get(n, '?').ljust(12)} {tag}")
    unknown = len(names) - len(with_repo)
    if unknown:
        print(f"\n{DIM}{unknown} binary belum punya sumber versi. Daftarkan: "
              f"upd script add NAME PATH OWNER/REPO{RESET}")
    return 0


def cmd_appimage(args):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    mapping = appimage_map()
    if args.action == "map":
        target, repo = args.target, (args.value or "")
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            print("Format repo harus OWNER/REPO, contoh: traycerai/traycer")
            return 2
        p = Path(target).expanduser()
        key = str(p) if p.exists() else target
        mapping[key] = repo
        APPIMAGE_MAP.write_text(json.dumps(mapping, indent=2))
        print(f"Dipetakan: {key} -> {repo}")
        return 0
    if args.action == "list":
        for k, v in mapping.items():
            print(f"{k}  ->  {v}")
        if not mapping:
            print("Belum ada pemetaan.")
        return 0
    print("Pakai: upd appimage map FILE OWNER/REPO | upd appimage list")
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog="upd", description="Update aplikasi di luar apt/snap")
    sub = ap.add_subparsers(dest="cmd")

    ls = sub.add_parser("list", aliases=["ls"],
                        help="daftar semua aplikasi terpasang, termasuk binary manual (offline)")
    ls.add_argument("--only", help="sumber tertentu, pisahkan koma: npm,pipx")
    ls.add_argument("--json", action="store_true", help="keluaran JSON")
    ls.set_defaults(func=cmd_list)

    sc = sub.add_parser("scan", help="lihat semua aplikasi dan versi terbaru")
    sc.add_argument("--only", help="sumber tertentu, pisahkan koma: npm,pipx")
    sc.add_argument("--outdated", action="store_true", help="hanya yang ada update")
    sc.add_argument("--json", action="store_true", help="keluaran JSON")
    sc.set_defaults(func=cmd_scan)

    up = sub.add_parser("update", help="jalankan update")
    up.add_argument("sources", nargs="?", help="sumber tertentu, pisahkan koma")
    up.add_argument("--only", help="sama dengan argumen sumber")
    up.add_argument("--yes", "-y", action="store_true", help="tanpa konfirmasi")
    up.add_argument("--dry-run", action="store_true", help="tampilkan perintah saja")
    up.add_argument("--binaries", action="store_true",
                    help="ikut update binary GitHub (helm, kind, terraform, ...)")
    up.add_argument("--only-binaries", action="store_true",
                    help="hanya binary GitHub, lewati paket manager")
    up.add_argument("--pick", action="append", default=[],
                    help="batasi ke sumber:nama tertentu (boleh berulang)")
    up.set_defaults(func=cmd_update)

    ai = sub.add_parser("appimage", help="kelola pemetaan AppImage -> repo GitHub")
    ai.add_argument("action", choices=["map", "list"])
    ai.add_argument("target", nargs="?")
    ai.add_argument("value", nargs="?")
    ai.set_defaults(func=cmd_appimage)

    sk = sub.add_parser("script", help="kelola tool hasil installer curl")
    sk.add_argument("action", choices=["add", "list"])
    sk.add_argument("name", nargs="?")
    sk.add_argument("path", nargs="?")
    sk.add_argument("value", nargs="?", help="OWNER/REPO GitHub")
    sk.add_argument("--cmd", help="perintah cek versi, mis. 'fzf --version'")
    sk.add_argument("--update", help="perintah update, mis. 'fzf --version'")
    sk.set_defaults(func=cmd_script)

    un = sub.add_parser("uninstall", aliases=["remove"], help="hapus aplikasi")
    un.add_argument("target", help="nama, atau sumber:nama (mis. npm:cline)")
    un.add_argument("--yes", "-y", action="store_true", help="tanpa konfirmasi")
    un.add_argument("--dry-run", action="store_true", help="tampilkan rencana saja")
    un.add_argument("--force", action="store_true", help="izinkan hapus tool inti")
    un.set_defaults(func=cmd_uninstall)

    cl = sub.add_parser("clean", help="bersihkan entri launcher yang yatim")
    cl.add_argument("filter", nargs="*", help="batasi ke nama tertentu, mis. traycer")
    cl.add_argument("--stale", action="store_true",
                    help="bersihkan versi lama: AppImage duplikat & folder runtime OpenBot")
    cl.add_argument("--yes", "-y", action="store_true", help="tanpa konfirmasi")
    cl.add_argument("--dry-run", action="store_true", help="tampilkan saja")
    cl.set_defaults(func=cmd_clean)

    mn = sub.add_parser("manual", help="binary yang tidak dikelola dpkg")
    mn.add_argument("--json", action="store_true", help="keluaran JSON")
    mn.set_defaults(func=cmd_manual)

    sr = sub.add_parser("sources", help="status tiap sumber")
    sr.set_defaults(func=cmd_sources)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
