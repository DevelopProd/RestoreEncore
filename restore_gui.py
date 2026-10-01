# -*- coding: utf-8 -*-
"""
Restore Encore — восстановление имён и расширений + поиск дубликатов MP3.
Компактный GUI 380x860, стиль Windows 11, тёмная/белая тема.
Также есть удаление одинаковых файлов (Mp3) только MD5 + ID3.
"""

import sys
import os
import shutil
import subprocess
import asyncio
import hashlib
from pathlib import Path
from collections import defaultdict

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QUrl
from PyQt6.QtGui import (
    QFont, QAction, QKeySequence, QColor, QIcon, QPixmap,
    QPainter, QBrush, QPen, QDesktopServices
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QListWidget,
    QListWidgetItem, QProgressBar, QFileDialog, QMessageBox,
    QTextEdit, QMenu, QTabWidget, QComboBox, QDialog,
    QDialogButtonBox, QToolButton
)

from detectors import detect_type, suggest_name
from icon import make_icon


# ============================================================
#  Константы
# ============================================================

GITHUB_URL = "https://github.com/DevelopProd/RestoreEncore"   # ← замени
APP_NAME = "Restore Encore"
APP_VERSION = "1.0.0"


# ============================================================
#  Утилиты
# ============================================================

def safe_name_local(s: str) -> str:
    for c in '<>:"/\\|?*\n\r\t':
        s = s.replace(c, "_")
    s = "".join(ch for ch in s if ch.isprintable())
    s = s.strip().strip(".")
    return s or "unknown"


