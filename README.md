# upd

Update apps that **apt and snap do not manage**: global npm and bun packages,
Python tools (pipx, uv), cargo binaries, flatpak, AppImage, and a few installers.

`scan` only checks. `update` is what changes the machine.

## GUI (GTK4 / libadwaita)

```bash
upd-gui
```

Or open **upd** from the app list. It starts on the **Update** tab and checks
the latest versions itself.

Two tabs: **Update** and **Installed**. Search, a refresh button, and an
**Update** button on each row that has an automatic path. Those rows also have
a checkbox. Tick a few, then press **Update N selected** in the header, or use
**Select all shown** / **Clear selection**. The same checkboxes work on
**Installed**.

Click a row for the detail page: source, version, file location, note, and
**How to update**. **Delete…** sits apart from Update and always asks first.
AppImage files and manual binaries go to Trash. Package managers use their own
uninstall command.

The menu also has **Update all**, **Clean orphaned launchers**, **Clean old
versions**, and **About**.

A row with an **Update** button does not also show an "update" chip. The
**Manual** chip appears only when a newer version exists and there is no
automatic path.

The window needs `/usr/bin/python3`, because PyGObject and GTK4 come from the
distro. If `python3` in the shell is a virtualenv without GTK, use the desktop
entry or `upd-gui`.

Source icons live in `icons/hicolor/` and are loaded from that folder.

## Install

One command, for your own user account:

```bash
curl -fsSL https://raw.githubusercontent.com/azmifarih/upd/main/install.sh | sh
```

Or from a clone:

```bash
git clone https://github.com/azmifarih/upd.git
cd upd
./install.sh
```

That copies upd to `~/.local` and adds the **upd** app. Open a new terminal if
`upd` is not found yet, then run `upd` or `upd-gui`.

```bash
./install.sh --prefix /usr/local   # system-wide; needs permission to write there
./install.sh --uninstall           # remove the install under the same prefix
```

The CLI needs `python3` 3.9 or newer. The window needs PyGObject, GTK4, and
libadwaita from the distro. On Debian or Ubuntu:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
```

If `python3` in the shell is a virtualenv without GTK, `upd-gui` still uses
`/usr/bin/python3`.

## Use

```bash
upd scan                  # every app, plus the latest version
upd scan --outdated       # only apps with an update
upd scan --json           # machine-readable
upd scan --only npm,pipx  # one or more sources

upd update --dry-run      # print the commands
upd update                # ask, then run
upd update --only npm -y  # one source, no prompt
upd update --pick script:pi -y   # one app

