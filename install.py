#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Front-end for installing the Freenove 7inch-C DSI panel (EK79007) driver.

Run this on the Raspberry Pi instead of calling build.sh by hand. One run is
three steps, in this order:

  1. Board             -- the panel is supported on a Raspberry Pi 5 only; any
                          other board makes the script quit with
                          EXIT_BOARD_UNSUPPORTED;
                          (the individual --rotation/--swapxy/--invx/--invy/
                          --sizex/--sizey flags set display/touch overlay
                          parameters that go into config.txt only -- the .dts is
                          never modified),
  2. DriverBuild   -- look at the drivers/build/ directory build.sh compiles
                      into: run a full build when it is missing, incomplete or
                      stale, and run build.sh with --install-only (deploy without
                      compiling) when it is already current;
  3. ConfigFile        -- ask which DSI connector (dsi0 / dsi1) the panel is wired
                          to and make the boot configuration say exactly that,
                          commenting out wrong or conflicting lines and appending
                          the correct ones at the bottom.

Installer ties the three together and owns the command line.

After those steps a fourth action drops a click-to-run launcher for brightness.py
into the desktop user's application menu (--remove-menu takes it back out). It
mirrors create_desktop_shortcut.py's create_application_to_programming and uses
brightness.png as the icon.

A fifth action deploys the GT911 touch range guard (drivers/touch-guard.py and
drivers/touch-guard.service) as a boot-time oneshot that re-probes the touch
when a cold boot leaves the GT911 on its 4096 power-on range. It is a fallback
for the kernel/overlay IOVCC power fix and stays a no-op once that fix already
yields the right range; --remove-guard takes it back out.

Before that launcher is created install.py checks its one dependency: the slider
(brightness.py) needs PyQt5, which Raspberry Pi OS ships as python3-pyqt5. If
the desktop interpreter cannot import it, install.py installs the package. This
is non-fatal -- the panel and touch work without the slider -- and --ensure-pyqt
runs just that check on its own.

Two flags are read-only queries rather than install steps: --screen and --touch
print the current display / touch overlay parameters found in config.txt,
falling back to the .dts defaults for anything not set there. They change
nothing and exit; use the individual flags above to modify a value.

Usage:
    sudo python3 install.py                 # fully interactive
    sudo python3 install.py --reinstall     # force the full compile+deploy run
    sudo python3 install.py --rotation 90   # set the display rotation directly
    python3 install.py --screen             # query the current display rotation
    python3 install.py --touch              # query the current touch config
    sudo python3 install.py --ensure-pyqt   # check/install PyQt5 for the slider and exit
    python3 install.py --remove-menu        # only take the brightness launcher back out
    sudo python3 install.py --uninstall     # remove drivers/build/, overlay, config lines and launcher

Exit codes: 0 success, 1 unsupported hardware, 2 an error, 3 the user cancelled.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# --- constants ---------------------------------------------------------------
# --- how the panel shows up in config.txt; see readme.md "Configuring config.txt"
PANEL_OVERLAY = "vc4-kms-dsi-freenove-panel"
# The KMS display stack is written vc4-kms-v3d and the firmware maps it onto the
# board-specific variant, so vc4-kms-v3d-pi4 / -pi5 / -autoprobe count as set too.
KMS_OVERLAY = "vc4-kms-v3d"
# Waveshare's official overlays register the same on-panel MCU node (I2C 0x45),
# so they must not stay enabled next to this driver.
CONFLICT_OVERLAYS = ("vc4-kms-dsi-waveshare-panel",)
DSI_INTERFACES = ("dsi0", "dsi1")
DEFAULT_INTERFACE = "dsi1"  # dsi1 is the overlay's default target
COMMENT_MARKERS = ("#", ";")

# --- brightness launcher: a click-to-run menu entry for brightness.py ---------
# Mirrors create_desktop_shortcut.create_application_to_programming. The .desktop
# file name must not carry spaces, so it is spelled out here rather than derived
# from the human-readable label.
MENU_DESKTOP_NAME = "freenove-brightness"
MENU_LABEL = "Freenove Panel Brightness"
MENU_COMMENT = "Adjust the Freenove 7inch-C DSI panel backlight"
BRIGHTNESS_SCRIPT = "brightness.py"
BRIGHTNESS_ICON = "brightness.png"
# The shell wrapper the menu entry runs (create_desktop_shortcut wraps run_app.sh
# the same way); it execs the copied brightness.py.
BRIGHTNESS_LAUNCHER_SH = "freenove-brightness.sh"

# --- touch range guard: a boot-time cold-boot fallback for the GT911 ---------
# The guard re-probes the touch when a cold boot left the chip on its default
# 4096 range. It is optional insurance for the kernel/overlay fix, so it lives
# in its own files and only becomes active once these deploy steps run: the
# .py is the guard, the .service is a systemd oneshot that runs it once per
# boot. The ExecStart inside the unit points at the installed .py path below,
# so the two destinations have to stay in sync with that file.
GUARD_SCRIPT = "touch-guard.py"
GUARD_UNIT = "touch-guard.service"
GUARD_UNIT_NAME = "freenove-touch-guard.service"
GUARD_SCRIPT_DEST = Path("/usr/local/bin/freenove-touch-guard.py")
GUARD_UNIT_DEST = Path("/etc/systemd/system/freenove-touch-guard.service")

# --- what a finished run of build.sh leaves in drivers/build/, i.e. how
#     "already compiled" is judged
MODULE_NAMES = ("panel-freenove-ek79007", "freenove-panel-regulator")
MODULE_SOURCES = ("Makefile", "panel-freenove-ek79007.c", "freenove-panel-regulator.c")
BUILD_DIR = "build"
# Layout: build.sh, the module sources, the .dts and brightness.py live in
# drivers/; the icon lives in logo/. install.py and the readmes stay at the root.
DRIVERS_DIR = "drivers"
LOGO_DIR = "logo"
# kbuild's own bookkeeping: its presence alongside the .ko is what makes the
# directory a real build tree rather than a folder someone dropped files into.
BUILD_META = ("modules.order", "Module.symvers")
# build.sh option: deploy what build/ already holds, compile nothing, install no
# package. Used when the build directory is complete and current.
INSTALL_ONLY_FLAG = "--install-only"
OVERLAY_DTS = "vc4-kms-dsi-freenove-panel.dts"
OVERLAY_DTBO = "vc4-kms-dsi-freenove-panel.dtbo"

# --- panel overlay tunables (screen + touch) ---------------------------------
# The individual flags --rotation/--swapxy/--invx/--invy/--sizex/--sizey set a
# value directly; it is written onto the panel dtoverlay line in config.txt as a
# boot-time override (the .dts itself is never modified, so no rebuild is forced).
# --screen and --touch are read-only: they print the current values found in
# config.txt, falling back to the .dts defaults below when a parameter is absent.
ROTATION_CHOICES = ("0", "90", "180", "270")
TOUCH_FLAG_KEYS = ("swapxy", "invx", "invy")
# .dts defaults, shown by the read-only query when config.txt does not set them.
DEFAULT_ROTATION = "0"
DEFAULT_SIZEX = "800"
DEFAULT_SIZEY = "480"

RUN_TIME = time.strftime("%Y-%m-%d %H:%M:%S")
WIDTH = 64

