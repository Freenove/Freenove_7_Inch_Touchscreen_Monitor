#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Brightness slider: a PyQt5 window that sets the panel backlight.

One horizontal slider, 5 on the left and 255 on the right. Every value the slider
settles on sends:

    echo X | sudo tee /sys/class/backlight/*/brightness

through bash, so the pipe and the * glob are expanded by the shell itself.

    python3 brightness.py

Needs python3-pyqt5. sudo asks for a password the first time it runs, so the
first drag may wait on the terminal prompt.
"""

from __future__ import annotations

import glob
import os
import sys

from PyQt5 import QtCore, QtWidgets

BRIGHTNESS_COMMAND = "echo {value} | sudo tee /sys/class/backlight/*/brightness"
# The bottom end is 5 rather than 0: very low duty cycles make the panel look
# switched off, which reads as a crash instead of as a dim screen.
SLIDER_MIN = 5
SLIDER_MAX = 255
FALLBACK_VALUE = 128
# Dragging fires valueChanged dozens of times a second; one shell per event
# would pile up processes, so the send waits until the slider stops moving.
SEND_DELAY_MS = 150
WINDOW_TITLE = "Brightness"

# A wide handle: this slider is operated on the panel's own touchscreen.
SLIDER_STYLE = """
QSlider::groove:horizontal {
    height: 4px;
    background: #d8d8d8;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #0073ef;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 26px;
    height: 26px;
    margin: -11px 0;
    border-radius: 13px;
    border: 1px solid #c8c8c8;
    background: #ffffff;
}
"""


def read_current_level() -> int | None:
    """The level the panel is at now, or None when nothing can be read."""
    for path in sorted(glob.glob("/sys/class/backlight/*/brightness")):
        try:
            return int(open(path, encoding="utf-8").read().strip())
        except (OSError, ValueError):
            continue
    return None


def quiet_qt_environment() -> None:
    """Silence the two warnings a plain PyQt5 start prints on Raspberry Pi OS.

    QStandardPaths complains because systemd hands out /run/user/<uid> as 0770
    (group readable through the tty ACL) while Qt insists on 0700, and the GTK
    platform theme logs a GLib assertion. Neither stops the window working, so
    rather than editing system configuration the environment is corrected here,
    before QApplication is created - which is the only moment it matters.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    try:
        usable = bool(runtime) and (os.stat(runtime).st_mode & 0o077) == 0
    except OSError:
        usable = False
    if not usable:
        private = f"/tmp/runtime-{os.environ.get('USER') or os.getuid()}"
        os.makedirs(private, exist_ok=True)
        os.chmod(private, 0o700)
        os.environ["XDG_RUNTIME_DIR"] = private
    # qt5 keeps Qt's own theme instead of the GTK bridge that emits the warning.
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "qt5")


def describe_backlight() -> str:
    """Where the brightness actually lives, as one reassuring startup line."""
    paths = sorted(glob.glob("/sys/class/backlight/*/brightness"))
    if not paths:
        return "no /sys/class/backlight/*/brightness node found"
    level = read_current_level()
    return f"{', '.join(paths)} (current level: {level if level is not None else 'unreadable'})"


class ClickableSlider(QtWidgets.QSlider):
    """A slider that jumps to the clicked spot instead of stepping by a page.

    Plain QSlider treats a click on the groove as a page step, so reaching a
    specific level means hunting for the handle and dragging it there - painful on
    a touchscreen. Pressing anywhere in the groove now sets that level directly,
    and the drag can keep going from there.
    """

    def value_at(self, event) -> int:
        position = event.position().toPoint() if hasattr(event, "position") else event.pos()
        groove = self.rect()
        return self.style().sliderValueFromPosition(
            self.minimum(), self.maximum(), position.x() - groove.left(), groove.width()
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setValue(self.value_at(event))
            # Held-down state keeps the poll from fighting the drag, exactly as a
            # normal handle drag would.
            self.setSliderDown(True)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.isSliderDown() and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.setValue(self.value_at(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        was_down = self.isSliderDown()
        super().mouseReleaseEvent(event)
        if was_down:
            self.setSliderDown(False)


class BrightnessWindow(QtWidgets.QWidget):
    """Slider on top, the value and the last command result underneath."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumWidth(560)

        self.slider = ClickableSlider(QtCore.Qt.Horizontal, self)
        self.slider.setRange(SLIDER_MIN, SLIDER_MAX)
        self.slider.setValue(read_current_level() or FALLBACK_VALUE)
        self.slider.setStyleSheet(SLIDER_STYLE)
        self.slider.valueChanged.connect(self.on_value_changed)

        self.value_label = QtWidgets.QLabel(self)
        self.status_label = QtWidgets.QLabel("ready", self)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(str(SLIDER_MIN)))
        row.addWidget(self.slider, stretch=1)
        row.addWidget(QtWidgets.QLabel(str(SLIDER_MAX)))

        column = QtWidgets.QVBoxLayout(self)
        column.addLayout(row)
        column.addWidget(self.value_label)
        column.addWidget(self.status_label)

        # Single shot timer restarted on every change: only the last position fires.
        self.send_timer = QtCore.QTimer(self)
        self.send_timer.setSingleShot(True)
        self.send_timer.setInterval(SEND_DELAY_MS)
        self.send_timer.timeout.connect(self.send_command)

        # QProcess keeps the window responsive; a blocking subprocess here would
        # freeze the UI for as long as sudo takes to answer.
        self.process = QtCore.QProcess(self)
        self.process.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.on_finished)

        self.output = ""
        self.show_value(self.slider.value())

    # --- slider events -------------------------------------------------------
    def on_value_changed(self, value: int) -> None:
        self.show_value(value)
        self.send_timer.start()

    def show_value(self, value: int) -> None:
        percent = round(100 * value / SLIDER_MAX)
        self.value_label.setText(f"brightness: {value} / {SLIDER_MAX}  ({percent}%)")

    # --- the command -------------------------------------------------------
    def command(self, value: int) -> str:
        return BRIGHTNESS_COMMAND.format(value=value)

    def send_command(self) -> None:
        command = self.command(self.slider.value())
        if self.process.state() != QtCore.QProcess.NotRunning:
            # The previous sudo call is still waiting (often for a password);
            # starting another one would just queue it behind the first.
            self.status_label.setText(f"still running: {command}")
            return
        self.output = ""
        self.status_label.setText(f"running: {command}")
        self.process.start("bash", ["-c", command])

    def read_output(self) -> None:
        self.output += bytes(self.process.readAllStandardOutput()).decode(errors="replace")

    def on_finished(self, exit_code: int, exit_status: QtCore.QProcess.ExitStatus) -> None:
        command = self.command(self.slider.value())
        detail = " ".join(self.output.split())
        if exit_code != 0:
            self.status_label.setText(f"failed ({exit_code}): {command} {detail}".strip())
        else:
            self.status_label.setText(f"ok: {command} -> {detail or 'no output'}")


def main() -> int:
    quiet_qt_environment()
    print(f"backlight: {describe_backlight()}")
    app = QtWidgets.QApplication(sys.argv[:1])
    window = BrightnessWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
