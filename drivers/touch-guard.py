#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fallback guard for the Freenove panel's GT911 touch range.

Why this exists
---------------
The GT911 shares the panel's IOVCC rail (MCU bit 4). On a cold power-on the
touch driver can probe for the chip before that rail is up and
stable, so reading the chip's config returns its power-on defaults: a 4096
input range instead of this panel's 800x480. Coordinates still come through,
but they are mapped into the wrong (compressed) region. A warm reboot does
not show it because the board never lost power, so the chip was already
settled when goodix re-probed.

The proper fix lives in the kernel/overlay: the MCU driver raises IOVCC at
probe and exposes it as a regulator the touch node references, which makes
the kernel defer the goodix probe until the supply is real. That fix covers
newer goodix builds that request the supply by name.

This script is the safety net for the cases it does not cover. Once, after
boot, it:
  1. finds the goodix I2C device and reads its ABS_X / ABS_Y range,
  2. if the range still looks like the 4096 default (i.e. the config read
     failed), re-binds the goodix driver (unbind, wait, bind),
  3. re-checks, and repeats a couple of times.
Because the display stack has already powered IOVCC by the time this runs,
the chip is awake and settled on the re-probe, so goodix reads the real
800x480 config. If the first range is already correct, this is a no-op.

Run by hand to just look:
    sudo python3 touch-guard.py --probe
Run by hand to fix:
    sudo python3 touch-guard.py
Installed as a systemd oneshot (see touch-guard.service) it runs once per
boot after the display is up. Needs root: writing the driver's bind / unbind
files is a privileged operation.

