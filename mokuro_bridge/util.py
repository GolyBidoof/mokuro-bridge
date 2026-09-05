from __future__ import annotations
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default

def _truthy(value: str | None) -> bool:
    """Parse a form/env string as boolean; unset/empty → False."""
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _ensure_python_deps(module_names, requirements_file: str) -> bool:
    """Make sure optional provider Python deps are installed before auth.

    module_names: importable top-level modules to check (e.g. ["requests"]).
    requirements_file: repo-relative requirements file to install from.

    Returns True when the deps are available. If any is missing, offers to
    install them right here (`pip install -r <file>`) — the user confirms
    once — and returns whether they're then available. Never installs without
    consent.
    """
    missing = [m for m in module_names if importlib.util.find_spec(m) is None]
    if not missing:
        return True
    print(
        f"mokuro-bridge needs: {', '.join(missing)}\n"
        f"Install with:  pip install -r {requirements_file}"
    )
    try:
        answer = input("Install now? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        return False
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", requirements_file]
        )
    except subprocess.CalledProcessError:
        print(
            f"error: 'pip install -r {requirements_file}' failed. "
            "Install it manually and re-run."
        )
        return False
    return all(importlib.util.find_spec(m) is not None for m in module_names)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip().strip(".")[:200]

_VOLUME_MARKERS = [
    # （１） (1) （上） etc.
    re.compile(r"\s*[（(]\s*[0-9０-９一二三四五六七八九十百上下中全]+\s*[）)]\s*$"),
    # 1巻 第2巻 １２巻
    re.compile(r"\s*第?\s*[0-9０-９一二三四五六七八九十百]+\s*巻\s*$"),
    # Vol.1 vol 2
    re.compile(r"\s*[Vv]ol\.?\s*[0-9０-９]+\s*$"),
    # trailing volume number after space: "…　6" / "… 3"
    re.compile(r"[\s　]+[0-9０-９]{1,3}\s*$"),
]

def series_title_from_volume(title: str) -> str:
    """
    Derive a shared series folder name from a volume title.

    推しが武道館いってくれたら死ぬ（２）【電子限定特典ペーパー付き】
      → 推しが武道館いってくれたら死ぬ
    メダリスト 1巻 → メダリスト
    「おかえり、パパ」【電子単行本】　6 → 「おかえり、パパ」
    """
    s = (title or "").strip()
    # Edition/bonus tags are volume-specific — drop them for the series folder.
    s = re.sub(r"[【〔\[][^】〕\]]*[】〕\]]", "", s)
    s = re.sub(r"[\s　]+", " ", s).strip()
    for pat in _VOLUME_MARKERS:
        nxt = pat.sub("", s).strip()
        if nxt and nxt != s:
            s = nxt
            break
    s = re.sub(r"[\s　]+", " ", s).strip(" -–—_|")
    return sanitize_filename(s) or sanitize_filename(title) or "manga"