# Exit codes are part of the contract: the caller tells "wrong board" from "the
# build failed" without parsing any output.
EXIT_OK = 0
EXIT_BOARD_UNSUPPORTED = 1
EXIT_ERROR = 2
EXIT_USER_ABORT = 3

# geteuid exists only on POSIX systems; the platform check in Installer.run()
# keeps this script on the Raspberry Pi, where a real uid is available.
IS_ROOT = getattr(os, "geteuid", lambda: 0)() == 0
# build.sh calls runuser(1), which only root may use, so every privileged
# command goes through this sudo prefix.
SUDO = [] if IS_ROOT else ["sudo"]


# --- console -----------------------------------------------------------------
class Console:
    """Output and prompting for one human at a terminal, plus the sudo wrapper."""

    @staticmethod
    def banner(title: str) -> None:
        print(f"\n{'=' * WIDTH}\n{title}\n{'=' * WIDTH}")

    @staticmethod
    def ask(text: str) -> str:
        """Read one line; EOF means nobody is there to answer."""
        try:
            return input(text).strip()
        except EOFError:
            print(
                "\nInput ended (not an interactive terminal); this step needs a person to answer."
            )
            raise SystemExit(EXIT_USER_ABORT)

    @staticmethod
    def confirm(text: str, default_yes: bool = False) -> bool:
        hint = "[Y/n]" if default_yes else "[y/N]"
        answer = Console.ask(f"{text} {hint} ").lower()
        if not answer:
            return default_yes
        return answer in ("y", "yes", "yea", "yeah")

    @staticmethod
    def items(label: str, entries: list[str]) -> None:
        if entries:
            print(label)
            for entry in entries:
                print(f"  - {entry}")

    @staticmethod
    def command(cmd: list[str], cwd: Path | None = None) -> int:
        """Run a command with its output shown live; return the exit status."""
        if cmd and cmd[0] == "sudo" and shutil.which("sudo") is None:
            print("Error: sudo is not available; run this script as root.")
            raise SystemExit(EXIT_ERROR)
        print("+ " + " ".join(cmd), flush=True)
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None).returncode


# --- board ------------------------------------------------------------------
@dataclass
class Board:
    """The machine the script is running on, as the device tree describes it."""

    model: str
    compatible: str

    @staticmethod
    def read_cstring(path: Path) -> str:
        """Device tree properties are NUL-terminated plain text."""
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        return raw.split(b"\0")[0].decode("utf-8", "replace").strip()

    @staticmethod
    def read_cpuinfo_field(prefixes: tuple[str, ...]) -> str:
        """Fallback for 32-bit kernels, which put the board name in /proc/cpuinfo."""
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if line.lower().startswith(prefixes):
                    value = line.split(":", 1)[1].strip()
                    if value:
                        return value
        except OSError:
            pass
        return ""

    @classmethod
    def detect(cls) -> "Board":
        model = cls.read_cstring(Path("/proc/device-tree/model")) or cls.read_cstring(
            Path("/sys/firmware/devicetree/base/model")
        )
        model = model or cls.read_cpuinfo_field(("model", "hardware"))

        compatible = ""
        for candidate in (
            Path("/proc/device-tree/compatible"),
            Path("/sys/firmware/devicetree/base/compatible"),
        ):
            try:
                compatible = candidate.read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if compatible:
                break
        return cls(model=model, compatible=compatible)

    @property
    def is_pi5(self) -> bool | None:
        """True / False, or None when the system exposes no usable identity."""
        # The BCM2712 SoC is exclusive to the Pi 5, so it settles the question
        # even when the model string is missing or has been customised.
        text = f"{self.model} {self.compatible}".lower()
        if re.search(r"raspberry\s*pi\s+5\b", text) or "bcm2712" in text:
            return True
        if re.search(r"raspberry\s*pi\b|bcm2711|bcm2710|bcm283", text):
            return False
        return None

    @property
    def description(self) -> str:
        return self.model or ("BCM2712" if "bcm2712" in self.compatible.lower() else "")


# --- driver build -------------------------------------------------------------
class DriverBuild:
    """The build/ directory build.sh compiles into, and whether it is usable.

    build.sh copies the Makefile and the two .c files into build/, compiles both
    modules there, writes the overlay .dtbo next to them and installs from that
    directory. A complete build/ that is newer than its sources is therefore the
    evidence that the compile step already ran, and build.sh can be skipped.

    Only the drivers/ folder is inspected, never the install destinations.
    `kernel` exists so tests can fake the running release.
    """

    def __init__(self, drivers_dir: Path, kernel: str | None = None):
        self.drivers_dir = drivers_dir
        self.kernel = kernel or platform.uname().release
        self.script = self.drivers_dir / "build.sh"
        self.build_dir = self.drivers_dir / BUILD_DIR

    # --- artifact locations --------------------------------------------------
    @property
    def modules(self) -> list[Path]:
        return [self.build_dir / f"{name}.ko" for name in MODULE_NAMES]

    @property
    def dtbo(self) -> Path:
        return self.build_dir / OVERLAY_DTBO

    @property
    def artifacts(self) -> list[Path]:
        """Everything a finished run leaves behind: .ko, .dtbo, kbuild metadata."""
        return (
            self.modules
            + [self.dtbo]
            + [self.build_dir / name for name in BUILD_META]
        )

    # --- individual checks ---------------------------------------------------
    def missing_problems(self) -> list[str]:
        """build/ either does not exist or does not hold every artifact."""
        if not self.build_dir.is_dir():
            return [f"{self.build_dir} does not exist; nothing has been compiled yet"]
        missing = [str(path.name) for path in self.artifacts if not path.is_file()]
        if missing:
            return [f"{self.build_dir} is incomplete, missing: {', '.join(missing)}"]
        return []

    def vermagic_ok(self, module: Path) -> bool:
        """Compare the compiled module's vermagic with the running kernel.

        A kernel upgrade invalidates an existing .ko (readme: re-run build.sh),
        so the vermagic has to name the current release. When modinfo is missing or
        fails we do not claim a mismatch: the file simply being present and current
        is the weaker, still-useful signal.
        """
        if shutil.which("modinfo") is None:
            return True
        try:
            result = subprocess.run(
                ["modinfo", "-F", "vermagic", str(module)], capture_output=True, text=True
            )
        except OSError:
            return True
        if result.returncode != 0:
            return True
        return self.kernel in result.stdout

    def vermagic_problems(self) -> list[str]:
        return [
            f"build/{module.name} was compiled for a kernel other than {self.kernel}; "
            "it needs a rebuild"
            for module in self.modules
            if module.is_file() and not self.vermagic_ok(module)
        ]

    def newest_source_mtime(self, names: tuple[str, ...]) -> float:
        stamps = [
            (self.drivers_dir / name).stat().st_mtime
            for name in names
            if (self.drivers_dir / name).is_file()
        ]
        return max(stamps, default=0.0)

    def stale_problems(self) -> list[str]:
        """Artifacts older than their sources are not the current build.

        Kbuild keeps the output file's mtime at compile time, so a source newer
        than the oldest artifact means the sources changed after that build.
        """
        problems: list[str] = []
        built = [module for module in self.modules if module.is_file()]
        if built:
            oldest = min(module.stat().st_mtime for module in built)
            if self.newest_source_mtime(MODULE_SOURCES) > oldest:
                names = "/".join(module.name for module in built)
                problems.append(f"the sources are newer than build/{names}; rebuild them")
        if self.dtbo.is_file():
            dts_stamp = self.newest_source_mtime((OVERLAY_DTS,))
            if dts_stamp > self.dtbo.stat().st_mtime:
                problems.append(
                    f"{OVERLAY_DTS} is newer than build/{OVERLAY_DTBO}; rebuild the overlay"
                )
        return problems

    # --- summary and execution -----------------------------------------------
    def problems(self) -> list[str]:
        """Everything that still forces a build.sh run; empty means it can be skipped."""
        return (
            self.missing_problems()
            + self.vermagic_problems()
            + self.stale_problems()
        )

    def install(self, options: tuple[str, ...] = ()) -> None:
        """Run build.sh, optionally with --install-only.

        It needs root because of runuser(1); the sudo calls inside build.sh reuse
        the credentials cached by this one.
        """
        status = Console.command(
            SUDO + ["bash", str(self.script), *options], cwd=self.drivers_dir
        )
        if status != 0:
            print(f"\nbuild.sh failed (exit code {status}); fix the build before retrying.")
            raise SystemExit(status)


