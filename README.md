# PiwnicaStyleDashboard

An animated **PCB desktop layer** with a **system dashboard** for Arch Linux (X11): faint circuit
traces over your wallpaper, signal pulses running along them with a green afterglow, and
terminal-style panels showing CPU, GPU, RAM, IOWait, network, system, physical disks and temperatures.
It lives under your windows and above the wallpaper, lets every click through to the desktop,
and **keeps off the character on your wallpaper** (holdout mask).

<p align="center"><img src="docs/screenshot.png" width="420" alt="Dashboard on a portrait monitor (demo data)"></p>

*Screenshot rendered with `piwnica-dashboard screenshot --demo` (fake data), wallpaper from `examples/orin`.*

## Features

- **Adapts to the machine by itself** — nothing to configure for a typical setup:
  - monitor: the first **portrait** monitor gets the dashboard (else the primary one); the others get the traces only;
  - GPU: **Intel** (i915 / xe, engine load from DRM fdinfo like `intel_gpu_top`), **AMD** (amdgpu sysfs),
    **NVIDIA** (`nvidia-smi`), picking the card that drives a display;
  - network: the interface with the default route;
  - disks: real disks from `lsblk` (no loop/zram/removable), usage in % and GB, read/write rate;
  - temperatures with **limits and colour zones**: green = idle/light work, yellow = normal under load,
    red = close to the limit (not recommended for long periods). Limits come from the hardware
    (Intel TjMax, amdgpu `temp*_crit`, NVIDIA slowdown temp) or vendor specs (AMD CPUs 90 °C,
    Intel Arc 90 °C, NVMe min(70 °C, drive WCTEMP)), each overridable.
