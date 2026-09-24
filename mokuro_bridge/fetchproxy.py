"""
Multi-port page-fetch accelerator.

Chrome allows 6 concurrent HTTP/1.1 connections per *origin*, and an origin is
scheme + host + port. The page CDN refuses HTTP/2, so a viewer download is
pinned to 6 sockets. Serving this proxy on several localhost ports gives the
browser 6 fresh sockets each, while this process does the fetching under no
browser limit at all.

The userscript needs no configuration: the bridge's /health already carries
``fetchProxyPorts``, so the extra lanes light up whenever the bridge is running.
Downloading does not depend on the bridge; with it stopped the viewer falls back
to the page/GM lanes.

Env:
  MOKURO_BRIDGE_FETCH_PORTS        extra listening ports (0 disables). Default 48.
  MOKURO_BRIDGE_FETCH_CONCURRENCY  max simultaneous upstream fetches (0 = no cap).
  MOKURO_BRIDGE_FETCH_UPSTREAM     override the CDN being accelerated.
"""

from __future__ import annotations

import os
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import CORS_ORIGINS

# How many extra ports to open. Each is worth 6 browser sockets; Chrome gives a
# profile roughly 300 sockets in total, so ~48 leaves room for the page, GM and
# trailing-dot lanes while staying under that ceiling.
FETCH_PORTS = max(0, int(os.environ.get("MOKURO_BRIDGE_FETCH_PORTS", "48")))
# 0 = unlimited, matching the standalone helper. Lower it if CloudFront starts
# throttling a very large burst.
FETCH_CONCURRENCY = max(0, int(os.environ.get("MOKURO_BRIDGE_FETCH_CONCURRENCY", "0")))

UPSTREAM = os.environ.get(
    "MOKURO_BRIDGE_FETCH_UPSTREAM", "https://bw-bv-epubs.bookwalker.jp"
).rstrip("/")
# Never forward anywhere else — this must not become an open proxy.
ALLOWED_HOSTS = {urlsplit(UPSTREAM).netloc, "bw-bv-epubs.bookwalker.jp"}

# Filled in by server.py once the sockets are actually bound; api.py reports it
# in /health so the userscript can discover the ports without configuration.
ACTIVE_PORTS: list[int] = []


def bind_sockets(host: str, ports: list[int]) -> tuple[list[socket.socket], list[int]]:
    """Bind *ports* up front so a clash is reported instead of crashing mid-serve.

    Returns (sockets, ports) for whichever ports we actually got; a port already
    in use is skipped with a warning rather than taking the whole bridge down.
    """
    sockets: list[socket.socket] = []
    bound: list[int] = []
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(2048)
            sock.set_inheritable(True)
        except OSError as exc:
            sock.close()
            print(f"  fetch proxy: port {port} unavailable ({exc.strerror}), skipping")
            continue
        sockets.append(sock)
        bound.append(port)
    return sockets, bound


def build_app(port_list: list[int]) -> Starlette:
    """The proxy itself: mirror the CDN path + signed query onto the real host."""

    # One client per server, so upstream connections are pooled and reused
    # across every port this server listens on.
    limits = httpx.Limits(
        max_connections=FETCH_CONCURRENCY or None,
        max_keepalive_connections=FETCH_CONCURRENCY or None,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=15.0),
            limits=limits,
        ) as client:
            app.state.client = client
            yield

    async def health(request):
        return JSONResponse({
            "bwddFetchProxy": True,
            "version": 1,
            "upstream": UPSTREAM,
            "portList": list(port_list),
            "concurrency": FETCH_CONCURRENCY,
        })

    async def proxy(request):
        target = UPSTREAM + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        if urlsplit(target).netloc not in ALLOWED_HOSTS:
            return JSONResponse({"error": "host not allowed"}, status_code=403)

        client: httpx.AsyncClient = request.app.state.client
        try:
            # stream=True: never hold a whole page in memory, and start the
            # browser's download as soon as the first byte lands.
            upstream = await client.send(
                client.build_request(request.method, target), stream=True
            )
        except Exception as exc:  # noqa: BLE001 - surface any transport failure
            return JSONResponse({"error": f"upstream fetch failed: {exc}"}, status_code=502)

        # content-encoding / content-length are deliberately not copied: httpx
        # has already decoded the body, so forwarding the originals would lie
        # about both the length and the encoding.
        headers = {
            "content-type": upstream.headers.get("content-type", "application/octet-stream"),
            "cache-control": "no-store",
            "x-bwdd-fetch-proxy": "mokuro-bridge",
        }
        if request.method == "HEAD":
            await upstream.aclose()
            return Response(status_code=upstream.status_code, headers=headers)
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=headers,
            background=BackgroundTask(upstream.aclose),
        )

    app = Starlette(
        routes=[
            Route("/__bwdd_health", health, methods=["GET"]),
            Route("/{path:path}", proxy, methods=["GET", "HEAD"]),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        # The credential rides in the signed URL, not a cookie, and the
        # userscript fetches with credentials:'omit'.
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )
    return app