# --- config.txt ---------------------------------------------------------------
@dataclass
class DtoverlayLine:
    """One dtoverlay= line from config.txt, live or already commented out."""

    index: int
    commented: bool
    name: str
    params: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, index: int, line: str) -> "DtoverlayLine | None":
        """Recognise live and commented lines; None for anything else."""
        text = line.strip()
        commented = False
        while text[:1] in COMMENT_MARKERS:
            commented = True
            text = text[1:].strip()

        match = re.match(r"^dtoverlay\s*=\s*(.*)$", text, re.I)
        if not match:
            return None

        value = match.group(1)
        # An overlay name never contains # or ;, so either one starts a comment.
        for marker in COMMENT_MARKERS:
            value = value.split(marker, 1)[0]
        value = value.strip()
        if not value:
            return None

        parts = [item.strip() for item in value.split(",")]
        return cls(index=index, commented=commented, name=parts[0], params=parts[1:])

    @property
    def is_active(self) -> bool:
        return not self.commented

    @property
    def is_panel(self) -> bool:
        return self.name == PANEL_OVERLAY

    @property
    def is_kms(self) -> bool:
        return self.name.startswith(KMS_OVERLAY)

    @property
    def is_conflict(self) -> bool:
        return self.name.startswith(CONFLICT_OVERLAYS)

    @property
    def interfaces(self) -> set[str]:
        """Connector named on the line; a bare line means the dsi1 default."""
        found = {p.lower() for p in self.params if p.lower() in DSI_INTERFACES}
        return found or {DEFAULT_INTERFACE}

    @property
    def extra_params(self) -> list[str]:
        """Tuning parameters (rotation, invx, ...) that must survive a rewrite."""
        return [p for p in self.params if p.lower() not in DSI_INTERFACES]

    def matches(self, iface: str) -> bool:
        return self.interfaces == {iface}

    def text(self) -> str:
        params = f",{','.join(self.params)}" if self.params else ""
        return f"dtoverlay={self.name}{params}"


def param_key(token: str) -> str:
    """Key part of an overlay parameter: 'rotation=90' -> 'rotation', 'invx' -> 'invx'."""
    return token.split("=", 1)[0].strip().lower()


def tunable_token(key: str, value: str | None) -> str:
    """Render one tunable as its dtoverlay token (bare flag when value is None)."""
    return key if value is None else f"{key}={value}"


def panel_overlay_line(
    iface: str, extra: list[str], tunables: dict[str, str | None] | None = None
) -> str:
    """The dtoverlay= line we consider correct, connector spelled out explicitly.

    Any carried-over parameter whose key is (re)set by tunables is dropped first,
    so a chosen value always wins over an old one and a re-run stays a no-op.
    """
    params = [iface] + [p for p in extra if param_key(p) not in DSI_INTERFACES]
    if tunables:
        keys = {param_key(k) for k in tunables}
        params = [p for p in params if param_key(p) not in keys]
        params += [tunable_token(k, v) for k, v in tunables.items()]
    return f"dtoverlay={PANEL_OVERLAY}" + ("," + ",".join(params) if params else "")


@dataclass
class ConfigPlan:
    """The proposed config.txt, plus what to tell the user about it."""

    lines: list[str]
    report: list[str]
    changed: bool


