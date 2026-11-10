# upd

Update aplikasi yang **tidak** dikelola apt/snap: paket global npm/bun, tool
Python (pipx, uv), binary cargo, flatpak, dan AppImage.

## GUI (GTK4 / libadwaita)

```bash
upd-gui        # dari terminal
```

Atau cari **upd** di daftar aplikasi. Saat dibuka, app langsung berada di tab
**Update** dan memeriksa versi terbaru sendiri.

Isinya: dua tab (Update / Terpasang), pencarian, tombol ⟳ untuk memeriksa
ulang, dan tombol Update per baris. Tiap baris yang punya jalur update punya
**kotak centang**; centang beberapa lalu tekan **Update N terpilih** di header
(atau menu **Pilih semua yang tampil** / **Batal pilih**). Centang juga berlaku
di tab Terpasang, jadi kamu bisa memilih sambil melihat daftar lengkap.

Klik satu baris untuk membuka halaman detail: sumber, versi, lokasi file,
catatan, **Cara update**, plus tombol **Hapus…** yang terpisah dari Update dan
selalu minta konfirmasi (AppImage dan binary manual dipindah ke Trash, paket
manager memakai perintah resminya).

Menu: Update semua, Bersihkan launcher yatim, Bersihkan sisa versi lama, Tentang.

Di baris yang punya tombol **Update**, tidak ada lagi chip "update" — tombolnya
sudah menyatakan hal yang sama. Chip `manual` hanya muncul kalau ada versi baru
tetapi tidak ada jalur otomatisnya. Halaman detail menampilkan **Cara update**
(perintah/aksi yang akan dijalankan) sebelum kamu menekan tombolnya.

GUI memakai `/usr/bin/python3` karena PyGObject + GTK4 datang dari distro.
Kalau `python3` di shell adalah venv (tanpa GTK), jalankan lewat desktop entry
atau `upd-gui`.

## Pasang

```bash
ln -sf "$PWD/upd.py" ~/.local/bin/upd
chmod +x upd.py
```

Butuh `python3` saja — tanpa dependensi.

## Pakai

```bash
upd scan                  # tabel semua aplikasi + versi terbaru
upd scan --outdated       # hanya yang ada update
upd scan --json           # untuk rutinitas / cron
upd scan --only npm,pipx  # sumber tertentu

upd update --dry-run      # lihat perintah yang akan dijalankan
upd update                # minta konfirmasi, lalu jalankan
upd update --only npm -y  # satu sumber, tanpa konfirmasi
upd update --pick script:pi -y   # satu aplikasi saja

upd sources               # status tiap sumber
```

`upd update` menjalankan semua sumber sekaligus: paket manager, asdf,
self-update tool, AppImage (unduh lalu timpa), dan ollama. AppImage dan ollama
mengunduh file besar (ratusan MB sampai ~1,5 GB), jadi `--dry-run` dulu kalau
koneksi terbatas.

`scan` selalu baca-saja. Hanya `update` yang mengubah sistem.

## Sumber

| sumber | deteksi | cek versi | perintah update |
|---|---|---|---|
| npm | `npm ls -g --json` + `lib/node_modules` di `/usr/local` | registry.npmjs.org | `npm install -g <pkg>@latest` (otomatis `sudo -n` kalau prefix root) |
| bun | `bun pm ls -g` | registry.npmjs.org | `bun add -g <pkg>` |
| pipx | `pipx list --json` | pypi.org | `pipx upgrade <pkg>` |
| uv | `uv tool list` | pypi.org | `uv tool upgrade <pkg>` |
| cargo | `cargo install --list` | crates.io | `cargo install --force <pkg>` |
| flatpak | `flatpak list` + `remote-ls --updates` | remote flatpak | `flatpak update -y <app>` |
| appimage | glob `*.AppImage` | metadata updater di dalam file | unduh + timpa (nama file tetap) |
| script | daftar tool installer curl | GitHub releases / commit | self-update / arsip rilis |
| asdf | `asdf plugin-list` + `asdf current` | `asdf list-all` (stabil) | `asdf install` + `asdf global` |
| manual | binary tanpa pemilik dpkg | GitHub releases bila dikenal | manual |

## Folder di luar PATH

`~/.local/opt`, `~/opt`, `~/tools`, dan `/opt` ikut dipindai sampai 3 tingkat,
jadi binary yang tidak pernah di-export ke PATH tetap ketemu. Yang dilewati:
folder `lib`, `share`, `node_modules`, `.git`, file `.so`, `.pak`, `.desktop`,
serta file AppImage (sudah ditangani sumber `appimage`).

Kalau ada folder lain, tambahkan ke `OFFPATH_ROOTS` di skrip.

## Versi yang tidak standar

Tidak semua tool menjawab `--version`. Yang punya perintah khusus:

| tool | perintah |
|---|---|
| helm | `helm version --short` |
| kubectl | `kubectl version --client` |
| k0sctl | `k0sctl version` |
| changie | `changie --version` |
| docker-compose | `docker-compose version` |
| flux | `flux --version` |

