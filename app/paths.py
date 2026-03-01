from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    # This file lives in app/, so repo root is one level up.
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    d = repo_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def downloads_dir() -> Path:
    d = repo_root() / "download"
    d.mkdir(parents=True, exist_ok=True)
    return d


def thumbs_dir() -> Path:
    d = data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d