class ConfigFile:
    """config.txt as an object: read it, plan an edit, apply the plan.

    Nothing is written during plan(), so --dry-run reports exactly what a real
    run would do. Decisions about a line are keyed by its index, so the render
    step stays a simple pass over the original text.
    """

    def __init__(self, path: Path, content: str | None = None):
        self.path = path
        self.reload(content)

    @staticmethod
    def locate(explicit: str | None = None, root: Path | None = None) -> Path:
        base = root or Path("/")
        if explicit:
            return Path(explicit)
        for candidate in ("boot/firmware/config.txt", "boot/config.txt"):
            path = base / candidate
            if path.is_file():
                return path
        print("Error: neither /boot/firmware/config.txt nor the older /boot/config.txt exists.")
        raise SystemExit(EXIT_ERROR)

    def reload(self, content: str | None = None) -> None:
        if content is None:
            content = self.path.read_bytes().decode("utf-8", "surrogateescape")
        self.content = content
        self.lines = content.splitlines(keepends=True)
        # Appended content follows the file's own line endings, so CRLF and LF never mix.
        self.ending = "\r\n" if "\r\n" in content else "\n"
        self.entries = [
            entry
            for entry in (DtoverlayLine.parse(i, line) for i, line in enumerate(self.lines))
            if entry
        ]

    # --- queries -------------------------------------------------------------
    def active(self) -> list[DtoverlayLine]:
        return [entry for entry in self.entries if entry.is_active]

    def panel_lines(self) -> list[DtoverlayLine]:
        return [entry for entry in self.active() if entry.is_panel]

    def correct_lines(self, iface: str) -> list[DtoverlayLine]:
        return [entry for entry in self.panel_lines() if entry.matches(iface)]

    def has_kms(self) -> bool:
        return any(entry.is_kms for entry in self.active())

    def enclosing_section(self, index: int) -> str:
        """Section a line belongs to; '' when it sits before the first header."""
        for line in reversed(self.lines[:index]):
            text = line.strip()
            if len(text) > 2 and text.startswith("[") and text.endswith("]"):
                return text[1:-1].lower()
        return ""

    def problems(self, iface: str) -> list[str]:
        """What is still wrong -- shared by the final check and the warning text."""
        panel = self.panel_lines()
        problems = []
        if not panel:
            problems.append("no active panel overlay")
        elif len(panel) > 1:
            problems.append(f"{len(panel)} active panel overlays; exactly one should remain")
        elif not panel[0].matches(iface):
            found = "/".join(sorted(panel[0].interfaces))
            problems.append(f"connector is {found}, not the requested {iface}")
        if not self.has_kms():
            problems.append(f"dtoverlay={KMS_OVERLAY} is missing")
        return problems

    def panel_tunables(self) -> dict[str, str | None]:
        """Read the overlay parameters off the active panel line for a query.

        Returns {param_key: value} where a bare flag (invx) maps to None and a
        key=value (rotation=90) maps to its value; interface names are skipped.
        An empty dict means no active panel overlay was found.
        """
        panel = self.panel_lines()
        found: dict[str, str | None] = {}
        if not panel:
            return found
        for token in panel[0].params:
            key = param_key(token)
            if key in DSI_INTERFACES:
                continue
            found[key] = token.split("=", 1)[1].strip() if "=" in token else None
        return found

    # --- planning ------------------------------------------------------------
    def plan(self, iface: str, tunables: dict[str, str | None] | None = None) -> ConfigPlan:
        report: list[str] = []
        # index -> (what to tell the user, ASCII tag written into config.txt)
        disable: dict[int, tuple[str, str]] = {}
        rewrite: dict[int, str] = {}

        correct = self.correct_lines(iface)
        keep = correct[0] if correct else None
        self.describe_keep(keep, iface, disable, report)

        carried = self.retire_panel_lines(iface, keep, disable)
        self.retire_conflicts(disable)

        # The readme asks for two lines: the panel overlay on the right connector,
        # plus the KMS display stack.
        need_panel_line = keep is None
        need_kms_line = not self.has_kms()
        if need_kms_line:
            report.append(f"no active dtoverlay={KMS_OVERLAY}; it gets appended as well.")

        if keep and carried:
            missing = [p for p in carried if p not in keep.extra_params]
            if missing:
                rewrite[keep.index] = panel_overlay_line(iface, keep.extra_params + missing)
                report.append(
                    f"line {keep.index + 1} takes over the parameters: {', '.join(missing)}"
                )

        if keep and tunables:
            self.apply_tunables(keep, iface, carried, tunables, rewrite, report)

        changed = bool(disable) or bool(rewrite) or need_panel_line or need_kms_line
        lines = self.render(disable, rewrite)
        if changed:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += self.ending
            lines += self.added_lines(
                iface, carried, need_panel_line, need_kms_line, report, tunables
            )
        report += [
            f"commented out original line {i + 1}: {reason}"
            for i, (reason, _) in sorted(disable.items())
        ]

        return ConfigPlan(lines=lines, report=report, changed=changed)

    def apply_tunables(
        self,
        keep: DtoverlayLine,
        iface: str,
        carried: list[str],
        tunables: dict[str, str | None],
        rewrite: dict[int, str],
        report: list[str],
    ) -> None:
        """Make the live panel line carry exactly the chosen overlay parameters.

        The carried-over tuning parameters and the new values are merged (a new
        value wins on a key clash); if that reproduces the line byte for byte,
        nothing is rewritten, so a re-run stays a no-op.
        """
        extra = list(keep.extra_params)
        for p in carried:
            if p not in extra:
                extra.append(p)
        target = panel_overlay_line(iface, extra, tunables)
        if target != keep.text():
            rewrite[keep.index] = target
            summary = ", ".join(tunable_token(k, v) for k, v in tunables.items())
            report.append(f"line {keep.index + 1} sets: {summary}")

    def describe_keep(
        self,
        keep: DtoverlayLine | None,
        iface: str,
        disable: dict[int, tuple[str, str]],
        report: list[str],
    ) -> None:
        if keep is None:
            report.append(
                f"no active {iface} configuration found; the fix goes at the end of the file."
            )
            return

        report.append(
            f"line {keep.index + 1} already has the correct {iface} configuration and stays."
        )
        for duplicate in self.correct_lines(iface)[1:]:
            disable[duplicate.index] = (
                f"duplicate of line {keep.index + 1}",
                "duplicate of an earlier line",
            )
        # A correct line hidden inside a board-specific [section] may never be applied.
        section = self.enclosing_section(keep.index)
        if section and section not in ("all", "pi5"):
            report.append(
                f"note: line {keep.index + 1} sits inside the [{section}] section; "
                "check that this section applies to the Raspberry Pi 5."
            )

    def retire_panel_lines(
        self, iface: str, keep: DtoverlayLine | None, disable: dict[int, tuple[str, str]]
    ) -> list[str]:
        """Retire the panel lines we are not keeping and return their extra parameters.

        A retired line is either a wrong-connector line or a duplicate flagged by
        describe_keep; either way its tuning parameters (rotation=90, invx, ...) are
        carried over, so fixing the connector never drops the tuning result.
        """
        carried: list[str] = []
        for entry in self.panel_lines():
            if keep is not None and entry.index == keep.index:
                continue
            if entry.index not in disable:
                found = "/".join(sorted(entry.interfaces))
                disable[entry.index] = (
                    f"connector is {found}, which is not the requested {iface}",
                    f"connector is {found}, expected {iface}",
                )
            carried += [p for p in entry.extra_params if p not in carried]
        return carried

    def retire_conflicts(self, disable: dict[int, tuple[str, str]]) -> None:
        for entry in self.active():
            if entry.is_conflict:
                disable[entry.index] = (
                    f"{entry.name} competes with this overlay for I2C 0x45 and must go",
                    f"{entry.name} claims the same I2C 0x45 node",
                )

    def render(self, disable: dict[int, tuple[str, str]], rewrite: dict[int, str]) -> list[str]:
        """Apply the per-line decisions, keeping every untouched line byte for byte."""
        new_lines = []
        for i, line in enumerate(self.lines):
            stripped = line.rstrip("\r\n")
            ending = line[len(stripped) :] or self.ending
            if i in disable:
                tag = disable[i][1]
                new_lines.append(f"# {stripped}  # <- disabled by install.py: {tag}{ending}")
            elif i in rewrite:
                new_lines.append(rewrite[i] + ending)
            else:
                new_lines.append(line)
        return new_lines

    def added_lines(
        self,
        iface: str,
        carried: list[str],
        need_panel_line: bool,
        need_kms_line: bool,
        report: list[str],
        tunables: dict[str, str | None] | None = None,
    ) -> list[str]:
        """The block appended at the bottom of the file, inside a [all] section."""
        body = []
        if need_kms_line:
            body.append(f"dtoverlay={KMS_OVERLAY}")
        if need_panel_line:
            body.append(panel_overlay_line(iface, carried, tunables))
        if not body:
            return []

        # Comments here stay ASCII-only: the firmware parses config.txt, so no
        # multibyte text belongs in that file.
        header = [
            "",
            f"# ---- Freenove 7inch-C DSI panel, added by install.py on {RUN_TIME} ----",
            "# The [all] header keeps these lines out of a board-specific section",
            "# that a previous section in this file might have opened.",
        ]
        report.append("appended at the end of the file: " + "  ".join(body))
        if need_panel_line and carried:
            report.append(f"carried over the existing parameters: {', '.join(carried)}")
        return [text + self.ending for text in header + body]

    # --- writing -------------------------------------------------------------
    def save(self, plan: ConfigPlan) -> None:
        """Back the file up, then replace it through sudo (the boot partition is root-owned)."""
        backup = self.path.with_name(f"{self.path.name}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
        status = Console.command(SUDO + ["cp", "-a", str(self.path), str(backup)])
        if status != 0:
            print("Error: backing up config.txt failed (sudo rights or a disk problem).")
            raise SystemExit(status)
        print(f"the original file is kept as: {backup}")

        # Write a temporary file first and copy it afterwards: the copy sees complete
        # content and a failure cannot damage the original file.
        # newline="" keeps the line endings the plan chose.
        handle, tmp_name = tempfile.mkstemp(suffix=".config.txt")
        try:
            with os.fdopen(
                handle, "w", encoding="utf-8", errors="surrogateescape", newline=""
            ) as tmp:
                tmp.write("".join(plan.lines))
            os.chmod(tmp_name, 0o644)
            status = Console.command(SUDO + ["cp", tmp_name, str(self.path)])
        finally:
            os.unlink(tmp_name)
        if status != 0:
            print(f"Error: writing {self.path} failed; restore it from {backup}.")
            raise SystemExit(status)

    def verify(self, iface: str) -> bool:
        """Re-read the file from disk and report what the firmware will act on."""
        fresh = ConfigFile(self.path)
        print("\nRelevant configuration now in effect:")
        for entry in fresh.active():
            if entry.is_panel or entry.is_kms:
                print(f"    {entry.text()}")

        problems = fresh.problems(iface)
        if problems:
            print(
                "Warning: the check did not pass: "
                + "; ".join(problems)
                + " -- review the lines above."
            )
            return False
        print(f"Check passed: {iface} is configured and the KMS stack is enabled.")
        return True

    def removal_plan(self) -> ConfigPlan:
        """Plan that deletes every line this installer put into config.txt.

        Two kinds of lines go: the panel overlay lines (this driver's config, live
        or already commented out) and the banner / disable markers this installer
        wrote (they always carry the "install.py" token or the fixed header text).
        Anything else, including a plain dtoverlay=vc4-kms-v3d the user had before,
        stays put -- it is the base display stack, not ours to remove.
        """
        own_markers = (
            "added by install.py",
            "disabled by install.py",
            "[all] header keeps these lines out of a board-specific section",
            "that a previous section in this file might have opened.",
        )
        panel_index = {entry.index for entry in self.entries if entry.is_panel}
        kept: list[str] = []
        report: list[str] = []
        for index, line in enumerate(self.lines):
            if index in panel_index or any(marker in line for marker in own_markers):
                report.append(f"remove line {index + 1}: {line.strip()}")
                continue
            kept.append(line)
        return ConfigPlan(lines=kept, report=report, changed=bool(report))


# --- brightness launcher ------------------------------------------------------
class MenuEntry:
    """A background launcher for brightness.py in the applications menu.
    Follows the create_desktop_shortcut.create_application_to_programming pattern,
    wrapper and all: brightness.py and brightness.png are copied into
    ~/.local/share/applications/ together with a small shell wrapper
    (freenove-brightness.sh), and the Type=Application .desktop file runs that
    wrapper -- which execs the copied brightness.py -- with Terminal=false, so a
    click starts the slider in the background and keeps working even if the project
    folder later moves. The installer normally runs under sudo, but the entry
    belongs to the person at the desk, so the home directory and the file ownership
    are taken from SUDO_USER rather than from the root account doing the writing (a
    launcher dropped in /root never shows up in the pi menu).
    """

    def __init__(self, drivers_dir: Path, logo_dir: Path):
        self.script = drivers_dir / BRIGHTNESS_SCRIPT
        self.icon = logo_dir / BRIGHTNESS_ICON

    @staticmethod
    def desktop_user() -> tuple[Path, int, int] | None:
        """(home, uid, gid) of the account that actually owns the desktop."""
        import pwd

        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            try:
                info = pwd.getpwnam(sudo_user)
            except KeyError:
                return None
            return Path(info.pw_dir), info.pw_uid, info.pw_gid
        info = pwd.getpwuid(os.getuid())
        return Path(info.pw_dir), info.pw_uid, info.pw_gid

    def app_dir(self, home: Path) -> Path:
        return home / ".local" / "share" / "applications"

    def path(self, home: Path) -> Path:
        return self.app_dir(home) / f"{MENU_DESKTOP_NAME}.desktop"

    def launcher_sh(self) -> str:
        """The shell wrapper brightness.py is launched through.

        This is the run_app.sh indirection from create_desktop_shortcut: rather
        than baking an interpreter path into the .desktop Exec, the entry runs this
        script, which resolves its own directory and execs the brightness.py copied
        beside it. It is the single place to adjust how the slider starts.
        """
        return (
            "#!/bin/sh\n"
            "# Menu launcher for the Freenove panel brightness slider.\n"
            "# Resolves its own folder so the brightness.py copied beside it is\n"
            "# found no matter where the desktop entry is clicked from, then execs\n"
            "# the slider in place (a background GUI, so the click returns).\n"
            'DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'exec python3 "$DIR/brightness.py"\n'
        )

    def content(self, dest_dir: Path) -> str:
        # Exec and Icon point at the copies that live beside the .desktop file in
        # the applications folder, so the launcher is self-contained and does not
        # depend on the project directory staying where it was. Exec goes through
        # the shell wrapper (create_desktop_shortcut style), not python directly.
        return (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            f"Name={MENU_LABEL}\n"
            f"Comment={MENU_COMMENT}\n"
            f"Exec=bash {dest_dir / BRIGHTNESS_LAUNCHER_SH}\n"
            f"Icon={dest_dir / BRIGHTNESS_ICON}\n"
            # background launch, no console window; brightness.py still writes the
            # backlight through 'sudo tee', so that needs a cached or passwordless
            # sudo for the click to take effect without a terminal.
            "Terminal=false\n"
            "Categories=Utility;\n"
        )

    def create(self) -> None:
        who = self.desktop_user()
        if who is None:
            print("Warning: could not resolve the desktop user; the launcher was not created.")
            return
        home, uid, gid = who
        dest_dir = self.app_dir(home)
        dest = self.path(home)

        if not self.script.is_file():
            print(f"Warning: {self.script} is missing; the launcher was not created.")
            return
        if not self.icon.is_file():
            print(f"Warning: {self.icon} is missing; the launcher falls back to the default icon.")

        launcher = dest_dir / BRIGHTNESS_LAUNCHER_SH
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.script, dest_dir / BRIGHTNESS_SCRIPT)
            if self.icon.is_file():
                shutil.copy2(self.icon, dest_dir / BRIGHTNESS_ICON)
            launcher.write_text(self.launcher_sh(), encoding="utf-8")
            dest.write_text(self.content(dest_dir), encoding="utf-8")
            # The wrapper is the one thing the menu runs, so it is the only +x; the
            # .desktop, the copied script and its icon are plain data.
            written = [
                (launcher, 0o755),
                (dest, 0o644),
                (dest_dir / BRIGHTNESS_SCRIPT, 0o644),
                (dest_dir / BRIGHTNESS_ICON, 0o644),
            ]
            # Root wrote these under sudo; hand them back to the desktop user so the
            # entry and its copies stay editable and removable by that account.
            for path, mode in written:
                if path.is_file():
                    path.chmod(mode)
                    os.chown(path, uid, gid)
            os.chown(dest_dir, uid, gid)
            print(f"Launcher created in {dest_dir} (the menu runs {BRIGHTNESS_LAUNCHER_SH}).")
        except OSError as exc:
            print(f"Warning: creating the launcher failed: {exc}")

    def remove(self, quiet: bool = False) -> None:
        who = self.desktop_user()
        if who is None:
            print("Warning: could not resolve the desktop user; nothing was removed.")
            return
        home, _uid, _gid = who
        dest_dir = self.app_dir(home)
        # The .desktop, its shell wrapper and the files it copied in there go together.
        files = [
            self.path(home),
            dest_dir / BRIGHTNESS_LAUNCHER_SH,
            dest_dir / BRIGHTNESS_SCRIPT,
            dest_dir / BRIGHTNESS_ICON,
        ]

        removed = False
        for item in files:
            if item.is_file():
                try:
                    item.unlink()
                    removed = True
                except OSError as exc:
                    print(f"Warning: removing {item.name} failed: {exc}")
        if removed:
            print(f"Launcher and its copies removed from {dest_dir}")
        elif not quiet:
            print(f"No launcher to remove in {dest_dir}")


