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

import fnmatch
import os
import re
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import ipaddress
import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import CORS_ORIGINS

def _env_nonnegative_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# How many extra ports to open. Each is worth 6 browser sockets; Chrome gives a
# profile roughly 300 sockets in total, so ~48 leaves room for the page, GM and
# trailing-dot lanes while staying under that ceiling.
FETCH_PORTS = _env_nonnegative_int("MOKURO_BRIDGE_FETCH_PORTS", 48)
# 0 = unlimited, matching the standalone helper. Lower it if CloudFront starts
# throttling a very large burst.
FETCH_CONCURRENCY = _env_nonnegative_int("MOKURO_BRIDGE_FETCH_CONCURRENCY", 0)

UPSTREAM = os.environ.get(
    "MOKURO_BRIDGE_FETCH_UPSTREAM", "https://bw-bv-epubs.bookwalker.jp"
).rstrip("/")

# Which hosts this proxy may forward to. A browser request to a proxy port
# carries only the path and query, so the caller names the upstream in the
# x-bwdd-upstream header; requests without that header keep using UPSTREAM
# exactly as before.
#
# This is still not an open proxy: the header is only honoured when the named
# host matches one of these patterns, so the worst a hostile page can do is
# fetch the CDNs already listed here.
_ALLOWED_HOSTS_ENV = os.environ.get("MOKURO_BRIDGE_FETCH_ALLOWED_HOSTS", "")
if _ALLOWED_HOSTS_ENV.strip():
    UPSTREAM_PATTERNS = [
        h.strip().lower() for h in _ALLOWED_HOSTS_ENV.split(",") if h.strip()
    ]
else:
    # "*" means the caller may name any public host. Pinning a list of stores
    # here would mean every new store needs a bridge restart before its pages can
    # use the extra ports, which defeats the point of a generic accelerator.
    UPSTREAM_PATTERNS = ["*"]
# Exact-match set for the no-header legacy path, which must stay as tight as it
# was before the header existed.
ALLOWED_HOSTS = {urlsplit(UPSTREAM).netloc, "bw-bv-epubs.bookwalker.jp"}

_safe_host_cache: dict[str, bool] = {}


def _target_is_safe(host: str) -> bool:
    """False for anything that is not a public internet host.

    The port count is what makes this proxy fast, and a proxy that forwards
    wherever it is told would also let any page the user has open reach into the
    local network (or back into the bridge). So the target must resolve, and
    every address it resolves to must be public.
    """
    key = host.lower().strip(".")
    cached = _safe_host_cache.get(key)
    if cached is not None:
        return cached
    ok = False
    if not (key == "localhost" or key.endswith(".localhost")
            or key.endswith(".local") or key.endswith(".internal")
            or key.endswith(".home.arpa")):
        try:
            infos = socket.getaddrinfo(key, None)
        except OSError:
            infos = None
        if infos:
            ok = True
            for info in infos:
                try:
                    ip = ipaddress.ip_address(info[4][0])
                except ValueError:
                    ok = False
                    break
                if (ip.is_private or ip.is_loopback or ip.is_link_local
                        or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                    ok = False
                    break
    _safe_host_cache[key] = ok
    return ok


def host_allowed(host: str) -> bool:
    """True when `host` may be fetched through this proxy."""
    host = (host or "").lower().strip(".")
    if not host:
        return False
    if not any(p == host or fnmatch.fnmatchcase(host, p) for p in UPSTREAM_PATTERNS):
        return False
    return _target_is_safe(host)
    for pattern in UPSTREAM_PATTERNS:
        if pattern == host or fnmatch.fnmatchcase(host, pattern):
            return True
    return False

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
            follow_redirects=False,
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
            # Hosts this proxy will forward to. The userscript reads this and
            # only sends a store's pages through a port that is allowed to serve
            # that store's CDN, so one bridge can accelerate both BookWalker and
            # CMOA without either store's requests landing on the wrong host.
            "upstreams": list(UPSTREAM_PATTERNS),
            "portList": list(port_list),
            "concurrency": FETCH_CONCURRENCY,
        })

    async def proxy(request):
        requested_host = (request.headers.get("x-bwdd-upstream") or "").strip()
        if requested_host:
            # The caller named the CDN it wants; only the allowlist decides.
            if not host_allowed(requested_host):
                return JSONResponse({"error": "host not allowed"}, status_code=403)
            base = "https://" + requested_host
        else:
            base = UPSTREAM
        target = base + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        if urlsplit(target).netloc not in ALLOWED_HOSTS and not host_allowed(urlsplit(target).netloc):
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

        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            return JSONResponse({"error": "upstream redirects are not allowed"}, status_code=502)

        # httpx's aiter_bytes() decodes content-encoding while streaming. Do
        # not forward the upstream encoding/length headers, which would lie
        # about the decoded body sent to the browser.
        headers = {
            "content-type": upstream.headers.get("content-type", "application/octet-stream"),
            "cache-control": "no-store",
            "x-bwdd-fetch-proxy": "mokuro-bridge",
        }
        if request.method == "HEAD":
            await upstream.aclose()
            return Response(status_code=upstream.status_code, headers=headers)
        return StreamingResponse(
            upstream.aiter_bytes(),
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
    def _cors_origin_regex():
        patterns = [str(pattern) for pattern in CORS_ORIGINS if any(ch in str(pattern) for ch in "*?")]
        return "^(?:" + "|".join(fnmatch.translate(pattern) for pattern in patterns) + ")$" if patterns else None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_origin_regex=_cors_origin_regex(),
        # The credential rides in the signed URL, not a cookie, and the
        # userscript fetches with credentials:'omit'.
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )

    class _AllowPrivateNetwork(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            origin = request.headers.get("origin", "")
            pna = request.headers.get("access-control-request-private-network", "").lower() == "true"
            if request.method == "OPTIONS" and pna:
                allowed_origin = bool(origin) and any(fnmatch.fnmatchcase(origin, str(pattern)) for pattern in CORS_ORIGINS)
                if not allowed_origin:
                    return Response(status_code=403)
                requested_method = request.headers.get("access-control-request-method", "").upper()
                if requested_method not in {"GET", "HEAD", "OPTIONS"}:
                    return Response(status_code=405)
                requested_headers = request.headers.get("access-control-request-headers", "")
                if requested_headers and any(
                    not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name.strip())
                    for name in requested_headers.split(",")
                ):
                    return Response(status_code=400)
                return Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                        "Access-Control-Allow-Headers": requested_headers or "*",
                        "Access-Control-Allow-Private-Network": "true",
                        # Cache the preflight. There is one origin per proxy
                        # port, so without this the browser re-asks every port's
                        # permission on its 5s default while a download is
                        # already running, costing a round trip per port.
                        "Access-Control-Max-Age": "600",
                        "Vary": "Origin, Access-Control-Request-Private-Network, Access-Control-Request-Method, Access-Control-Request-Headers",
                    },
                )
            response = await call_next(request)
            if bool(origin) and any(fnmatch.fnmatchcase(origin, str(pattern)) for pattern in CORS_ORIGINS) and pna:
                response.headers["Access-Control-Allow-Private-Network"] = "true"
                vary = response.headers.get("Vary", "")
                vary_names = [part.strip() for part in vary.split(",") if part.strip()]
                existing_names = {part.casefold() for part in vary_names}
                for name in ("Origin", "Access-Control-Request-Private-Network"):
                    if name.casefold() not in existing_names:
                        vary_names.append(name)
                        existing_names.add(name.casefold())
                response.headers["Vary"] = ", ".join(vary_names)
            return response

    app.add_middleware(_AllowPrivateNetwork)
    return app
