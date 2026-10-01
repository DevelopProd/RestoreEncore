# -*- coding: utf-8 -*-
"""Определение типа файла по сигнатуре + извлечение имени из метаданных."""

import zipfile
import struct
from pathlib import Path
from xml.etree import ElementTree as ET


# ---------- Сигнатуры ----------

SIGNATURES = [
    # (имя, смещение, байты)
    ("mp3_id3", 0, b"ID3"),
    ("jpg",     0, b"\xFF\xD8\xFF"),
    ("png",     0, b"\x89PNG\r\n\x1a\n"),
    ("gif",     0, b"GIF87a"),
    ("gif",     0, b"GIF89a"),
    ("bmp",     0, b"BM"),
    ("webp",    0, b"RIFF"),         # + "WEBP" на 8
    ("pdf",     0, b"%PDF"),
    ("zip",     0, b"PK\x03\x04"),
    ("zip_empty", 0, b"PK\x05\x06"),
    ("rar",     0, b"Rar!\x1a\x07\x00"),
    ("rar5",    0, b"Rar!\x1a\x07\x01\x00"),
    ("7z",      0, b"7z\xBC\xAF\x27\x1C"),
    ("flac",    0, b"fLaC"),
    ("ogg",     0, b"OggS"),
    ("wav",     0, b"RIFF"),         # + "WAVE" на 8
    ("mp4",     4, b"ftyp"),
    ("avi",     0, b"RIFF"),         # + "AVI " на 8
    ("mkv",     0, b"\x1A\x45\xDF\xA3"),
    ("mov",     4, b"ftypqt"),
    ("exe",     0, b"MZ"),
    ("elf",     0, b"\x7fELF"),
]


def detect_type(path: Path) -> str:
    """Возвращает строковый тип файла или 'unknown'."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return "unknown"

    if len(head) < 4:
        return "unknown"

    # MP3 без ID3 — FF Ex/Fx
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"

    for name, off, sig in SIGNATURES:
        if head[off:off + len(sig)] == sig:
            # уточнения по RIFF
            if name in ("webp", "wav", "avi"):
                if len(head) < 12:
                    continue
                sub = head[8:12]
                if name == "webp" and sub == b"WEBP":
                    return "webp"
                if name == "wav" and sub == b"WAVE":
                    return "wav"
                if name == "avi" and sub == b"AVI ":
                    return "avi"
                continue
            # уточнения по ftyp
            if name == "mp4":
                sub = head[8:12]
                if sub == b"qt  ":
                    return "mov"
                return "mp4"
            if name == "mov" and head[4:8] == b"ftyp":
                sub = head[8:12]
                if sub == b"qt  ":
                    return "mov"
                continue
            return name
    return "unknown"


# ---------- Извлечение имён ----------

def safe_name(s: str) -> str:
    for c in '<>:"/\\|?*\n\r\t':
        s = s.replace(c, "_")
    # удаляем все непечатаемые символы (включая \x00)
    s = "".join(ch for ch in s if ch.isprintable())
    s = s.strip().strip(".")
    return s or "unknown"


# --- MP3 / FLAC / OGG ---

def name_from_mp3(path: Path):
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError
    except ImportError:
        return None
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return None
    except Exception:
        return None

    def g(k):
        v = tags.get(k)
        return str(v).strip() if v else ""

    artist, title, track = g("TPE1"), g("TIT2"), g("TRCK")
    if not title:
        return None
    num = ""
    if track:
        first = track.split("/")[0].strip()
        if first.isdigit():
            num = f"{int(first):02d} "
    base = f"{num}{artist} - {title}" if artist else f"{num}{title}"
    return safe_name(base)


def name_from_flac(path: Path):
    try:
        from mutagen.flac import FLAC
        f = FLAC(path)
    except Exception:
        return None
    artist = (f.get("artist") or [""])[0].strip()
    title = (f.get("title") or [""])[0].strip()
    track = (f.get("tracknumber") or [""])[0].strip()
    if not title:
        return None
    num = ""
    if track.isdigit():
        num = f"{int(track):02d} "
    base = f"{num}{artist} - {title}" if artist else f"{num}{title}"
    return safe_name(base)


def name_from_ogg(path: Path):
    try:
        from mutagen.oggvorbis import OggVorbis
        f = OggVorbis(path)
    except Exception:
        return None
    artist = (f.get("artist") or [""])[0].strip()
    title = (f.get("title") or [""])[0].strip()
    track = (f.get("tracknumber") or [""])[0].strip()
    if not title:
        return None
    num = ""
    if track.isdigit():
        num = f"{int(track):02d} "
    base = f"{num}{artist} - {title}" if artist else f"{num}{title}"
    return safe_name(base)


# --- JPG / EXIF ---

def name_from_jpg(path: Path):
    """Имя из даты съёмки, если есть EXIF."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except ImportError:
        return None
    try:
        img = Image.open(path)
        exif = img._getexif() if hasattr(img, "_getexif") else None
        if not exif:
            return None
        data = {TAGS.get(k, k): v for k, v in exif.items()}
        dt = data.get("DateTimeOriginal") or data.get("DateTime")
        if not dt:
            return None
        # "2023:05:12 18:34:56" -> "2023-05-12_18-34-56"
        s = str(dt).replace(":", "-", 2).replace(":", "-").replace(" ", "_")
        return safe_name(f"IMG_{s}")
    except Exception:
        return None