# --- touch range guard -------------------------------------------------------
class TouchGuard:
    """Deploy and enable the GT911 cold-boot range guard as a boot oneshot.

    This is a fallback, not the primary fix: once the kernel/overlay raises
    IOVCC before the touch probes, the guard reads the correct 800x480 range at
    boot and does nothing. It only re-binds the touch driver when a cold boot
    left the GT911 on its 4096 power-on default (coordinates fine, mapping
    wrong). Two files go in, both root-owned:
      drivers/touch-guard.py       -> /usr/local/bin/freenove-touch-guard.py
      drivers/touch-guard.service  -> /etc/systemd/system/freenove-touch-guard.service
    Writing those paths and enabling the unit need root, so every privileged
    step runs through the SUDO wrapper install.py already uses. Nothing here is
    fatal to the rest of the install: a missing source or a failed enable only
    skips / warns about the guard.
    """

    def __init__(self, drivers_dir: Path):
        self.script_src = drivers_dir / GUARD_SCRIPT
        self.unit_src = drivers_dir / GUARD_UNIT

    def available(self) -> bool:
        return self.script_src.is_file() and self.unit_src.is_file()

    def install(self) -> bool:
        """Copy the script + unit and enable the service; return success."""
        if not self.available():
            print(
                f"Warning: {GUARD_SCRIPT} / {GUARD_UNIT} not found under drivers/; "
                "the touch range guard was not installed."
            )
            return False
        for src, dest in (
            (self.script_src, GUARD_SCRIPT_DEST),
            (self.unit_src, GUARD_UNIT_DEST),
        ):
            # install(1) sets content, mode and ownership in one go; the .py is
            # run through python3 by the unit, so a plain 0644 is enough.
            status = Console.command(SUDO + ["install", "-m", "0644", str(src), str(dest)])
            if status != 0:
                print(f"Warning: installing {dest} failed (exit {status}); the guard is incomplete.")
                return False
        # daemon-reload so systemd sees the new unit, then enable so it runs at
        # every boot. A failed enable is reported but does not abort the run.
        Console.command(SUDO + ["systemctl", "daemon-reload"])
        status = Console.command(SUDO + ["systemctl", "enable", GUARD_UNIT_NAME])
        if status != 0:
            print("Warning: the guard is deployed but enabling it failed; it will not run at boot.")
            return False
        print(f"Touch range guard deployed and enabled: {GUARD_UNIT_NAME}")
        return True

    def remove(self, quiet: bool = False) -> None:
        """Disable the service, then delete both files it installed.

        disable comes before rm: a unit deleted while still enabled leaves a
        dangling symlink in systemd's wants directory. Everything is best
        effort, so an --uninstall on a system that never got the guard stays
        quiet.
        """
        Console.command(SUDO + ["systemctl", "disable", "--now", GUARD_UNIT_NAME])
        removed = False
        for dest in (GUARD_UNIT_DEST, GUARD_SCRIPT_DEST):
            if dest.exists() and Console.command(SUDO + ["rm", "-f", str(dest)]) == 0:
                removed = True
        Console.command(SUDO + ["systemctl", "daemon-reload"])
        if removed:
            print(f"Touch range guard removed: {GUARD_UNIT_NAME} and its script")
        elif not quiet:
            print("No touch range guard installation found to remove.")