upd sources               # which sources are present
```

`upd update` can cover package managers, asdf, self-update tools, AppImage
downloads, and ollama. AppImage and ollama downloads are large (hundreds of MB,
ollama about 1.5 GB), so run `--dry-run` first on a slow connection.

A daily check is the same read-only scan, once:

```bash
upd scan --outdated
```

That command does not install anything.

## Sources

| source | how it is found | latest version | update command |
|---|---|---|---|
| npm | `npm ls -g --json`, plus `lib/node_modules` under `/usr/local` | registry.npmjs.org | `npm install -g <pkg>@latest` (`sudo -n` when the prefix is root) |
| bun | `bun pm ls -g` | registry.npmjs.org | `bun add -g <pkg>` |
| pipx | `pipx list --json` | pypi.org | `pipx upgrade <pkg>` |
| uv | `uv tool list` | pypi.org | `uv tool upgrade <pkg>` |
| cargo | `cargo install --list` | crates.io | `cargo install --force <pkg>` |
| flatpak | `flatpak list` and `remote-ls --updates` | the flatpak remote | `flatpak update -y <app>` |
| appimage | `*.AppImage` | updater metadata inside the file | download and replace (filename stays) |
| script | known curl installers | GitHub releases or commit | self-update or the release archive |
| asdf | `asdf plugin-list` and `asdf current` | `asdf list-all` (stable only) | `asdf install` then `asdf global` |
| openbot | `~/.config/OpenBot/provider-runtimes` | npm, for display only | not updated by upd |
| manual | binaries dpkg does not own | GitHub releases when known | manual |

A pipx venv with no `pip` cannot `pipx upgrade`. upd then runs `pipx reinstall`
for that package name.

A git pull (`oh-my-zsh` and similar) is `git pull --ff-only`. Files marked
skip-worktree are copied aside, the pull runs, then those files and the
skip-worktree bit are put back.

Scans collect sources in parallel.

## Folders outside PATH

`~/.local/opt`, `~/opt`, `~/tools`, and `/opt` are scanned three levels deep,
so a binary that was never added to `PATH` still shows up. Skipped: `lib`,
`share`, `node_modules`, `.git`, `.so`, `.pak`, `.desktop`, and AppImage files
(those belong to the `appimage` source).

Add another root in `OFFPATH_ROOTS` in the script.

## Versions that are not `--version`

| tool | command |
|---|---|
| helm | `helm version --short` |
| kubectl | `kubectl version --client` |
| k0sctl | `k0sctl version` |
| changie | `changie --version` |
| docker-compose | `docker-compose version` |
| flux | `flux --version` |

A source build with no version (`dev build`, `vdev`) is marked `dev`, not `?`.

## Where a binary came from

A file in `/usr/bin` is not assumed to be apt. upd reads
`/var/lib/dpkg/info/*.list`.

```bash
upd manual          # binaries no dpkg package owns
upd manual --json
```

Symlinks are skipped (asdf shims, npm, pipx). A file that starts with `#!` is
a wrapper, not a raw binary, and is skipped unless it has install metadata
(see launchers below).

## PATH folders from shell rc

Directories added by `~/.zshrc`, `~/.zshenv`, `~/.bashrc`, `~/.profile`, and
`/etc/profile.d/*.sh` are scanned. That covers `~/go/bin`, `~/.pulumi/bin`,
`~/opt/bin`, and `/opt/*/bin`.

## GitHub token

Latest-version checks use the GitHub API: 60 requests per hour without a
token, 5,000 with one. upd uses `GITHUB_TOKEN` or `GH_TOKEN` when set, and
otherwise `gh auth token`. The token is never printed or written to the cache.
On a rate limit the latest column is `?` and the note says to set
`GITHUB_TOKEN`.

## `curl | sh` installers

These are detected from where they live (`~/.bun`, `~/.local/bin`,
`~/.cargo/bin`, `~/.deno`, `/usr/local/bin`, `~/.oh-my-zsh`). `/usr/bin` is
left to apt.

Built-in names: bun, uv, rustup, deno, ollama, starship, zoxide, oh-my-zsh.
Tools with a self-update run it (`bun upgrade`, `uv self update`,
`rustup self update`). The others are reported with a manual command.

**ollama** is different. The update downloads the official
`ollama-linux-amd64.tar.zst` (about 1.5 GB), installs `bin/ollama` and
`lib/ollama/` (an existing CUDA folder is left in place), and restarts the
service only when it is already active. No installer script is run.

Register another tool once:

```bash
upd script add fzf ~/.local/bin/fzf junegunn/fzf --cmd "fzf --version"
upd script list
```

The latest version is the newest GitHub release. With no release, the top tag
is used. A commit-only repo (oh-my-zsh) is compared by commit hash.

## AppImage

The version comes from inside the AppImage: `X-AppImage-Version` in the
`.desktop` file, then `resources/app-update.yml` (electron-updater) with
`provider: github` plus `owner/repo`, or `provider: generic` plus a feed URL.
Extracted metadata is cached by mtime and size.

A `generic` feed is read from `latest-linux.yml`, including `sha512` when the
feed publishes one.

```bash
upd update --pick appimage:Cloudroom.AppImage -y
```

The steps: download the newest `.AppImage` asset, check sha512 when the feed
has one, check the ELF magic and the size, back up under `~/.cache/upd/backup/`,
replace the old file (**the filename stays**, so the menu entry still points at
it), then read the version inside the new file. If that version is not the one
requested, the old file is restored.

Map an AppImage that has no updater metadata:

```bash
upd appimage map ~/Applications/traycer-desktop.AppImage owner/repo
upd appimage list
```

An older copy of the same app (two AppImage files with the same `Name`) is
marked as leftover, not as an update. Remove it with `upd clean --stale`.

```bash
upd list                  # installed apps, offline
upd list --only npm
upd list --json

