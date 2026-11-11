# normal_install — submit the Freenove 7inch-C DSI panel driver upstream

Read this in other languages: [English](readme.md) | [简体中文](readme_cn.md)

This folder is the **in-tree** version: it lays out the two drivers, the overlay
and the DT bindings so they can be merged into `raspberrypi/linux`. Once merged,
a user only writes one `dtoverlay=` line to light the panel — no compiling.
(For personal use without submitting, use the sibling `manual_install/`
out-of-tree approach instead.)

Follow the steps below on the Raspberry Pi to download, build, check and submit.
Prose is kept short; commands are copy-pasteable.

## Contents

```
normal_install/
├── setup.py                                   # one-shot integrator into the kernel tree (idempotent)
├── drivers/
│   ├── gpu/drm/panel/panel-freenove-ek79007.c     # DRM panel driver
│   └── regulator/freenove-panel-regulator.c        # on-panel I2C MCU (GPIO/regulator/backlight)
├── arch/arm/boot/dts/overlays/
│   └── vc4-kms-dsi-freenove-panel-overlay.dts      # device-tree overlay
└── Documentation/devicetree/bindings/
    ├── display/panel/freenove,ek79007-7inch-c.yaml # panel binding
    └── regulator/freenove,panel-mcu.yaml           # MCU binding
```

## Prerequisites

- Raspberry Pi 5, **64-bit** Raspberry Pi OS (Bookworm 6.12 / Trixie 6.18, both verified).
- Network access (apt, pip, git clone).

## Step 1: install build dependencies

```bash
sudo apt update
sudo apt install -y git build-essential bc bison flex libssl-dev \
    libncurses-dev device-tree-compiler rsync swig python3-dev python3-venv
```

## Step 2: (optional) clone the kernel tree

> `setup.py` auto-clones `~/linux` when it is missing, so this step is optional.
> Do it manually only to pick a specific branch or location.

```bash
cd ~
uname -r                       # kernel major version: 6.12 -> rpi-6.12.y, 6.18 -> rpi-6.18.y
git clone --depth 1 -b rpi-6.18.y https://github.com/raspberrypi/linux.git
cd linux
make kernelversion             # note it; the ->set callback signature must match
```

> `gpio_chip->set` return type changes across kernels: `int` up to 6.8, `void`
> for 6.9..6.16, `int` again from 6.17. This source is written for the **current
> mainline (>= 6.17, `int`)**. If you build on a 6.9..6.16 tree and hit
> `incompatible-pointer-types`, change `freenove_panel_gpio_set` to `void`
> (`return;` on the bounds check, drop the trailing `return 0;`).

## Step 3: get normal_install onto the Pi and integrate it into the tree

First copy the whole `normal_install/` from Windows to the Pi (either way):

```bash
# A: scp (run in Windows PowerShell, set your Pi IP)
scp -r "normal_install" pi@<Pi-IP>:~/Freenove_7_Inch_Touchscreen_Monitor/

# B: if the repo is already on the Pi, git pull to update it
```

Then run `setup.py` on the Pi; it clones `~/linux` if missing, copies the
sources and registers vendor-prefix / Kconfig / Makefile / overlays:

```bash
python3 ~/Freenove_7_Inch_Touchscreen_Monitor/normal_install/setup.py
# or point at another tree: python3 .../setup.py /home/pi/linux
```

A run of `[copy] ... / [reg] ...` lines means success. The script is idempotent.

## Step 4: configure and build

```bash
cd ~/linux
make bcm2712_defconfig                    # Pi 5; fall back to make bcm2711_defconfig
./scripts/config --module DRM_PANEL_FREENOVE_EK79007
./scripts/config --module REGULATOR_FREENOVE_PANEL
make olddefconfig
grep -E 'DRM_PANEL_FREENOVE_EK79007|REGULATOR_FREENOVE_PANEL' .config   # both should be =m

make -j$(nproc) Image modules dtbs
find . -name 'panel-freenove-ek79007.ko' -o \
       -name 'freenove-panel-regulator.ko' -o \
       -name 'vc4-kms-dsi-freenove-panel.dtbo'                          # all three = success
```

> Do not build a single `.ko` or a single `.dtbo` target: the former lacks
> `vmlinux.o` so modpost floods with undefined symbols, the latter mis-resolves
> its path on arm64. Always use `make Image modules dtbs`.

## Step 5: pre-submission checks

**5.1 code style** (expect 0 errors / 0 warnings / 0 checks):
```bash
cd ~/linux
scripts/checkpatch.pl --strict --file drivers/gpu/drm/panel/panel-freenove-ek79007.c
scripts/checkpatch.pl --strict --file drivers/regulator/freenove-panel-regulator.c
```