# --- PyQt5 dependency for the brightness slider ------------------------------
class PyQtDependency:
    """Ensure drivers/brightness.py can import PyQt5, installing it when not.

    The slider needs PyQt5; Raspberry Pi OS ships it as the apt package
    python3-pyqt5. This is a prerequisite of the desktop launcher rather than of
    the driver, so callers treat a failure as a warning, never a reason to abort
    -- the panel and touch work fine without the slider.

    The check imports the exact modules brightness.py uses under the same
    `python3` the launcher runs it with, so a pass means the window really
    starts: QtWidgets pulls in the Qt libraries, and a missing shared object
    fails here even when a bare PyQt5 folder exists. It runs in a subprocess so
    install.py itself stays standard-library only.
    """

    IMPORT_TEST = "import PyQt5.QtCore, PyQt5.QtWidgets"
    PACKAGE = "python3-pyqt5"
    INTERPRETER = "python3"

    @classmethod
    def available(cls) -> bool | None:
        """True/False whether the launcher interpreter imports PyQt5, None if absent."""
        if shutil.which(cls.INTERPRETER) is None:
            return None
        result = subprocess.run(
            [cls.INTERPRETER, "-c", cls.IMPORT_TEST], capture_output=True, text=True
        )
        return result.returncode == 0

    @staticmethod
    def apt() -> str | None:
        """apt-get if present, else apt, else None."""
        for name in ("apt-get", "apt"):
            path = shutil.which(name)
            if path:
                return path
        return None

    @classmethod
    def ensure(cls, install: bool = True) -> bool:
        """Check, and install the package when it is missing. Return success."""
        state = cls.available()
        if state is None:
            print(f"Warning: '{cls.INTERPRETER}' was not found; cannot verify PyQt5.")
            return False
        if state:
            print(f"PyQt5 is already available to {cls.INTERPRETER}.")
            return True

        print(f"PyQt5 is missing for {cls.INTERPRETER}; the brightness slider needs it.")
        if not install:
            return False

        apt = cls.apt()
        if apt is None:
            print(f"Warning: no apt found; install {cls.PACKAGE} manually for the slider.")
            return False

        print(f"Installing {cls.PACKAGE} (this uses sudo).")
        # An index refresh that fails is worth warning about but not fatal: a
        # warm cache can still install the package, so the attempt proceeds.
        if Console.command(SUDO + [apt, "update"]) != 0:
            print("Warning: 'apt update' failed; trying the install with the cache as-is.")
        if Console.command(SUDO + [apt, "install", "-y", cls.PACKAGE]) != 0:
            print(f"Warning: installing {cls.PACKAGE} failed.")
            return False
        # Re-check in a fresh interpreter so the just-installed package shows up.
        if cls.available():
            print(f"Installed {cls.PACKAGE}; PyQt5 is now available.")
            return True
        print(f"Warning: {cls.PACKAGE} was installed but still is not importable.")
        return False


