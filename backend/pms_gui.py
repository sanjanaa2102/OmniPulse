"""
Phase 6 - PMS GUI (v5: theme system + calmer, averaged display)

WHAT'S NEW IN v5:
- Light/dark theme toggle in Settings. Real bedside monitors often
  have a day/night mode for exactly this reason - dim for a patient's
  room overnight, brighter for daytime use. Switching theme rebuilds
  the interface cleanly (only allowed while not actively monitoring,
  which Settings already enforces).
- Displayed numbers are now averaged over the last few seconds and
  only update every ~4 seconds, instead of visibly changing every
  single second. IMPORTANT: this ONLY affects what number is shown.
  The per-vital accent color, the risk banner, the event log, and any
  sound alert all still react every second off the raw reading - nothing
  about actual alert responsiveness is delayed, only the readability
  of the number itself.

Everything from v4 (settings dialog, interactive finger prompt,
session stats/export, sound toggle, athletic HR adjustment) is
unchanged in behavior.
"""

import sys
import json
import os
import time
import math

from PySide6.QtCore import Qt, QThread, Signal, Property, QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QFont, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QMessageBox,
    QGraphicsOpacityEffect, QListWidget, QListWidgetItem,
    QDialog, QCheckBox, QDialogButtonBox, QFormLayout, QStackedWidget,
    QScrollArea
)
import pyqtgraph as pg

from serial_reader import PMSSerialReader
from hr_algorithm import compute_heart_rate
from rr_algorithm import compute_respiratory_rate
from bp_algorithm import extract_crest_time_ratio, BPCalibration
from ews_algorithm import compute_ews, generate_patient_message, get_age_band
from smoothing import ReadingSmoother

CALIBRATION_FILE = "calibration_points.json"

# ---------------------------------------------------------------- #
# Theme system - COLORS is a live, mutable dict; set_theme() swaps
# its contents in place so anything that reads COLORS at construction
# time (which is everything here) picks up the new palette the next
# time it's built.
# ---------------------------------------------------------------- #
THEMES = {
    "dark": {
        "background":   "#0D1B2A",
        "surface":      "#152A3D",
        "text_primary": "#EAF2F8",
        "text_muted":   "#7A93A8",
        "stable":       "#5FB0DE",
        "watch":        "#F0B03D",
        "critical":     "#F16B57",
        "neutral":      "#3A4F63",
        "banner_text":  "#0D1B2A",
    },
    "light": {
        "background":   "#F2F5F8",
        "surface":      "#FFFFFF",
        "text_primary": "#12222F",
        "text_muted":   "#5C7285",
        "stable":       "#2C7DA0",
        "watch":        "#C97A1F",
        "critical":     "#C13B2A",
        "neutral":      "#C7D2DA",
        "banner_text":  "#FFFFFF",
    },
}

COLORS = dict(THEMES["dark"])

def set_theme(name):
    COLORS.clear()
    COLORS.update(THEMES.get(name, THEMES["dark"]))


def font_display(size=26, bold=True):
    f = QFont()
    f.setFamilies(["Cascadia Mono", "Consolas", "Courier New"])
    f.setPointSize(size)
    f.setBold(bold)
    return f

def font_label(size=10, bold=False):
    f = QFont()
    f.setFamilies(["Segoe UI Semibold", "Segoe UI", "Arial"])
    f.setPointSize(size)
    f.setBold(bold)
    return f

def get_input_style():
    """A function, not a frozen string - so it reflects the current
    theme every time it's called, rather than only whatever theme
    was active when the module first loaded."""
    return f"""
        QLineEdit {{
            background-color: {COLORS['surface']};
            color: {COLORS['text_primary']};
            border: 1px solid {COLORS['neutral']};
            border-radius: 6px;
            padding: 5px 8px;
        }}
        QCheckBox {{ color: {COLORS['text_primary']}; }}
        QPushButton {{
            background-color: {COLORS['stable']};
            color: white;
            border: none;
            border-radius: 6px;
            padding: 6px 16px;
            font-weight: 600;
        }}
        QPushButton:hover {{ opacity: 0.9; }}
    """

