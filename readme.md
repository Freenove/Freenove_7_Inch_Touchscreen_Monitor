# Freenove 7inch-C DSI Panel — Raspberry Pi 5 driver

Read this in other languages: [English](readme.md) | [简体中文](readme_cn.md)

Driver for the Freenove "7inch C" MIPI-DSI touchscreen (EK79007 glass, 1024x600,
GT911 touch) on a **Raspberry Pi 5**. The panel is driven by two kernel modules
(a DRM panel driver and an on-panel I2C MCU driver for power/reset/backlight)
plus a device-tree overlay.

Requirements: Raspberry Pi 5, **64-bit** Raspberry Pi OS (Bookworm 6.12 or
Trixie 6.18 — both verified). A 32-bit image boots an arm64 kernel on the Pi 5
but cannot build the arm64 modules, so use the 64-bit OS.

This repository offers the driver in two folders, for two different install
styles:

| Folder | Style | What it does |
|---|---|---|
| `manual_install/` | **Manual / out-of-tree** | `install.py` compiles the modules + overlay on the Pi and deploys them, edits `config.txt`, and adds a brightness launcher + touch guard. Re-run it after a kernel upgrade. |
| `normal_install/` | **In-tree / auto** | The same driver laid out for `raspberrypi/linux`, with DT bindings, a `setup.py` integrator and a step-by-step guide to build, check and submit it upstream. Once merged, the driver ships with the kernel and a single `dtoverlay=` line enables it — no user compile, works on 32- and 64-bit images alike. |

## Quick start

Manual (out-of-tree) install on the Pi:
```bash
cd manual_install
sudo python3 install.py
```

In-tree / upstream submission: see `normal_install/readme.md`.

## Documentation

- `manual_install/readme.md` / `manual_install/readme_cn.md` — the manual
  installer usage (English / 简体中文).
- `normal_install/readme.md` — hands-on guide to integrate, build, check and
  submit the driver to the official kernel.
