#!/usr/bin/env python3
"""A dependency-free WebDAV server for exercising the bridge's WebDAV provider.

The WebDAV destination talks PROPFIND, MKCOL, PUT, GET, HEAD and DELETE over
HTTP Basic auth. Pointing the bridge at a real NAS or Nextcloud while iterating
is slow and needs an account, and the quick WebDAV servers available in
packaging all pull in third-party packages. This module is a stdlib-only target
for the provider's pytest suite and for a click-through demo:

    python tools/dav_server.py --port 8099 --user dav --password dav

It serves one directory at ``/dav/``, implements exactly the subset the
provider uses, and refuses any request that would touch a path outside that
directory. It is a test double, not a NAS: one account, no LOCK/UNLOCK, no
ranges, no quotas, and no durability beyond atomic PUT replacement.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import http.server
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from email.utils import formatdate
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

VERSION = "1.0"

# The demo base URL includes the mount, so the server also serves the same tree
# at "/" for clients handed a bare host.
DAV_MOUNT = "/dav"

ALLOW = "OPTIONS, GET, HEAD, PROPFIND, MKCOL, PUT, DELETE"

# A Windows drive letter in a URL path is never a valid collection name here and
# could turn into an absolute path on a Windows host.
DRIVE_LETTER = re.compile(r"^[A-Za-z]:")

COPY_CHUNK = 64 * 1024


class RequestRejected(Exception):
    """A request the server refuses, carrying the status and reason to send."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class DavRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mokuro-dav"
    sys_version = ""

    # -- responses -------------------------------------------------------

    def log_message(self, format, *args):  # noqa: A002 (BaseHTTPRequestHandler signature)
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def log_error(self, format, *args):  # noqa: A002 (BaseHTTPRequestHandler signature)
        if getattr(self.server, "verbose", False):
            super().log_error(format, *args)

    def _start_response(self, status, length, content_type=None, headers=()):
        self._response_started = True
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for name, value in headers:
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

    def _respond(self, status, body=b"", content_type=None, headers=()):
        # RFC 7230 forbids Content-Length on a 204; every other response needs
        # one to keep HTTP/1.1 framing unambiguous.
        length = None if status == 204 else len(body)
        self._start_response(status, length, content_type, headers)
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _plain(self, status, message, headers=()):
        self._respond(status, (message + "\n").encode("utf-8"), "text/plain; charset=utf-8", headers)

    def _challenge(self):
        # A failed PUT can still have an unread request body, so the connection
        # cannot be reused after a 401.
        self.close_connection = True
        self._plain(401, "Unauthorized", (("WWW-Authenticate", 'Basic realm="dav"'),))
        return False

    # -- request plumbing ------------------------------------------------

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic" or not token:
            return self._challenge()
        try:
            supplied = base64.b64decode(token, validate=True)
        except (binascii.Error, ValueError):
            try:
                supplied = base64.b64decode(token)
            except (binascii.Error, ValueError):
                return self._challenge()
        expected = f"{self.server.dav_user}:{self.server.dav_password}".encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            return self._challenge()
        return True

    def _dispatch(self, action):
        self._response_started = False
        try:
            if not self._authorized():
                return
            action()
        except RequestRejected as exc:
            if not self._response_started:
                self._plain(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except ValueError as exc:
            if not self._response_started:
                self._plain(400, str(exc) or "Bad Request")
        except OSError:
            # A fresh failure can still get a 500; a failure after the headers
            # went out can only cut the connection.
            if not self._response_started:
                self._plain(500, "Internal Server Error")
            else:
                self.close_connection = True

    def _decode_target(self):
        """Return ``(mount prefix, path segments)`` for the request target.

        Segments are percent-decoded one at a time, so an encoded slash cannot
        smuggle in a second segment. ``/dav/...`` and the same tree at
        ``/...`` both work, which keeps a base URL with or without the demo
        mount usable.
        """
        raw_path = urllib.parse.urlsplit(self.path).path
        if raw_path == DAV_MOUNT or raw_path.startswith(DAV_MOUNT + "/"):
            prefix = DAV_MOUNT
            raw_path = raw_path[len(DAV_MOUNT):]
        else:
            prefix = ""
        segments = []
        for raw_segment in raw_path.split("/"):
            if not raw_segment:
                continue
            try:
                segment = urllib.parse.unquote(raw_segment, encoding="utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise RequestRejected(400, "Bad Request") from exc
            if segment == ".":
                continue
            if (
                segment == ".."
                or "\x00" in segment
                or "\\" in segment
                or "/" in segment
                or DRIVE_LETTER.match(segment)
            ):
                raise RequestRejected(403, "Forbidden")
            segments.append(segment)
        return prefix, segments

    def _target(self):
        return self._disk_path(self._decode_target()[1])

    def _disk_path(self, segments):
        try:
            resolved = self.server.dav_root.joinpath(*segments).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise RequestRejected(403, "Forbidden") from exc
        try:
            resolved.relative_to(self.server.dav_root)
        except ValueError as exc:
            raise RequestRejected(403, "Forbidden") from exc
        return resolved

    def _read_body(self):
        encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in encoding:
            return self._read_chunked()
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            length = int(raw)
        except ValueError as exc:
            raise RequestRejected(400, "Bad Content-Length") from exc
        if length < 0:
            raise RequestRejected(400, "Bad Content-Length")
        return self.rfile.read(length)

    def _read_chunked(self):
        chunks = []
        while True:
            line = self.rfile.readline(65536)
            if not line:
                raise RequestRejected(400, "Truncated chunked body")
            size_field = line.split(b";", 1)[0].strip()
            try:
                size = int(size_field, 16)
            except ValueError as exc:
                raise RequestRejected(400, "Bad chunk size") from exc
            if size == 0:
                while True:
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                return b"".join(chunks)
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise RequestRejected(400, "Truncated chunked body")
            chunks.append(chunk)
            self.rfile.read(2)  # CRLF after the chunk data

    # -- methods ---------------------------------------------------------

    def do_OPTIONS(self):
        self._dispatch(self._options)

    def _options(self):
        self._respond(200, headers=(("DAV", "1"), ("Allow", ALLOW)))

    def do_PROPFIND(self):
        self._dispatch(self._propfind)

    def _propfind(self):
        # The standard request body names the properties the client wants. The
        # response includes all of them regardless, which is allowed and keeps
        # this server from needing a second XML parser path; the body still has
        # to be drained so the connection stays framed.
        self._read_body()
        prefix, segments = self._decode_target()
        depth = self._depth()
        target = self._disk_path(segments)
        if not target.exists():
            raise RequestRejected(404, "Not Found")
        entries = [target]
        if depth == 1 and target.is_dir():
            with os.scandir(target) as iterator:
                entries.extend(sorted((Path(entry.path) for entry in iterator), key=lambda p: p.name))
        self._respond(207, self._multistatus(prefix, entries), 'application/xml; charset="utf-8"')

    def _depth(self):
        raw = self.headers.get("Depth")
        if raw is None:
            # RFC 4918 defaults a missing Depth to infinity, but a bodyless
            # PROPFIND from curl should describe the resource, not walk the
            # tree. The provider always sends an explicit Depth.
            return 0
        value = raw.strip().lower()
        if value == "0":
            return 0
        if value == "1":
            return 1
        raise RequestRejected(403, "Depth not supported")

    def _multistatus(self, prefix, paths):
        root = self.server.dav_root
        lines = ['<?xml version="1.0" encoding="utf-8"?>', '<D:multistatus xmlns:D="DAV:">']
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                # A child can disappear between scandir and stat; a multistatus
                # simply leaves it out.
                continue
            is_dir = path.is_dir()
            href = self._href(prefix, path)
            if is_dir and not href.endswith("/"):
                href += "/"
            resourcetype = "<D:collection/>" if is_dir else ""
            lines.extend(
                [
                    "<D:response>",
                    f"<D:href>{xml_escape(href)}</D:href>",
                    "<D:propstat>",
                    "<D:prop>",
                    f"<D:resourcetype>{resourcetype}</D:resourcetype>",
                    f"<D:getcontentlength>{0 if is_dir else stat.st_size}</D:getcontentlength>",
                    f"<D:getlastmodified>{formatdate(stat.st_mtime, usegmt=True)}</D:getlastmodified>",
                    f"<D:displayname>{xml_escape(path.name or root.name)}</D:displayname>",
                    "</D:prop>",
                    "<D:status>HTTP/1.1 200 OK</D:status>",
                    "</D:propstat>",
                    "</D:response>",
                ]
            )
        lines.append("</D:multistatus>")
        return "\n".join(lines).encode("utf-8")

    def _href(self, prefix, path):
        relative = path.relative_to(self.server.dav_root)
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in relative.parts)
        href = f"{prefix}/{quoted}" if quoted else f"{prefix}/"
        return href or "/"

    def do_GET(self):
        self._dispatch(self._get)

    def _get(self):
        target = self._target()
        try:
            stream = target.open("rb")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise RequestRejected(404, "Not Found") from None
        except PermissionError:
            raise RequestRejected(403, "Forbidden") from None
        with stream:
            self._start_response(200, os.fstat(stream.fileno()).st_size, "application/octet-stream")
            while True:
                chunk = stream.read(COPY_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_HEAD(self):
        self._dispatch(self._head)

    def _head(self):
        target = self._target()
        try:
            with target.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise RequestRejected(404, "Not Found") from None
        except PermissionError:
            raise RequestRejected(403, "Forbidden") from None
        self._start_response(200, size, "application/octet-stream")

    def do_PUT(self):
        self._dispatch(self._put)

    def _put(self):
        target = self._target()
        if target.is_dir():
            self.close_connection = True  # request body has not been read
            raise RequestRejected(405, "Method Not Allowed")
        if not target.parent.is_dir():
            self.close_connection = True  # request body has not been read
            raise RequestRejected(409, "Conflict")
        body = self._read_body()
        existed = os.path.lexists(target)
        # A reader must never observe a half-written file, so the bytes land in
        # a sibling temp file and appear under the real name in one rename.
        handle, temp_name = tempfile.mkstemp(prefix=".dav-put-", dir=str(target.parent))
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        self._respond(204 if existed else 201)

    def do_MKCOL(self):
        self._dispatch(self._mkcol)

    def _mkcol(self):
        self._read_body()
        target = self._target()
        if os.path.lexists(target):
            raise RequestRejected(405, "Method Not Allowed")
        parent = target.parent
        if not parent.exists():
            raise RequestRejected(409, "Conflict")
        if not parent.is_dir():
            raise RequestRejected(404, "Not Found")
        try:
            os.mkdir(target)
        except FileExistsError:
            raise RequestRejected(405, "Method Not Allowed") from None
        except FileNotFoundError:
            raise RequestRejected(409, "Conflict") from None
        except NotADirectoryError:
            raise RequestRejected(404, "Not Found") from None
        self._respond(201)

    def do_DELETE(self):
        self._dispatch(self._delete)

    def _delete(self):
        target = self._target()
        if target == self.server.dav_root:
            # Deleting the root would erase the server's own storage, and no
            # provider call needs it. A relative URL typo must not do it.
            raise RequestRejected(403, "Forbidden")
        if not os.path.lexists(target):
            raise RequestRejected(404, "Not Found")
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except FileNotFoundError:
            raise RequestRejected(404, "Not Found") from None
        self._respond(204)

    def _method_not_allowed(self):
        self.close_connection = True
        self._plain(405, "Method Not Allowed", (("Allow", ALLOW),))

    def _unsupported(self):
        self._dispatch(self._method_not_allowed)

    # Methods the provider never sends still answer with the shared Allow list
    # instead of BaseHTTPRequestHandler's bare 501.
    do_POST = _unsupported
    do_PATCH = _unsupported
    do_COPY = _unsupported
    do_MOVE = _unsupported
    do_LOCK = _unsupported
    do_UNLOCK = _unsupported
    do_PROPPATCH = _unsupported
    do_TRACE = _unsupported


def _make_server(host, port, root, user, password, verbose=False):
    server = http.server.ThreadingHTTPServer((host, port), DavRequestHandler)
    server.daemon_threads = True
    server.dav_root = root
    server.dav_user = user
    server.dav_password = password
    server.verbose = verbose
    return server


def start_test_server(root, user="dav", password="dav", port=0):
    """Start the server on a background thread; return ``(server, thread, base_url)``.

    ``port=0`` asks the OS for a free port, and the returned ``base_url`` is
    already answering when this function returns. The caller owns shutdown::

        server, thread, base_url = start_test_server(tmp_path)
        try:
            ...
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
    """
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    server = _make_server("127.0.0.1", port, root_path.resolve(), user, password)
    thread = threading.Thread(target=server.serve_forever, name="dav-server", daemon=True)
    thread.start()
    host, actual_port = server.server_address[:2]
    base_url = f"http://{host}:{actual_port}/dav/"
    deadline = time.monotonic() + 5.0
    while True:
        try:
            with socket.create_connection((host, actual_port), timeout=0.25):
                break
        except OSError:
            if time.monotonic() > deadline:
                server.shutdown()
                thread.join()
                server.server_close()
                raise
            time.sleep(0.01)
    return server, thread, base_url


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dav_server.py",
        description="Dependency-free WebDAV test server for the bridge's WebDAV provider.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8099, help="port to bind, 0 picks a free one (default: 8099)")
    parser.add_argument("--user", default="dav", help="Basic auth user (default: dav)")
    parser.add_argument("--password", default="dav", help="Basic auth password (default: dav)")
    parser.add_argument(
        "--dir",
        dest="root",
        metavar="PATH",
        help="directory to serve, created if missing (default: a fresh temp directory)",
    )
    parser.add_argument("--verbose", action="store_true", help="log every request")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args(argv)

    if args.root:
        root = Path(args.root)
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix="mokuro-dav-"))
    # Resolve before it becomes dav_root: on macOS /tmp and /var are symlinks,
    # and the escape check compares fully resolved paths on both sides.
    root = root.resolve()

    try:
        server = _make_server(args.host, args.port, root, args.user, args.password, args.verbose)
    except OSError as exc:
        print(f"dav_server.py: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1

    host, port = server.server_address[:2]
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    print(
        f"WebDAV test server: http://{display_host}:{port}/dav/  "
        f"(user {args.user}, password {args.password}, root {root})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
