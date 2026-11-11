#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py — integrate the Freenove 7inch-C DSI panel driver into a
raspberrypi/linux source tree in one run.

It does two things, all idempotent (re-run is safe: no duplicate inserts, and
the sources are refreshed to the latest copy):
  A. Copy the source files from this folder into the kernel tree:
       panel .c / regulator .c / overlay .dts / panel .yaml / mcu .yaml
  B. Register into the tree's existing files (add a few lines, not full copies):
       vendor-prefixes.yaml, the two Kconfigs, the two Makefiles, overlays/Makefile

Usage (on the Pi):
    python3 setup.py                  # defaults to ~/linux; auto git-clones it if missing (~2-3 GB)
    python3 setup.py /home/pi/linux   # or point at a specific kernel tree
If the target is already a kernel tree, cloning is skipped and it just copies + registers.
"""
import os
import platform
import re
import shutil
import subprocess
import sys

def real_home():
    # sudo 下 ~ 会指向 /root；用 SUDO_USER 还原真实调用者的家目录，
    # 这样无论是否 sudo，默认目标都落在用户自己的 ~/linux。
    su = os.environ.get("SUDO_USER")
    if su:
        try:
            import pwd
            return pwd.getpwnam(su).pw_dir
        except (KeyError, ImportError):
            pass
    return os.path.expanduser("~")


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TARGET = os.path.join(real_home(), "linux")
KERNEL = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET)


def hp(*parts):
    return os.path.join(HERE, *parts)


def kp(*parts):
    return os.path.join(KERNEL, *parts)


def read(f):
    with open(f, encoding="utf-8") as fh:
        return fh.read()


def write(f, s):
    # newline="\n" 强制 LF，避免带回 CRLF 触发 checkpatch 的 DOS-line-endings
    with open(f, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(s)


def note(msg):
    print(msg)


def is_kernel_tree(path):
    return (os.path.isfile(os.path.join(path, "Makefile"))
            and os.path.isdir(os.path.join(path, "kernel"))
            and os.path.isdir(os.path.join(path, "drivers", "gpu", "drm", "panel")))


def kernel_branch():
    # 从运行内核推分支：6.18.x -> rpi-6.18.y；取不到就用 master
    m = re.match(r"(\d+)\.(\d+)", platform.release() or "")
    return f"rpi-{m.group(1)}.{m.group(2)}.y" if m else "master"


# --- 0. 目标内核树：不能是 normal_install 自身；不存在则自动克隆 ----------
if os.path.abspath(KERNEL) == os.path.abspath(HERE):
    sys.exit(
        "Error: the target kernel tree cannot be normal_install itself "
        "(source would equal destination).\n"
        "Just run it directly (it clones ~/linux by default), or point at a "
        "tree: python3 setup.py /home/pi/linux"
    )

if not is_kernel_tree(KERNEL):
    want = kernel_branch()
    branches = [want, "master"] if want != "master" else ["master"]
    print(f"Target {KERNEL} is not a raspberrypi/linux kernel tree yet; "
          "auto-cloning.")
    print("Note: a shallow clone is about 2-3 GB and may take a few minutes "
          "depending on the network.")
    created = not os.path.exists(KERNEL)
    ok = False
    for b in branches:
        note(f"Trying: git clone --depth 1 -b {b} "
             f"https://github.com/raspberrypi/linux.git {KERNEL}")
        r = subprocess.run(["git", "clone", "--depth", "1", "-b", b,
                            "https://github.com/raspberrypi/linux.git", KERNEL])
        if r.returncode == 0:
            ok = True
            break
        if created and os.path.isdir(KERNEL):
            shutil.rmtree(KERNEL, ignore_errors=True)   # 清掉失败的半成品再换分支重试
    if not ok:
        sys.exit("Clone failed: check network/git, or clone manually and "
                 "retry:\n"
                 f"    git clone --depth 1 -b {want} "
                 f"https://github.com/raspberrypi/linux.git {KERNEL}")
    note("Clone complete.")

if not is_kernel_tree(KERNEL):
    sys.exit(f"Error: {KERNEL} exists but is not a kernel tree root "
             "(missing Makefile / kernel/ or drivers/gpu/drm/panel).")

# --- A. 拷贝源文件 ----------------------------------------------------------
COPY = [
    ("drivers/gpu/drm/panel/panel-freenove-ek79007.c",
     ("drivers", "gpu", "drm", "panel", "panel-freenove-ek79007.c")),
    ("drivers/regulator/freenove-panel-regulator.c",
     ("drivers", "regulator", "freenove-panel-regulator.c")),
    ("arch/arm/boot/dts/overlays/vc4-kms-dsi-freenove-panel-overlay.dts",
     ("arch", "arm", "boot", "dts", "overlays",
      "vc4-kms-dsi-freenove-panel-overlay.dts")),
    ("Documentation/devicetree/bindings/display/panel/freenove,ek79007-7inch-c.yaml",
     ("Documentation", "devicetree", "bindings", "display", "panel",
      "freenove,ek79007-7inch-c.yaml")),
    ("Documentation/devicetree/bindings/regulator/freenove,panel-mcu.yaml",
     ("Documentation", "devicetree", "bindings", "regulator",
      "freenove,panel-mcu.yaml")),
]

for src_rel, dst_parts in COPY:
    src = hp(*src_rel.split("/"))
    dst = kp(*dst_parts)
    if not os.path.isfile(src):
        note(f"[copy] source missing, skipped: {src_rel}")
        continue
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst) and os.path.samefile(src, dst):
        note(f"[copy] source and destination are the same file, skipped: {src_rel}")
        continue
    shutil.copyfile(src, dst)
    note(f"[copy] {src_rel} -> {os.path.relpath(dst, KERNEL)}")

# --- B1. vendor-prefixes.yaml：在 patternProperties 段内插入 freenove ------
VP_ENTRY = '  "^freenove,.*":\n    description: Freenove\n'
vp = kp("Documentation", "devicetree", "bindings", "vendor-prefixes.yaml")
if os.path.isfile(vp):
    t = read(vp)
    if '"^freenove,' in t:
        note("[reg] vendor-prefixes.yaml: already present, skipped")
    else:
        lines = t.splitlines(keepends=True)
        key_re = re.compile(r'^\s+"\^([a-zA-Z0-9\-]+),')
        start = next((i for i, ln in enumerate(lines)
                      if ln.startswith("patternProperties:")), None)
        if start is None:
            sys.exit("[reg] ERROR: vendor-prefixes.yaml has no 'patternProperties:' "
                     "block; refusing to insert. Add the freenove prefix manually.")
        else:
            # 默认插到段首（patternProperties: 的下一行），若找到首个字母序大于
            # freenove 的键，则插到它前面；碰到非缩进行说明出了块，也停在段首。
            insert_at = start + 1
            for i in range(start + 1, len(lines)):
                m = key_re.match(lines[i])
                if m:
                    if m.group(1) > "freenove":
                        insert_at = i
                        break
                elif lines[i].strip() and not lines[i][0].isspace():
                    break
            lines.insert(insert_at, VP_ENTRY)
            write(vp, "".join(lines))
            note("[reg] vendor-prefixes.yaml: inserted freenove prefix")
else:
    note(f"[reg] missing file: {vp}")

# --- B2/B3. Kconfig：在最后一个 endmenu 之前插入 config 段 ------------------
PANEL_KCONFIG = (
    "config DRM_PANEL_FREENOVE_EK79007\n"
    "\ttristate \"Freenove 7-inch EK79007 DSI panel\"\n"
    "\tdepends on DRM_MIPI_DSI\n"
    "\tdepends on BACKLIGHT_CLASS_DEVICE\n"
    "\tselect VIDEOMODE_HELPERS\n"
    "\thelp\n"
    "\t  Say Y here if you want to enable support for the Freenove 7-inch\n"
    "\t  DSI touch panel (EK79007 glass, 1024x600) used on Raspberry Pi.\n"
)
REG_KCONFIG = (
    "config REGULATOR_FREENOVE_PANEL\n"
    "\ttristate \"Freenove DSI panel MCU regulator/backlight\"\n"
    "\tdepends on I2C\n"
    "\tdepends on OF\n"
    "\tdepends on GPIOLIB\n"
    "\tdepends on BACKLIGHT_CLASS_DEVICE\n"
    "\tselect REGMAP_I2C\n"
    "\thelp\n"
    "\t  Say Y here if you want to enable support for the I2C MCU found on\n"
    "\t  the Freenove 7-inch DSI panel. It provides GPIOs for power/reset\n"
    "\t  and a PWM-based backlight.\n"
)


def insert_kconfig(rel, block, marker):
    f = kp(*rel)
    if not os.path.isfile(f):
        note(f"[reg] missing file: {os.path.join(*rel)}")
        return
    t = read(f)
    if marker in t:
        note(f"[reg] {os.path.join(*rel)}: {marker} already present, skipped")
        return
    lines = t.splitlines(keepends=True)
    idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == "endmenu":
            idx = i
    if idx is None:
        write(f, (t if t.endswith("\n") else t + "\n") + "\n" + block)
        note(f"[reg] {os.path.join(*rel)}: no endmenu found, appended at end")
    else:
        lines.insert(idx, "\n" + block)
        write(f, "".join(lines))
        note(f"[reg] {os.path.join(*rel)}: inserted {marker} before endmenu")


insert_kconfig(("drivers", "gpu", "drm", "panel", "Kconfig"),
               PANEL_KCONFIG, "DRM_PANEL_FREENOVE_EK79007")
insert_kconfig(("drivers", "regulator", "Kconfig"),
               REG_KCONFIG, "REGULATOR_FREENOVE_PANEL")

# --- B4/B5. Makefile：追加 obj 行 -------------------------------------------


def append_makefile(rel, line):
    f = kp(*rel)
    if not os.path.isfile(f):
        note(f"[reg] missing file: {os.path.join(*rel)}")
        return
    t = read(f)
    if line.strip() in t:
        note(f"[reg] {os.path.join(*rel)}: obj line already present, skipped")
        return
    write(f, (t if t.endswith("\n") else t + "\n") + line + "\n")
    note(f"[reg] {os.path.join(*rel)}: appended obj line")


append_makefile(("drivers", "gpu", "drm", "panel", "Makefile"),
                "obj-$(CONFIG_DRM_PANEL_FREENOVE_EK79007) += panel-freenove-ek79007.o")
append_makefile(("drivers", "regulator", "Makefile"),
                "obj-$(CONFIG_REGULATOR_FREENOVE_PANEL) += freenove-panel-regulator.o")

# --- B6. overlays/Makefile：在 dtbo 列表里加一行 ----------------------------
DTBO = "vc4-kms-dsi-freenove-panel.dtbo"
ov_mk = kp("arch", "arm", "boot", "dts", "overlays", "Makefile")
if os.path.isfile(ov_mk):
    t = read(ov_mk)
    if DTBO in t:
        note(f"[reg] overlays/Makefile: {DTBO} already present, skipped")
    else:
        lines = t.splitlines(keepends=True)
        hdr = None
        for i, ln in enumerate(lines):
            if re.match(r'^dtbo-\$\(CONFIG_ARCH_BCM2835\)\s*\+=', ln):
                hdr = i
                break
        if hdr is None:
            for i, ln in enumerate(lines):
                if re.match(r'^dtbo-.*\s*\+=', ln):
                    hdr = i
                    break
        if hdr is None:
            note("[reg] overlays/Makefile: no dtbo block found, add it manually")
        else:
            lines.insert(hdr + 1, "\t" + DTBO + " \\\n")
            write(ov_mk, "".join(lines))
            note(f"[reg] overlays/Makefile: inserted {DTBO}")
else:
    note(f"[reg] missing file: {ov_mk}")

note("\nsetup done: sources copied and the kernel tree registered. "
     "Continue with make / checkpatch / dt_binding_check.")