# --- DOCX / XLSX / PPTX ---

def _core_props(zip_path: Path):
    try:
        with zipfile.ZipFile(zip_path) as z:
            for candidate in ("docProps/core.xml", "docProps\\core.xml"):
                if candidate in z.namelist():
                    with z.open(candidate) as f:
                        xml = f.read()
                    break
            else:
                return None
        root = ET.fromstring(xml)
        ns = {
            "dc": "http://purl.org/dc/elements/1.1/",
            "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
            "dcterms": "http://purl.org/dc/terms/",
        }
        title = (root.findtext("dc:title", namespaces=ns) or "").strip()
        creator = (root.findtext("dc:creator", namespaces=ns) or "").strip()
        modified = (root.findtext("dcterms:modified", namespaces=ns) or "").strip()
        return title, creator, modified
    except Exception:
        return None


def name_from_ooxml(path: Path, prefix: str):
    props = _core_props(path)
    if not props:
        return None
    title, creator, modified = props
    if title:
        base = f"{creator} - {title}" if creator else title
        return safe_name(f"{prefix}_{base}")
    if modified:
        base = modified.replace(":", "-").replace("T", "_").split(".")[0]
        return safe_name(f"{prefix}_{base}")
    return None


# ---------- Общий вызов ----------

def suggest_name(path: Path, ftype: str):
    """Возвращает (базовое_имя_без_расширения, расширение) или (None, ext)."""
    ext_map = {
        "mp3": "mp3", "mp3_id3": "mp3",
        "jpg": "jpg", "png": "png", "gif": "gif", "bmp": "bmp", "webp": "webp",
        "pdf": "pdf", "zip": "zip", "zip_empty": "zip",
        "rar": "rar", "rar5": "rar", "7z": "7z",
        "flac": "flac", "ogg": "ogg", "wav": "wav",
        "mp4": "mp4", "avi": "avi", "mkv": "mkv", "mov": "mov",
        "exe": "exe", "elf": "", "unknown": "",
    }

    if ftype in ("mp3", "mp3_id3"):
        n = name_from_mp3(path)
        return (n, "mp3") if n else (None, "mp3")
    if ftype == "flac":
        n = name_from_flac(path)
        return (n, "flac") if n else (None, "flac")
    if ftype == "ogg":
        n = name_from_ogg(path)
        return (n, "ogg") if n else (None, "ogg")
    if ftype == "jpg":
        n = name_from_jpg(path)
        return (n, "jpg") if n else (None, "jpg")

    if ftype == "zip":
        # попробуем распознать OOXML
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                if "word/document.xml" in names:
                    n = name_from_ooxml(path, "DOC")
                    return (n, "docx") if n else (None, "docx")
                if "xl/workbook.xml" in names:
                    n = name_from_ooxml(path, "XLS")
                    return (n, "xlsx") if n else (None, "xlsx")
                if "ppt/presentation.xml" in names:
                    n = name_from_ooxml(path, "PPT")
                    return (n, "pptx") if n else (None, "pptx")
        except Exception:
            pass
        return (None, "zip")

    ext = ext_map.get(ftype, "")
    return (None, ext)