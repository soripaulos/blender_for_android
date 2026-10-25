# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Remote control: drive a running Blender from a terminal on the same device.

On a desktop Blender is scripted from the shell (``blender file.blend -P build.py -f 1``).
On Android there is no shell to start it from and no ``argv`` to pass: Blender is a
library inside a ``NativeActivity``. This module gives Termux (or anything else on the
device) the same reach through a loopback socket instead. The client is ``blender-cli``,
which lives next to this file (``blender_cli.py``) and is served to Termux by the server
itself, so a fresh install is one pasted ``curl`` line.

Security: the server listens on ``127.0.0.1`` only, is off until the user starts it, and
every connection has to present a random token that is only shown inside Blender. Any
app on the device can open a loopback socket, so the token is what keeps them out --
whoever holds it can run arbitrary Python as Blender.

The network side (:mod:`.server`) never touches ``bpy``. Requests that do are queued and
run from a ``bpy.app.timers`` callback, on the main thread, with the same context a
script started with ``--python`` would get: a window, its screen, and the largest 3D
viewport.
"""

__all__ = (
    "config_load",
    "config_save",
    "is_running",
    "start",
    "status",
    "stop",
)

import contextlib
import io
import json
import os
import secrets
import sys
import traceback

from .server import (
    DEFAULT_PORT,
    PROTOCOL_VERSION,
    Server,
)

_server = None
_executing = False
_last_error = ""

# How often the timer looks for queued requests. Small enough that a command feels
# immediate, large enough to cost nothing while idle.
_POLL_INTERVAL = 0.05

CLI_FILENAME = "blender_cli.py"


def _log(msg):
    print("Remote control:", msg)


# -----------------------------------------------------------------------------
# Paths and configuration

def _files_dir():
    # Published by BlenderActivity. Fall back to the user resource directory so this still
    # works (for testing) on a platform that is not Android.
    path = os.environ.get("BLENDER_ANDROID_FILES_DIR")
    if path:
        return path
    import bpy
    return bpy.utils.resource_path('USER')


def config_path():
    import bpy
    return os.path.join(bpy.utils.user_resource('CONFIG'), "remote_control.json")


def workspace_path():
    """Where relative paths sent by the client land: uploads, renders, scratch files."""
    return os.path.join(_files_dir(), "remote")


def marker_path():
    """
    Present while the server runs. ``BlenderActivity`` watches it and keeps a foreground
    service alive meanwhile, because a backgrounded app without one is frozen by the
    cached-apps freezer within seconds -- exactly when the user has switched to Termux.
    """
    return os.path.join(_files_dir(), "remote_control.active")


def config_load():
    config = {"autostart": False, "port": DEFAULT_PORT, "token": ""}
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            config.update({k: v for k, v in data.items() if k in config})
    except (OSError, ValueError):
        pass
    if not isinstance(config["port"], int) or not (0 < config["port"] < 65536):
        config["port"] = DEFAULT_PORT
    if not isinstance(config["token"], str) or len(config["token"]) < 16:
        config["token"] = new_token()
        config_save(config)
    return config


def config_save(config):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    # The token is a credential: keep the file private even on a shared filesystem.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    os.replace(tmp, path)


def new_token():
    return secrets.token_urlsafe(24)


def setup_command(config=None):
    """The one line the user pastes into Termux to install ``blender-cli`` and pair it."""
    if config is None:
        config = config_load()
    return "curl -fsS http://127.0.0.1:{:d}/install | sh -s -- {:s}".format(config["port"], config["token"])


# -----------------------------------------------------------------------------
# Server lifecycle

def is_running():
    return _server is not None and _server.running


def status():
    config = config_load()
    return {
        "running": is_running(),
        "port": _server.port if is_running() else config["port"],
        "clients": _server.client_count if is_running() else 0,
        "autostart": config["autostart"],
        "workspace": workspace_path(),
        "error": _last_error,
    }


def start(port=None):
    """Start the server. Returns an error message, or an empty string on success."""
    global _server, _last_error
    import bpy

    if is_running():
        return ""
    config = config_load()
    if port is not None:
        config["port"] = port
        config_save(config)

    server = Server(
        token=config["token"],
        port=config["port"],
        executor=_execute,
        workspace=workspace_path(),
        http_pages={
            "/": _page_index,
            "/install": lambda: _page_install(server),
            "/blender-cli": _page_cli,
        },
        info=_hello_info,
        log=_log,
    )
    try:
        server.start()
    except OSError as ex:
        _last_error = "Cannot listen on port {:d}: {:s}".format(config["port"], ex.strerror or str(ex))
        _log(_last_error)
        return _last_error

    _server = server
    _last_error = ""
    _marker_write(True)
    if not bpy.app.timers.is_registered(_timer):
        # Persistent: a script that opens a .blend must not stop the server that ran it.
        bpy.app.timers.register(_timer, first_interval=_POLL_INTERVAL, persistent=True)
    _tag_redraw_preferences()
    return ""


def stop(*, tag_redraw=True):
    """
    :arg tag_redraw: Pass False at exit: by then the window manager is gone, and reaching
       for it through ``bpy.context`` takes the whole process down.
    """
    global _server
    server, _server = _server, None
    if server is not None:
        server.stop()
    _marker_write(False)
    if tag_redraw:
        _tag_redraw_preferences()


def regenerate_token():
    config = config_load()
    config["token"] = new_token()
    config_save(config)
    if is_running():
        # Existing clients authenticated with the old token; drop them.
        port = _server.port
        stop()
        start(port)
    return config["token"]


def _marker_write(active):
    path = marker_path()
    try:
        if active:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                # The PID lets the activity ignore a marker left behind by a killed process.
                fh.write("{:d}\n".format(os.getpid()))
        elif os.path.exists(path):
            os.remove(path)
    except OSError as ex:
        _log("cannot update {:s}: {:s}".format(path, str(ex)))


def _timer():
    if not is_running():
        return None
    if _executing:
        # A running script re-entered the event loop (a modal operator, a redraw). Never
        # start a second request inside the first.
        return _POLL_INTERVAL
    _server.pump(max_jobs=1)
    return 0.0 if _server is not None and _server.has_jobs() else _POLL_INTERVAL


def autostart():
    """Called once after startup: start the server if the user asked for that."""
    try:
        if config_load()["autostart"]:
            start()
        else:
            # A marker from a process that was killed while serving.
            _marker_write(False)
    except Exception as ex:
        _log("autostart failed: {!r}".format(ex))
    return None


def _tag_redraw_preferences():
    import bpy
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'PREFERENCES':
                area.tag_redraw()


# -----------------------------------------------------------------------------
# HTTP pages (no token needed: nothing here is secret)

def _cli_source():
    with open(os.path.join(os.path.dirname(__file__), CLI_FILENAME), "rb") as fh:
        return fh.read()


def _page_cli():
    return _cli_source(), "text/x-python; charset=utf-8"


def _page_index():
    text = (
        "Blender remote control (protocol {:d}).\n"
        "Install the Termux client with:\n"
        "  curl -fsS http://127.0.0.1:{:d}/install | sh -s -- <token>\n"
        "The token is shown in Blender: Preferences > System > Remote Control.\n"
    ).format(PROTOCOL_VERSION, _server.port if _server else DEFAULT_PORT)
    return text.encode("utf-8"), "text/plain; charset=utf-8"


def _page_install(server):
    script = _INSTALL_SCRIPT.replace("@PORT@", str(server.port))
    return script.encode("utf-8"), "text/x-shellscript; charset=utf-8"


_INSTALL_SCRIPT = r"""#!/bin/sh
# Installs blender-cli into Termux and pairs it with the running Blender.
# Usage: curl -fsS http://127.0.0.1:@PORT@/install | sh -s -- <token>
set -e
PORT=@PORT@
TOKEN="$1"
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
BIN="$PREFIX/bin"

