# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Transport half of the remote control server: sockets, framing, authentication and file
transfer. Nothing here imports ``bpy``, so it can be tested with a plain interpreter
(``build_files/android/termux/test_remote_control.py``); the half that touches Blender is
the ``executor`` handed to :class:`Server`, which only ever runs on Blender's main thread.

Wire format, both directions: one JSON object per line (UTF-8), optionally followed by
exactly ``size`` raw bytes when the object carries a ``size`` key. Raw bytes keep a file
transfer from being inflated by base64 or held whole in memory.

The first object a client sends must be ``{"op": "hello", "token": ...}``. Anything else,
or a wrong token, ends the connection. A request that starts with ``GET `` is answered as
HTTP instead, which is how the Termux installer and the CLI itself are fetched with a
plain ``curl``; those pages carry no secrets and need no token.
"""

__all__ = (
    "DEFAULT_PORT",
    "PROTOCOL_VERSION",
    "Server",
)

import hmac
import json
import os
import queue
import shutil
import socket
import threading
import time

DEFAULT_PORT = 8642
PROTOCOL_VERSION = 1

# A line is a JSON header. Scripts travel inside it, so it has to hold a large one, but a
# client that never sends a newline must not be able to grow it without bound.
_MAX_HEADER = 64 * 1024 * 1024
_CHUNK = 256 * 1024

# Operations that touch Blender data and therefore have to wait for the main thread.
# Everything else (file transfer, listing) is plain file I/O and is answered here.
MAIN_THREAD_OPS = frozenset({"exec", "eval", "quit"})


class ProtocolError(Exception):
    pass


class Connection:
    """One client. Reads happen on its own thread, writes may come from any thread."""

    def __init__(self, sock, address):
        self.sock = sock
        self.address = address
        self.rfile = sock.makefile("rb")
        self._send_lock = threading.Lock()
        self.closed = False
        # Persistent namespace for interactive sessions, created on first use by the executor.
        self.namespace = None

    def send(self, header, payload=None):
        if payload is not None:
            header = dict(header, size=len(payload))
        data = (json.dumps(header, default=repr) + "\n").encode("utf-8")
        with self._send_lock:
            if self.closed:
                return False
            try:
                self.sock.sendall(data)
                if payload:
                    self.sock.sendall(payload)
            except OSError:
                self.closed = True
                return False
        return True

    def send_file(self, header, path):
        size = os.path.getsize(path)
        data = (json.dumps(dict(header, size=size)) + "\n").encode("utf-8")
        with self._send_lock, open(path, "rb") as fh:
            self.sock.sendall(data)
            remaining = size
            while remaining > 0:
                chunk = fh.read(min(_CHUNK, remaining))
                if not chunk:
                    # Truncated under us; pad so the stream stays in step with the header.
                    chunk = b"\0" * remaining
                self.sock.sendall(chunk)
                remaining -= len(chunk)

    def read_line(self):
        line = self.rfile.readline(_MAX_HEADER + 1)
        if not line:
            return None
        if len(line) > _MAX_HEADER:
            raise ProtocolError("header too large")
        return line

    def read_header(self):
        line = self.read_line()
        if line is None:
            return None
        return self.parse_header(line)

    @staticmethod
    def parse_header(line):
        try:
            header = json.loads(line.decode("utf-8"))
        except ValueError as ex:
            raise ProtocolError("malformed request: {:s}".format(str(ex)))
        if not isinstance(header, dict):
            raise ProtocolError("request must be a JSON object")
        return header

    def read_payload_into(self, size, fh):
        remaining = size
        while remaining > 0:
            chunk = self.rfile.read(min(_CHUNK, remaining))
            if not chunk:
                raise ProtocolError("connection closed during transfer")
            if fh is not None:
                fh.write(chunk)
            remaining -= len(chunk)

    def read_payload(self, size):
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = self.rfile.read(min(_CHUNK, remaining))
            if not chunk:
                raise ProtocolError("connection closed during transfer")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self):
        with self._send_lock:
            self.closed = True
        for fn in (self.rfile.close, lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except OSError:
                pass


class Job:
    """A request waiting for the main thread."""

    __slots__ = ("conn", "request", "done")

    def __init__(self, conn, request):
        self.conn = conn
        self.request = request
        self.done = threading.Event()


class Server:
    """
    :arg token: Shared secret every client must present.
    :arg executor: ``executor(job)`` runs a :data:`MAIN_THREAD_OPS` request. It is only
       called from :meth:`pump`, which the owner calls on Blender's main thread, and it
       answers through ``job.conn.send``.
    :arg workspace: Directory relative transfer paths resolve against.
    :arg http_pages: ``{path: callable() -> (bytes, content_type)}`` served without a token.
    :arg info: ``callable() -> dict`` merged into the hello reply.
    """

    def __init__(
            self, *,
            token,
            executor,
            workspace,
            port=DEFAULT_PORT,
            host="127.0.0.1",
            http_pages=None,
            info=None,
            log=None,
    ):
        if not token:
            raise ValueError("a token is required")
        self.token = token
        self.executor = executor
        self.workspace = workspace
        self.host = host
        self.port = port
        self.http_pages = http_pages or {}
        self.info = info
        self.log = log or (lambda msg: None)

        self._sock = None
        self._thread = None
        self._jobs = queue.Queue()
        self._connections = set()
        self._connections_lock = threading.Lock()
        self._stopping = threading.Event()

    # -------------------------------------------------------------------------
    # Lifecycle

    @property
    def running(self):
        return self._sock is not None

    def start(self):
        if self._sock is not None:
            return
        os.makedirs(self.workspace, exist_ok=True)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
            sock.listen(8)
        except OSError:
            sock.close()
            raise
        # Port 0 asks the system for a free one; report the real number.
        self.port = sock.getsockname()[1]
        self._stopping.clear()
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name="remote-control", daemon=True)
        self._thread.start()
        self.log("listening on {:s}:{:d}".format(self.host, self.port))

    def stop(self):
        sock, self._sock = self._sock, None
        if sock is None:
            return
        self._stopping.set()
        # shutdown() first: on Linux close() alone does not wake a thread blocked in
        # accept(), and the port then stays taken until it returns.
        for fn in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
            try:
                fn()
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2.0)
        self._thread = None
        with self._connections_lock:
            connections = list(self._connections)
        for conn in connections:
            conn.close()
        # Release anybody still waiting on the main thread.
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            job.done.set()
        self.log("stopped")

    @property
    def client_count(self):
        with self._connections_lock:
            return len(self._connections)

    # -------------------------------------------------------------------------
    # Main thread

    def has_jobs(self):
        return not self._jobs.empty()

    def pump(self, max_jobs=1):
        """Run up to ``max_jobs`` queued requests. Call from the main thread only."""
        handled = 0
        while handled < max_jobs:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            try:
                if not job.conn.closed:
                    self.executor(job)
            except BaseException as ex:
                job.conn.send({
                    "id": job.request.get("id"),
                    "type": "result",
                    "ok": False,
                    "error": "internal error: {!r}".format(ex),
                })
            finally:
                job.done.set()
            handled += 1
        return handled

    # -------------------------------------------------------------------------
    # Network thread

    def _accept_loop(self):
        sock = self._sock
        while not self._stopping.is_set():
            try:
                client, address = sock.accept()
            except OSError:
                break
            # Belt and braces: the socket is bound to loopback, but refuse anything else
            # outright should it ever be bound wider.
            if self.host in {"127.0.0.1", "localhost"} and address[0] != "127.0.0.1":
                client.close()
                continue
            threading.Thread(
                target=self._serve, args=(client, address), name="remote-control-client", daemon=True,
            ).start()

    def _serve(self, client, address):
        conn = Connection(client, address)
        with self._connections_lock:
            self._connections.add(conn)
        try:
            first = conn.read_line()
            if first is None:
                return
            if first.startswith((b"GET ", b"HEAD ")):
                self._serve_http(conn, first)
                return
            if not self._handshake(conn, first):
                return
            while not self._stopping.is_set():
                header = conn.read_header()
                if header is None:
                    break
                self._dispatch(conn, header)
        except ProtocolError as ex:
            conn.send({"type": "error", "error": str(ex)})
        except OSError:
            pass
        finally:
            conn.close()
            with self._connections_lock:
                self._connections.discard(conn)

    def _handshake(self, conn, line):
        header = conn.parse_header(line)
        token = header.get("token")
        if header.get("op") != "hello" or not isinstance(token, str) or not hmac.compare_digest(
                token.encode("utf-8"), self.token.encode("utf-8")):
            # Slow down guessing. The token is long enough that this is only manners.
            time.sleep(0.5)
            conn.send({"type": "error", "error": "authentication failed: wrong or missing token"})
            self.log("rejected a client with a wrong token")
            return False
        reply = {"type": "hello", "ok": True, "protocol": PROTOCOL_VERSION, "workspace": self.workspace}
        if self.info is not None:
            try:
                reply.update(self.info())
            except Exception:
                pass
        conn.send(reply)
        return True

    def _dispatch(self, conn, header):
        op = header.get("op")
        rid = header.get("id")
        if op in MAIN_THREAD_OPS:
            job = Job(conn, header)
            self._jobs.put(job)
            # One request at a time per connection keeps replies in order.
            while not job.done.wait(0.25):
                if self._stopping.is_set() or conn.closed:
                    return
            return
        handler = getattr(self, "_op_" + str(op), None)
        if handler is None:
            # Skip any payload so the stream stays in step.
            if header.get("size"):
                conn.read_payload_into(int(header["size"]), None)
            conn.send({"id": rid, "type": "result", "ok": False, "error": "unknown operation {!r}".format(op)})
            return
        try:
            handler(conn, header)
        except ProtocolError:
            raise
        except OSError as ex:
            conn.send({"id": rid, "type": "result", "ok": False, "error": str(ex)})

    def resolve(self, path):
        if not isinstance(path, str) or not path:
            path = "."
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(self.workspace, path)
        return os.path.normpath(path)

    # -------------------------------------------------------------------------
    # Operations answered on the network thread

    def _op_ping(self, conn, header):
        conn.send({"id": header.get("id"), "type": "result", "ok": True, "value": "pong"})

    def _op_put(self, conn, header):
        rid = header.get("id")
        size = int(header.get("size", 0))
        path = self.resolve(header.get("path"))
        tmp = path + ".part"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fh = open(tmp, "wb")
        except OSError as ex:
            # The payload must still be consumed, or the next header would be read from the
            # middle of the file.
            conn.read_payload_into(size, None)
            conn.send({"id": rid, "type": "result", "ok": False, "error": str(ex)})
            return
        try:
            with fh:
                conn.read_payload_into(size, fh)
            os.replace(tmp, path)
        except BaseException as ex:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if isinstance(ex, OSError):
                # Part of the payload may still be unread, so the stream cannot be trusted
                # past this point: end the connection with the reason.
                raise ProtocolError("cannot write {:s}: {:s}".format(path, str(ex)))
            raise
        conn.send({"id": rid, "type": "result", "ok": True, "value": path})

    def _op_get(self, conn, header):
        rid = header.get("id")
        path = self.resolve(header.get("path"))
        if not os.path.isfile(path):
            conn.send({"id": rid, "type": "result", "ok": False, "error": "no such file: {:s}".format(path)})
            return
        conn.send_file({"id": rid, "type": "file", "path": path}, path)

    def _op_ls(self, conn, header):
        rid = header.get("id")
        path = self.resolve(header.get("path"))
        if os.path.isfile(path):
            entries = [_stat_entry(os.path.dirname(path), os.path.basename(path))]
        else:
            entries = [_stat_entry(path, name) for name in sorted(os.listdir(path))]
        conn.send({"id": rid, "type": "result", "ok": True, "value": entries, "path": path})

    def _op_rm(self, conn, header):
        rid = header.get("id")
        path = self.resolve(header.get("path"))
        if path in {os.path.normpath(self.workspace), "/"}:
            conn.send({"id": rid, "type": "result", "ok": False, "error": "refusing to remove {:s}".format(path)})
            return
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        conn.send({"id": rid, "type": "result", "ok": True, "value": path})

    def _op_mkdir(self, conn, header):
        path = self.resolve(header.get("path"))
        os.makedirs(path, exist_ok=True)
        conn.send({"id": header.get("id"), "type": "result", "ok": True, "value": path})

    # -------------------------------------------------------------------------
    # HTTP, for the installer

    def _serve_http(self, conn, request):
        # Drain the request headers.
        while True:
            line = conn.read_line()
            if not line or line in {b"\r\n", b"\n"}:
                break
        try:
            method, target = request.decode("latin-1").split()[:2]
        except ValueError:
            method, target = "GET", "/"
        path = target.split("?", 1)[0]
        page = self.http_pages.get(path)
        if page is None:
            body, ctype, status = b"not found\n", "text/plain; charset=utf-8", "404 Not Found"
        else:
            body, ctype = page()
            status = "200 OK"
        head = (
            "HTTP/1.0 {:s}\r\n"
            "Content-Type: {:s}\r\n"
            "Content-Length: {:d}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).format(status, ctype, len(body)).encode("latin-1")
        with conn._send_lock:
            conn.sock.sendall(head if method == "HEAD" else head + body)


def _stat_entry(directory, name):
    full = os.path.join(directory, name)
    try:
        st = os.stat(full)
        return {"name": name, "size": st.st_size, "dir": os.path.isdir(full), "mtime": st.st_mtime}
    except OSError:
        return {"name": name, "size": 0, "dir": False, "mtime": 0}
