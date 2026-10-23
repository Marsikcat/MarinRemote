"""Colours and the application stylesheet (MarinCall's palette, follows Windows' light/dark)."""

import ctypes
import sys

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

THEMES = {
    "dark": {
        "rail": "#1e1f22", "side": "#2b2d31", "main": "#313338", "float": "#111214",
        "hover": "#35373c", "active": "#404249", "input": "#383a40", "card": "#2b2d31",
        "border": "#1f2023", "divider": "#3f4147",
        "text": "#dbdee1", "muted": "#949ba4", "header": "#f2f3f5", "icon": "#b5bac1",
    },
    "light": {
        "rail": "#e3e5e8", "side": "#f2f3f5", "main": "#ffffff", "float": "#ffffff",
        "hover": "#e8e9ec", "active": "#d7d9dc", "input": "#ebedef", "card": "#f2f3f5",
        "border": "#e1e2e4", "divider": "#e1e2e4",
        "text": "#313338", "muted": "#5c5e66", "header": "#060607", "icon": "#4e5058",
    },
}

ACCENT = "#5865f2"
GREEN, RED, YELLOW = "#23a55a", "#f23f43", "#f0b232"
FONT_FAMILY = "Segoe UI Variable Text"
MONO_FAMILY = "Cascadia Mono"


def windows_prefers_light():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 1
    except OSError:
        return False


def shade(hex_color, factor):
    """factor < 1 darkens, > 1 lightens."""
    c = QColor(hex_color)
    return (c.darker(int(100 / factor)) if factor < 1 else c.lighter(int(100 * factor))).name()


def mix(a, b, t):
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


class Theme:
    """The active palette. Widgets read colours from here (T.c['text'] …)."""

    def __init__(self):
        self.name = "dark"
        self.c = {}
        self.load()

    def load(self):
        self.name = "light" if windows_prefers_light() else "dark"
        c = dict(THEMES[self.name])
        c.update(accent=ACCENT, accent_hover=shade(ACCENT, 0.85), accent_soft=mix(c["main"], ACCENT, 0.18),
                 green=GREEN, red=RED, yellow=YELLOW, red_soft=mix(c["main"], RED, 0.16),
                 yellow_soft=mix(c["main"], YELLOW, 0.14))
        self.c = c

    @property
    def light(self):
        return self.name == "light"


T = Theme()


def apply(app: QApplication):
    T.load()
    f = QFont(FONT_FAMILY)
    f.setPointSizeF(10)
    f.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(f)
    app.setStyleSheet(stylesheet())


def mono_font(size=11):
    f = QFont(MONO_FAMILY)
    f.setStyleHint(QFont.Monospace)
    f.setPointSizeF(size)
    return f


