"""Custom date picker — a field that opens a clean calendar dialog.

Built from scratch (not QCalendarWidget) so it's crash-free and shows ONLY the
real days of the chosen month (no leading/trailing days from adjacent months).
Day counts come from Python's `calendar` module, so leap years etc. are correct.
Month and Year are dropdowns; chevrons step months. "All dates" clears the
filter; "Today" jumps to today.
"""

from __future__ import annotations

import calendar as _calendar
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QDate, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from .icons import icon_pixmap
from .theme import C

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


class DatePickerDialog(QDialog):
    def __init__(self, parent: QWidget, initial: Optional[QDate]) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.result_date: Optional[QDate] = initial
        self._cleared = False

        now = datetime.now()
        self._year = initial.year() if initial else now.year
        self._month = initial.month() if initial else now.month
        self._initial = initial
        self._day_buttons: dict[tuple[int, int], QPushButton] = {}
        self._build()
        self._rebuild()

    # --- build ---------------------------------------------------------------

    def _build(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background: {C.CARD}; border: 1px solid {C.BORDER_STRONG}; "
            f"border-radius: 12px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        # Header: ‹  [Month]  [Year]  ›
        hdr = QHBoxLayout()
        prev = self._nav("chevron_left", self._prev_month)
        nxt = self._nav("chevron_right", self._next_month)
        self.month_cb = QComboBox(); self.month_cb.addItems(MONTHS)
        self.month_cb.setCurrentIndex(self._month - 1)
        self.month_cb.currentIndexChanged.connect(self._on_combo)
        self.year_cb = QComboBox()
        self.year_cb.addItems([str(y) for y in range(2020, datetime.now().year + 6)])
        self.year_cb.setCurrentText(str(self._year))
        self.year_cb.currentIndexChanged.connect(self._on_combo)
        hdr.addWidget(prev); hdr.addStretch()
        hdr.addWidget(self.month_cb); hdr.addWidget(self.year_cb)
        hdr.addStretch(); hdr.addWidget(nxt)
        v.addLayout(hdr)

        # Grid: weekday row + 6 weeks
        grid = QGridLayout(); grid.setSpacing(2)
        for c, wd in enumerate(WEEKDAYS):
            lab = QLabel(wd); lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setStyleSheet(f"color: {C.MUTED}; font-weight: 700; font-size: 12px;")
            grid.addWidget(lab, 0, c)
        for r in range(6):
            for c in range(7):
                b = QPushButton()
                b.setFixedSize(QSize(44, 36))
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _=False, rr=r, cc=c: self._pick(rr, cc))
                self._day_buttons[(r, c)] = b
                grid.addWidget(b, r + 1, c)
        v.addLayout(grid)

        # Footer: All dates | Today
        ftr = QHBoxLayout()
        all_btn = QPushButton("All dates")
        all_btn.clicked.connect(self._all)
        today = QPushButton("Today")
        today.clicked.connect(self._today)
        ftr.addWidget(all_btn); ftr.addStretch(); ftr.addWidget(today)
        v.addLayout(ftr)

    def _nav(self, icon: str, slot) -> QPushButton:
        b = QPushButton()
        b.setIcon(QIcon(icon_pixmap(icon, C.OCEAN, 15)))
        b.setIconSize(QSize(15, 15))
        b.setFixedSize(30, 30)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet("QPushButton{border:none;background:transparent;border-radius:8px;}"
                        f"QPushButton:hover{{background:{C.OCEAN_TINT};}}")
        b.clicked.connect(slot)
        return b

    # --- navigation ----------------------------------------------------------

    def _on_combo(self) -> None:
        self._month = self.month_cb.currentIndex() + 1
        self._year = int(self.year_cb.currentText())
        self._rebuild()

    def _sync_combos(self) -> None:
        self.month_cb.blockSignals(True); self.year_cb.blockSignals(True)
        self.month_cb.setCurrentIndex(self._month - 1)
        self.year_cb.setCurrentText(str(self._year))
        self.month_cb.blockSignals(False); self.year_cb.blockSignals(False)

    def _prev_month(self) -> None:
        self._month -= 1
        if self._month < 1:
            self._month, self._year = 12, self._year - 1
        self._sync_combos(); self._rebuild()

    def _next_month(self) -> None:
        self._month += 1
        if self._month > 12:
            self._month, self._year = 1, self._year + 1
        self._sync_combos(); self._rebuild()

    def _today(self) -> None:
        n = datetime.now()
        self._year, self._month = n.year, n.month
        self._sync_combos(); self._rebuild()

    # --- grid ----------------------------------------------------------------

    def _rebuild(self) -> None:
        # Authoritative: 0 marks days outside this month → blank cell.
        weeks = _calendar.Calendar(firstweekday=6).monthdayscalendar(self._year, self._month)
        self._cell_day = {}
        for r in range(6):
            week = weeks[r] if r < len(weeks) else [0] * 7
            for c in range(7):
                day = week[c]
                b = self._day_buttons[(r, c)]
                if day == 0:
                    b.setText("")
                    b.setEnabled(False)
                    b.setStyleSheet("QPushButton{border:none;background:transparent;}")
                else:
                    b.setText(str(day))
                    b.setEnabled(True)
                    self._cell_day[(r, c)] = day
                    sel = (self._initial is not None and self._initial.year() == self._year
                           and self._initial.month() == self._month and self._initial.day() == day)
                    if sel:
                        b.setStyleSheet(
                            f"QPushButton{{border:none;border-radius:8px;background:{C.OCEAN};"
                            f"color:white;font-weight:700;}}")
                    else:
                        b.setStyleSheet(
                            f"QPushButton{{border:none;border-radius:8px;background:transparent;"
                            f"color:{C.TEXT};}}QPushButton:hover{{background:{C.OCEAN_TINT};}}")

    # --- results -------------------------------------------------------------

    def _pick(self, r: int, c: int) -> None:
        day = getattr(self, "_cell_day", {}).get((r, c))
        if not day:
            return
        self.result_date = QDate(self._year, self._month, day)
        self.accept()

    def _all(self) -> None:
        self.result_date = None
        self.accept()


class DateFilter(QPushButton):
    """A field that shows the selected date (or 'All dates') and opens the
    calendar dialog. `changed` fires when the selection changes."""

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._date: Optional[QDate] = None
        self.setFixedWidth(150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton{{background:{C.CARD};border:1px solid {C.BORDER_STRONG};"
            f"border-radius:9px;padding:8px 12px;text-align:left;color:{C.TEXT};font-weight:600;}}"
            f"QPushButton:hover{{border-color:{C.OCEAN_LIGHT};}}")
        self.clicked.connect(self._open)
        self._update_text()

    def date(self) -> Optional[QDate]:
        return self._date

    def _update_text(self) -> None:
        self.setText(self._date.toString("yyyy-MM-dd") if self._date else "All dates")

    def _open(self) -> None:
        dlg = DatePickerDialog(self, self._date)
        dlg.adjustSize()
        gp = self.mapToGlobal(self.rect().bottomLeft())
        dlg.move(gp.x(), gp.y() + 4)
        if dlg.exec():
            self._date = dlg.result_date
            self._update_text()
            self.changed.emit()