def make_github_icon(size: int = 20) -> QIcon:
    """Программно рисуем значок GitHub (октакат)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor("#FFFFFF")))
    p.setPen(QPen(Qt.PenStyle.NoPen))
    # круг
    p.drawEllipse(0, 0, size, size)
    # «кошка» — упрощённая морда
    p.setBrush(QBrush(QColor("#202020")))
    cx, cy = size / 2, size / 2 + size * 0.05
    # голова
    p.drawEllipse(int(cx - size * 0.30), int(cy - size * 0.28),
                  int(size * 0.60), int(size * 0.55))
    # уши
    from PyQt6.QtGui import QPolygon
    from PyQt6.QtCore import QPoint
    left_ear = QPolygon([
        QPoint(int(cx - size * 0.32), int(cy - size * 0.20)),
        QPoint(int(cx - size * 0.18), int(cy - size * 0.42)),
        QPoint(int(cx - size * 0.05), int(cy - size * 0.22)),
    ])
    right_ear = QPolygon([
        QPoint(int(cx + size * 0.32), int(cy - size * 0.20)),
        QPoint(int(cx + size * 0.18), int(cy - size * 0.42)),
        QPoint(int(cx + size * 0.05), int(cy - size * 0.22)),
    ])
    p.drawPolygon(left_ear)
    p.drawPolygon(right_ear)
    # глаза
    p.setBrush(QBrush(QColor("#FFFFFF")))
    p.drawEllipse(int(cx - size * 0.16), int(cy - size * 0.10),
                  int(size * 0.10), int(size * 0.10))
    p.drawEllipse(int(cx + size * 0.06), int(cy - size * 0.10),
                  int(size * 0.10), int(size * 0.10))
    p.end()
    return QIcon(pix)


# ============================================================
#  Вкладка 1. Восстановление имён
# ============================================================

SHAZAM_OFFSETS = [60, 30, 90, 15, 0]


async def shazam_try_offset(shazam, file_path: str, offset: int):
    try:
        if offset == 0:
            result = await shazam.recognize(file_path)
        else:
            result = await shazam.recognize(file_path, offset=offset)
        if result.get("track"):
            t = result["track"]
            artist = t.get("subtitle", "").strip()
            title = t.get("title", "").strip()
            if title:
                return artist, title
    except Exception as e:
        err = str(e).lower()
        if "429" in err or "too many" in err:
            raise
    return None


async def shazam_recognize_multi(file_path: str, log_callback=None):
    from shazamio import Shazam
    shazam = Shazam()
    for offset in SHAZAM_OFFSETS:
        try:
            if log_callback:
                log_callback(f"    Shazam: offset={offset}с...")
            result = await shazam_try_offset(shazam, file_path, offset)
            if result:
                artist, title = result
                if log_callback:
                    log_callback(f"    ✓ offset={offset}с: {artist} - {title}")
                return artist, title, offset
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "too many" in err:
                if log_callback:
                    log_callback("    429 (rate limit), ждём 20с...")
                await asyncio.sleep(20)
                continue
            if log_callback:
                log_callback(f"    ошибка offset={offset}: {e}")
        await asyncio.sleep(3)
    return None


class RestoreWorker(QThread):
    progress = pyqtSignal(int, int)
    row = pyqtSignal(str, str, str, str)
    log = pyqtSignal(str)
    finished_ok = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, sources, dst, copy_mode, strip, use_shazam, delete_src):
        super().__init__()
        self.sources = sources
        self.dst = dst
        self.copy_mode = copy_mode
        self.strip = strip
        self.use_shazam = use_shazam
        self.delete_src = delete_src and copy_mode
        self._stop = False

    def stop(self):
        self._stop = True

    def _collect_files(self):
        result = []
        seen = set()
        for src in self.sources:
            try:
                if src.is_dir():
                    for f in sorted(src.iterdir()):
                        if f.is_file():
                            key = str(f.resolve())
                            if key not in seen:
                                seen.add(key)
                                result.append((f, src))
                elif src.is_file():
                    key = str(src.resolve())
                    if key not in seen:
                        seen.add(key)
                        result.append((src, src.parent))
            except (OSError, ValueError) as e:
                self.log.emit(f"  [!] Пропуск источника {src}: {e}")
        return result

    def run(self):
        try:
            if self.strip:
                for src in self.sources:
                    if src.is_dir():
                        try:
                            subprocess.run(
                                ["attrib", "-h", "-s", "-r", str(src / "*.*"), "/s", "/d"],
                                shell=False, capture_output=True, timeout=300
                            )
                        except Exception:
                            pass
                self.log.emit("Атрибуты сняты.")

            items = self._collect_files()
            total = len(items)
            self.progress.emit(0, total)
            self.log.emit(f"К обработке: {total} файлов")
            if self.delete_src:
                self.log.emit("Режим: удалять оригиналы после восстановления.")

            stats = {"total": total, "ok": 0, "named": 0, "generic": 0,
                     "unknown": 0, "shazam": 0, "deleted": 0, "broken": 0}
            index_lines = ["Новое имя | Исходник | Тип | Источник имени"]
            counters = {}

            for i, (f, base_dir) in enumerate(items, 1):
                if self._stop:
                    self.log.emit("Остановлено.")
                    break

                try:
                    _ = f.name
                    _ = str(f)
                except (OSError, ValueError, UnicodeError) as e:
                    stats["broken"] += 1
                    stats["unknown"] += 1
                    self.log.emit(f"  [!!] Битое имя, пропуск: {e}")
                    self.progress.emit(i, total)
                    continue

                out_dir = self.dst if self.copy_mode else f.parent
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                except (OSError, ValueError) as e:
                    self.log.emit(f"  [!!] Не могу создать {out_dir}: {e}")
                    self.progress.emit(i, total)
                    continue

                try:
                    ftype = detect_type(f)
                except (OSError, ValueError, UnicodeError) as e:
                    stats["broken"] += 1
                    stats["unknown"] += 1
                    self.log.emit(f"  [!!] Ошибка чтения {f.name}: {e}")
                    self.progress.emit(i, total)
                    continue

                if ftype == "unknown":
                    stats["unknown"] += 1
                    self.log.emit(f"  [--] {f.name}")
                    self.progress.emit(i, total)
                    continue

                base = None
                ext = ""
                try:
                    base, ext = suggest_name(f, ftype)
                except (OSError, ValueError, UnicodeError) as e:
                    self.log.emit(f"  [!!] Метаданные {f.name}: {e}")

                if not ext:
                    stats["unknown"] += 1
                    self.progress.emit(i, total)
                    continue

                name_src = ""

                if base is None and ftype in ("mp3", "mp3_id3", "flac", "ogg") and self.use_shazam:
                    self.log.emit(f"  [??] {f.name} — нет тегов, Shazam...")
                    try:
                        result = asyncio.run(
                            shazam_recognize_multi(str(f), log_callback=self.log.emit)
                        )
                        if result:
                            artist, title, offset = result
                            base = safe_name_local(f"{artist} - {title}" if artist else title)
                            name_src = "Shazam"
                            stats["shazam"] += 1
                        else:
                            self.log.emit("    ✗ не найдено ни на одном offset")
                    except Exception as e:
                        self.log.emit(f"  [!!] Shazam: {e}")

                counters[ext] = counters.get(ext, 0) + 1

                if base:
                    base = safe_name_local(base)
                    if base and base != "unknown":
                        new_name = f"{base}.{ext}"
                        if not name_src:
                            name_src = "метаданные"
                            stats["named"] += 1
                    else:
                        new_name = f"{ext}_{counters[ext]:05d}.{ext}"
                        name_src = "по номеру"
                        stats["generic"] += 1
                else:
                    new_name = f"{ext}_{counters[ext]:05d}.{ext}"
                    name_src = "по номеру"
                    stats["generic"] += 1

                try:
                    if f.name == new_name and f.parent == out_dir:
                        self.log.emit(f"  [=] {f.name} — уже так называется")
                        stats["ok"] += 1
                        self.progress.emit(i, total)
                        continue
                except (OSError, ValueError, UnicodeError):
                    pass

                try:
                    target = self._unique(out_dir, new_name, exclude=f)
                except (OSError, ValueError, UnicodeError) as e:
                    stats["broken"] += 1
                    stats["unknown"] += 1
                    self.log.emit(f"  [!!] Битое имя для {f.name}: {e}")
                    self.progress.emit(i, total)
                    continue

                try:
                    if self.copy_mode:
                        shutil.copy2(f, target)
                    else:
                        if f.parent == out_dir:
                            f.rename(target)
                        else:
                            shutil.move(str(f), str(target))
                except (OSError, ValueError, UnicodeError) as e:
                    stats["broken"] += 1
                    stats["unknown"] += 1
                    self.log.emit(f"  [!!] {f.name}: {e}")
                    self.progress.emit(i, total)
                    continue

                stats["ok"] += 1

                if self.delete_src:
                    try:
                        if f.exists():
                            os.remove(f)
                            stats["deleted"] += 1
                    except (OSError, ValueError, UnicodeError) as e:
                        self.log.emit(f"  [!!] не удалось удалить {f.name}: {e}")

                self.row.emit(f.name, target.name, ext.upper(), name_src)
                index_lines.append(f"{target.name} | {f.name} | {ext} | {name_src}")
                self.progress.emit(i, total)

            if self.copy_mode:
                try:
                    (self.dst / "index.txt").write_text(
                        "\n".join(index_lines), encoding="utf-8"
                    )
                    self.log.emit("Отчёт: index.txt")
                except Exception:
                    pass

            self.finished_ok.emit(stats)
        except Exception as e:
            self.error.emit(str(e))

    @staticmethod
    def _unique(folder, name, exclude=None):
        target = folder / name
        try:
            if exclude and target.resolve() == exclude.resolve():
                return target
        except (OSError, ValueError):
            pass
        if not target.exists():
            return target
        stem, suf = target.stem, target.suffix
        i = 1
        while True:
            cand = folder / f"{stem} ({i}){suf}"
            try:
                if exclude and cand.resolve() == exclude.resolve():
                    return cand
            except (OSError, ValueError):
                pass
            if not cand.exists():
                return cand
            i += 1


# ============================================================
#  Вкладка 2. Дубликаты MP3 (MD5 + ID3, без fpcalc)
# ============================================================

def file_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """MD5 всего файла. Читаем кусками."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def tag_key(path: Path) -> str:
    """Нормализованный ключ по ID3: artist|title|album."""
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError
    except ImportError:
        return ""
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return ""
    except Exception:
        return ""

    def g(k):
        v = tags.get(k)
        return str(v).strip().lower() if v else ""

    artist = g("TPE1")
    title = g("TIT2")
    album = g("TALB")
    if title:
        return f"{artist}|{title}|{album}"
    return ""