**5.2 binding schema check** (needs dtschema, in a venv):
```bash
python3 -m venv ~/dtsenv
source ~/dtsenv/bin/activate
pip install --retries 10 --timeout 120 dtschema yamllint   # retry or add --index-url https://pypi.org/simple if it times out

cd ~/linux
make dt_binding_check \
  DT_SCHEMA_FILES=Documentation/devicetree/bindings/display/panel/freenove,ek79007-7inch-c.yaml
make dt_binding_check \
  DT_SCHEMA_FILES=Documentation/devicetree/bindings/regulator/freenove,panel-mcu.yaml
```

> Lines like `brcm,stb-pcie.yaml ... missing type definition` are noise from
> other in-tree schemas not matching the installed dtschema version — not our
> files. Ignore them.

## Step 6: confirm the real email

Both yaml already list `Zhentao-Lin <173039613@qq.com>` as maintainer; make sure
it matches the `git config` in the next step:
```bash
cd ~/linux
nano Documentation/devicetree/bindings/display/panel/freenove,ek79007-7inch-c.yaml
nano Documentation/devicetree/bindings/regulator/freenove,panel-mcu.yaml
```

## Step 7: split into commits and open a Pull Request

```bash
cd ~/linux
git config user.name  "Your Name"
git config user.email "your@email"

git add Documentation/devicetree/bindings/vendor-prefixes.yaml
git commit -s -m "dt-bindings: Add freenove vendor prefix"

git add Documentation/devicetree/bindings/display/panel/freenove,ek79007-7inch-c.yaml \
        Documentation/devicetree/bindings/regulator/freenove,panel-mcu.yaml
git commit -s -m "dt-bindings: Add bindings for the Freenove 7inch-C DSI panel"

git add drivers/gpu/drm/panel/panel-freenove-ek79007.c \
        drivers/gpu/drm/panel/Kconfig drivers/gpu/drm/panel/Makefile
git commit -s -m "drm/panel: ek79007: Add support for the Freenove 7inch-C panel"

git add drivers/regulator/freenove-panel-regulator.c \
        drivers/regulator/Kconfig drivers/regulator/Makefile
git commit -s -m "regulator: Add Freenove DSI panel MCU regulator/backlight driver"

git add arch/arm/boot/dts/overlays/vc4-kms-dsi-freenove-panel-overlay.dts \
        arch/arm/boot/dts/overlays/Makefile
git commit -s -m "ARM: dts: overlays: Add vc4-kms-dsi-freenove-panel"
```

In each commit body add: hardware background, the two-image test result, and
answers to the two likely review questions — **why not extend
`panel-waveshare-dsi`** (same I2C 0x45 MCU) and **why not split into an MFD**.

Push to your fork and open the PR:
```bash
# fork raspberrypi/linux on GitHub first
git remote add mine https://github.com/<your-account>/linux.git
git push mine HEAD:refs/heads/freenove-dsi-panel
# on GitHub: open a Pull Request from mine/freenove-dsi-panel -> raspberrypi/linux
```

## Appendix: on-device runtime check (optional)

You already validated the hardware with the out-of-tree driver on both images;
to re-verify inside a self-built kernel, **use a spare SD card** (bricking risk):
```bash
sudo make modules_install
sudo cp arch/arm64/boot/Image.gz /boot/firmware/kernel8.img
find . -name 'vc4-kms-dsi-freenove-panel.dtbo' -exec sudo cp -v {} /boot/firmware/overlays/ \;
# add to config.txt:
#   dtoverlay=vc4-kms-v3d
#   dtoverlay=vc4-kms-dsi-freenove-panel,dsi1
sudo reboot
dmesg | grep -iE 'dsi|panel|freenove|goodix'
```

## Appendix: common pitfalls

| Symptom | Cause / fix |
|---|---|
| `make defconfig` recursive dependency | `BACKLIGHT_CLASS_DEVICE`/`GPIOLIB` used `select`; use `depends on` (setup.py already does) |
| config not shown in menuconfig | placed after `endmenu`; setup.py inserts before `endmenu` |
| `make .../foo.ko` undefined symbols | single module lacks vmlinux.o; use `make Image modules dtbs` |
| checkpatch DOS line endings | file is CRLF; on the Pi `sed -i 's/\r$//' <file>` (setup.py writes LF) |
| `dt-doc-validate not found` | dtschema not installed / not in PATH; `source ~/dtsenv/bin/activate` before `make` |
| `->set` incompatible-pointer-types | kernel is 6.9..6.16 (void); see Step 2 signature note |
