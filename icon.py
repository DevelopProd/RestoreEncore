# -*- coding: utf-8 -*-
"""Программная генерация иконки — чтобы не таскать .ico с собой."""

from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QFont, QPen
from PyQt6.QtCore import Qt, QRect


def make_icon() -> QIcon:
    """Рисуем простую иконку: синий круг с нотой/стрелкой восстановления."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icon = QIcon()
    for s in sizes:
        pix = QPixmap(s, s)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # фон — круг с градиентом
        from PyQt6.QtGui import QRadialGradient
        grad = QRadialGradient(s * 0.35, s * 0.3, s * 0.9)
        grad.setColorAt(0.0, QColor(90, 160, 240))
        grad.setColorAt(1.0, QColor(30, 70, 140))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor(20, 50, 100), max(1, s // 32)))
        p.drawEllipse(1, 1, s - 2, s - 2)

        # символ ноты/стрелки
        p.setPen(QPen(QColor(255, 255, 255), max(1, s // 20)))
        f = QFont("Segoe UI Symbol", int(s * 0.55))
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRect(0, 0, s, s), Qt.AlignmentFlag.AlignCenter, "♫")

        p.end()
        icon.addPixmap(pix)
    return icon