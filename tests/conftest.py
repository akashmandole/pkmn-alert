"""Shared test fixtures. Makes ``src/`` importable without an editable install."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()