def get_lineedit_style():
    """Applied directly to each QLineEdit instance, rather than only
    relying on it cascading down from a parent dialog's stylesheet -
    that inheritance wasn't reliably landing (typed text was showing
    in a default color regardless of theme). Setting it on the
    widget itself guarantees it actually takes."""
    return f"""
        QLineEdit {{
            background-color: {COLORS['surface']};
            color: {COLORS['text_primary']};
            border: 1px solid {COLORS['neutral']};
            border-radius: 6px;
            padding: 5px 8px;
            selection-background-color: {COLORS['stable']};
            selection-color: {COLORS['banner_text']};
        }}
    """


def _clear_layout(layout):
    """Recursively removes and deletes every widget/sub-layout from a
    layout, so it can be safely detached and a fresh one built."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        else:
            sub = item.layout()
            if sub is not None:
                _clear_layout(sub)


# ---------------------------------------------------------------- #
# DisplayAverager - smooths what's SHOWN, not what's SCORED
# ---------------------------------------------------------------- #
class DisplayAverager:
    """
    Buffers raw per-second readings and releases a new averaged value
    only every `interval` seconds, so the on-screen number holds
    steady and readable instead of visibly changing every second.
    Deliberately does NOT touch the EWS score or the per-vital accent
    color - those keep reacting every second off the raw reading.
    """
    def __init__(self, interval=4.0):
        self.interval = interval
        self._buffer = []
        self._last_release = time.time()
        self._last_value = None

    def reset(self):
        self._buffer = []
        self._last_release = time.time()
        self._last_value = None

    def update(self, value):
        if value is not None:
            self._buffer.append(value)
        now = time.time()
        if now - self._last_release >= self.interval:
            self._last_release = now
            if self._buffer:
                self._last_value = sum(self._buffer) / len(self._buffer)
                self._buffer = []
        return self._last_value


# ---------------------------------------------------------------- #
# SensorWorker - unchanged core logic, settings-aware
# ---------------------------------------------------------------- #
class SensorWorker(QThread):
    vitals_updated = Signal(dict)
    status_updated = Signal(str)

    def __init__(self, port, age=None, athletic=False):
        super().__init__()
        self.port = port
        self.age = age
        self.athletic = athletic
        self._running = True
        self.hr_smoother = ReadingSmoother(history_size=5, max_jump=25)

        self.bp_cal = BPCalibration()
        if os.path.exists(CALIBRATION_FILE):
            with open(CALIBRATION_FILE) as f:
                for p in json.load(f):
                    self.bp_cal.add_point(p["crest_time_ratio"], p["sys"], p["dia"])

    def stop(self):
        self._running = False

    def run(self):
        try:
            reader = PMSSerialReader(port=self.port)
        except Exception as e:
            self.status_updated.emit(f"Could not open {self.port}: {e}")
            return

        self.status_updated.emit("Connected. Waiting for finger...")
        last_emit = time.time()

        while self._running:
            reader.read_line()

            if time.time() - last_emit >= 1.0:
                last_emit = time.time()

                if not reader.finger_present:
                    self.status_updated.emit("Waiting for finger...")
                    continue

                hr_raw, hr_valid_raw = compute_heart_rate(reader.get_ir_window(seconds=10))
                hr, hr_valid = self.hr_smoother.update(hr_raw, hr_valid_raw)

                rr, rr_valid = compute_respiratory_rate(reader.get_ir_window())

                ct_ratio = extract_crest_time_ratio(reader.get_ir_window(seconds=10))
                bp_sys, bp_dia = self.bp_cal.estimate(ct_ratio) if ct_ratio else (None, None)

                spo2 = reader.latest_device_spo2 if reader.latest_device_spo2_valid else None

                risk, score, breakdown = compute_ews(
                    hr=hr if hr_valid else None,
                    rr=rr if rr_valid else None,
                    spo2=spo2,
                    sys_bp=bp_sys,
                    age=self.age,
                    athletic=self.athletic,
                )

                self.status_updated.emit("Monitoring...")
                self.vitals_updated.emit({
                    "hr": hr if hr_valid else None,
                    "rr": rr if rr_valid else None,
                    "spo2": spo2,
                    "bp_sys": bp_sys,
                    "bp_dia": bp_dia,
                    "risk": risk,
                    "score": score,
                    "breakdown": breakdown,
                    "cal_points": len(self.bp_cal.points),
                })

        reader.close()


# ---------------------------------------------------------------- #
# AnimatedValueLabel
# ---------------------------------------------------------------- #
class AnimatedValueLabel(QLabel):
    def __init__(self, text="--"):
        super().__init__(text)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(1.0)
        self._last_text = text
        self._anim = None

    def set_value_animated(self, new_text):
        if new_text == self._last_text:
            return
        self._last_text = new_text
        self.setText(new_text)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(280)
        self._anim.setStartValue(0.25)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start()


# ---------------------------------------------------------------- #
# Sparkline
# ---------------------------------------------------------------- #
class Sparkline(pg.PlotWidget):
    def __init__(self, color, max_points=60):
        super().__init__()
        self.setBackground(COLORS["surface"])
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)
        self.showGrid(x=False, y=False)
        self.setFixedHeight(34)
        self._data = []
        self._max_points = max_points
        self._curve = self.plot(pen=pg.mkPen(color=color, width=2))

    def add_point(self, value):
        if value is None:
            return
        self._data.append(value)
        if len(self._data) > self._max_points:
            self._data.pop(0)
        self._curve.setData(self._data)

    def set_color(self, color):
        self._curve.setPen(pg.mkPen(color=color, width=2))


# ---------------------------------------------------------------- #
# VitalCard
# ---------------------------------------------------------------- #
class VitalCard(QFrame):
    def __init__(self, label_text):
        super().__init__()
        self.setObjectName("vitalCard")
        self._accent = COLORS["neutral"]

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(4)

        name = QLabel(label_text)
        name.setFont(font_label(10))
        name.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")

        self.value_label = AnimatedValueLabel("--")
        self.value_label.setFont(font_display(26))
        self.value_label.setStyleSheet(f"color: {COLORS['text_primary']}; background: transparent;")

        self.sparkline = Sparkline(self._accent)

        layout.addWidget(name)
        layout.addWidget(self.value_label)
        layout.addWidget(self.sparkline)
        self.setLayout(layout)
        self._apply_style()

    def set_value(self, text, numeric=None):
        self.value_label.set_value_animated(text)
        if numeric is not None:
            self.sparkline.add_point(numeric)

    def set_status(self, score, has_value):
        if not has_value:
            self._accent = COLORS["neutral"]
        elif score >= 3:
            self._accent = COLORS["critical"]
        elif score >= 1:
            self._accent = COLORS["watch"]
        else:
            self._accent = COLORS["stable"]
        self._apply_style()
        self.sparkline.set_color(self._accent)

    def _apply_style(self):
        self.setStyleSheet(f"""
            QFrame#vitalCard {{
                background-color: {COLORS['surface']};
                border-left: 4px solid {self._accent};
                border-radius: 10px;
            }}
        """)


# ---------------------------------------------------------------- #
# HeartbeatPulse
# ---------------------------------------------------------------- #
class HeartbeatPulse(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(7)
        self._bpm = 70
        self._color = QColor(COLORS["stable"])
        self._start = time.time()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(40)

    def set_bpm(self, bpm):
        if bpm and 20 <= bpm <= 220:
            self._bpm = bpm

    def set_color(self, hex_color):
        self._color = QColor(hex_color)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        period = 60.0 / self._bpm
        phase = ((time.time() - self._start) % period) / period
        intensity = math.exp(-phase * 5.5)
        c = QColor(self._color)
        c.setAlphaF(0.20 + 0.75 * intensity)
        painter.fillRect(self.rect(), c)


# ---------------------------------------------------------------- #
# RiskBanner
# ---------------------------------------------------------------- #
class RiskBanner(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(86)
        self._bg = QColor(COLORS["neutral"])

        self.headline = QLabel("Waiting for readings...")
        self.headline.setFont(font_label(13, bold=True))
        self.headline.setStyleSheet(f"color: {COLORS['banner_text']}; background: transparent;")
        self.headline.setAlignment(Qt.AlignCenter)
        self.headline.setWordWrap(True)

        self.caption = QLabel("")
        self.caption.setFont(font_label(8))
        self.caption.setStyleSheet(f"color: {COLORS['banner_text']}; background: transparent;")
        self.caption.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 10, 24, 8)
        layout.setSpacing(3)
        layout.addWidget(self.headline)
        layout.addWidget(self.caption)

        self._anim = QPropertyAnimation(self, b"bgColor", self)
        self._anim.setDuration(500)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)

    def get_bg_color(self):
        return self._bg

    def set_bg_color(self, color):
        self._bg = color
        self.update()

    bgColor = Property(QColor, get_bg_color, set_bg_color)

    def set_risk(self, risk_text, score, message):
        target = {
            "STABLE": QColor(COLORS["stable"]),
            "WATCH": QColor(COLORS["watch"]),
            "CRITICAL": QColor(COLORS["critical"]),
        }.get(risk_text, QColor(COLORS["neutral"]))

        self.headline.setText(message)
        self.caption.setText(f"Early Warning Score: {score}   \u00b7   {risk_text}")
        self._anim.stop()
        self._anim.setStartValue(self._bg)
        self._anim.setEndValue(target)
        self._anim.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._bg)
        painter.drawRoundedRect(self.rect(), 12, 12)


# ---------------------------------------------------------------- #
# EventLog
# ---------------------------------------------------------------- #
class EventLog(QFrame):
    def __init__(self, max_entries=8):
        super().__init__()
        self.setObjectName("eventLog")
        self.max_entries = max_entries

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(6)

        title = QLabel("ACTIVITY LOG")
        title.setFont(font_label(9, bold=True))
        title.setStyleSheet(f"color: {COLORS['text_muted']}; background: transparent;")
        layout.addWidget(title)

        self.list_widget = QListWidget()
        self.list_widget.setFixedHeight(100)
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: transparent;
                color: {COLORS['text_primary']};
                border: none;
                font-size: 9pt;
            }}
        """)
        layout.addWidget(self.list_widget)
        self.setLayout(layout)
        self.setStyleSheet(f"""
            QFrame#eventLog {{
                background-color: {COLORS['surface']};
                border-radius: 10px;
            }}
        """)

    def add_entry(self, text, color):
        timestamp = time.strftime("%H:%M:%S")
        item = QListWidgetItem(f"{timestamp}   \u2014   {text}")
        item.setForeground(QColor(color))
        self.list_widget.insertItem(0, item)
        while self.list_widget.count() > self.max_entries:
            self.list_widget.takeItem(self.list_widget.count() - 1)


