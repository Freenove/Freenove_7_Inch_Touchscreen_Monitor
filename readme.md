# Freenove 7inch-C DSI Panel — `install.py` Usage

Read this in other languages: [English](readme.md) | [简体中文](readme_cn.md)

`install.py` is the one-command front-end that installs the Freenove
7-DSI-TOUCH-C panel driver (EK79007 glass, 1024x600) on a Raspberry Pi 5.

## What it does

One run performs the following four steps, in order:

1. **Board check** — the panel is supported on a Raspberry Pi 5 only; any other
   board makes the script stop with exit code `1`.
2. **Build & install** — compiles the two kernel modules and the device tree
   overlay and deploys them. When `drivers/build/` is already complete and
   current, the compile is skipped and only the deploy runs.
3. **config.txt** — asks which DSI connector the panel cable is in (`dsi0` /
   `dsi1`) and makes `/boot/firmware/config.txt` match exactly: commenting out
   wrong or conflicting lines and appending the correct ones at the end.
4. **Brightness launcher** — adds a click-to-run backlight slider entry to the
   desktop user's application menu.

The script is idempotent (safe to re-run) and has no non-interactive mode: run
it from a terminal and answer the prompts.

## Requirements

- A Raspberry Pi 5 running Raspberry Pi OS (Bookworm / Trixie). **This panel is
  supported on a Raspberry Pi 5 (BCM2712) only** — a Pi 3 / Pi 4 will not drive
  it (see Notes for why), and `install.py` stops with exit code `1` on any other
  board.
- Python 3 — the installer itself uses only the standard library.
- Root — run it with `sudo`.
- The backlight slider it launches (`drivers/brightness.py`) needs `python3-pyqt5`.

## Project layout

Everything is driven from `install.py`, which stays at the project root. The
files it calls are split into two folders:

```
Freenove_TouchScreen_Driver_Install/
├── install.py                         # the front-end you run
├── readme.md / readme_cn.md           # this documentation
├── drivers/                           # everything build.sh compiles and deploys
│   ├── build.sh                       #   the build/deploy script
│   ├── Makefile                       #   kernel-module build rules
│   ├── panel-freenove-ek79007.c        #   DRM panel driver source
│   ├── freenove-panel-regulator.c      #   on-panel MCU (GPIO + backlight) source
│   ├── vc4-kms-dsi-freenove-panel.dts  #   device tree overlay source
│   ├── brightness.py                   #   the backlight slider
│   └── build/                          #   (generated) compiled .ko/.dtbo output
└── logo/
    └── brightness.png                 # icon for the menu launcher
```

`build.sh` always works relative to its own folder, so `build/` is produced at
`drivers/build/` no matter where you call it from. `install.py` looks for the
sources, the artifacts and the launcher files in these folders automatically.

## Usage

```bash
sudo python3 install.py
```

## Command-line options

### Install flow

| Option | What it does |
|---|---|
| *(no option)* | Full interactive install: board check → build/deploy → `config.txt` → brightness launcher → summary |
| `--reinstall` | Ignore the `drivers/build/` freshness check and force a full "compile + deploy" |
| `--rotation X` | Set the display rotation directly (`0` / `90` / `180` / `270`) |
| `--swapxy` | Set the touch X/Y swap flag directly |
| `--invx` | Set the touch X-invert flag directly |
| `--invy` | Set the touch Y-invert flag directly |
| `--sizex N` / `--sizey N` | Set the touch coordinate range directly |

All of these only write `config.txt` (a boot-time override); the `.dts` is never modified and nothing is recompiled.

### Querying the current config (read-only)

`--screen` and `--touch` are pure queries. They read the current overlay
parameters from `config.txt`, show the `.dts` default for anything not set
there, change nothing, and exit (no board check, no build). To modify a value,
use the individual `--rotation` / `--swapxy` / `--invx` / `--invy` / `--sizex` /
`--sizey` flags above.

| Option | What it prints |
|---|---|
| `--screen` | The current display rotation (`0` / `90` / `180` / `270`) |
| `--touch` | The current touch config: `swapxy` / `invx` / `invy` on/off + the `sizex` / `sizey` coordinate range |

### Removing what was installed

Each option below performs only that cleanup and then exits; `--uninstall` runs
all of them. They take effect **immediately** (there is no preview mode).

| Option | What it removes |
|---|---|
| `--clean` | The generated `drivers/build/` and the root `__pycache__` directory |
| `--remove-overlay` | `vc4-kms-dsi-freenove-panel.dtbo` from `/boot/firmware/overlays/` |
| `--remove-config` | The Freenove `dtoverlay` lines this installer added to `config.txt` (your own `dtoverlay=vc4-kms-v3d` is left untouched) |
| `--remove-menu` | The brightness launcher and its copies in `~/.local/share/applications/` |
| `--uninstall` | All of the above |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Unsupported hardware (not a Raspberry Pi 5) |
| `2` | An error |
| `3` | The user cancelled |

## Notes

- **Pi 5 only — why a Pi 4 stays dark** — the overlay binds the panel to the
  Pi 5 (BCM2712) display path: the on-panel MCU / GT911 touch are grafted onto
  `&i2c_csi_dsi` and the DSI host onto `&dsi1`. A Pi 4 (BCM2711) routes its DSI
  display connector, CSI and I2C differently, so on that board the MCU at I2C
  `0x45` never appears, the regulator / panel drivers have nothing to bind to,
  and the power-up + DCS init sequence that lights the panel never runs. This is
  a hardware / device-tree difference, not something a newer OS image fixes.
- **Reboot to apply** — the overlay and `config.txt` changes take effect after
  `sudo reboot`.
- **After a kernel upgrade** — once you run `apt full-upgrade`, the compiled
  modules no longer match the running kernel; re-run `sudo python3 install.py`
  to rebuild them.
- **Brightness launcher** — it starts `brightness.py` in the background
  (`Terminal=false`). `brightness.py` writes the backlight through `sudo tee`,
  so a click only changes the brightness when `sudo` is already cached or the
  command is configured passwordless (NOPASSWD).