Exit codes: 0 (range ok, fixed, or nothing to guard), 2 (range still wrong
after the attempts), 3 (no bound touch device at the given I2C address).
"""

from __future__ import annotations

import argparse
import fcntl
import os
import struct
import sys
import time
from pathlib import Path

# --- where the pieces live in sysfs ------------------------------------------
INPUT_CLASS = Path("/sys/class/input")
I2C_DEVICES = Path("/sys/bus/i2c/devices")

# The GT911 sits at I2C address 0x5d. The touch is located by that address
# rather than by driver name because the name differs per kernel: the vendor /
# Raspberry Pi build registers the driver as "Goodix-TS" (module goodix_ts)
# while the mainline goodix.c uses "goodix". Keying on the address, and on the
# driver the device is actually bound to, works for both.
DEFAULT_TOUCH_ADDR = 0x5D

# --- the number that tells us the config read failed -------------------------
# GT911 comes out of POR reporting a 4096-wide range, so both axes max at
# 4095. This panel is 800x480, so a healthy read gives maxima near 799/479.
# Anything at or above this threshold can only be the default, never the real
# panel, so it is the trigger to re-bind. (A genuine 4096-wide digitiser is not
# what this overlay drives, so there is no false positive to worry about here.)
DEFAULT_RANGE_MAX = 4095
SUSPECT_MIN = 4000

# ABS codes and the EVIOCGABS ioctl, built the way the kernel encodes it so the
# magic number is not hand-typed: _IOC(_IOC_READ, 'E', 0x40 + axis, size).
ABS_X = 0x00
ABS_Y = 0x01
_IOC_READ = 2
_IOC_NRSHIFT, _IOC_NRBITS = 0, 8
_IOC_TYPESHIFT, _IOC_TYPEBITS = 8, 8
_IOC_SIZESHIFT, _IOC_SIZEBITS = 16, 14
_IOC_DIRSHIFT = 30
_ABSINFO_SIZE = 24  # struct input_absinfo is six __s32 fields


def _eviocgabs(axis: int) -> int:
    return (
        (_IOC_READ << _IOC_DIRSHIFT)
        | (ord("E") << _IOC_TYPESHIFT)
        | ((0x40 + axis) << _IOC_NRSHIFT)
        | (_ABSINFO_SIZE << _IOC_SIZESHIFT)
    )


def log(msg: str) -> None:
    print(f"[touch-guard] {msg}", flush=True)


# --- locating the touch device -----------------------------------------------
def bound_devices(addr: int) -> list[str]:
    """I2C client names (e.g. '11-005d') for a device at 'addr' that is bound.

    Matched by bus address, so it works whatever the driver is called; only
    devices that already have a driver attached are returned, because a device
    with no driver has nothing to re-bind.
    """
    out = []
    for entry in I2C_DEVICES.glob(f"*-{addr:04x}"):
        if (entry / "driver").exists():
            out.append(entry.name)
    return sorted(out)


def driver_dir(i2c_name: str) -> Path | None:
    """The sysfs driver directory the touch client is bound to, by realpath."""
    link = I2C_DEVICES / i2c_name / "driver"
    if not link.exists():
        return None
    return Path(os.path.realpath(str(link)))


def event_device_for(i2c_name: str) -> Path | None:
    """The /dev/input/eventN path whose sysfs device sits under this I2C client.

    Matching on the sysfs path (rather than the input device's name) is the
    robust link: goodix names the input device differently across kernel
    versions, but its device tree always nests under .../<i2c_name>/input/...
    """
    for ev in sorted(INPUT_CLASS.glob("event*")):
        real = os.path.realpath(str(ev / "device"))
        if f"/{i2c_name}/" in real or real.endswith(f"/{i2c_name}"):
            return Path("/dev/input") / ev.name
    return None


def read_axis_max(event_path: Path, axis: int) -> int | None:
    """Maximum reported for one ABS axis, or None if it cannot be read."""
    try:
        with open(event_path, "rb") as handle:
            raw = fcntl.ioctl(handle, _eviocgabs(axis), b"\0" * _ABSINFO_SIZE)
        # struct input_absinfo { value; minimum; maximum; fuzz; flat; resolution }
        _value, _minimum, maximum = struct.unpack("@iii", raw[:12])
        return maximum
    except (OSError, struct.error):
        return None


def current_range(event_path: Path) -> tuple[int, int] | None:
    x = read_axis_max(event_path, ABS_X)
    y = read_axis_max(event_path, ABS_Y)
    if x is None or y is None:
        return None
    return x, y


# --- deciding and acting -----------------------------------------------------
def looks_default(rng: tuple[int, int]) -> bool:
    x, y = rng
    return x >= SUSPECT_MIN or y >= SUSPECT_MIN


def rebind(i2c_name: str, settle: float) -> None:
    """unbind -> bind so the touch driver probes again against a powered chip.

    Uses the driver the device is actually bound to, so it is independent of
    whether that driver is called 'goodix' or 'Goodix-TS'.
    """
    drv = driver_dir(i2c_name)
    if drv is None:
        log(f"{i2c_name}: no bound driver, cannot re-bind")
        return
    unbind = drv / "unbind"
    bind = drv / "bind"
    try:
        unbind.write_text(i2c_name)
    except OSError as exc:
        log(f"unbind {i2c_name} failed: {exc}")
        return
    time.sleep(settle)
    try:
        bind.write_text(i2c_name)
    except OSError as exc:
        log(f"bind {i2c_name} failed: {exc}")
        return
    time.sleep(settle)


def guard_once(i2c_name: str, attempts: int, settle: float) -> bool:
    """Re-bind up to 'attempts' times until the range stops looking default."""
    for attempt in range(1, attempts + 1):
        log(f"{i2c_name}: re-bind attempt {attempt}/{attempts}")
        rebind(i2c_name, settle)
        event_path = event_device_for(i2c_name)
        if event_path is None:
            log(f"{i2c_name}: no input device after re-bind yet")
            continue
        rng = current_range(event_path)
        if rng is None:
            log(f"{i2c_name}: could not read range from {event_path}")
            continue
        log(f"{i2c_name}: range now {rng[0]}x{rng[1]}")
        if not looks_default(rng):
            log(f"{i2c_name}: range looks correct, done")
            return True
    return False


# --- device discovery at boot (the touch node may not be bound yet) ----------
def wait_for_touch(timeout: float, addr: int) -> list[str]:
    """Poll until the touch device has a bound driver, or timeout."""
    deadline = time.monotonic() + timeout
    while True:
        devices = bound_devices(addr)
        if devices:
            return devices
        if time.monotonic() >= deadline:
            return []
        time.sleep(0.5)


def probe(devices: list[str]) -> None:
    """Report what each bound touch device currently reports, change nothing."""
    if not devices:
        log("no bound touch device")
        return
    for name in devices:
        event_path = event_device_for(name)
        if event_path is None:
            log(f"{name}: no /dev/input node found")
            continue
        rng = current_range(event_path)
        if rng is None:
            log(f"{name}: {event_path} range unreadable")
            continue
        verdict = "DEFAULT (wrong)" if looks_default(rng) else "ok"
        log(f"{name}: {event_path} range {rng[0]}x{rng[1]} -> {verdict}")


# --- entry point -------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--probe",
        action="store_true",
        help="only print the current touch range for each bound device and exit",
    )
    parser.add_argument(
        "--addr",
        type=lambda text: int(text, 0),
        default=DEFAULT_TOUCH_ADDR,
        help="I2C address of the touch controller, decimal or 0x hex (default 0x5d)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=1.0,
        help="seconds to wait after unbind and after bind (default 1.0)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="max re-bind tries per device before giving up (default 3)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=10.0,
        help="seconds to wait for the goodix device to appear at boot (default 10)",
    )
    parser.add_argument(
        "--power-settle",
        type=float,
        default=6.0,
        help="seconds to let the display stack raise IOVCC before acting (default 6)",
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    devices = wait_for_touch(args.wait, args.addr)
    if not devices:
        log(f"no bound touch device at I2C address 0x{args.addr:02x}; "
            "touch may be disabled, unloaded, or still deferring")
        return 3

    if args.probe:
        probe(devices)
        return 0

    # Give the KMS bring-up time to power the panel (and with it IOVCC), so a
    # re-bind below happens against an awake, settled GT911.
    log(f"waiting {args.power_settle:.0f}s for the panel rail to settle")
    time.sleep(max(0.0, args.power_settle))

    # Re-discover after the wait: the node may have re-numbered on a defer/
    # retry, and a device that was still deferred may have appeared.
    devices = bound_devices(args.addr)
    ok = True
    for name in devices:
        event_path = event_device_for(name)
        rng = current_range(event_path) if event_path else None
        if rng is None:
            log(f"{name}: range unreadable, will try a re-bind anyway")
        elif not looks_default(rng):
            log(f"{name}: range {rng[0]}x{rng[1]} already correct, no action")
            continue
        if not guard_once(name, args.attempts, args.settle):
            log(f"{name}: range still looks default after {args.attempts} attempts")
            ok = False

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