# ---------------------------------------------------------------- #
# SearchRing
# ---------------------------------------------------------------- #
class SearchRing(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(120, 120)
        self._active = False
        self._start = time.time()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(40)

    def set_active(self, active):
        if active != self._active:
            self._active = active
            self._start = time.time()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        base = QColor(COLORS["stable"]) if self._active else QColor(COLORS["neutral"])

        if self._active:
            period = 2.0
            elapsed = time.time() - self._start
            for offset in (0.0, 0.5):
                phase = ((elapsed + offset * period) % period) / period
                radius = 20 + phase * 38
                alpha = max(0.0, 1.0 - phase) * 0.55
                pen_color = QColor(base)
                pen_color.setAlphaF(alpha)
                painter.setPen(QPen(pen_color, 3))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(int(cx - radius), int(cy - radius), int(radius * 2), int(radius * 2))

        inner = QColor(base)
        inner.setAlphaF(0.85)
        painter.setPen(Qt.NoPen)
        painter.setBrush(inner)
        painter.drawEllipse(int(cx - 18), int(cy - 18), 36, 36)


# ---------------------------------------------------------------- #
# PromptPage
# ---------------------------------------------------------------- #
class PromptPage(QWidget):
    IDLE_MESSAGES = [
        "Ready when you are.",
        "Open Settings, enter patient details, then press Start Monitoring.",
    ]
    WAITING_MESSAGES = [
        "Hi there \u2014 let's check your vitals.",
        "Please place your finger gently on the sensor.",
        "Hold still and I'll take it from here...",
        "Searching for your pulse...",
    ]

    def __init__(self):
        super().__init__()
        self._mode = "idle"
        self._msg_index = 0

        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)
        layout.setContentsMargins(20, 60, 20, 60)

        self.ring = SearchRing()
        layout.addWidget(self.ring, alignment=Qt.AlignCenter)

        self.message_label = AnimatedValueLabel(self.IDLE_MESSAGES[0])
        self.message_label.setFont(font_label(13, bold=True))
        self.message_label.setStyleSheet(f"color: {COLORS['text_primary']}; background: transparent;")
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setWordWrap(True)
        self.message_label.setFixedWidth(380)
        layout.addWidget(self.message_label, alignment=Qt.AlignCenter)

        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._cycle_message)
        self._timer.start(2800)

    def set_mode(self, mode):
        if mode != self._mode:
            self._mode = mode
            self._msg_index = 0
            self._show_current()
        self.ring.set_active(mode == "waiting")

    def _cycle_message(self):
        messages = self.WAITING_MESSAGES if self._mode == "waiting" else self.IDLE_MESSAGES
        self._msg_index = (self._msg_index + 1) % len(messages)
        self._show_current()

    def _show_current(self):
        messages = self.WAITING_MESSAGES if self._mode == "waiting" else self.IDLE_MESSAGES
        self.message_label.set_value_animated(messages[self._msg_index])


