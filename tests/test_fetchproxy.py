"""Tests for the multi-port fetch accelerator.

Hermetic: no BookWalker, no CDN, no mokuro. A throwaway HTTP server stands in
for the CDN, and a Starlette TestClient drives the proxy in-process.
"""

from __future__ import annotations

import gzip
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import fetchproxy

REPO_ROOT = Path(__file__).resolve().parent.parent
BODY = b"pretend-this-is-a-page.jpeg"


def free_ports(count: int) -> list[int]:
    """Distinct ports nothing is listening on. Racy in principle, fine locally."""
    holders = []
    for _ in range(count):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        holders.append(sock)
    ports = [sock.getsockname()[1] for sock in holders]
    for sock in holders:
        sock.close()
    return ports


class _Upstream(BaseHTTPRequestHandler):
    """Records what the proxy asked for and answers like the CDN."""

    seen: list[tuple[str, str]] = []

    def do_GET(self):  # noqa: N802 - stdlib naming
        type(self).seen.append(("GET", self.path))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        if self.path == "/gzip":
            encoded = gzip.compress(BODY)
            self.send_header("Content-Encoding", "gzip")
        else:
            encoded = BODY
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_HEAD(self):  # noqa: N802 - stdlib naming
        type(self).seen.append(("HEAD", self.path))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.end_headers()

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def upstream():
    _Upstream.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def proxied(upstream, monkeypatch):
    monkeypatch.setattr(fetchproxy, "UPSTREAM", upstream)
    monkeypatch.setattr(fetchproxy, "ALLOWED_HOSTS", {urlsplit(upstream).netloc})
    with TestClient(fetchproxy.build_app([9001, 9002])) as client:
        yield client


# --------------------------------------------------------------- binding

def test_bind_sockets_returns_every_free_port():
    ports = free_ports(3)
    sockets, bound = fetchproxy.bind_sockets("127.0.0.1", ports)
    try:
        assert bound == ports
        for port in bound:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
    finally:
        for sock in sockets:
            sock.close()


def test_bind_sockets_skips_port_already_in_use():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken = busy.getsockname()[1]
    spare = free_ports(1)[0]
    sockets, bound = fetchproxy.bind_sockets("127.0.0.1", [taken, spare])
    try:
        assert taken not in bound, "a busy port must be skipped, not fatal"
        assert bound == [spare]
    finally:
        for sock in sockets:
            sock.close()
        busy.close()


def test_bind_sockets_survives_every_port_being_taken():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        sockets, bound = fetchproxy.bind_sockets("127.0.0.1", [busy.getsockname()[1]])
        assert bound == [] and sockets == []
    finally:
        busy.close()


# ---------------------------------------------------------------- health

def test_health_advertises_the_bound_ports(proxied):
    payload = proxied.get("/__bwdd_health").json()
    assert payload["bwddFetchProxy"] is True
    assert payload["portList"] == [9001, 9002]
    assert payload["upstream"].startswith("http://127.0.0.1:")


def test_health_marks_that_the_ports_belong_to_the_bridge(proxied):
    # The userscript keys off bwddFetchProxy to tell this helper apart from the
    # standalone one, so the flag must not be renamed casually.
    assert proxied.get("/__bwdd_health").status_code == 200
    assert "bwddFetchProxy" in proxied.get("/__bwdd_health").json()


def _preflight(client, origin):
    return client.options(
        "/page.jpeg",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Private-Network": "true",
        },
    )


def test_private_network_preflight_is_allowed_for_any_origin_by_default(proxied):
    """The bridge is generic: it serves whichever store the userscript runs on,
    so it must not be pinned to one store's origin list. This is what makes a
    new store work with no bridge-side configuration."""
    for origin in (
        "https://viewer-trial.bookwalker.jp",
        "https://www.cmoa.jp",
        "https://some-future-store.example",
    ):
        response = _preflight(proxied, origin)
        assert response.status_code == 204, origin
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers["access-control-allow-private-network"] == "true"
        # The preflight is cached, so a download does not re-ask per port.
        assert response.headers["access-control-max-age"] == "600"