def get_mp3_info(path: Path):
    """Длительность и битрейт — чтобы выбрать лучший файл из группы."""
    try:
        from mutagen.mp3 import MP3
        audio = MP3(path)
        if audio.info:
            return int(audio.info.length), int(audio.info.bitrate / 1000)
    except Exception:
        pass
    return 0, 0


def fmt_dur(sec: int) -> str:
    if not sec:
        return "--:--"
    return f"{sec // 60:02d}:{sec % 60:02d}"


def fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} Б"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} КБ"
    return f"{b / 1024 / 1024:.1f} МБ"


class DupWorker(QThread):
    progress = pyqtSignal(int, int)
    group_found = pyqtSignal(int, list)   # (номер группы, [(path, size, dur, br, is_keeper)])
    log = pyqtSignal(str)
    finished_ok = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, sources, action_mode):
        """
        action_mode: 'preview' | 'move' | 'delete'
        """
        super().__init__()
        self.sources = sources
        self.action_mode = action_mode
        self._stop = False

    def stop(self):
        self._stop = True

    def _collect_mp3(self):
        files = []
        seen = set()
        for src in self.sources:
            try:
                if src.is_dir():
                    for f in src.rglob("*"):
                        if f.is_file():
                            try:
                                k = str(f.resolve())
                            except Exception:
                                continue
                            if k in seen:
                                continue
                            seen.add(k)
                            if detect_type(f) in ("mp3", "mp3_id3"):
                                files.append(f)
                elif src.is_file():
                    try:
                        k = str(src.resolve())
                    except Exception:
                        continue
                    if k not in seen:
                        seen.add(k)
                        if detect_type(src) in ("mp3", "mp3_id3"):
                            files.append(src)
            except Exception as e:
                self.log.emit(f"[!] {src}: {e}")
        return files

    def run(self):
        try:
            files = self._collect_mp3()
            total = len(files)
            self.progress.emit(0, total)
            self.log.emit(f"Найдено MP3: {total}")
            if total == 0:
                self.finished_ok.emit({"groups": 0, "duplicates": 0,
                                       "deleted": 0, "moved": 0, "errors": 0})
                return

            # 1. Группировка по MD5 всего файла — точные копии
            self.log.emit("Считаю хэши файлов...")
            hash_map = defaultdict(list)
            for i, f in enumerate(files, 1):
                if self._stop:
                    break
                h = file_hash(f)
                if h:
                    try:
                        size = f.stat().st_size
                    except Exception:
                        size = 0
                    dur, br = get_mp3_info(f)
                    hash_map[h].append((f, size, dur, br))
                self.progress.emit(i, total)

            # 2. Из оставшихся (уникальных по MD5) группируем по тегу
            self.log.emit("Считаю теги...")
            tag_map = defaultdict(list)
            for h, items in hash_map.items():
                if len(items) == 1:
                    f, size, dur, br = items[0]
                    key = tag_key(f)
                    if key:
                        tag_map[key].append((f, size, dur, br))

            # 3. Итоговые группы
            groups = {}
            for h, items in hash_map.items():
                if len(items) > 1:
                    groups[f"hash:{h[:8]}"] = ("точные копии", items)
            for k, items in tag_map.items():
                if len(items) > 1:
                    groups[f"tag:{k}"] = ("по тегу", items)

            self.log.emit(f"Групп дубликатов: {len(groups)}")

            stats = {"groups": 0, "duplicates": 0, "deleted": 0,
                     "moved": 0, "errors": 0}

            # 4. Обработка групп
            for gi, (key, (reason, items)) in enumerate(groups.items(), 1):
                if self._stop:
                    self.log.emit("Остановлено.")
                    break

                # «Хранитель»: макс битрейт → макс длительность → макс размер
                items_sorted = sorted(
                    items,
                    key=lambda x: (-x[3], -x[2], -x[1])
                )
                dupes = items_sorted[1:]

                group_info = []
                for idx, (p, size, dur, br) in enumerate(items_sorted):
                    group_info.append((str(p), size, dur, br, idx == 0))

                stats["groups"] += 1
                stats["duplicates"] += len(dupes)
                self.group_found.emit(gi, group_info)

                if self.action_mode != "preview":
                    for (p, size, dur, br) in dupes:
                        if self._stop:
                            break
                        try:
                            if self.action_mode == "move":
                                dup_dir = p.parent / "duplicates"
                                dup_dir.mkdir(parents=True, exist_ok=True)
                                target = dup_dir / p.name
                                if target.exists():
                                    suffix = hashlib.md5(str(p).encode()).hexdigest()[:6]
                                    target = dup_dir / f"{p.stem}_{suffix}{p.suffix}"
                                shutil.move(str(p), str(target))
                                stats["moved"] += 1
                            elif self.action_mode == "delete":
                                os.remove(p)
                                stats["deleted"] += 1
                        except Exception as e:
                            stats["errors"] += 1
                            self.log.emit(f"  [!!] {p.name}: {e}")

            self.finished_ok.emit(stats)
        except Exception as e:
            self.error.emit(str(e))


# ============================================================
#  Стили
# ============================================================