# ---------------------------------------------------------------- #
# SettingsDialog
# ---------------------------------------------------------------- #
class SettingsDialog(QDialog):
    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Patient Settings")
        self.setStyleSheet(f"background-color: {COLORS['background']};" + get_input_style())
        self.resize(380, 380)

        form = QFormLayout()
        form.setSpacing(12)
        label_style = f"color: {COLORS['text_muted']};"

        self.name_input = QLineEdit(current.get("name", ""))
        self.id_input = QLineEdit(current.get("patient_id", ""))
        age_val = current.get("age")
        self.age_input = QLineEdit(str(age_val) if age_val is not None else "")
        self.port_input = QLineEdit(current.get("port", "COM3"))

        for field in (self.name_input, self.id_input, self.age_input, self.port_input):
            field.setStyleSheet(get_lineedit_style())

        self.athletic_check = QCheckBox("Athletic / naturally low resting heart rate")
        self.athletic_check.setChecked(current.get("athletic", False))
        self.sound_check = QCheckBox("Play a gentle sound if risk becomes Critical")
        self.sound_check.setChecked(current.get("sound_alerts", False))
        self.light_mode_check = QCheckBox("Light mode")
        self.light_mode_check.setChecked(current.get("light_mode", False))

        def lbl(text):
            l = QLabel(text)
            l.setStyleSheet(label_style)
            return l

        form.addRow(lbl("Patient name"), self.name_input)
        form.addRow(lbl("Patient ID"), self.id_input)
        form.addRow(lbl("Age (years)"), self.age_input)
        form.addRow(lbl("Sensor COM port"), self.port_input)
        form.addRow(self.athletic_check)
        form.addRow(self.sound_check)
        form.addRow(self.light_mode_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        outer = QVBoxLayout()
        outer.addLayout(form)
        outer.addWidget(buttons)
        self.setLayout(outer)

    def get_values(self):
        try:
            age = int(self.age_input.text().strip())
        except ValueError:
            age = None
        return {
            "name": self.name_input.text().strip(),
            "patient_id": self.id_input.text().strip(),
            "age": age,
            "port": self.port_input.text().strip() or "COM3",
            "athletic": self.athletic_check.isChecked(),
            "sound_alerts": self.sound_check.isChecked(),
            "light_mode": self.light_mode_check.isChecked(),
        }


# ---------------------------------------------------------------- #
# Main window
# ---------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Patient Monitoring System")
        self.resize(640, 840)
        self.worker = None
        self._last_risk = None
        self.settings = {
            "name": "", "patient_id": "", "age": None, "port": "COM3",
            "athletic": False, "sound_alerts": False, "light_mode": False,
        }
        self._reset_session_stats()
        self.hr_avg = DisplayAverager(interval=4.0)
        self.rr_avg = DisplayAverager(interval=4.0)
        self.spo2_avg = DisplayAverager(interval=4.0)
        self.bp_sys_avg = DisplayAverager(interval=4.0)
        self.bp_dia_avg = DisplayAverager(interval=4.0)
        self.setStyleSheet(f"background-color: {COLORS['background']};")
        self.build_ui()

    def _reset_session_stats(self):
        self.session_stats = {"hr": [], "rr": [], "spo2": [], "bp_sys": [], "bp_dia": []}
        self.session_events = []

    def _reset_display_averagers(self):
        for avg in (self.hr_avg, self.rr_avg, self.spo2_avg, self.bp_sys_avg, self.bp_dia_avg):
            avg.reset()

    def build_ui(self):
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.pulse = HeartbeatPulse()
        outer.addWidget(self.pulse)

        content = QVBoxLayout()
        content.setContentsMargins(24, 18, 24, 20)
        content.setSpacing(14)

        header = QHBoxLayout()
        self.settings_btn = QPushButton("\u2699 Settings")
        self.settings_btn.setStyleSheet(get_input_style())
        self.settings_btn.clicked.connect(self.open_settings)
        header.addWidget(self.settings_btn)

        self.patient_summary = QLabel("No patient details entered")
        self.patient_summary.setFont(font_label(10))
        self.patient_summary.setStyleSheet(f"color: {COLORS['text_muted']};")
        header.addWidget(self.patient_summary)
        header.addStretch()

        self.connect_btn = QPushButton("Start Monitoring")
        self.connect_btn.setStyleSheet(get_input_style())
        self.connect_btn.clicked.connect(self.toggle_monitoring)
        header.addWidget(self.connect_btn)
        content.addLayout(header)

        self.age_band_label = QLabel("")
        self.age_band_label.setFont(font_label(9))
        self.age_band_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        content.addWidget(self.age_band_label)

        self.status_label = QLabel("Not connected.")
        self.status_label.setFont(font_label(9))
        self.status_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        content.addWidget(self.status_label)

        self.stack = QStackedWidget()

        self.prompt_page = PromptPage()
        self.stack.addWidget(self.prompt_page)

        self.monitoring_page = QWidget()
        mon_layout = QVBoxLayout()
        mon_layout.setContentsMargins(0, 0, 0, 0)
        mon_layout.setSpacing(14)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.hr_card = VitalCard("HEART RATE  \u00b7  bpm")
        self.rr_card = VitalCard("RESPIRATORY RATE  \u00b7  br/min")
        self.spo2_card = VitalCard("SpO2  \u00b7  %")
        self.bp_card = VitalCard("BLOOD PRESSURE  \u00b7  mmHg")
        grid.addWidget(self.hr_card, 0, 0)
        grid.addWidget(self.rr_card, 0, 1)
        grid.addWidget(self.spo2_card, 1, 0)
        grid.addWidget(self.bp_card, 1, 1)
        mon_layout.addLayout(grid)

        self.risk_banner = RiskBanner()
        mon_layout.addWidget(self.risk_banner)

        self.event_log = EventLog()
        mon_layout.addWidget(self.event_log)

        self.monitoring_page.setLayout(mon_layout)
        self.stack.addWidget(self.monitoring_page)
        self.stack.setCurrentWidget(self.prompt_page)

        content.addWidget(self.stack)

        footer = QHBoxLayout()
        self.cal_label = QLabel("BP calibration: 0 point(s) loaded")
        self.cal_label.setFont(font_label(9))
        self.cal_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        footer.addWidget(self.cal_label)
        footer.addStretch()

        self.export_btn = QPushButton("Export Report")
        self.export_btn.setStyleSheet(get_input_style())
        self.export_btn.clicked.connect(self.export_report)
        footer.addWidget(self.export_btn)
        content.addLayout(footer)

        # Wrap everything below the pulse strip in a scroll area. On
        # some Windows displays (especially with scaling), the real
        # rendered content can end up taller than the window - without
        # this, whatever sits at the bottom (like Export Report) can
        # end up pushed off-screen with no way to reach it.
        content_widget = QWidget()
        content_widget.setLayout(content)

        scroll = QScrollArea()
        scroll.setWidget(content_widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"QScrollArea {{ background-color: {COLORS['background']}; border: none; }}")

        outer.addWidget(scroll)
        self.setLayout(outer)

    def _rebuild_ui(self):
        old_layout = self.layout()
        if old_layout is not None:
            _clear_layout(old_layout)
            QWidget().setLayout(old_layout)  # detach the empty layout from self
        self.setStyleSheet(f"background-color: {COLORS['background']};")
        self.build_ui()
        self._update_patient_summary()

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            old_light_mode = self.settings.get("light_mode", False)
            self.settings = dialog.get_values()
            self._update_patient_summary()
            if self.settings.get("light_mode", False) != old_light_mode:
                set_theme("light" if self.settings["light_mode"] else "dark")
                self._rebuild_ui()

    def _update_patient_summary(self):
        name = self.settings.get("name") or "Unnamed patient"
        age = self.settings.get("age")
        age_text = f"age {age}" if age is not None else "age not set"
        self.patient_summary.setText(f"  {name}  \u00b7  {age_text}")

    def toggle_monitoring(self):
        if self.worker is None:
            if self.settings.get("age") is None:
                QMessageBox.warning(self, "Age required", "Please open Settings and enter the patient's age before starting.")
                return

            band = get_age_band(self.settings["age"])
            band_text = "Child reference ranges (age < 12)" if band == "child" else "Adult reference ranges"
            if self.settings.get("athletic") and band == "adult":
                band_text += "  \u00b7  athletic HR adjustment applied"
            self.age_band_label.setText(f"Age band in use: {band_text}")

            self._reset_session_stats()
            self._reset_display_averagers()
            self._session_start = time.time()

            self.worker = SensorWorker(self.settings["port"], self.settings["age"], self.settings.get("athletic", False))
            self.worker.vitals_updated.connect(self.on_vitals_updated)
            self.worker.status_updated.connect(self.on_status_updated)
            self.worker.start()
            self.connect_btn.setText("Stop Monitoring")
            self.settings_btn.setEnabled(False)
            self._last_risk = None
            self.prompt_page.set_mode("waiting")
            self.stack.setCurrentWidget(self.prompt_page)
        else:
            self.worker.stop()
            self.worker.wait()
            self.worker = None
            self.connect_btn.setText("Start Monitoring")
            self.settings_btn.setEnabled(True)
            self.status_label.setText("Stopped.")
            self.prompt_page.set_mode("idle")
            self.stack.setCurrentWidget(self.prompt_page)

    def on_status_updated(self, text):
        self.status_label.setText(text)
        if text == "Waiting for finger...":
            self.prompt_page.set_mode("waiting")
            self.stack.setCurrentWidget(self.prompt_page)

    def on_vitals_updated(self, data):
        self.stack.setCurrentWidget(self.monitoring_page)

        breakdown = data["breakdown"]
        risk = data["risk"]

        # --- displayed numbers: averaged + delayed for readability ---
        hr_disp = self.hr_avg.update(data["hr"])
        self.hr_card.set_value(f"{hr_disp:.0f}" if hr_disp is not None else "--", hr_disp)
        self.hr_card.set_status(breakdown["hr"], data["hr"] is not None)  # accent stays on RAW/fast signal
        if data["hr"] is not None:
            self.session_stats["hr"].append(data["hr"])

        rr_disp = self.rr_avg.update(data["rr"])
        self.rr_card.set_value(f"{rr_disp:.1f}" if rr_disp is not None else "--", rr_disp)
        self.rr_card.set_status(breakdown["rr"], data["rr"] is not None)
        if data["rr"] is not None:
            self.session_stats["rr"].append(data["rr"])

        spo2_disp = self.spo2_avg.update(data["spo2"])
        self.spo2_card.set_value(f"{spo2_disp:.0f}" if spo2_disp is not None else "--", spo2_disp)
        self.spo2_card.set_status(breakdown["spo2"], data["spo2"] is not None)
        if data["spo2"] is not None:
            self.session_stats["spo2"].append(data["spo2"])

        bp_sys_disp = self.bp_sys_avg.update(data["bp_sys"])
        bp_dia_disp = self.bp_dia_avg.update(data["bp_dia"])
        bp_text = f"{bp_sys_disp:.0f}/{bp_dia_disp:.0f}" if bp_sys_disp is not None and bp_dia_disp is not None else "--"
        self.bp_card.set_value(bp_text, bp_sys_disp)
        self.bp_card.set_status(breakdown["sys_bp"], data["bp_sys"] is not None)
        if data["bp_sys"] is not None:
            self.session_stats["bp_sys"].append(data["bp_sys"])
            self.session_stats["bp_dia"].append(data["bp_dia"])

        # --- risk banner, pulse, log, sound: all stay on the RAW, fast, per-second signal ---
        message = generate_patient_message(risk, breakdown)
        self.risk_banner.set_risk(risk, data["score"], message)

        pulse_color = {
            "STABLE": COLORS["stable"],
            "WATCH": COLORS["watch"],
            "CRITICAL": COLORS["critical"],
        }.get(risk, COLORS["stable"])
        self.pulse.set_color(pulse_color)
        if data["hr"] is not None:
            self.pulse.set_bpm(data["hr"])

        if self._last_risk is not None and risk != self._last_risk:
            worst = max(breakdown, key=lambda k: breakdown[k])
            names = {"hr": "heart rate", "rr": "breathing rate", "spo2": "oxygen level", "sys_bp": "blood pressure"}
            entry_text = f"Risk changed to {risk} ({names.get(worst, 'vitals')})"
            self.event_log.add_entry(entry_text, pulse_color)
            self.session_events.append(f"{time.strftime('%H:%M:%S')} - {entry_text}")

            if risk == "CRITICAL" and self.settings.get("sound_alerts"):
                QApplication.beep()

        self._last_risk = risk
        self.cal_label.setText(f"BP calibration: {data['cal_points']} point(s) loaded")

    def export_report(self):
        lines = []
        lines.append("PATIENT MONITORING SYSTEM - SESSION REPORT")
        lines.append("=" * 50)
        lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Patient: {self.settings.get('name') or 'Not entered'}")
        lines.append(f"Patient ID: {self.settings.get('patient_id') or 'Not entered'}")
        lines.append(f"Age: {self.settings.get('age', '--')}")
        lines.append(f"Athletic HR adjustment: {'Yes' if self.settings.get('athletic') else 'No'}")
        lines.append("")

        def summarize(name, values, unit):
            if not values:
                return f"{name}: no readings recorded"
            return f"{name}: min {min(values):.1f}{unit}  max {max(values):.1f}{unit}  avg {sum(values)/len(values):.1f}{unit}  (n={len(values)})"

        lines.append("VITALS SUMMARY (raw readings, not the smoothed display)")
        lines.append("-" * 50)
        lines.append(summarize("Heart rate", self.session_stats["hr"], " bpm"))
        lines.append(summarize("Respiratory rate", self.session_stats["rr"], " br/min"))
        lines.append(summarize("SpO2", self.session_stats["spo2"], "%"))
        lines.append(summarize("Systolic BP", self.session_stats["bp_sys"], " mmHg"))
        lines.append(summarize("Diastolic BP", self.session_stats["bp_dia"], " mmHg"))
        lines.append("")

        lines.append("RISK EVENTS")
        lines.append("-" * 50)
        lines.extend(self.session_events if self.session_events else ["No risk-level changes recorded this session."])
        lines.append("")
        lines.append(self.cal_label.text())

        filename = f"session_report_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        full_path = os.path.abspath(filename)
        with open(filename, "w") as f:
            f.write("\n".join(lines))

        QMessageBox.information(self, "Report saved", f"Session report saved to:\n\n{full_path}")

    def closeEvent(self, event):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