def test_private_network_preflight_refuses_others_when_origins_are_narrowed(monkeypatch, upstream):
    """Setting CORS_ORIGINS still tightens it back to a list."""
    from starlette.testclient import TestClient as _TC
    monkeypatch.setattr(fetchproxy, "CORS_ORIGINS", ["https://viewer-trial.bookwalker.jp"])
    monkeypatch.setattr(fetchproxy, "UPSTREAM", upstream)
    with _TC(fetchproxy.build_app([9001, 9002])) as client:
        assert _preflight(client, "https://viewer-trial.bookwalker.jp").status_code == 204
        denied = _preflight(client, "https://not-a-viewer.example")
        assert denied.status_code == 403


# ---------------------------------------------------------------- proxying

def test_proxy_forwards_path_and_signed_query(proxied):
    path = "/6_product/abc/1/2/item/xhtml/p-0001.xhtml/deadbeef.jpeg"
    query = "Policy=eyJ&Signature=abc%7Edef&Key-Pair-Id=APKA"
    response = proxied.get(f"{path}?{query}")
    assert response.status_code == 200
    assert _Upstream.seen == [("GET", f"{path}?{query}")], "path and query must survive intact"


def test_proxy_streams_the_body_back(proxied):
    response = proxied.get("/page.jpeg")
    assert response.content == BODY
    assert response.headers["content-type"] == "image/jpeg"


def test_proxy_decodes_compressed_upstream_bodies(proxied):
    response = proxied.get("/gzip")
    assert response.status_code == 200
    assert response.content == BODY
    assert "content-encoding" not in response.headers


def test_proxy_marks_its_responses_and_forbids_caching(proxied):
    response = proxied.get("/page.jpeg")
    assert response.headers["x-bwdd-fetch-proxy"] == "mokuro-bridge"
    assert response.headers["cache-control"] == "no-store"


def test_head_passes_status_through_with_no_body(proxied):
    response = proxied.head("/page.jpeg")
    assert response.status_code == 200
    assert response.content == b""
    assert _Upstream.seen == [("HEAD", "/page.jpeg")]


def test_unreachable_upstream_returns_502(monkeypatch):
    dead = free_ports(1)[0]
    monkeypatch.setattr(fetchproxy, "UPSTREAM", f"http://127.0.0.1:{dead}")
    monkeypatch.setattr(fetchproxy, "ALLOWED_HOSTS", {f"127.0.0.1:{dead}"})
    with TestClient(fetchproxy.build_app([])) as client:
        response = client.get("/page.jpeg")
    assert response.status_code == 502
    assert "upstream fetch failed" in response.json()["error"]


def test_a_host_outside_the_allow_list_is_refused(proxied, monkeypatch):
    # Guards against this ever behaving as an open proxy.
    monkeypatch.setattr(fetchproxy, "ALLOWED_HOSTS", set())
    assert proxied.get("/page.jpeg").status_code == 403


# ------------------------------------------------------------------ env

def _with_env(**env) -> str:
    """Import fetchproxy in a clean interpreter with *env* set."""
    code = (
        "import os, json;"
        f"os.environ.update({env!r});"
        "from mokuro_bridge import fetchproxy as f;"
        "print(json.dumps({'ports': f.FETCH_PORTS,"
        " 'concurrency': f.FETCH_CONCURRENCY, 'upstream': f.UPSTREAM}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    import json
    return json.loads(result.stdout)


def test_port_count_defaults_to_48():
    assert _with_env()["ports"] == 48


def test_port_count_comes_from_env():
    assert _with_env(MOKURO_BRIDGE_FETCH_PORTS="12")["ports"] == 12


def test_zero_ports_disables_the_feature():
    assert _with_env(MOKURO_BRIDGE_FETCH_PORTS="0")["ports"] == 0


def test_concurrency_is_uncapped_by_default():
    assert _with_env()["concurrency"] == 0


def test_upstream_can_be_pointed_elsewhere_for_testing():
    env = _with_env(MOKURO_BRIDGE_FETCH_UPSTREAM="https://example.test/")
    assert env["upstream"] == "https://example.test", "trailing slash is stripped"