- **pacman / yay panel**: while an update runs, a bar for the current package and one for the whole run, each
  with an estimated time left, the phase (download → verify → install → hooks, with the hook's name), download
  speed with a sparkline, downloaded/total size and how much disk space the update adds; `yay` AUR builds show
  the package being built. Between updates: the last `-Syu` (when, how many packages, how long), pending
  updates (`checkupdates` from `pacman-contrib`, `yay -Qua`, every 30 min), packages changed per day over 30 days,
  **reboot needed** (the running kernel's modules are gone, or glibc / systemd / mesa / firmware changed after boot)
  and leftover `.pacnew` files. No root: it reads `/var/log/pacman.log`, `/proc` and the package cache.
  pacman does not tell anyone else which package it is unpacking, so the per-package bar while installing is
  an estimate (marked `~`), as is the download bar when the file in progress sits in pacman's root-only temp dir.
- **Proxmox VE panel**: reads node load, guests, storage usage, configured NFS shares and physical disk inventory
  through a read-only API token. Configure `[proxmox]` with `host = "192.168.1.100"` and `node = "ariku"` for
  that PVE host; the dashboard polls disk inventory every minute and storage configuration every five minutes.
  SMART is polled every five minutes and guest package/Docker inventories are scanned read-only every 30 minutes
  over SSH to the PVE node (default user `root`), using `pct` and QEMU Guest Agent; nothing is installed in
  the guests. Run
  `piwnica-dashboard inventory` for the full disk, SMART, LXC/VM package-version and Docker image-tag report.
  Known application executables are queried separately with read-only version commands. Package update
  candidates come from each guest's cached package-manager metadata; Docker registry tags are not checked online.
- **Updates panel** — what in the homelab needs an update or an intervention: the Proxmox host, LXC/VM guests,
  Docker containers, services, the router, PCs and IoT devices, most urgent first. It shows the verdict of the
  ariku.pl inventory (`GET /api/infra/inventory`, the same list as the companion app), so the dashboard never judges
  on its own: red = critical (security updates, a known-vulnerable version, down for 15+ min), yellow = needs
  attention (container restarting, updates waiting 30+ days, a source that stopped reporting), green = an update
  is available. It uses the Luneta device token (`~/.config/piwnica-dashboard/luneta.token`, owner only) and
  refreshes every 5 min. The list of versions stays in memory, never on disk. Guest package updates show up
  once the afterlife PVE collector (`deploy/inventory`) runs on the Proxmox host.
  **Switch: hide or show what is ok** (picked up while the dashboard runs, no restart):

  ```toml
  [updates]
  show_ok = false   # false = only what needs attention (default), true = also everything that is ok
  ```

  **Focus on the attack surface** (default): security updates, known-vulnerable versions and outages always show;
  of the rest only the Proxmox host, the router, network services and community-scripts apps (`cs:*`, from LXCs
  installed with `bash -c "$(curl … community-scripts/ProxmoxVE/main/ct/<app>.sh)"`) stay on screen. IoT firmware,
  game-server images, HA add-ons and plain package updates in guests are only counted (`N routine hidden`):

  ```toml
  [updates]
  focus = "security"   # or "all"; picked up without a restart
  ```

  `piwnica-dashboard updates` prints the same list in a terminal; `--all` includes the ok ones and the routine
  updates, `--json` gives raw output. `scripts/update-homelab.py` installs the APT updates on the Proxmox host
  (`dist-upgrade`) and running LXCs (`upgrade`) after a confirmation, security updates first; community-scripts
  apps are only listed with their `pct exec <id> -- update` command.
- **Claude panel** — the plan limits Claude Code's `/usage` shows: the 5-hour session and the week, with live
  reset countdowns, extra usage when enabled and the week split by product. It reads Claude Code's OAuth token
  from `~/.claude/.credentials.json` (read-only, never refreshed here -- that would log Claude Code out) and asks
  `api.anthropic.com/api/oauth/usage` every minute. The endpoint is not documented and may change; an expired
  token (no Claude Code use for ~8 h) keeps the last numbers on screen. `piwnica-dashboard claude` prints them.
- **Builds panel (Forgejo Actions)** — monitors long-running CI/CD builds on Forgejo (`git.ariku.pl`),
  specifically projects like `afterlife`, `luneta` and `companion_app` (Android). It shows their current progress
  (running stage, elapsed time, progress bar with sweeping beam animation, queued jobs) and detected versions/tags.
  Uses a Forgejo personal access token (`~/.config/piwnica-dashboard/forgejo.token`, chmod 600) and polls every 30 s:

  ```toml
  [builds]
  enabled = true
  forgejo_url = "https://git.ariku.pl"
  token_file = "~/.config/piwnica-dashboard/forgejo.token"
  repos = ["afterlife", "luneta", "companion_app"]
  interval = 30
  ```

  `piwnica-dashboard builds` prints current CI build progress and versions in the terminal (`--json` for raw output).
- **Holdout**: give it a PNG mask (alpha = character) and neither the animation, the tint nor the panels
  touch that area; the panels are placed in the free space around the character, or skipped if there is none.
- **Cheap**: only changed areas are repainted (~30 fps), rendered client-side so the X server only copies
  them; data is sampled once per second, slow sensors every 5 s. About 20 % of one CPU core for three
  monitors with the dashboard (plus ~4 % in Xorg). **While a game runs** (Steam/Proton, Lutris, Heroic/Wine,
  gamescope -- even windowed or alt-tabbed) or a fullscreen window has focus, the traces on the other monitors
  switch off and the **dashboard keeps running**, to watch the game's load (`dashboard_in_games = false` to stop it too).
- **Click-through**, in the "below" window layer: desktop icons and the desktop menu keep working.

## Optional: homelab alerts

Set `[infra] url` and `token` in the config and the `sysmon.sh` title bar shows what
needs attention in your homelab -- counts of critical/warning items and the most urgent
ones, polled every 5 minutes in the background. Any server can feed it; it expects

```json
{"counts": {"critical": 0, "warning": 2, "info": 5},
 "alerts": [{"level": "critical|warning|info", "title": "...", "reason": "..."}]}
```

with the token sent as `Authorization: Bearer <token>`. Empty `url` = off (the default).
If the server is unreachable, the last counts stay on screen, dimmed and marked stale.

## Requirements

Arch Linux, an X11 session with a **compositing** window manager (XFCE: *Window Manager Tweaks → Compositor*;
elsewhere e.g. `picom`). Wayland is not supported.

```sh
sudo pacman -S --needed python python-gobject python-cairo python-numpy gtk3 xorg-xprop ttf-jetbrains-mono
```

## Install

User-local, no sudo:

```sh
git clone https://github.com/arikukaenbyou/PiwnicaStyleDashboard.git
cd PiwnicaStyleDashboard
./install.sh            # runs the tests, installs to ~/.local, autostarts on login, starts now
./install.sh --remove   # uninstall
```

System-wide package: `cd packaging && makepkg -si` (after a release tag exists).

## Usage

```sh
piwnica-dashboard claude      # Claude plan limits (5h session, week)
piwnica-dashboard builds      # Forgejo CI builds progress and versions
piwnica-dashboard updates     # what in the homelab needs an update (--all: also the ok ones)
piwnica-dashboard detect      # what was detected: monitors, holdouts, GPU, network, disks, sensors + limits
piwnica-dashboard run         # start (the autostart entry does this on login)
piwnica-dashboard screenshot --demo out.png --wallpaper wall.png --holdout holdout.png
```

Configuration: `~/.config/piwnica-dashboard/config.toml` is written on first start with every value set to
`auto` and commented. Set a monitor by connector name (`monitor = "DP-1"`), a holdout path, or a temperature
limit (`cpu_limit = 95`) when the automatic guess is wrong.

### Holdout mask

A PNG with **exactly the monitor's size**; alpha > 0 where the character is (a soft, blurred edge looks best).
With `path = "auto"` (default) it is looked up as `holdout-<W>x<H>.png` **next to the monitor's XFCE wallpaper**,
so keeping the wallpaper and its mask in one folder is enough. See `examples/orin/` for a pair.

## Tests

```sh
python -m unittest discover -s tests -t .
```

The collectors are tested on fake `/proc` and `/sys` trees (Intel i915/xe, AMD, NVIDIA, Intel and AMD CPUs,
NVMe, Super I/O), plus monitor choice, config, panel layout around the example character and the rendered
frame (nothing is drawn over the character). CI runs them in an Arch Linux container.

## Licence

MIT, see [LICENSE](LICENSE). The example wallpaper has its own note in [examples/orin](examples/orin/README.md).

---

## Po polsku

**PiwnicaStyleDashboard** to animowana warstwa pulpitu dla Arch Linuksa (X11). Na tapecie rysuje ścieżki
płytki drukowanej z impulsami i zielonym afterglow, a do tego panele w stylu terminala: CPU, GPU, RAM,
IOWait, sieć, system, dyski fizyczne (% i GB) oraz temperatury. Warstwa leży pod oknami, przepuszcza
kliknięcia i omija postać na tapecie (maska holdout).

- **Sama się dopasowuje:** dashboard trafia na pierwszy pionowy monitor, GPU Intel, AMD i NVIDIA są
  rozpoznawane automatycznie, sieć to interfejs z trasą domyślną, dyski to fizyczne dyski z `lsblk`.
- **Temperatury z progami:** zielony to spoczynek, żółty to norma pod obciążeniem, czerwony to blisko
  limitu. Limity pochodzą ze sprzętu albo ze specyfikacji producenta i można je nadpisać w configu.
- **Instalacja:** `./install.sh` bez sudo (zależności wypisze do `sudo pacman -S …`).
  Diagnostyka: `piwnica-dashboard detect`. Konfiguracja: `~/.config/piwnica-dashboard/config.toml`.
- **Panel pacman / yay:** w trakcie aktualizacji pasek bieżącej paczki i całości z szacowanym czasem do końca,
  faza (pobieranie, weryfikacja, instalacja, hooki), prędkość pobierania i przyrost zajętego miejsca, budowanie
  paczek AUR przez yay. Poza aktualizacją: ostatni `-Syu`, oczekujące paczki (`checkupdates`, `yay -Qua`),
  potrzebny restart i pliki `.pacnew`. Bez roota, z `/var/log/pacman.log` i `/proc`.
- **Panel Proxmox VE:** obciążenie węzła, maszyny wirtualne, wykorzystanie storage, skonfigurowane udziały NFS
  i inwentarz fizycznych dysków pobierane przez token API tylko do odczytu. Dla PVE `ariku` ustaw
  `[proxmox] host = "192.168.1.100"` i `node = "ariku"`.
  SMART jest odczytywany co 5 minut, a wersje pakietów/obrazów Docker skanowane tylko do odczytu co 30 minut
  przez SSH do hosta PVE (domyślnie `root`), za pomocą `pct` i QEMU Guest Agent; nic nie jest instalowane
  w gościach. Znane aplikacje są odpytane ich poleceniami wersji tylko do odczytu.
  `piwnica-dashboard inventory` wyświetla pełny raport. Aktualizacje pakietów pochodzą z lokalnego cache
  metadanych gości, a tagi obrazów Docker nie są sprawdzane online w rejestrach.
- **Panel aktualizacji (`updates`):** co w ekosystemie wymaga aktualizacji albo interwencji: host Proxmoxa, LXC/VM,
  kontenery Dockera, usługi, router, komputery i IoT, od najpilniejszych. Statusy liczy inwentarz ariku.pl
  (`/api/infra/inventory`, ta sama lista co w companion app), a dashboard tylko je pokazuje. Czerwony to krytyczne,
  żółty to wymaga uwagi, zielony to dostępna aktualizacja. Potrzebny jest token urządzenia Lunety
  (`~/.config/piwnica-dashboard/luneta.token`), odświeżanie jest co 5 min, a lista nie jest zapisywana na dysku.
  **Przełącznik ukrywania pozycji „ok”:** `[updates] show_ok = false` (domyślnie, tylko to, co wymaga uwagi)
  albo `true` (wszystko). Zmiana działa bez restartu. W terminalu: `piwnica-dashboard updates [--all]`.
- **Gry:** gdy działa gra (Steam/Proton, Lutris, Heroic/Wine, gamescope), także w oknie i po Alt+Tab,
  animacja na pozostałych monitorach się wyłącza, a dashboard działa dalej (`dashboard_in_games = false`, żeby też gasł).
- **Maska postaci:** PNG w rozmiarze monitora, gdzie alfa > 0 oznacza postać. Plik
  `holdout-<szer>x<wys>.png` obok tapety XFCE jest wykrywany sam. Przykład jest w `examples/orin/`.