def stylesheet():
    c = T.c
    return f"""
    * {{ outline: none; }}
    QWidget {{ color: {c['text']}; background: transparent; }}
    QMainWindow, #Root, QDialog {{ background: {c['main']}; }}
    QToolTip {{ background: {c['float']}; color: {c['header']}; border: none;
                padding: 6px 9px; border-radius: 5px; }}

    #Sidebar {{ background: {c['side']}; }}
    #NavButton {{ background: transparent; color: {c['muted']}; border: none; border-radius: 6px;
                  padding: 9px 12px; text-align: left; font-weight: 600; }}
    #NavButton:hover {{ background: {c['hover']}; color: {c['text']}; }}
    #NavButton:checked {{ background: {c['active']}; color: {c['header']}; }}
    #Card {{ background: {c['card']}; border-radius: 10px; }}
    #Row {{ background: transparent; border-radius: 6px; }}
    #Row:hover {{ background: {c['hover']}; }}
    #Warning {{ background: {c['yellow_soft']}; border-radius: 8px; }}
    #Danger {{ background: {c['red_soft']}; border-radius: 8px; }}
    #Info {{ background: {c['accent_soft']}; border-radius: 8px; }}
    #TopBar {{ background: {c['side']}; border-bottom: 1px solid {c['border']}; }}

    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['rail'] if not T.light else '#c4c9ce'};
                                   border-radius: 3px; min-height: 30px; margin: 0 2px; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page
        {{ height: 0; background: none; }}
    QScrollBar:horizontal {{ height: 0; }}

    QLineEdit, QPlainTextEdit {{
        background: {c['input']}; color: {c['text']}; border: none; border-radius: 6px;
        padding: 8px 10px; selection-background-color: {c['accent']}; selection-color: white; }}

    QComboBox {{ background: {c['input']}; border: none; border-radius: 6px; padding: 6px 10px;
                 color: {c['text']}; min-height: 20px; }}
    QComboBox:hover {{ background: {c['active']}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{ background: {c['float']}; color: {c['text']}; border: none;
        padding: 4px; selection-background-color: {c['accent']}; selection-color: white; }}

    QPushButton {{ background: {c['accent']}; color: white; border: none; border-radius: 4px;
                   padding: 8px 16px; font-weight: 600; }}
    QPushButton:hover {{ background: {c['accent_hover']}; }}
    QPushButton:disabled {{ background: {c['active']}; color: {c['muted']}; }}
    QPushButton[kind="secondary"] {{ background: {c['active']}; color: {c['header']}; }}
    QPushButton[kind="secondary"]:hover {{ background: {shade(c['active'], 1.15) if not T.light else '#c9ccd1'}; }}
    QPushButton[kind="danger"] {{ background: {c['red']}; }}
    QPushButton[kind="danger"]:hover {{ background: {shade(c['red'], 0.85)}; }}
    QPushButton[kind="success"] {{ background: {c['green']}; }}
    QPushButton[kind="success"]:hover {{ background: {shade(c['green'], 0.85)}; }}
    QPushButton[kind="link"] {{ background: transparent; color: {c['text']}; font-weight: 500;
                                padding: 6px 8px; }}
    QPushButton[kind="link"]:hover {{ text-decoration: underline; }}
    QPushButton[kind="small"] {{ background: {c['active']}; color: {c['header']}; padding: 5px 10px;
                                 font-weight: 600; }}
    QPushButton[kind="small"]:hover {{ background: {shade(c['active'], 1.15) if not T.light else '#c9ccd1'}; }}

    QMenu {{ background: {c['float']}; border: none; border-radius: 6px; padding: 6px; }}
    QMenu::item {{ padding: 7px 26px 7px 10px; border-radius: 3px; color: {c['text']}; }}
    QMenu::item:selected {{ background: {c['accent']}; color: white; }}
    QMenu::item:disabled {{ color: {c['muted']}; }}
    QMenu::separator {{ height: 1px; background: {c['divider']}; margin: 4px 6px; }}

    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px;
                            border: 2px solid {c['icon']}; background: transparent; }}
    QCheckBox::indicator:checked {{ border: 2px solid {c['accent']}; background: {c['accent']}; }}
    QCheckBox::indicator:disabled {{ border-color: {c['active']}; }}
    QCheckBox:disabled {{ color: {c['muted']}; }}
    QTableWidget {{ background: {c['card']}; border: none; border-radius: 8px; gridline-color: transparent; }}
    QTableWidget::item {{ padding: 4px 6px; }}
    QHeaderView::section {{ background: {c['side']}; color: {c['muted']}; border: none; padding: 6px;
                            font-weight: 600; }}

    QLabel[role="h1"] {{ color: {c['header']}; font-size: 18pt; font-weight: 700; }}
    QLabel[role="h2"] {{ color: {c['header']}; font-size: 13pt; font-weight: 700; }}
    QLabel[role="caption"] {{ color: {c['muted']}; font-size: 8pt; font-weight: 700; }}
    QLabel[role="muted"] {{ color: {c['muted']}; }}
    QLabel[role="hint"] {{ color: {c['muted']}; font-size: 9pt; }}
    QLabel[role="error"] {{ color: {c['red']}; }}
    QLabel[role="code"] {{ color: {c['header']}; }}
    """


def style_window(widget):
    """Dark native title bar on Windows 10/11 to match the theme."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi

        def colorref(hex_color):
            q = QColor(hex_color)
            return ctypes.c_int(q.red() | (q.green() << 8) | (q.blue() << 16))

        dark = ctypes.c_int(0 if T.light else 1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)            # immersive dark
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colorref(T.c["rail"])), 4)  # caption
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(colorref(T.c["header"])), 4)  # title text
    except (OSError, AttributeError):
        pass
