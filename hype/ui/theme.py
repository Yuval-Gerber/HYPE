"""Ocean-blue light theme — palette + global stylesheet.

A single source of truth for colors and the Qt stylesheet so every widget looks
consistent. Light background, ocean-blue accents, green/red for P&L, rounded
cards. Used by app.py via apply_theme().
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication

from .resources import asset_path


class C:
    """Palette."""

    OCEAN = "#0277BD"          # primary ocean blue
    OCEAN_DARK = "#015E96"
    OCEAN_DEEP = "#013E63"
    OCEAN_LIGHT = "#4FA3D1"
    OCEAN_TINT = "#E3F2FB"     # very light blue wash

    BG = "#F4F7FA"             # app background
    CARD = "#FFFFFF"
    CARD_ALT = "#FAFCFE"
    BORDER = "#E2E8F0"
    BORDER_STRONG = "#CBD5E1"

    TEXT = "#16212B"
    MUTED = "#64748B"
    FAINT = "#94A3B8"

    GREEN = "#15A66B"          # profit
    GREEN_BG = "#E6F7F0"
    RED = "#E5484D"            # loss
    RED_BG = "#FCEBEC"
    AMBER = "#E8A317"
    AMBER_BG = "#FCF3DD"

    WHITE = "#FFFFFF"


def apply_theme(app: QApplication) -> None:
    """Apply the global font, light palette, and stylesheet.

    Forces the Fusion style + a light palette so the app stays light even when
    macOS is in dark mode (otherwise unstyled widget backgrounds render dark),
    and so spinbox sub-controls can be styled by the stylesheet.
    """
    app.setStyle("Fusion")
    app.setPalette(_light_palette())
    app.setFont(QFont("SF Pro Display", 13) if _has_font("SF Pro Display")
                else QFont("Helvetica Neue", 13))
    app.setStyleSheet(stylesheet())


def _light_palette() -> QPalette:
    c = C
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(c.BG))
    p.setColor(QPalette.ColorRole.WindowText, QColor(c.TEXT))
    p.setColor(QPalette.ColorRole.Base, QColor(c.CARD))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(c.CARD_ALT))
    p.setColor(QPalette.ColorRole.Text, QColor(c.TEXT))
    p.setColor(QPalette.ColorRole.Button, QColor(c.CARD))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(c.TEXT))
    p.setColor(QPalette.ColorRole.Highlight, QColor(c.OCEAN))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(c.OCEAN_DEEP))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor("white"))
    return p


def _has_font(name: str) -> bool:
    from PyQt6.QtGui import QFontDatabase
    return name in QFontDatabase.families()


def stylesheet() -> str:
    c = C
    _chev_up = asset_path("icons/chevron_up.png").replace("\\", "/")
    _chev_down = asset_path("icons/chevron_down.png").replace("\\", "/")
    return f"""
    * {{
        color: {c.TEXT};
        font-family: "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
    }}
    QMainWindow, QWidget#root, QWidget#loginRoot {{
        background: {c.BG};
    }}

    /* ---- Cards ---- */
    QFrame#card {{
        background: {c.CARD};
        border: 1px solid {c.BORDER};
        border-radius: 14px;
    }}
    QFrame#statTile {{
        background: {c.CARD};
        border: 1px solid {c.BORDER};
        border-radius: 12px;
    }}

    /* ---- Buttons ---- */
    QPushButton {{
        background: {c.CARD};
        border: 1px solid {c.BORDER_STRONG};
        border-radius: 9px;
        padding: 8px 16px;
        color: {c.TEXT};
        font-weight: 600;
    }}
    QPushButton:hover {{ background: {c.OCEAN_TINT}; border-color: {c.OCEAN_LIGHT}; }}
    QPushButton:pressed {{ background: {c.OCEAN_TINT}; }}
    QPushButton:disabled {{ color: {c.FAINT}; background: {c.CARD_ALT}; border-color: {c.BORDER}; }}

    QPushButton#primary {{
        background: {c.OCEAN}; color: white; border: none; padding: 10px 18px;
    }}
    QPushButton#primary:hover {{ background: {c.OCEAN_DARK}; }}
    QPushButton#primary:pressed {{ background: {c.OCEAN_DEEP}; }}

    QPushButton#danger {{ background: {c.RED}; color: white; border: none; }}
    QPushButton#danger:hover {{ background: #C93B40; }}
    QPushButton#ghost {{ background: transparent; border: none; color: {c.MUTED}; }}
    QPushButton#ghost:hover {{ color: {c.OCEAN}; }}

    /* ---- Inputs ---- */
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {c.CARD}; border: 1px solid {c.BORDER_STRONG};
        border-radius: 9px; padding: 8px 12px; selection-background-color: {c.OCEAN_LIGHT};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1.5px solid {c.OCEAN};
    }}

    /* ---- Spinbox steppers (custom chevrons, not the chunky native arrows) ---- */
    QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border; subcontrol-position: top right;
        width: 22px; border: none; border-left: 1px solid {c.BORDER};
        border-top-right-radius: 9px; background: {c.CARD_ALT};
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border; subcontrol-position: bottom right;
        width: 22px; border: none; border-left: 1px solid {c.BORDER};
        border-bottom-right-radius: 9px; background: {c.CARD_ALT};
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {c.OCEAN_TINT}; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{_chev_up}"); width: 10px; height: 10px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{_chev_down}"); width: 10px; height: 10px; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: url("{_chev_down}"); width: 10px; height: 10px; }}
    QDateEdit::drop-down {{ border: none; width: 24px; }}
    QDateEdit::down-arrow {{ image: url("{_chev_down}"); width: 11px; height: 11px; }}

    /* ---- Tabs ---- */
    QTabWidget::pane {{ border: none; background: transparent; }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{
        background: transparent; color: {c.MUTED};
        padding: 10px 18px; margin-right: 4px; border: none;
        border-radius: 9px; font-weight: 600;
    }}
    QTabBar::tab:selected {{ background: {c.OCEAN_TINT}; color: {c.OCEAN_DARK}; }}
    QTabBar::tab:hover:!selected {{ color: {c.TEXT}; }}

    /* ---- Tables ---- */
    QHeaderView::section {{
        background: {c.CARD_ALT}; color: {c.MUTED}; border: none;
        border-bottom: 1px solid {c.BORDER}; padding: 8px 10px; font-weight: 600;
    }}
    QTableWidget, QTableView {{
        background: {c.CARD}; border: 1px solid {c.BORDER}; border-radius: 12px;
        gridline-color: {c.BORDER}; selection-background-color: {c.OCEAN_TINT};
        selection-color: {c.TEXT};
    }}
    QTableWidget::item {{ padding: 6px; }}

    /* ---- Scrollbars ---- */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c.BORDER_STRONG}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {c.FAINT}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

    QToolTip {{
        background: {c.OCEAN_DEEP}; color: white; border: none;
        padding: 7px 10px; border-radius: 6px; font-weight: 600;
    }}

    /* ---- Dialogs / message boxes (light, not system-dark) ---- */
    QDialog, QMessageBox {{ background: {c.BG}; }}
    QDialog QLabel, QMessageBox QLabel {{ color: {c.TEXT}; background: transparent; }}
    QMessageBox QPushButton {{ min-width: 80px; }}
    """