DARK_QSS = """
* {
    font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
    font-size: 9pt;
    color: #FFFFFF;
}
QMainWindow, QWidget { background: #202020; }
QMenuBar { background: #202020; border: none; padding: 2px; }
QMenuBar::item { padding: 4px 10px; background: transparent; border-radius: 4px; }
QMenuBar::item:selected { background: #2D2D2D; }
QMenu { background: #2B2B2B; border: 1px solid #3A3A3A; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 16px; border-radius: 4px; }
QMenu::item:selected { background: #0078D4; }
QMenu::separator { height: 1px; background: #3A3A3A; margin: 4px 8px; }
QTabWidget::pane { border: 1px solid #3A3A3A; border-radius: 6px; }
QTabBar::tab {
    background: #2A2A2A; color: #C0C0C0; padding: 6px 14px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #0078D4; color: #FFFFFF; }
QTabBar::tab:hover:!selected { background: #333333; }
QLineEdit {
    background: #2B2B2B; border: 1px solid #3A3A3A; border-radius: 6px;
    padding: 6px 10px; selection-background-color: #0078D4;
}
QLineEdit:hover { border-color: #4A4A4A; }
QLineEdit:focus { border: 1px solid #0078D4; background: #2E2E2E; }
QLineEdit:disabled { color: #6A6A6A; }
QComboBox {
    background: #2B2B2B; border: 1px solid #3A3A3A; border-radius: 6px;
    padding: 4px 8px; color: #FFFFFF;
}
QComboBox:hover { border-color: #4A4A4A; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #2B2B2B; border: 1px solid #3A3A3A;
    selection-background-color: #0078D4; color: #FFFFFF;
}
QPushButton {
    background: #2D2D2D; border: 1px solid #3A3A3A; border-radius: 6px;
    padding: 6px 12px; color: #FFFFFF;
}
QPushButton:hover { background: #333333; border-color: #4A4A4A; }
QPushButton:pressed { background: #262626; }
QPushButton:disabled { background: #262626; color: #6A6A6A; border-color: #2E2E2E; }
QPushButton#primary {
    background: #0078D4; border: 1px solid #0078D4; font-weight: 600;
}
QPushButton#primary:hover { background: #1084D8; }
QPushButton#primary:pressed { background: #006CBE; }
QPushButton#primary:disabled { background: #2A3A4A; color: #6A6A6A; border-color: #2A3A4A; }
QPushButton#danger {
    background: #C42B1C; border: 1px solid #C42B1C; color: #FFFFFF; font-weight: 600;
}
QPushButton#danger:hover { background: #D63B2C; }
QPushButton#subtle { background: transparent; border: 1px solid #3A3A3A; }
QCheckBox { spacing: 8px; padding: 2px 0; }
QCheckBox::indicator {
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #4A4A4A; background: #2B2B2B;
}
QCheckBox::indicator:hover { border-color: #6A6A6A; }
QCheckBox::indicator:checked { background: #0078D4; border-color: #0078D4; }
QCheckBox::indicator:disabled { border-color: #3A3A3A; background: #262626; }
QCheckBox:disabled { color: #5A5A5A; }
QProgressBar {
    background: #2B2B2B; border: 1px solid #3A3A3A; border-radius: 6px;
    text-align: center; height: 18px; font-size: 8pt; color: #E0E0E0;
}
QProgressBar::chunk { background: #0078D4; border-radius: 5px; }
QListWidget {
    background: #2B2B2B; border: 1px solid #3A3A3A; border-radius: 8px;
    padding: 2px; outline: none;
}
QListWidget::item { padding: 3px 6px; border-radius: 4px; margin: 1px 2px; }
QListWidget::item:hover { background: #333333; }
QListWidget::item:selected { background: #0078D4; color: #FFFFFF; }
QTextEdit {
    background: #1A1A1A; border: 1px solid #2E2E2E; border-radius: 8px;
    padding: 6px; color: #C8C8C8; selection-background-color: #0078D4;
}
QLabel { background: transparent; }
QLabel#section { color: #A0A0A0; font-size: 8pt; font-weight: 600; padding: 6px 2px 2px 2px; }
QLabel#status { color: #8A8A8A; font-size: 8pt; padding: 2px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #4A4A4A; border-radius: 4px; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: #6A6A6A; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar::horizontal { background: transparent; height: 8px; margin: 2px; }
QScrollBar::handle:horizontal { background: #4A4A4A; border-radius: 4px; min-width: 20px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""

LIGHT_QSS = """
* {
    font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
    font-size: 9pt;
    color: #1A1A1A;
}
QMainWindow, QWidget { background: #F3F3F3; }
QMenuBar { background: #F3F3F3; border: none; padding: 2px; }
QMenuBar::item { padding: 4px 10px; background: transparent; border-radius: 4px; }
QMenuBar::item:selected { background: #E5E5E5; }
QMenu { background: #FFFFFF; border: 1px solid #D0D0D0; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 16px; border-radius: 4px; }
QMenu::item:selected { background: #0078D4; color: #FFFFFF; }
QMenu::separator { height: 1px; background: #D0D0D0; margin: 4px 8px; }
QTabWidget::pane { border: 1px solid #C8C8C8; border-radius: 6px; }
QTabBar::tab {
    background: #E8E8E8; color: #333333; padding: 6px 14px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #0078D4; color: #FFFFFF; }
QTabBar::tab:hover:!selected { background: #DDDDDD; }
QLineEdit {
    background: #FFFFFF; border: 1px solid #C8C8C8; border-radius: 6px;
    padding: 6px 10px; selection-background-color: #0078D4;
}
QLineEdit:hover { border-color: #A0A0A0; }
QLineEdit:focus { border: 1px solid #0078D4; }
QLineEdit:disabled { color: #A0A0A0; background: #F0F0F0; }
QComboBox {
    background: #FFFFFF; border: 1px solid #C8C8C8; border-radius: 6px;
    padding: 4px 8px; color: #1A1A1A;
}
QComboBox:hover { border-color: #A0A0A0; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #FFFFFF; border: 1px solid #C8C8C8;
    selection-background-color: #0078D4; color: #FFFFFF;
}
QPushButton {
    background: #FFFFFF; border: 1px solid #C8C8C8; border-radius: 6px;
    padding: 6px 12px; color: #1A1A1A;
}
QPushButton:hover { background: #F5F5F5; border-color: #A0A0A0; }
QPushButton:pressed { background: #E8E8E8; }
QPushButton:disabled { background: #F0F0F0; color: #A0A0A0; border-color: #E0E0E0; }
QPushButton#primary {
    background: #0078D4; border: 1px solid #0078D4; color: #FFFFFF; font-weight: 600;
}
QPushButton#primary:hover { background: #1084D8; }
QPushButton#primary:pressed { background: #006CBE; }
QPushButton#primary:disabled { background: #B0C8E0; color: #FFFFFF; border-color: #B0C8E0; }
QPushButton#danger {
    background: #C42B1C; border: 1px solid #C42B1C; color: #FFFFFF; font-weight: 600;
}
QPushButton#danger:hover { background: #D63B2C; }
QPushButton#subtle { background: transparent; border: 1px solid #C8C8C8; }
QCheckBox { spacing: 8px; padding: 2px 0; }
QCheckBox::indicator {
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #A0A0A0; background: #FFFFFF;
}
QCheckBox::indicator:hover { border-color: #0078D4; }
QCheckBox::indicator:checked { background: #0078D4; border-color: #0078D4; }
QCheckBox::indicator:disabled { border-color: #D0D0D0; background: #F0F0F0; }
QCheckBox:disabled { color: #A0A0A0; }
QProgressBar {
    background: #FFFFFF; border: 1px solid #C8C8C8; border-radius: 6px;
    text-align: center; height: 18px; font-size: 8pt; color: #1A1A1A;
}
QProgressBar::chunk { background: #0078D4; border-radius: 5px; }
QListWidget {
    background: #FFFFFF; border: 1px solid #C8C8C8; border-radius: 8px;
    padding: 2px; outline: none;
}
QListWidget::item { padding: 3px 6px; border-radius: 4px; margin: 1px 2px; }
QListWidget::item:hover { background: #F0F0F0; }
QListWidget::item:selected { background: #0078D4; color: #FFFFFF; }
QTextEdit {
    background: #FAFAFA; border: 1px solid #D0D0D0; border-radius: 8px;
    padding: 6px; color: #333333; selection-background-color: #0078D4;
}
QLabel { background: transparent; }
QLabel#section { color: #666666; font-size: 8pt; font-weight: 600; padding: 6px 2px 2px 2px; }
QLabel#status { color: #777777; font-size: 8pt; padding: 2px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #C0C0C0; border-radius: 4px; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: #A0A0A0; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px; }
QScrollBar::handle:horizontal { background: #C0C0C0; border-radius: 4px; min-width: 20px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


# ============================================================
#  Список путей с drag&drop
# ============================================================

class DropListWidget(QListWidget):
    dropped = pyqtSignal(list)

    def __init__(self, accept_files=True, accept_dirs=True):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setFont(QFont("Segoe UI", 8))
        self.accept_files = accept_files
        self.accept_dirs = accept_dirs

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if not p.exists():
                continue
            if p.is_dir() and not self.accept_dirs:
                continue
            if p.is_file() and not self.accept_files:
                continue
            paths.append(str(p))
        if paths:
            self.dropped.emit(paths)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Delete:
            for item in self.selectedItems():
                self.takeItem(self.row(item))
        else:
            super().keyPressEvent(e)


# ============================================================
#  Вкладка: Восстановление
# ============================================================

class RestoreTab(QWidget):
    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.worker = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(10, 10, 10, 10)

        lbl1 = QLabel("ИСТОЧНИК (папки и/или файлы)")
        lbl1.setObjectName("section")
        root.addWidget(lbl1)

        self.src_list = DropListWidget()
        self.src_list.setFixedHeight(80)
        self.src_list.dropped.connect(self._add_paths)
        root.addWidget(self.src_list)

        h1 = QHBoxLayout()
        h1.setSpacing(6)

        b_folder = QPushButton("+ Папка")
        b_folder.clicked.connect(self.add_folder)
        h1.addWidget(b_folder)

        b_files = QPushButton("+ Файлы")
        b_files.clicked.connect(self.add_files)
        h1.addWidget(b_files)

        b_clear = QPushButton("Очистить")
        b_clear.setObjectName("subtle")
        b_clear.clicked.connect(lambda: self.src_list.clear())
        h1.addWidget(b_clear)

        root.addLayout(h1)

        lbl2 = QLabel("НАЗНАЧЕНИЕ")
        lbl2.setObjectName("section")
        root.addWidget(lbl2)

        self.dst_edit = QLineEdit()
        self.dst_edit.setPlaceholderText("По умолчанию: <папка_файла>/restored")
        root.addWidget(self.dst_edit)

        b3 = QPushButton("Обзор...")
        b3.clicked.connect(self.pick_dst)
        root.addWidget(b3)

        lbl3 = QLabel("ОПЦИИ")
        lbl3.setObjectName("section")
        root.addWidget(lbl3)

        self.chk_copy = QCheckBox("Копировать (оригиналы не трогать)")
        self.chk_copy.setChecked(True)
        self.chk_copy.stateChanged.connect(self.on_copy_changed)
        root.addWidget(self.chk_copy)

        self.chk_delete = QCheckBox("Удалять оригиналы после восстановления")
        self.chk_delete.setChecked(False)
        root.addWidget(self.chk_delete)

        self.chk_strip = QCheckBox("Снять атрибуты «скрытый/системный»")
        self.chk_strip.setChecked(True)
        root.addWidget(self.chk_strip)

        self.chk_shazam = QCheckBox("Распознавать без тегов через Shazam")
        self.chk_shazam.setChecked(True)
        root.addWidget(self.chk_shazam)

        h_btn = QHBoxLayout()
        h_btn.setSpacing(6)
        self.btn_start = QPushButton("▶  Запустить")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumHeight(34)
        self.btn_start.clicked.connect(self.start)
        h_btn.addWidget(self.btn_start, 3)

        self.btn_stop = QPushButton("■")
        self.btn_stop.setMinimumHeight(34)
        self.btn_stop.setFixedWidth(38)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        h_btn.addWidget(self.btn_stop)

        self.btn_open = QPushButton("📂")
        self.btn_open.setMinimumHeight(34)
        self.btn_open.setFixedWidth(38)
        self.btn_open.clicked.connect(self.open_result)
        h_btn.addWidget(self.btn_open)

        root.addLayout(h_btn)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v / %m")
        root.addWidget(self.progress)

        lbl4 = QLabel("РЕЗУЛЬТАТЫ")
        lbl4.setObjectName("section")
        root.addWidget(lbl4)

        self.list_view = QListWidget()
        self.list_view.setFont(QFont("Segoe UI", 8))
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._list_menu)
        root.addWidget(self.list_view, 3)

        lbl5 = QLabel("ЛОГ")
        lbl5.setObjectName("section")
        root.addWidget(lbl5)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 8))
        self.log_view.setFixedHeight(80)
        root.addWidget(self.log_view)

        self.status = QLabel("Готово.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.on_copy_changed()

    # --- работа со списком ---

    def _add_paths(self, paths):
        existing = {self.src_list.item(i).text() for i in range(self.src_list.count())}
        added = 0
        for p in paths:
            if p in existing:
                continue
            item = QListWidgetItem(p)
            item.setToolTip(p)
            self.src_list.addItem(item)
            existing.add(p)
            added += 1
        if added and not self.dst_edit.text().strip():
            first = Path(paths[0])
            base = first if first.is_dir() else first.parent
            self.dst_edit.setText(str(base / "restored"))

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Выберите папку")
        if d:
            self._add_paths([d])

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выберите файлы", "", "Все файлы (*.*)"
        )
        if files:
            self._add_paths(files)

    def on_copy_changed(self):
        if self.chk_copy.isChecked():
            self.dst_edit.setEnabled(True)
            self.chk_delete.setEnabled(True)
        else:
            self.dst_edit.setEnabled(False)
            self.dst_edit.setText("")
            self.chk_delete.setEnabled(False)

    def pick_dst(self):
        d = QFileDialog.getExistingDirectory(self, "Выберите папку назначения")
        if d:
            self.dst_edit.setText(d)

    def append_log(self, text):
        self.log_view.append(text)

    def add_row(self, src, dst, ftype, name_src):
        text = f"{src}  →  {dst}"
        item = QListWidgetItem(text)
        item.setToolTip(
            f"Исходник:  {src}\nНовое имя: {dst}\nТип:       {ftype}\nИсточник:  {name_src}"
        )
        dark = self.settings.value("dark", True, type=bool)
        if name_src in ("метаданные", "Shazam"):
            item.setForeground(QColor("#4EC97B") if dark else QColor("#1A7A3E"))
        else:
            item.setForeground(QColor("#8A8A8A") if dark else QColor("#888888"))
        self.list_view.addItem(item)
        self.list_view.scrollToBottom()

    def _list_menu(self, pos):
        item = self.list_view.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        act = menu.addAction("Скопировать")
        res = menu.exec(self.list_view.mapToGlobal(pos))
        if res == act:
            QApplication.clipboard().setText(item.toolTip())

    def open_result(self):
        if self.chk_copy.isChecked():
            out = self.dst_edit.text().strip()
            if out and Path(out).is_dir():
                self._open_in_explorer(out)
                return
        if self.src_list.count() > 0:
            first = Path(self.src_list.item(0).text())
            base = first if first.is_dir() else first.parent
            if base.is_dir():
                self._open_in_explorer(str(base))

    @staticmethod
    def _open_in_explorer(path):
        if sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def start(self):
        if self.src_list.count() == 0:
            QMessageBox.warning(self, "Ошибка", "Добавь хотя бы одну папку или файл.")
            return
        sources = [Path(self.src_list.item(i).text()) for i in range(self.src_list.count())]
        sources = [p for p in sources if p.exists()]
        if not sources:
            QMessageBox.warning(self, "Ошибка", "Ни один из путей не существует.")
            return

        copy_mode = self.chk_copy.isChecked()
        delete_src = self.chk_delete.isChecked() and copy_mode

        if delete_src:
            ans = QMessageBox.warning(
                self, "Внимание",
                "Оригиналы будут удалены после успешного восстановления.\n"
                "Отменить это будет невозможно.\n\nПродолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        if copy_mode:
            dst = self.dst_edit.text().strip()
            if not dst:
                first = sources[0]
                base = first if first.is_dir() else first.parent
                dst = str(base / "restored")
                self.dst_edit.setText(dst)
            Path(dst).mkdir(parents=True, exist_ok=True)
        else:
            dst = None

        self.list_view.clear()
        self.log_view.clear()
        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status.setText("Работаю...")

        self.worker = RestoreWorker(
            sources, Path(dst) if dst else Path("."),
            copy_mode, self.chk_strip.isChecked(),
            self.chk_shazam.isChecked(), delete_src
        )
        self.worker.progress.connect(self.on_progress)
        self.worker.row.connect(self.add_row)
        self.worker.log.connect(self.append_log)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.status.setText("Останавливаю...")

    def on_progress(self, cur, total):
        self.progress.setMaximum(total or 1)
        self.progress.setValue(cur)

    def on_finished(self, stats):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        deleted_part = f"  •  удалено: {stats['deleted']}" if stats.get("deleted") else ""
        broken_part = f"  •  битых: {stats['broken']}" if stats.get("broken") else ""
        self.status.setText(
            f"Всего: {stats['total']}  •  OK: {stats['ok']}  •  "
            f"метаданные: {stats['named']}  •  Shazam: {stats['shazam']}  •  "
            f"по номеру: {stats['generic']}  •  не опознано: {stats['unknown']}"
            f"{deleted_part}{broken_part}"
        )

    def on_error(self, msg):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status.setText("Ошибка.")
        QMessageBox.critical(self, "Ошибка", msg)


# ============================================================
#  Вкладка: Дубликаты
# ============================================================

class DupTab(QWidget):
    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.worker = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(10, 10, 10, 10)

        lbl1 = QLabel("ПАПКИ С MP3 (можно несколько)")
        lbl1.setObjectName("section")
        root.addWidget(lbl1)

        self.src_list = DropListWidget(accept_files=True, accept_dirs=True)
        self.src_list.setFixedHeight(70)
        self.src_list.dropped.connect(self._add_paths)
        root.addWidget(self.src_list)

        h1 = QHBoxLayout()
        h1.setSpacing(6)
        b_folder = QPushButton("+ Папка")
        b_folder.clicked.connect(self.add_folder)
        h1.addWidget(b_folder)
        b_files = QPushButton("+ Файлы")
        b_files.clicked.connect(self.add_files)
        h1.addWidget(b_files)
        b_clear = QPushButton("Очистить")
        b_clear.setObjectName("subtle")
        b_clear.clicked.connect(lambda: self.src_list.clear())
        h1.addWidget(b_clear)
        root.addLayout(h1)

        lbl2 = QLabel("ДЕЙСТВИЕ С ДУБЛИКАТАМИ")
        lbl2.setObjectName("section")
        root.addWidget(lbl2)

        self.action_combo = QComboBox()
        self.action_combo.addItem("Только показать (ничего не трогать)", "preview")
        self.action_combo.addItem("Переместить в подпапку duplicates/", "move")
        self.action_combo.addItem("Удалить безвозвратно", "delete")
        self.action_combo.setCurrentIndex(0)
        root.addWidget(self.action_combo)

        hint = QLabel(
            "Оставляем один файл из группы: с самым высоким битрейтом,\n"
            "затем — с большей длительностью, затем — с большим размером."
        )
        hint.setObjectName("status")
        hint.setWordWrap(True)
        root.addWidget(hint)

        h_btn = QHBoxLayout()
        h_btn.setSpacing(6)
        self.btn_start = QPushButton("▶  Найти дубликаты")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumHeight(34)
        self.btn_start.clicked.connect(self.start)
        h_btn.addWidget(self.btn_start, 3)

        self.btn_stop = QPushButton("■")
        self.btn_stop.setMinimumHeight(34)
        self.btn_stop.setFixedWidth(38)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        h_btn.addWidget(self.btn_stop)

        root.addLayout(h_btn)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v / %m")
        root.addWidget(self.progress)

        lbl3 = QLabel("ГРУППЫ ДУБЛИКАТОВ")
        lbl3.setObjectName("section")
        root.addWidget(lbl3)

        self.list_view = QListWidget()
        self.list_view.setFont(QFont("Consolas", 8))
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._list_menu)
        root.addWidget(self.list_view, 4)

        lbl4 = QLabel("ЛОГ")
        lbl4.setObjectName("section")
        root.addWidget(lbl4)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 8))
        self.log_view.setFixedHeight(70)
        root.addWidget(self.log_view)

        self.status = QLabel("Готово.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    def _add_paths(self, paths):
        existing = {self.src_list.item(i).text() for i in range(self.src_list.count())}
        for p in paths:
            if p in existing:
                continue
            item = QListWidgetItem(p)
            item.setToolTip(p)
            self.src_list.addItem(item)
            existing.add(p)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Выберите папку с MP3")
        if d:
            self._add_paths([d])

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выберите MP3", "", "Аудио (*.mp3 *.MP3);;Все файлы (*.*)"
        )
        if files:
            self._add_paths(files)

    def append_log(self, text):
        self.log_view.append(text)

    def add_group(self, gi, items):
        # заголовок группы
        header = QListWidgetItem(f"── Группа {gi} ──")
        header.setForeground(QColor("#0078D4"))
        header.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.list_view.addItem(header)

        for (path, size, dur, br, is_keeper) in items:
            marker = "★" if is_keeper else "×"
            name = Path(path).name
            text = f"  {marker}  {name}"
            sub = QListWidgetItem(text)
            sub.setToolTip(
                f"{path}\n"
                f"Размер: {fmt_size(size)}\n"
                f"Длительность: {fmt_dur(dur)}\n"
                f"Битрейт: {br} kbps\n"
                f"{'ОСТАВИТЬ' if is_keeper else 'дубликат'}"
            )
            dark = self.settings.value("dark", True, type=bool)
            if is_keeper:
                sub.setForeground(QColor("#4EC97B") if dark else QColor("#1A7A3E"))
            else:
                sub.setForeground(QColor("#E06A6A") if dark else QColor("#B02020"))
            sub.setData(Qt.ItemDataRole.UserRole, path)
            self.list_view.addItem(sub)

        self.list_view.scrollToBottom()

    def _list_menu(self, pos):
        item = self.list_view.itemAt(pos)
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        act_copy = menu.addAction("Скопировать путь")
        act_open = None
        if path:
            act_open = menu.addAction("Открыть в проводнике")
        res = menu.exec(self.list_view.mapToGlobal(pos))
        if res == act_copy:
            QApplication.clipboard().setText(path or item.text())
        elif act_open and res == act_open:
            folder = str(Path(path).parent)
            if sys.platform == "win32":
                subprocess.Popen(["explorer", folder])

    def start(self):
        if self.src_list.count() == 0:
            QMessageBox.warning(self, "Ошибка", "Добавь хотя бы одну папку или файл.")
            return
        sources = [Path(self.src_list.item(i).text()) for i in range(self.src_list.count())]
        sources = [p for p in sources if p.exists()]
        if not sources:
            QMessageBox.warning(self, "Ошибка", "Ни один из путей не существует.")
            return

        action = self.action_combo.currentData()
        if action in ("move", "delete"):
            warn = (
                "Файлы-дубликаты будут перемещены в подпапку «duplicates/»."
                if action == "move"
                else "Файлы-дубликаты будут УДАЛЕНЫ безвозвратно.\n"
                     "Восстановить их будет невозможно."
            )
            ans = QMessageBox.warning(
                self, "Подтверждение", warn + "\n\nПродолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        self.list_view.clear()
        self.log_view.clear()
        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status.setText("Сканирую...")

        self.worker = DupWorker(sources, action)
        self.worker.progress.connect(self.on_progress)
        self.worker.group_found.connect(self.add_group)
        self.worker.log.connect(self.append_log)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.status.setText("Останавливаю...")

    def on_progress(self, cur, total):
        self.progress.setMaximum(total or 1)
        self.progress.setValue(cur)

    def on_finished(self, stats):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        parts = [
            f"Групп: {stats['groups']}",
            f"дубликатов: {stats['duplicates']}",
        ]
        if stats.get("moved"):
            parts.append(f"перемещено: {stats['moved']}")
        if stats.get("deleted"):
            parts.append(f"удалено: {stats['deleted']}")
        if stats.get("errors"):
            parts.append(f"ошибок: {stats['errors']}")
        self.status.setText("  •  ".join(parts))

    def on_error(self, msg):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status.setText("Ошибка.")
        QMessageBox.critical(self, "Ошибка", msg)


# ============================================================
#  Диалог «О программе»
# ============================================================

class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("О программе")
        self.setFixedSize(360, 320)
        self.setWindowIcon(make_icon())

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(18, 18, 18, 18)

        # Логотип приложения
        logo = QLabel()
        logo.setPixmap(make_icon().pixmap(64, 64))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(logo)

        title = QLabel(APP_NAME)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        title.setFont(f)
        root.addWidget(title)

        ver = QLabel(f"Версия {APP_VERSION}")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setObjectName("status")
        root.addWidget(ver)

        desc = QLabel(
            "Восстановление имён и расширений файлов.\n"
            "Определение по сигнатуре, ID3 / EXIF / core.xml,\n"
            "распознавание аудио через Shazam.\n"
            "Отдельный режим поиска дубликатов MP3."
        )
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        root.addWidget(desc)

        root.addSpacing(4)

        # Подпись
        dev = QLabel("Разработчик: DevProduction (2026)")
        dev.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(dev)

        prog = QLabel("Программист: Deepseek")
        prog.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(prog)

        root.addStretch()

        # GitHub-ссылка с иконкой
        gh_btn = QToolButton()
        gh_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        gh_btn.setIcon(make_github_icon(20))
        gh_btn.setIconSize(gh_btn.iconSize())
        gh_btn.setText("  Открыть на GitHub")
        gh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        gh_btn.setStyleSheet("""
            QToolButton {
                background: transparent; border: 1px solid #3A3A3A;
                border-radius: 6px; padding: 6px 12px;
                color: #4EA3FF;
            }
            QToolButton:hover { background: rgba(78, 163, 255, 0.12); }
        """)
        gh_btn.clicked.connect(self._open_github)
        h = QHBoxLayout()
        h.addStretch()
        h.addWidget(gh_btn)
        h.addStretch()
        root.addLayout(h)

        # Кнопка Закрыть
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _open_github(self):
        QDesktopServices.openUrl(QUrl(GITHUB_URL))


# ============================================================
#  Главное окно
# ============================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setFixedSize(400, 860)
        self.settings = QSettings("DevProduction", "RestoreFiles")
        self.setWindowIcon(make_icon())
        self._build_menu()
        self._build_ui()
        self._restore_settings()

    def _build_menu(self):
        m = self.menuBar().addMenu("Меню")

        a1 = QAction("Выход", self)
        a1.setShortcut(QKeySequence("Ctrl+Q"))
        a1.triggered.connect(self.close)
        m.addAction(a1)

        m.addSeparator()

        self.act_theme = QAction("Тёмная тема", self, checkable=True)
        self.act_theme.setChecked(True)
        self.act_theme.triggered.connect(self._apply_theme)
        m.addAction(self.act_theme)

        m.addSeparator()

        a2 = QAction("О программе", self)
        a2.triggered.connect(self._about)
        m.addAction(a2)

    def _about(self):
        dlg = AboutDialog(self)
        dlg.exec()

    def _apply_theme(self):
        dark = self.act_theme.isChecked()
        self.settings.setValue("dark", dark)
        self.setStyleSheet(DARK_QSS if dark else LIGHT_QSS)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(0)
        root.setContentsMargins(6, 4, 6, 6)

        self.tabs = QTabWidget()
        self.restore_tab = RestoreTab(self.settings)
        self.dup_tab = DupTab(self.settings)
        self.tabs.addTab(self.restore_tab, "Восстановление")
        self.tabs.addTab(self.dup_tab, "Дубликаты MP3")
        root.addWidget(self.tabs)

        self._apply_theme()

    def _restore_settings(self):
        dark = self.settings.value("dark", True, type=bool)
        self.act_theme.setChecked(dark)
        self._apply_theme()

        last = self.settings.value("last_src", "", type=str)
        if last and Path(last).exists():
            self.restore_tab._add_paths([last])
            if Path(last).is_dir():
                self.restore_tab.dst_edit.setText(str(Path(last) / "restored"))

    def closeEvent(self, e):
        if self.restore_tab.src_list.count() > 0:
            self.settings.setValue(
                "last_src", self.restore_tab.src_list.item(0).text()
            )
        self.settings.setValue("dark", self.act_theme.isChecked())
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()