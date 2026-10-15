"""Pin the upstream files that define `series.json`.

Nothing upstream is vendored (mokuro-reader is GPL-3.0, this project is MIT):
`data/series_json_pin.json` records repo, commit and per-file SHA-256, and
`--check-series-reference` re-fetches them. `--refresh-series-reference` re-pins.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from . import APP_NAME, __version__

PIN_PATH = Path(__file__).with_name("data") / "series_json_pin.json"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{commit}/{path}"
COMMITS_URL = "https://api.github.com/repos/{repo}/commits/{ref}"
USER_AGENT = f"{APP_NAME}/{__version__}"
DEFAULT_TIMEOUT_S = 15.0

REQUIRED_ROLES = (
    "generator",
    "sanitize",
    "identity",
    "entry-measurement",
    "writer",
    "char-counts",
    "ordering",
    "spec",
)

_CA_FALLBACKS = (
    "/etc/ssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
)


class ReferenceError(RuntimeError):
    pass


def load_pin() -> dict:
    try:
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    except OSError as error:
        raise ReferenceError(f"cannot read {PIN_PATH}: {error}") from error
    except ValueError as error:
        raise ReferenceError(f"{PIN_PATH} is not valid JSON: {error}") from error
    if not isinstance(pin, dict):
        raise ReferenceError(f"{PIN_PATH} is not an object")
    return pin


def pinned_files(pin: Optional[dict] = None) -> list[dict]:
    pin = pin if pin is not None else load_pin()
    return [
        {
            "repo": upstream.get("repo"),
            "license": upstream.get("license"),
            "commit": upstream.get("commit"),
            "path": entry.get("path"),
            "role": entry.get("role"),
            "sha256": entry.get("sha256"),
            "bytes": entry.get("bytes"),
        }
        for upstream in pin.get("upstream", [])
        for entry in upstream.get("files", [])
    ]


def verify(pin: Optional[dict] = None) -> list[str]:
    """Offline shape check. Returns the problems, empty when usable."""
    try:
        pin = pin if pin is not None else load_pin()
    except ReferenceError as error:
        return [str(error)]
    problems: list[str] = []
    if not isinstance(pin.get("upstream"), list) or not pin["upstream"]:
        problems.append("pin has no 'upstream' entries")
    seen_roles: set[str] = set()
    for upstream in pin.get("upstream", []):
        repo = upstream.get("repo")
        commit = upstream.get("commit")
        if not isinstance(repo, str) or "/" not in repo:
            problems.append(f"bad repo in pin: {repo!r}")
        if not isinstance(commit, str) or len(commit) != 40:
            problems.append(f"{repo}: commit is not a 40-character sha: {commit!r}")
        if not upstream.get("license"):
            problems.append(f"{repo}: no license recorded")
        files = upstream.get("files")
        if not isinstance(files, list) or not files:
            problems.append(f"{repo}: no files pinned")
            continue
        for entry in files:
            path = entry.get("path")
            digest = entry.get("sha256")
            if not isinstance(path, str) or not path:
                problems.append(f"{repo}: file entry without a path")
                continue
            if not isinstance(digest, str) or len(digest) != 64:
                problems.append(f"{repo}: {path}: sha256 is not a 64-character hex digest")
            elif any(char not in "0123456789abcdef" for char in digest):
                problems.append(f"{repo}: {path}: sha256 is not lowercase hex")
            role = entry.get("role")
            if not isinstance(role, str) or not role:
                problems.append(f"{repo}: {path}: no role recorded")
            else:
                seen_roles.add(role)
    missing = [role for role in REQUIRED_ROLES if role not in seen_roles]
    if missing:
        problems.append(f"pin does not cover the required roles: {', '.join(missing)}")
    return problems


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    try:
        if context.cert_store_stats().get("x509_ca", 0) > 0:
            return context
    except Exception:
        return context
    for candidate in _CA_FALLBACKS:
        if Path(candidate).is_file():
            try:
                return ssl.create_default_context(cafile=candidate)
            except ssl.SSLError:
                continue
    return context


def _http_get(url: str, *, timeout: float = DEFAULT_TIMEOUT_S, token: Optional[str] = None) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(  # noqa: S310 (fixed https URLs)
        request, timeout=timeout, context=_ssl_context()
    ) as response:
        return response.read()


def _default_fetcher(url: str) -> bytes:
    return _http_get(url, token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check(pin: Optional[dict] = None, *, fetcher: Callable[[str], bytes] = None) -> dict:
    """Re-fetch every pinned file and each branch HEAD. Never raises."""
    pin = pin if pin is not None else load_pin()
    fetcher = fetcher or _default_fetcher
    mismatched: list[dict] = []
    drift: list[dict] = []
    moved: list[dict] = []
    errors: list[str] = []
    checked = 0
    branches: dict[str, str] = {}
    for upstream in pin.get("upstream", []):
        repo = upstream.get("repo")
        branch = upstream.get("branch") or "main"
        try:
            head = json.loads(fetcher(COMMITS_URL.format(repo=repo, ref=branch)).decode("utf-8"))
            branches[repo] = head.get("sha", "")
        except (urllib.error.URLError, ValueError, KeyError) as error:
            errors.append(f"{repo}: cannot read {branch} HEAD ({error})")
            branches[repo] = ""
    for entry in pinned_files(pin):
        try:
            data = fetcher(RAW_URL.format(repo=entry["repo"], commit=entry["commit"], path=entry["path"]))
        except (urllib.error.URLError, urllib.error.HTTPError) as error:
            errors.append(f"{entry['repo']}:{entry['path']}: fetch failed ({error})")
            continue
        checked += 1
        digest = _sha256(data)
        if digest != entry["sha256"]:
            mismatched.append({**entry, "actual": digest})
        head = branches.get(entry["repo"])
        if head and head != entry["commit"]:
            try:
                head_digest = _sha256(
                    fetcher(RAW_URL.format(repo=entry["repo"], commit=head, path=entry["path"]))
                )
            except (urllib.error.URLError, urllib.error.HTTPError):
                continue
            if head_digest != entry["sha256"]:
                drift.append({**entry, "head": head, "head_sha256": head_digest})
    for repo, head in branches.items():
        pinned = next(
            (item.get("commit") for item in pin.get("upstream", []) if item.get("repo") == repo), ""
        )
        if head and head != pinned:
            moved.append({"repo": repo, "pinned": pinned, "head": head})
    return {
        "ok": not mismatched and not errors,
        "checked": checked,
        "mismatched": mismatched,
        "drift": drift,
        "moved": moved,
        "errors": errors,
    }


def refresh(*, fetcher: Callable[[str], bytes] = None, now: Optional[str] = None) -> dict:
    """Re-pin every file at its branch HEAD, keeping the editorial metadata."""
    pin = load_pin()
    fetcher = fetcher or _default_fetcher
    refreshed = {
        "schema": pin.get("schema", 1),
        "updated": now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upstream": [],
    }
    for key in ("note", "introduced_in"):
        if key in pin:
            refreshed[key] = pin[key]
    for upstream in pin.get("upstream", []):
        repo = upstream["repo"]
        branch = upstream.get("branch") or "main"
        head = json.loads(fetcher(COMMITS_URL.format(repo=repo, ref=branch)).decode("utf-8"))
        commit = head["sha"]
        files = [
            {
                "path": entry["path"],
                "role": entry["role"],
                "sha256": _sha256(data),
                "bytes": len(data),
            }
            for entry, data in (
                (entry, fetcher(RAW_URL.format(repo=repo, commit=commit, path=entry["path"])))
                for entry in upstream.get("files", [])
            )
        ]
        refreshed["upstream"].append(
            {
                "repo": repo,
                "license": upstream.get("license"),
                "branch": branch,
                "commit": commit,
                "why": upstream.get("why"),
                "files": files,
            }
        )
    write_pin(refreshed)
    return refreshed


def write_pin(pin: dict) -> None:
    PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PIN_PATH.with_name(f".{PIN_PATH.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(pin, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, PIN_PATH)