# --- installer ----------------------------------------------------------------
class Installer:
    """Command line plus the steps, in the order the readme asks for."""

    def __init__(self, args: argparse.Namespace, project_dir: Path):
        self.args = args
        self.project_dir = project_dir
        self.drivers_dir = project_dir / DRIVERS_DIR
        self.logo_dir = project_dir / LOGO_DIR
        # Overlay parameters collected by step_tune from the individual flags;
        # empty means the config step writes no extra tunables.
        self.tunables: dict[str, str | None] = {}

    # --- step 1 --------------------------------------------------------------
    def step_board(self) -> None:
        Console.banner("[1/3] Checking the board model")
        board = Board.detect()
        is_pi5 = board.is_pi5

        if is_pi5 is None:
            print(
                "The board model cannot be confirmed from the system "
                f"(read: {board.description or 'nothing'})."
            )
            if not Console.confirm("Please confirm: is this device a Raspberry Pi 5?", False):
                self.exit_unsupported(board.description)
            return

        if not is_pi5:
            self.exit_unsupported(board.description)
        print(f"Detected hardware: {board.description}, which this panel supports.")

    @staticmethod
    def exit_unsupported(description: str) -> None:
        print("\nThis hardware does not support the panel; please use a Raspberry Pi 5.")
        if description:
            print(f"(detected device: {description})")
        raise SystemExit(EXIT_BOARD_UNSUPPORTED)

    # --- step 1b: panel overlay tunables (individual flags) -------------------
    def step_tune(self) -> None:
        """Collect the overlay parameters set directly by the individual flags.

        Only --rotation / --swapxy / --invx / --invy / --sizex / --sizey reach
        this step; they are kept on the instance and written onto the panel
        dtoverlay line later, in step_config. Nothing is written here: the .dts
        keeps its defaults, only config.txt carries the override, so no rebuild
        is forced. (--screen / --touch are read-only queries handled before the
        install flow starts and never add tunables.)
        """
        a = self.args
        if not (
            a.rotation is not None
            or a.swapxy
            or a.invx
            or a.invy
            or a.sizex is not None
            or a.sizey is not None
        ):
            return
        Console.banner("Setting panel overlay parameters (screen / touch)")

        if a.rotation is not None:
            self.tunables["rotation"] = a.rotation
        if a.swapxy:
            self.tunables["swapxy"] = None
        if a.invx:
            self.tunables["invx"] = None
        if a.invy:
            self.tunables["invy"] = None
        if a.sizex is not None:
            self.tunables["sizex"] = str(a.sizex)
        if a.sizey is not None:
            self.tunables["sizey"] = str(a.sizey)

        summary = ", ".join(tunable_token(k, v) for k, v in self.tunables.items())
        print(f"Overlay parameters (written into config.txt): {summary}")

    # --- read-only query (--screen / --touch) --------------------------------
    def query_requested(self) -> bool:
        return bool(self.args.screen or self.args.touch)

    def run_query(self) -> None:
        """Print the current screen / touch overlay parameters from config.txt.

        Purely read-only: no board check, no build, no writes. Any parameter the
        panel dtoverlay line does not set is reported with its .dts default and
        marked as such.
        """
        tunables = ConfigFile(ConfigFile.locate()).panel_tunables()
        if self.args.screen:
            self.report_screen(tunables)
        if self.args.touch:
            self.report_touch(tunables)

    def report_screen(self, tunables: dict[str, str | None]) -> None:
        Console.banner("Current display configuration (read from config.txt)")
        self.print_value("rotation", tunables, DEFAULT_ROTATION)

    def report_touch(self, tunables: dict[str, str | None]) -> None:
        Console.banner("Current touch configuration (read from config.txt)")
        for flag in TOUCH_FLAG_KEYS:
            on = flag in tunables
            print(f"  {flag} = {'on' if on else 'off'}{self.origin(on)}")
        self.print_value("sizex", tunables, DEFAULT_SIZEX)
        self.print_value("sizey", tunables, DEFAULT_SIZEY)

    def print_value(self, key: str, tunables: dict[str, str | None], default: str) -> None:
        value = tunables.get(key)
        from_file = value is not None
        print(f"  {key} = {value if from_file else default}{self.origin(from_file)}")

    @staticmethod
    def origin(from_file: bool) -> str:
        return "  (config.txt)" if from_file else "  (.dts default, not set)"

    # --- step 2 --------------------------------------------------------------
    def step_install(self) -> None:
        Console.banner("[2/3] Building and installing the driver (build.sh)")
        driver = DriverBuild(self.drivers_dir)
        if not driver.script.is_file():
            print(f"Error: the install script {driver.script} was not found")
            raise SystemExit(EXIT_ERROR)

        # A current build/ only costs the deployment half, never the whole run: a
        # folder copied to another card still gets its modules into extra/.
        options: tuple[str, ...] = ()
        full_build = True
        if self.args.reinstall:
            print("--reinstall: forcing the full compile and deploy run.")
        else:
            problems = driver.problems()
            if problems:
                Console.items("A full build is needed:", problems)
            else:
                full_build = False
                print(
                    f"{driver.build_dir} already holds the two .ko, the .dtbo and the "
                    "kbuild metadata, all matching the running kernel."
                )
                print(
                    f"Compile step skipped: build.sh runs with {INSTALL_ONLY_FLAG}, "
                    "which still deploys the modules and the overlay, removes the legacy "
                    "module and refreshes depmod."
                )
                options = (INSTALL_ONLY_FLAG,)

        job = "deploy only, no compile" if options else "compile and deploy"
        # When nothing usable has been compiled yet (no build/, or an incomplete,
        # stale or --reinstall-forced one), running the installer already means the
        # user wants the driver built, so go straight to it without prompting. Only
        # the already-built deploy-only path asks, because that is the one run a
        # person may want to skip.
        if full_build:
            print(f"build.sh will run ({job}) and needs sudo; starting the compile.")
        elif not Console.confirm(
            f"build.sh will run ({job}) and needs sudo. Continue?",
            True,
        ):
            print("Installation cancelled.")
            raise SystemExit(EXIT_USER_ABORT)

        driver.install(options)
        print("\nDriver modules and overlay installed.")

    # --- step 3 --------------------------------------------------------------
    def step_config(self) -> None:
        Console.banner("[3/3] Configuring /boot/firmware/config.txt")
        config = ConfigFile(ConfigFile.locate())
        print(f"Configuration file: {config.path}")
        iface = self.choose_interface()
        print(f"Target connector: {iface}")
        if self.tunables:
            summary = ", ".join(tunable_token(k, v) for k, v in self.tunables.items())
            print(f"Overlay parameters: {summary}")

        plan = config.plan(iface, self.tunables)
        print()
        for item in plan.report:
            print(f"  - {item}")

        if not plan.changed:
            print("\nconfig.txt needs no change.")
            config.verify(iface)
            return

        if not Console.confirm(
            "\nApply the changes above to config.txt?", True
        ):
            print("The config.txt edit was cancelled.")
            raise SystemExit(EXIT_USER_ABORT)

        config.save(plan)
        config.verify(iface)

    def choose_interface(self) -> str:
        print("\nWhich connector on the Raspberry Pi 5 is the panel cable in?")
        print("  0 -> dsi0")
        print("  1 -> dsi1 (default)")
        while True:
            answer = Console.ask("Enter 0 or 1 (empty for dsi1): ").lower()
            if not answer or answer in ("1", "dsi1"):
                return "dsi1"
            if answer in ("0", "dsi0"):
                return "dsi0"
            print("Unrecognised answer, try again.")

    # --- brightness launcher -------------------------------------------------
    def step_menu(self) -> None:
        """Drop (or refresh) the brightness.py launcher into the applications menu.

        remove-then-create keeps it idempotent, so a re-run never stacks up stale
        copies, and it doubles as the refresh path when brightness.py changed.
        """
        Console.banner("Adding the brightness launcher (brightness.py)")
        # The slider is a PyQt5 app, so make sure the desktop interpreter can
        # import it before dropping the launcher in. Non-fatal: the panel works
        # without the slider, so a failure only warns and the install continues.
        if not PyQtDependency.ensure():
            print(
                "The launcher is created regardless; the slider will not start "
                "until python3-pyqt5 is installed."
            )
        entry = MenuEntry(self.drivers_dir, self.logo_dir)
        entry.remove(quiet=True)
        entry.create()

    # --- touch range guard ---------------------------------------------------
    def step_guard(self) -> None:
        """Deploy the boot-time GT911 range guard (a cold-boot fallback)."""
        Console.banner("Installing the GT911 touch range guard (cold-boot fallback)")
        guard = TouchGuard(self.drivers_dir)
        if not guard.available():
            print("Guard sources are not under drivers/; skipping this step.")
            return
        guard.install()

    # --- cleanup operations --------------------------------------------------
    # Each one deletes what install.py put in place and nothing else, so a single
    # flag takes just that folder or file back out; --uninstall runs them all.
    def cleanup_requested(self) -> bool:
        return any(
            (
                self.args.uninstall,
                self.args.clean,
                self.args.remove_overlay,
                self.args.remove_config,
                self.args.remove_menu,
                self.args.remove_guard,
            )
        )

    def run_cleanup(self) -> None:
        Console.banner("Removing what install.py placed")
        everything = self.args.uninstall
        if everything or self.args.clean:
            self.clean_workspace()
        if everything or self.args.remove_overlay:
            self.remove_overlay()
        if everything or self.args.remove_config:
            self.remove_config()
        if everything or self.args.remove_menu:
            self.remove_menu()
        if everything or self.args.remove_guard:
            self.remove_guard()
        # Only an overlay / config.txt removal needs a reboot to take effect; a
        # workspace clean or a menu removal is already complete on disk.
        if everything or self.args.remove_overlay or self.args.remove_config:
            print("\nCleanup done. Reboot (sudo reboot) for config.txt or overlay changes to take effect.")
        else:
            print("\nCleanup done.")

    def clean_workspace(self) -> None:
        """Delete the drivers/build/ tree and the root __pycache__/ a run leaves."""
        for target in (self.drivers_dir / BUILD_DIR, self.project_dir / "__pycache__"):
            if not target.is_dir():
                print(f"{target} does not exist; nothing to clean.")
                continue
            try:
                shutil.rmtree(target)
                print(f"Removed {target}")
            except OSError as exc:
                print(
                    f"Warning: could not remove {target} ({exc}); "
                    "if it is root-owned, re-run with sudo."
                )

    def remove_overlay(self) -> None:
        candidates = [
            Path(base) / "overlays" / OVERLAY_DTBO for base in ("/boot/firmware", "/boot")
        ]
        found = [path for path in candidates if path.is_file()]
        if not found:
            print(f"No {OVERLAY_DTBO} under /boot/firmware/overlays or /boot/overlays.")
            return
        for path in found:
            status = Console.command(SUDO + ["rm", "-f", str(path)])
            print(f"Removed overlay: {path}" if status == 0 else f"Warning: {path} not removed.")

    def remove_config(self) -> None:
        config = ConfigFile(ConfigFile.locate())
        plan = config.removal_plan()
        if not plan.changed:
            print(f"No Freenove lines to remove in {config.path}.")
            return
        print(f"config.txt ({config.path}):")
        for item in plan.report:
            print(f"  - {item}")
        config.save(plan)

    def remove_menu(self) -> None:
        MenuEntry(self.drivers_dir, self.logo_dir).remove()

    def remove_guard(self) -> None:
        TouchGuard(self.drivers_dir).remove()

    # --- wrap-up -------------------------------------------------------------
    def step_summary(self) -> None:
        Console.banner("Driver installed and configured")
        print("The overlay takes effect after a reboot: sudo reboot")
        print("After the reboot, check the result with:")
        print("  dmesg | grep -iE 'dsi|panel|freenove|goodix'")
        print("  lsmod | grep -E 'panel|regulator'")
        print("  cat /sys/class/drm/card*-*/status")
        print("  ls /sys/class/backlight/")

        # Never reboot on our own; only ask a human sitting at a terminal.
        if not sys.stdin.isatty():
            return
        if Console.confirm("Reboot now?", True):
            Console.command(SUDO + ["reboot"])

    # --- main flow -----------------------------------------------------------
    def run(self) -> int:
        if os.name != "posix":
            print("This script must run on the Linux system of a Raspberry Pi.")
            return EXIT_ERROR
        # Standalone cleanup: take back out what install.py placed, then stop.
        # Nothing is built or reconfigured, so no board check and no sudo are
        # needed beyond the one command each removal uses.
        if self.cleanup_requested():
            self.run_cleanup()
            return EXIT_OK
        # Standalone read-only query: print the current screen / touch overlay
        # parameters from config.txt and stop, without a board check or a build.
        if self.query_requested():
            self.run_query()
            return EXIT_OK
        # Standalone dependency action: check PyQt5 for the slider, install it if
        # missing, then stop. It uses apt (and so sudo) but needs no board check.
        if self.args.ensure_pyqt:
            return EXIT_OK if PyQtDependency.ensure() else EXIT_ERROR
        self.step_board()
        self.step_tune()
        self.step_install()
        self.step_config()
        self.step_menu()
        self.step_guard()
        self.step_summary()
        return EXIT_OK