upd scan                  # compare with the latest version (needs the network)
```

`list` answers what is on this machine. `scan` answers what is behind. `list`
also includes manual binaries. Their version is `-` when there is no way to
read it.

## Script launchers

Some tools are shell launchers, not binaries. `~/.local/bin/pi` can `exec`
into `~/.pi/agent/install/releases/<version>/...`. A shebang alone used to hide
them. The check is now:

1. **Shebang** — `#!/usr/bin/python*`, pipx, or `node`/`bun` is left to pipx,
   uv, or npm. Other shell launchers continue.
2. **Metadata** — `install/current-version` plus `node_modules/.bin/<name>`,
   or the nearest `package.json`.
3. **Probe** — `<launcher> --version`.
4. **Update command** — for a managed install, `<name> update` when
   `<name> update --help` succeeds.

Anything with no metadata still needs `upd script add NAME PATH OWNER/REPO`.

## OpenBot runtimes

OpenBot keeps its own runtimes in
`~/.config/OpenBot/provider-runtimes/<app>/<target>/<version>`. They show up
as the `openbot` source. The note can mention a newer npm release. **upd does
not install that update.** OpenBot does. Example: `pi`, `codex`.

A user install of Pi (`~/.pi/agent`) is a separate `script:pi` row and updates
with `pi update`.

## GitHub binaries

```bash
upd update --binaries --dry-run
upd update --binaries --only script
```

Pick a `linux/amd64` asset, download it, extract tar.gz or zip, check the ELF
magic and a minimum size, back up under `~/.cache/upd/backup/`, replace the
file, then run `--version`, `version`, or `--help`. If the new binary does not
run, the old one is restored.

A target in `/usr/local/bin` is written directly when possible. If the
directory is root-owned, that one file uses `sudo -n`.

If sudo asks for a password, call upd by its full path:

```bash
sudo ~/.local/bin/upd update --binaries --only-binaries -y
```

`sudo upd` fails because sudo drops the user `PATH`, and `upd` lives in
`~/.local/bin`, which is not in root's `secure_path`.

Repos with no GitHub binary use their official URL (`helm` from get.helm.sh,
`terraform` from releases.hashicorp.com).

## asdf

`asdf current` is the **active** version. A plugin with no active version is
skipped, not shown as `?`. The latest version comes from `asdf list-all` for
that plugin. Pre-releases (`alpha`, `beta`, `rc`, `dev`) are dropped.

```bash
upd update --pick asdf:kubectl -y
# asdf install kubectl 1.37.2 && asdf global kubectl 1.37.2
```

After install, upd checks `asdf current <plugin>`. If it does not point at the
new version, the update is reported as failed.

## npm globals in another prefix

`npm ls -g` only sees the user prefix (nvm and similar). Packages installed
with `npm -g --prefix /usr/local` (for example `elasticdump`) are read from
`/usr/local/lib/node_modules/*/package.json` and updated with the same command
plus `sudo -n` when that folder is root-owned.

## Old versions

```bash
upd clean --stale --dry-run
upd clean --stale -y
```

This moves leftover AppImage copies (two files with the same `Name`) and old
OpenBot runtime folders
(`~/.config/OpenBot/provider-runtimes/<app>/<target>/<old-version>`) to Trash.
It does not delete them.

## Remove an app

```bash
upd uninstall <name>               # show the plan, then ask y/N
upd uninstall npm:cline --dry-run
upd uninstall npm:cline -y
upd remove Cloudroom.AppImage      # AppImage goes to Trash
```

- The name must match one app. If it exists in several sources, use `source:name`.
- Core tools (`npm`, `pnpm`, `bun`, `uv`, `cargo`, `rustup`, `pipx`, `asdf`,
  `git`, `docker`, `python3`) are refused unless you pass `--force`.
- AppImage files move to `~/.local/share/Trash` with a `.trashinfo` file.
- Manual binaries and asdf plugins are not deleted for you. The command shows
  the file and the manual step.