Build dari source tanpa versi (`dev build`, `vdev`) ditandai `dev`, bukan `?`.

## Cek asal-usul binary

`/usr/bin` tidak otomatis berarti apt. `upd` membaca daftar file milik semua
paket dari `/var/lib/dpkg/info/*.list`, jadi kepemilikan bisa dipastikan tanpa
menebak dari lokasi.

```bash
upd manual          # semua binary yang tidak dimiliki paket dpkg
upd manual --json
```

Symlink dilewati karena biasanya milik manajer lain (asdf shim, npm, pipx).
File yang diawali `#!` juga dilewati karena itu script pembungkus, bukan binary.

## Folder PATH dari shell rc

Folder yang ditambahkan `~/.zshrc`, `~/.zshenv`, `~/.bashrc`, `~/.profile`, dan
`/etc/profile.d/*.sh` ikut dipindai, jadi binary di lokasi tak lazim
(`~/go/bin`, `~/.pulumi/bin`, `~/opt/bin`, `/opt/*/bin`) tetap ketemu.

## Token GitHub

Cek versi terbaru memakai API GitHub: 60 permintaan/jam tanpa token, 5.000/jam
dengan token. `upd` memakai `GITHUB_TOKEN`/`GH_TOKEN` bila ada, kalau tidak
mengambil dari `gh auth token`. Token tidak pernah ditampilkan atau ditulis ke
cache. Kalau kena batas, kolom versi terbaru berisi `?` dengan catatan
`rate limit`.

## Installer `curl | sh`

Tool yang dipasang lewat script installer dideteksi dari lokasinya
(`~/.bun`, `~/.local/bin`, `~/.cargo/bin`, `~/.deno`, `/usr/local/bin`,
`~/.oh-my-zsh`). `/usr/bin` sengaja dilewati karena itu wilayah apt.

Bawaan yang dikenali: bun, uv, rustup, deno, ollama, starship, zoxide,
oh-my-zsh. Yang punya self-update dijalankan otomatis (`bun upgrade`,
`uv self update`, `rustup self update`). Yang tidak punya hanya dilaporkan
beserta perintah manualnya.

**ollama** dikecualikan dari aturan itu: update-nya mengunduh
`ollama-linux-amd64.tar.zst` resmi (~1,5 GB), memasang `bin/ollama` dan
`lib/ollama/` (folder CUDA yang sudah ada tidak disentuh), lalu me-restart
service hanya kalau service-nya memang sedang aktif. Tidak ada skrip installer
yang dijalankan.

Tool lain bisa didaftarkan sekali:

```bash
upd script add fzf ~/.local/bin/fzf junegunn/fzf --cmd "fzf --version"
upd script list
```

Versi terbaru diambil dari release GitHub terakhir; kalau repo tidak punya
release, dipakai tag teratas. Untuk repo yang hanya punya commit (mis.
oh-my-zsh), yang dibandingkan adalah hash commit.

## AppImage

Versi dibaca dari metadata di dalam AppImage itu sendiri: `X-AppImage-Version`
di file `.desktop`, lalu `resources/app-update.yml` (electron-updater) yang
menyebut `provider: github` + `owner/repo`, atau `provider: generic` + URL feed.
Semua hasil extract di-cache per mtime+size, jadi `scan` berikutnya instan.

Kalau aplikasinya memakai feed `generic` (bukan GitHub), versi terbaru diambil
dari `latest-linux.yml` di feed itu, termasuk `sha512` untuk verifikasi unduhan.

Update berjalan otomatis:

```bash
upd update --pick appimage:Cloudroom.AppImage -y
```

Alurnya: unduh asset `.AppImage` terbaru → cek sha512 (kalau feed menyediakannya)
→ cek magic ELF + ukuran → backup ke `~/.cache/upd/backup/` → timpa file lama
(**nama file tidak diubah**, jadi entri menu tetap menunjuk ke tempat yang sama)
→ baca ulang versi di dalam file baru; kalau isinya bukan versi yang diminta,
versi lama dikembalikan.

Yang tidak punya metadata updater sama sekali tetap bisa dipetakan manual:

```bash
upd appimage map ~/Applications/traycer-desktop.AppImage owner/repo
upd appimage list
```

Versi lama dari aplikasi yang sama (dua file AppImage dengan `Name` yang sama)
ditandai sisa, bukan update; bersihkan dengan `upd clean --stale`.

```bash
upd list                  # semua yang terpasang, offline (~3 detik)
upd list --only npm       # satu sumber
upd list --json           # untuk diolah

upd scan                  # bandingkan dengan versi terbaru (butuh jaringan)
```

`list` menjawab "apa saja yang ada di mesin ini"; `scan` menjawab "mana yang
ketinggalan". `list` juga memuat binary manual yang tidak dikelola paket apa
pun — versinya `-` kalau tidak ada cara membacanya.

## Launcher script (deteksi otomatis)

Banyak tool dipasang sebagai **shell launcher**, bukan binary — contohnya
`~/.local/bin/pi` yang `exec` ke `~/.pi/agent/install/releases/<versi>/...`.
Dulu semuanya dilewati karena ber-shebang. Sekarang dibedakan:

1. **Shebang** — `#!/usr/bin/python*`, `pipx`, atau `node`/`bun` dilewati
   karena sudah diurus sumbernya (pipx, uv, npm). Shell launcher lanjut.
2. **Metadata** — dicari `install/current-version` + `node_modules/.bin/<nama>`
   (managed install), atau `package.json` terdekat (npm-style).
3. **Probe** — versi dibaca dari `<launcher> --version`.
4. **Perintah update** — kalau managed install dan `<nama> update --help`
   berhasil, dipakai `<nama> update`.

Yang tidak punya metadata sama sekali tetap perlu didaftarkan manual:
`upd script add NAMA PATH OWNER/REPO`.

## Runtime OpenBot

OpenBot membundel runtime sendiri di `~/.config/OpenBot/provider-runtimes/<app>/<target>/<versi>`.
Runtime itu muncul sebagai sumber `openbot`, dibandingkan dengan rilis npm, dan
**tidak** diupdate oleh `upd` — OpenBot yang mengurusnya. Contoh: `pi`.

Install Pi milik user (`~/.pi/agent`) terdeteksi terpisah sebagai `script:pi`
dan diupdate dengan `pi update`.

## Update binary dari GitHub

```bash
upd update --binaries --dry-run     # lihat asset yang akan diunduh
upd update --binaries --only script # jalankan
```

Alurnya: pilih asset `linux/amd64` → unduh → ekstrak (tar.gz/zip) → cek magic
ELF dan ukuran minimal → backup ke `~/.cache/upd/backup/` → timpa → jalankan
`--version`/`version`/`--help`. Kalau binary baru gagal jalan, versi lama
dipulihkan otomatis.

Target di `/usr/local/bin` ditangani otomatis: `upd` mencoba menulis langsung,
dan kalau lokasinya milik root ia memakai `sudo -n` untuk file itu saja.

Kalau sudo di mesinmu minta password, jalankan dengan path penuh:

```bash
sudo ~/.local/bin/upd update --binaries --only-binaries -y
```

`sudo upd` tidak jalan karena sudo membuang PATH user; `upd` ada di
`~/.local/bin` yang bukan bagian `secure_path` root.

Repo yang tidak menyediakan binary di GitHub memakai URL resmi
(`helm` → get.helm.sh, `terraform` → releases.hashicorp.com).

## asdf

`asdf current` dipakai untuk tahu versi yang **aktif**; plugin yang tidak punya
versi aktif dilewati (bukan ditampilkan sebagai `?`). Versi terbaru diambil dari
`asdf list-all <plugin>` milik plugin itu sendiri — bukan dari daftar repo yang
dihardcode — dan versi pra-rilis (`alpha`, `beta`, `rc`, `dev`) dibuang.

```bash
upd update --pick asdf:kubectl -y
# asdf install kubectl 1.37.2 && asdf global kubectl 1.37.2
```

Setelah memasang, `upd` memeriksa `asdf current <plugin>` benar-benar menunjuk
versi baru; kalau tidak, update dilaporkan gagal.

## npm global di prefix lain

`npm ls -g` hanya melihat prefix npm milik user (nvm dsb.). Paket yang dipasang
dengan `npm -g --prefix /usr/local` (mis. `elasticdump`) dibaca langsung dari
`/usr/local/lib/node_modules/*/package.json`, lalu diupdate dengan perintah yang
sama + `sudo -n` bila foldernya milik root.

## Bersihkan sisa versi lama

```bash
upd clean --stale --dry-run
upd clean --stale -y
```

Yang dibersihkan: AppImage lama dari aplikasi yang sama (dua file dengan `Name`
identik) dan folder versi lama runtime OpenBot
(`~/.config/OpenBot/provider-runtimes/<app>/<target>/<versi-lama>`). Semua
dipindah ke Trash, bukan dihapus.

## Hapus aplikasi

```bash
upd uninstall <nama>               # tampilkan rencana, lalu tanya y/N
upd uninstall npm:cline --dry-run  # lihat perintahnya saja
upd uninstall npm:cline -y         # tanpa konfirmasi
upd remove Cloudroom.AppImage      # AppImage -> Trash, bukan hapus permanen
```

- Nama harus unik. Kalau ada di beberapa sumber, pakai `sumber:nama`.
- Tool inti (`npm`, `pnpm`, `bun`, `uv`, `cargo`, `rustup`, `pipx`, `asdf`,
  `git`, `docker`, `python3`) ditolak kecuali memakai `--force`.
- AppImage dipindah ke `~/.local/share/Trash` (ada `.trashinfo`), jadi masih
  bisa dikembalikan.
- Binary manual dan plugin asdf tidak dihapus otomatis; hanya ditunjukkan
  lokasi file dan perintah manualnya.

## Rutin

Cek harian tanpa menyentuh apa pun:

```
0 8 * * *  ~/.local/bin/upd scan --outdated --json > ~/.cache/upd/harian.json
```
