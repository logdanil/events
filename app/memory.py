"""Контроль памяти процесса.

После скана Python освобождает объекты, но glibc не возвращает освобождённые
страницы ОС (фрагментация арен при разборе страниц в потоках) — RSS «залипает»
на пике. malloc_trim(0) принудительно отдаёт их; на не-glibc (macOS) это no-op.
"""
from __future__ import annotations

import ctypes
import gc
import logging
import resource
import sys

log = logging.getLogger(__name__)

_libc = None


def _malloc_trim() -> bool:
    global _libc
    if not sys.platform.startswith("linux"):
        return False
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        return bool(_libc.malloc_trim(0))
    except (OSError, AttributeError):
        return False


def rss_mb() -> float:
    """Текущий RSS процесса в мегабайтах (Linux отдаёт КБ, macOS — байты)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def release(reason: str = "") -> float:
    """Сборка мусора + возврат памяти ОС; возвращает RSS после."""
    gc.collect()
    _malloc_trim()
    mb = rss_mb()
    log.info("Память%s: RSS %.0f МБ", f" после {reason}" if reason else "", mb)
    return mb