# --- entry point --------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-command installer for the Freenove 7inch-C DSI panel "
            "on a Raspberry Pi 5"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help="ignore the already-installed check and re-run build.sh",
    )
    parser.add_argument(
        "--screen",
        action="store_true",
        help="query the current display rotation from config.txt and exit (read-only)",
    )
    parser.add_argument(
        "--touch",
        action="store_true",
        help="query the current touch config (swapxy/invx/invy + coordinate range) from config.txt and exit (read-only)",
    )
    parser.add_argument(
        "--rotation",
        choices=ROTATION_CHOICES,
        metavar="DEG",
        help="set the display rotation directly (0/90/180/270)",
    )
    parser.add_argument(
        "--swapxy", action="store_true", help="set the touch swap-xy flag directly"
    )
    parser.add_argument(
        "--invx", action="store_true", help="set the touch invert-x flag directly"
    )
    parser.add_argument(
        "--invy", action="store_true", help="set the touch invert-y flag directly"
    )
    parser.add_argument(
        "--sizex", type=int, metavar="N", help="set the touch X coordinate range directly"
    )
    parser.add_argument(
        "--sizey", type=int, metavar="N", help="set the touch Y coordinate range directly"
    )
    parser.add_argument(
        "--remove-menu",
        action="store_true",
        help="remove the brightness.py launcher (and its copies) and exit",
    )
    parser.add_argument(
        "--remove-guard",
        action="store_true",
        help="disable and delete the GT911 touch range guard (service + script) and exit",
    )
    parser.add_argument(
        "--ensure-pyqt",
        action="store_true",
        help="check PyQt5 for the brightness slider, install python3-pyqt5 if missing, then exit",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="delete the drivers/build/ and __pycache__ directories and exit",
    )
    parser.add_argument(
        "--remove-overlay",
        action="store_true",
        help="delete the .dtbo this installer put in /boot/firmware/overlays and exit",
    )
    parser.add_argument(
        "--remove-config",
        action="store_true",
        help="delete the Freenove dtoverlay lines from config.txt and exit",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="run every cleanup step: drivers/build/ and __pycache__, the overlay .dtbo, config.txt lines and the launcher",
    )
    return parser


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    return Installer(build_parser().parse_args(), project_dir).run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(EXIT_USER_ABORT)
