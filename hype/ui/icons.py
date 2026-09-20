"""Load + tint the monochrome icon PNGs to any color (idle slate / active ocean)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap

from .resources import asset_path

_cache: dict[tuple[str, str, int], QPixmap] = {}


def icon_pixmap(name: str, color: str, size: int = 22) -> QPixmap:
    """Return a tinted, scaled pixmap for assets/icons/<name>.png."""
    key = (name, color, size)
    if key in _cache:
        return _cache[key]
    src = QPixmap(asset_path(f"icons/{name}.png"))
    if not src.isNull():
        src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(src.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawPixmap(0, 0, src)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(out.rect(), QColor(color))
    p.end()
    _cache[key] = out
    return out