if ! command -v python3 >/dev/null 2>&1; then
  echo "blender-cli needs Python; installing it with pkg..."
  pkg install -y python
fi

TMP="$BIN/.blender-cli.$$"
curl -fsS "http://127.0.0.1:$PORT/blender-cli" -o "$TMP"
# Point the shebang straight at Termux's interpreter rather than relying on
# termux-exec to rewrite /usr/bin/env.
PY="$(command -v python3)"
sed "1s|.*|#!$PY|" "$TMP" > "$BIN/blender-cli"
rm -f "$TMP"
chmod 700 "$BIN/blender-cli"
echo "Installed $BIN/blender-cli"

if [ -n "$TOKEN" ]; then
  "$BIN/blender-cli" pair "$TOKEN" --port "$PORT"
fi
"$BIN/blender-cli" status || true
echo
echo "Try:  blender-cli --python-expr 'import bpy; bpy.ops.mesh.primitive_monkey_add()'"
echo "Help: blender-cli --help"
"""


# -----------------------------------------------------------------------------
# Main-thread execution

def _hello_info():
    import bpy
    return {
        "blender": bpy.app.version_string,
        "file": bpy.data.filepath,
        "python": sys.version.split()[0],
    }


class _StreamWriter(io.TextIOBase):
    """Forwards ``print`` output to the client line by line, and still to the log."""

    def __init__(self, conn, rid, kind, mirror):
        super().__init__()
        self._conn = conn
        self._rid = rid
        self._kind = kind
        self._mirror = mirror
        self._buffer = []

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        if self._mirror is not None:
            try:
                self._mirror.write(text)
            except Exception:
                pass
        self._buffer.append(text)
        if "\n" in text or sum(map(len, self._buffer)) > 8192:
            self.flush()
        return len(text)

    def flush(self):
        if self._buffer:
            data = "".join(self._buffer)
            self._buffer.clear()
            self._conn.send({"id": self._rid, "type": self._kind, "data": data})


def _context_override(mode):
    """The context a ``--python`` script would see: a window, and a 3D viewport if any."""
    import bpy
    if mode == "none":
        return contextlib.nullcontext()
    wm = bpy.context.window_manager
    if wm is None or not wm.windows:
        return contextlib.nullcontext()
    window = bpy.context.window or wm.windows[0]
    screen = window.screen
    kwargs = {"window": window, "screen": screen}
    if mode != "window" and screen is not None:
        areas = [area for area in screen.areas if area.type == 'VIEW_3D']
        if areas:
            area = max(areas, key=lambda a: a.width * a.height)
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            kwargs["area"] = area
            if region is not None:
                kwargs["region"] = region
    return bpy.context.temp_override(**kwargs)


def _new_namespace(filename):
    import bpy
    return {
        "__name__": "__main__",
        "__file__": filename,
        "__builtins__": __builtins__,
        "bpy": bpy,
        # The Python console's conveniences, handy in one-liners.
        "C": bpy.context,
        "D": bpy.data,
    }


def _format_exception(filename):
    exc_type, exc, tb = sys.exc_info()
    # Drop the frames of this module so the traceback starts at the user's code.
    while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
        tb = tb.tb_next
    return "".join(traceback.format_exception(exc_type, exc, tb))


def _json_value(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _after_script(message):
    import bpy
    # Show the result: a script run from a timer otherwise leaves the viewport stale until
    # the user touches it.
    wm = bpy.context.window_manager
    if wm is not None:
        for window in wm.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    # One undo step per command, so a script can be taken back from the Edit menu.
    try:
        with _context_override("window"):
            bpy.ops.ed.undo_push(message=message)
    except Exception:
        pass


def _quit():
    import bpy
    stop()
    with _context_override("window"):
        bpy.ops.wm.quit_blender()
    return None


def _execute(job):
    global _executing
    import bpy

    request = job.request
    conn = job.conn
    rid = request.get("id")
    op = request.get("op")

    if op == "quit":
        conn.send({"id": rid, "type": "result", "ok": True, "value": None})
        # Deferred so the reply leaves before the process does.
        bpy.app.timers.register(_quit, first_interval=0.2)
        return

    code = request.get("code", "")
    if not isinstance(code, str):
        conn.send({"id": rid, "type": "result", "ok": False, "error": "code must be a string"})
        return
    filename = request.get("name") or ("<expr>" if op == "eval" else "<remote>")
    mode = "eval" if op == "eval" else request.get("mode", "exec")
    if mode not in {"exec", "eval", "single"}:
        mode = "exec"

    if request.get("session"):
        if conn.namespace is None:
            conn.namespace = _new_namespace("<console>")
        namespace = conn.namespace
    else:
        namespace = _new_namespace(filename)

    argv = request.get("argv")
    if not isinstance(argv, list):
        argv = []
    old_argv = sys.argv
    # Mirrors the command line a desktop script parses: everything after "--" is its own.
    sys.argv = ["blender", "--python", filename, "--"] + [str(a) for a in argv]

    old_cwd = os.getcwd()
    workspace = workspace_path()
    out = _StreamWriter(conn, rid, "stdout", sys.__stdout__)
    err = _StreamWriter(conn, rid, "stderr", sys.__stderr__)

    reply = {"id": rid, "type": "result", "ok": True, "value": None}
    _executing = True
    try:
        os.makedirs(workspace, exist_ok=True)
        os.chdir(workspace)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                compiled = compile(code, filename, mode)
                with _context_override(request.get("context", "view3d")):
                    value = eval(compiled, namespace) if mode == "eval" else exec(compiled, namespace)
                if mode == "eval":
                    reply["value"] = _json_value(value)
                    reply["repr"] = repr(value)
                elif "__result__" in namespace:
                    reply["value"] = _json_value(namespace["__result__"])
            except SystemExit as ex:
                if ex.code not in {None, 0}:
                    reply.update(ok=False, error="SystemExit: {!s}".format(ex.code), exit_code=ex.code)
            except BaseException:
                reply.update(ok=False, error=_format_exception(filename))
    finally:
        _executing = False
        out.flush()
        err.flush()
        sys.argv = old_argv
        try:
            os.chdir(old_cwd)
        except OSError:
            pass

    if mode != "eval":
        _after_script("Remote: {:s}".format(os.path.basename(filename)))
    conn.send(reply)
