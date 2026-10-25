#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
blender-cli: control Blender for Android from Termux, the way ``blender`` is driven from
a shell on a desktop.

    blender-cli scene.blend --python build.py -o out/frame_### -f 1
    blender-cli --python-expr "import bpy; bpy.ops.mesh.primitive_monkey_add()"
    blender-cli run build.py -- --size 2
    blender-cli shell

The arguments are read in order and acted on one at a time, exactly like Blender's own
command line, so ``-o`` and ``-E`` apply to the renders that follow them. Scripts are read
here and sent over, so they can live anywhere Termux can read. Files Blender cannot see
(Termux's home directory is private to Termux) are copied across and renders are copied
back automatically.

This file is standalone: it only needs Python's standard library. It is installed by
pasting the setup line Blender shows in Preferences > System > Remote Control.
"""

import json
import os
import socket
import subprocess
import sys
import time
import uuid

VERSION = "1.0"
PROTOCOL_VERSION = 1
DEFAULT_PORT = 8642
PACKAGE = "org.blender.blender"
ACTIVITY = PACKAGE + "/.BlenderActivity"
CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
    "blender-cli", "config.json",
)
# Paths Blender can open directly once it has "All files access". Anything else (notably
# Termux's own home) is private to Termux and has to be copied across.
SHARED_PREFIXES = ("/storage/", "/sdcard/", "/mnt/media_rw/")

EXIT_OK = 0
EXIT_SCRIPT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONNECTION = 3

USAGE = """\
usage: blender-cli [blender arguments...]
       blender-cli <command> [arguments...]

Blender-style arguments (processed in order, like the desktop command line):
  FILE.blend                  open a .blend (copied over if Blender cannot read it)
  -P, --python FILE           run a Python script
  --python-expr EXPR          run a Python expression/statement
  --python-text NAME          run a text block of the open file
  --python-exit-code N        exit with N when a script fails (default 1)
  -f, --render-frame FRAMES   render frames: 1 | 1,5,9 | 10..20 | -1 (current)
  -a, --render-anim           render the scene's frame range
  -s, --frame-start N         set the start frame
  -e, --frame-end N           set the end frame
  -j, --frame-jump N          set the frame step
  -o, --render-output PATH    output path, # becomes the frame number
                              (a Termux path: rendered remotely, copied back)
  -F, --render-format FMT     PNG, JPEG, OPEN_EXR, FFMPEG, ...
  -x, --use-extension 0|1     add the file extension to output names
  -E, --engine ENGINE         BLENDER_EEVEE, CYCLES, BLENDER_WORKBENCH (or "help")
  -S, --scene NAME            switch to a scene
  -t, --threads N             render threads (0 = all cores)
  --new                       start a new file from the startup scene
  --save [PATH]               save the open file (PATH: save as; Termux paths copied back)
  --quit                      quit Blender when done
  -v, --version               print Blender's version
  -b, --background, --factory-startup, -noaudio
                              accepted for compatibility, no effect (Blender is already
                              running with its interface)
  --                          the rest goes to the scripts' sys.argv

Commands:
  pair TOKEN [--port N]       store the token Blender shows (setup does this for you)
  status                      check the connection and show what Blender has open
  run SCRIPT [ARGS...]        run a script; ARGS reach it after "--" in sys.argv
  exec CODE                   run Python code
  eval EXPR                   evaluate an expression and print the result
  shell                       interactive Python console running inside Blender
  open FILE.blend             open a file
  save [PATH]                 save (as PATH)
  render [blender args]       shorthand: blender-cli render -o out/#### -a
  push LOCAL [REMOTE]         copy a file to Blender's workspace (or REMOTE path)
  pull REMOTE [LOCAL]         copy a file back from Blender
  ls [REMOTE]                 list Blender's workspace (or REMOTE directory)
  rm REMOTE                   delete a file or directory on Blender's side
  launch                      start Blender (it starts the server itself if enabled)
  quit                        quit Blender

Options for every command:
  --host HOST, --port N, --token TOKEN   override the stored connection settings
  --no-launch                 do not start Blender when it is not answering
  --timeout SECONDS           how long to wait for Blender to come up (default 90)

Environment: BLENDER_REMOTE_HOST, BLENDER_REMOTE_PORT, BLENDER_REMOTE_TOKEN.
Relative remote paths are relative to Blender's workspace, shown by "status".
"""


class CLIError(Exception):
    def __init__(self, message, code=EXIT_USAGE):
        super().__init__(message)
        self.code = code


class ScriptError(Exception):
    pass


def err(msg):
    sys.stderr.write("blender-cli: {:s}\n".format(msg))


# -----------------------------------------------------------------------------
# Configuration

def config_load():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def config_save(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    fd = os.open(CONFIG_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    os.replace(CONFIG_PATH + ".tmp", CONFIG_PATH)


# -----------------------------------------------------------------------------
# Connection

class Client:
    def __init__(self, host, port, token):
        self.host = host
        self.port = port
        self.token = token
        self.sock = None
        self.rfile = None
        self.hello = {}
        self._next_id = 0

    def connect(self, timeout=5.0):
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(None)
        self.sock = sock
        self.rfile = sock.makefile("rb")
        self._send({"op": "hello", "token": self.token, "client": "blender-cli/" + VERSION})
        reply = self._read_header()
        if reply is None:
            raise CLIError("Blender closed the connection during the handshake", EXIT_CONNECTION)
        if reply.get("type") == "error" or not reply.get("ok"):
            raise CLIError(
                "{:s}\nGet the setup line from Blender: Preferences > System > Remote Control."
                .format(reply.get("error", "handshake refused")), EXIT_CONNECTION,
            )
        self.hello = reply
        return reply

    def close(self):
        for fn in (lambda: self.rfile and self.rfile.close(), lambda: self.sock and self.sock.close()):
            try:
                fn()
            except OSError:
                pass

    # Framing ------------------------------------------------------------

    def _send(self, header, payload=None):
        if payload is not None:
            header = dict(header, size=len(payload))
        self.sock.sendall((json.dumps(header) + "\n").encode("utf-8"))
        if payload:
            self.sock.sendall(payload)

    def _read_header(self):
        line = self.rfile.readline()
        if not line:
            return None
        return json.loads(line.decode("utf-8"))

    def _read_exact(self, size, fh=None):
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = self.rfile.read(min(256 * 1024, remaining))
            if not chunk:
                raise CLIError("connection closed during transfer", EXIT_CONNECTION)
            if fh is not None:
                fh.write(chunk)
            else:
                chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def request(self, header, payload=None, on_file=None, quiet=False):
        """Send a request and stream its output until the result arrives."""
        self._next_id += 1
        rid = self._next_id
        self._send(dict(header, id=rid), payload)
        while True:
            reply = self._read_header()
            if reply is None:
                raise CLIError("Blender closed the connection", EXIT_CONNECTION)
            kind = reply.get("type")
            if kind == "stdout":
                if not quiet:
                    sys.stdout.write(reply.get("data", ""))
                    sys.stdout.flush()
            elif kind == "stderr":
                if not quiet:
                    sys.stderr.write(reply.get("data", ""))
                    sys.stderr.flush()
            elif kind == "file":
                size = int(reply.get("size", 0))
                if on_file is None:
                    self._read_exact(size)
                else:
                    on_file(reply, size)
                # A file answers its request by itself; no result follows it.
                return reply
            elif kind == "error":
                raise CLIError(reply.get("error", "error"), EXIT_CONNECTION)
            elif kind == "result":
                return reply

    # Operations ---------------------------------------------------------

    def run(self, code, name="<remote>", argv=(), session=False, mode="exec", quiet=False, context=None):
        header = {"op": "exec", "code": code, "name": name, "argv": list(argv), "session": session, "mode": mode}
        if context:
            header["context"] = context
        reply = self.request(header, quiet=quiet)
        if not reply.get("ok"):
            raise ScriptError(reply.get("error", "script failed"))
        return reply.get("value")

    def eval(self, expr):
        reply = self.request({"op": "eval", "code": expr})
        if not reply.get("ok"):
            raise ScriptError(reply.get("error", "evaluation failed"))
        return reply

    def put(self, local, remote):
        with open(local, "rb") as fh:
            data = fh.read()
        reply = self.request({"op": "put", "path": remote}, payload=data)
        if not reply.get("ok"):
            raise CLIError("upload failed: {:s}".format(reply.get("error", "")), EXIT_SCRIPT_ERROR)
        return reply["value"]

    def get(self, remote, local):
        written = []

        def on_file(reply, size):
            target = local
            if os.path.isdir(target):
                target = os.path.join(target, os.path.basename(reply.get("path", remote)))
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            tmp = target + ".part"
            with open(tmp, "wb") as fh:
                self._read_exact(size, fh)
            os.replace(tmp, target)
            written.append(target)

        reply = self.request({"op": "get", "path": remote}, on_file=on_file)
        if reply.get("type") == "result" and not reply.get("ok"):
            raise CLIError("download failed: {:s}".format(reply.get("error", "")), EXIT_SCRIPT_ERROR)
        return written[0] if written else None

    def simple(self, op, **kwargs):
        reply = self.request(dict(kwargs, op=op))
        if not reply.get("ok"):
            raise CLIError(reply.get("error", "{:s} failed".format(op)), EXIT_SCRIPT_ERROR)
        return reply.get("value")


def launch_blender():
    """Bring Blender up with Termux's ``am``. Returns False when that is not possible."""
    for cmd in (["am", "start", "-n", ACTIVITY], ["/system/bin/am", "start", "-n", ACTIVITY]):
        try:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False


def open_client(opts):
    config = config_load()
    host = opts.host or os.environ.get("BLENDER_REMOTE_HOST") or config.get("host") or "127.0.0.1"
    port = int(opts.port or os.environ.get("BLENDER_REMOTE_PORT") or config.get("port") or DEFAULT_PORT)
    token = opts.token or os.environ.get("BLENDER_REMOTE_TOKEN") or config.get("token")
    if not token:
        raise CLIError(
            "not paired with Blender yet.\n"
            "In Blender open Preferences > System > Remote Control, press Start and\n"
            "\"Copy Setup Command\", then paste that line here.", EXIT_CONNECTION,
        )
    client = Client(host, port, token)
    try:
        client.connect()
        return client
    except OSError as ex:
        first_error = ex
    if opts.no_launch:
        raise CLIError(
            "Blender is not answering on {:s}:{:d} ({:s}).".format(host, port, str(first_error)), EXIT_CONNECTION,
        )
    err("Blender is not answering; starting it...")
    if not launch_blender():
        raise CLIError("could not start Blender with 'am'. Open it by hand and try again.", EXIT_CONNECTION)
    deadline = time.monotonic() + opts.timeout
    while time.monotonic() < deadline:
        time.sleep(1.0)
        try:
            client.connect()
            return client
        except CLIError:
            raise
        except OSError:
            continue
    raise CLIError(
        "Blender started but the remote control server did not come up.\n"
        "Enable Preferences > System > Remote Control > Start Automatically.", EXIT_CONNECTION,
    )


# -----------------------------------------------------------------------------
# Helpers

def is_shared(path):
    real = os.path.realpath(path)
    return real.startswith(SHARED_PREFIXES) or real in {p.rstrip("/") for p in SHARED_PREFIXES}


def py(value):
    """Python literal for a value, to splice into generated code."""
    return repr(value)


def parse_frames(spec):
    frames = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ".." in part:
            a, b = part.split("..", 1)
            a, b = int(a), int(b)
            step = 1 if b >= a else -1
            frames.extend(range(a, b + step, step))
        else:
            frames.append(int(part))
    if not frames:
        raise CLIError("no frames in {!r}".format(spec))
    return frames


class Runner:
    """Carries out Blender-style arguments in order, like ``creator_args.cc`` does."""

    def __init__(self, client):
        self.client = client
        self.script_args = []
        self.exit_code_on_error = EXIT_SCRIPT_ERROR
        self.failed = False
        self.quit = False
        # Local destination of the render output, when Blender renders somewhere else.
        self.output_local = None
        self.output_remote = None
        self.workspace = client.hello.get("workspace", "")

    # Remote file access --------------------------------------------------

    def remote_exists(self, path):
        reply = self.client.eval("__import__('os').path.exists({:s})".format(py(path)))
        return bool(reply.get("value"))

    def remote_writable_dir(self, path):
        reply = self.client.eval(
            "(lambda os, d: os.path.isdir(d) and os.access(d, os.W_OK))(__import__('os'), {:s})".format(py(path)))
        return bool(reply.get("value"))

    def input_path(self, path):
        """A path Blender can open for a local file, copying it across when needed."""
        local = os.path.abspath(os.path.expanduser(path))
        if is_shared(local) and self.remote_exists(local):
            return local
        if not os.path.isfile(local):
            # Maybe it names a file on Blender's side already (workspace-relative or absolute).
            if self.remote_exists(path):
                return path
            raise CLIError("no such file: {:s}".format(path), EXIT_USAGE)
        remote = "files/" + os.path.basename(local)
        err("copying {:s} to Blender".format(path))
        return self.client.put(local, remote)

    # Execution ----------------------------------------------------------

    def run(self, code, name, quiet=False):
        try:
            return self.client.run(code, name=name, argv=self.script_args, quiet=quiet)
        except ScriptError as ex:
            sys.stderr.write(str(ex))
            if not str(ex).endswith("\n"):
                sys.stderr.write("\n")
            self.failed = True
            return None

    def python_file(self, path):
        local = os.path.expanduser(path)
        try:
            with open(local, "r", encoding="utf-8") as fh:
                code = fh.read()
        except OSError as ex:
            raise CLIError("cannot read {:s}: {:s}".format(path, ex.strerror or str(ex)))
        self.run(code, os.path.abspath(local))

    def python_expr(self, expr):
        self.run(expr, "<string>")

    def python_text(self, name):
        self.run(
            "import bpy\n"
            "_text = bpy.data.texts.get({0:s})\n"
            "if _text is None:\n"
            "    raise NameError('no text block named ' + {0:s})\n"
            "exec(compile(_text.as_string(), _text.name, 'exec'), {{'__name__': '__main__', 'bpy': bpy}})\n"
            .format(py(name)), "<text:{:s}>".format(name))

    def open_file(self, path):
        remote = self.input_path(path)
        self.run(
            "import bpy\nbpy.ops.wm.open_mainfile(filepath={:s})\n"
            "print('Opened', bpy.data.filepath)\n".format(py(remote)), "<open>")

    def new_file(self):
        self.run("import bpy\nbpy.ops.wm.read_homefile()\n", "<new>")

    def save(self, path=None):
        if path is None:
            self.run(
                "import bpy\n"
                "if not bpy.data.filepath:\n"
                "    raise RuntimeError('the file has never been saved: give a path, --save FILE.blend')\n"
                "bpy.ops.wm.save_mainfile()\nprint('Saved', bpy.data.filepath)\n", "<save>")
            return
        local = os.path.abspath(os.path.expanduser(path))
        if is_shared(local) and self.remote_writable_dir(os.path.dirname(local)):
            target, copy_back = local, False
        else:
            target, copy_back = "files/" + os.path.basename(local), True
        saved = self.run(
            "import bpy, os\n"
            "_p = os.path.abspath({:s})\n"
            "os.makedirs(os.path.dirname(_p), exist_ok=True)\n"
            "bpy.ops.wm.save_as_mainfile(filepath=_p)\n"
            "print('Saved', _p)\n"
            "__result__ = _p\n".format(py(target)), "<save>")
        if copy_back and saved:
            self.client.get(saved, local)
            err("copied to {:s}".format(local))

    def scene(self, name):
        self.run(
            "import bpy\n"
            "_s = bpy.data.scenes.get({0:s})\n"
            "if _s is None:\n"
            "    raise NameError('no scene named ' + {0:s})\n"
            "bpy.context.window.scene = _s\n".format(py(name)), "<scene>")

    def set_scene_value(self, attr, value, name):
        self.run("import bpy\nbpy.context.scene.{:s} = {:s}\n".format(attr, py(value)), name)

    def engine(self, name):
        if name == "help":
            self.run(
                "import bpy\n"
                "print('Engines:', ', '.join(e.identifier for e in "
                "bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items))\n", "<engine>")
            return
        self.set_scene_value("render.engine", name, "<engine>")

    def render_format(self, fmt):
        self.set_scene_value("render.image_settings.file_format", fmt.upper(), "<format>")

    def render_output(self, path):
        if path.startswith("//"):
            # Relative to the .blend: Blender writes there itself.
            self.output_local = None
            self.output_remote = path
        else:
            local = os.path.abspath(os.path.expanduser(path))
            if is_shared(local) and self.remote_writable_dir(os.path.dirname(local) or "/"):
                self.output_local = None
                self.output_remote = local
            else:
                # Render under the workspace and bring the frames back afterwards.
                self.output_local = local
                self.output_remote = "{:s}/renders/{:s}/{:s}".format(
                    self.workspace.rstrip("/"), uuid.uuid4().hex[:12], os.path.basename(local) or "####")
        self.set_scene_value("render.filepath", self.output_remote, "<output>")

    def _fetch_renders(self, paths):
        if not paths:
            return
        if self.output_local is None:
            for p in paths:
                print("Saved:", p)
            return
        dest_dir = os.path.dirname(self.output_local) or "."
        os.makedirs(dest_dir, exist_ok=True)
        for p in paths:
            target = os.path.join(dest_dir, os.path.basename(p))
            self.client.get(p, target)
            print("Saved:", target)

    # Which files a render wrote is found by comparing the output directory before and
    # after, rather than predicting names: whether "#" is replaced in a still's name, and
    # what a movie is called, differ between versions and formats.
    _RENDER_PRELUDE = (
        "import bpy, os\n"
        "_scene = bpy.context.scene\n"
        "_dir = os.path.dirname(os.path.abspath(bpy.path.abspath(_scene.render.filepath))) or '.'\n"
        "def _snapshot():\n"
        "    try:\n"
        "        return {e.path: e.stat().st_mtime_ns for e in os.scandir(_dir) if e.is_file()}\n"
        "    except OSError:\n"
        "        return {}\n"
        "_before = _snapshot()\n"
        "_range = (_scene.frame_start, _scene.frame_end)\n"
        "def _on_write(scene, *_args):\n"
        "    print('Rendered frame', scene.frame_current, flush=True)\n"
        "bpy.app.handlers.render_write.append(_on_write)\n"
    )
    _RENDER_EPILOGUE = (
        "finally:\n"
        "    bpy.app.handlers.render_write.remove(_on_write)\n"
        "    _scene.frame_start, _scene.frame_end = _range\n"
        "__result__ = sorted(p for p, t in _snapshot().items() if _before.get(p) != t)\n"
    )

    def render_frames(self, spec):
        frames = None if spec.strip() in {"-1", "current"} else parse_frames(spec)
        code = self._RENDER_PRELUDE + (
            # Like the desktop's -f, each frame is a one-frame animation render, so output
            # names follow the same rules: "#" is the frame number, appended when missing.
            "try:\n"
            "    for _f in ({:s} or [_scene.frame_current]):\n"
            "        _scene.frame_start = _scene.frame_end = _f\n"
            "        _res = bpy.ops.render.render(animation=True)\n"
            "        if 'FINISHED' not in _res:\n"
            "            raise RuntimeError('render failed or was cancelled at frame %d' % _f)\n"
        ).format(py(frames)) + self._RENDER_EPILOGUE
        self._fetch_renders(self.run(code, "<render>"))

    def render_anim(self):
        code = self._RENDER_PRELUDE + (
            "try:\n"
            "    _res = bpy.ops.render.render(animation=True)\n"
            "    if 'FINISHED' not in _res:\n"
            "        raise RuntimeError('animation render failed or was cancelled')\n"
        ) + self._RENDER_EPILOGUE
        self._fetch_renders(self.run(code, "<render>"))

    def version(self):
        print("Blender", self.client.hello.get("blender", "?"), "(Android, remote)")
        print("blender-cli", VERSION)

    # Argument loop ------------------------------------------------------

    def threads(self, value):
        n = int(value)
        code = "import bpy\n_r = bpy.context.scene.render\n_r.threads_mode = {:s}\n".format(
            py('AUTO' if n == 0 else 'FIXED'))
        if n:
            code += "_r.threads = {:d}\n".format(n)
        self.run(code, "<threads>")

    def set_quit(self):
        self.quit = True

    def set_exit_code(self, value):
        self.exit_code_on_error = int(value)

    def actions(self):
        """``{flag: (takes_value, handler)}``, named as in ``blender --help``."""
        def scene_int(attr):
            return lambda v: self.set_scene_value(attr, int(v), "<" + attr + ">")

        def use_extension(v):
            self.set_scene_value("render.use_file_extension", v not in {"0", "false"}, "<ext>")

        table = {
            ("-P", "--python"): (True, self.python_file),
            ("--python-expr",): (True, self.python_expr),
            ("--python-text",): (True, self.python_text),
            ("--python-exit-code",): (True, self.set_exit_code),
            ("-f", "--render-frame"): (True, self.render_frames),
            ("-a", "--render-anim"): (False, self.render_anim),
            ("-s", "--frame-start"): (True, scene_int("frame_start")),
            ("-e", "--frame-end"): (True, scene_int("frame_end")),
            ("-j", "--frame-jump"): (True, scene_int("frame_step")),
            ("-o", "--render-output"): (True, self.render_output),
            ("-F", "--render-format"): (True, self.render_format),
            ("-x", "--use-extension"): (True, use_extension),
            ("-E", "--engine"): (True, self.engine),
            ("-S", "--scene"): (True, self.scene),
            ("-t", "--threads"): (True, self.threads),
            ("--new",): (False, self.new_file),
            ("--quit",): (False, self.set_quit),
            ("-v", "--version"): (False, self.version),
            # Accepted so desktop command lines work unchanged; Blender is already running
            # with its interface, so none of them has anything to do here.
            ("-b", "--background", "--factory-startup", "-noaudio", "--no-window-focus", "-y",
             "--enable-autoexec", "-Y", "--disable-autoexec", "--disable-crash-handler"): (False, lambda: None),
        }
        return {flag: action for flags, action in table.items() for flag in flags}

    def process(self, args):
        # Script arguments are known before anything runs, as on the desktop.
        if "--" in args:
            i = args.index("--")
            self.script_args = args[i + 1:]
            args = args[:i]
        actions = self.actions()
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--save":
                # Optional value: a path only when the next word is not another flag.
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    self.save(args[i + 1])
                    i += 2
                else:
                    self.save()
                    i += 1
                continue
            action = actions.get(a)
            if action is None:
                if a.startswith("-"):
                    raise CLIError("unknown argument {:s} (see blender-cli --help)".format(a))
                self.open_file(a)
                i += 1
                continue
            takes_value, handler = action
            if takes_value:
                if i + 1 >= len(args):
                    raise CLIError("{:s} needs a value".format(a))
                handler(args[i + 1])
                i += 2
            else:
                handler()
                i += 1

        if self.quit:
            self.client.simple("quit")
        return self.exit_code_on_error if self.failed else EXIT_OK


# -----------------------------------------------------------------------------
# Commands

class Options:
    host = None
    port = None
    token = None
    no_launch = False
    timeout = 90.0


def extract_options(args):
    """Pull the connection options out of anywhere before ``--``."""
    opts = Options()
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest.extend(args[i:])
            break
        if a in {"--host", "--port", "--token", "--timeout"}:
            if i + 1 >= len(args):
                raise CLIError("{:s} needs a value".format(a))
            v = args[i + 1]
            if a == "--host":
                opts.host = v
            elif a == "--port":
                opts.port = int(v)
            elif a == "--token":
                opts.token = v
            else:
                opts.timeout = float(v)
            i += 2
            continue
        if a == "--no-launch":
            opts.no_launch = True
        else:
            rest.append(a)
        i += 1
    return opts, rest


def cmd_pair(opts, args):
    if not args:
        raise CLIError("usage: blender-cli pair TOKEN [--port N]")
    config = config_load()
    config["token"] = args[0]
    if opts.port:
        config["port"] = opts.port
    if opts.host:
        config["host"] = opts.host
    config_save(config)
    print("Paired. Settings saved to", CONFIG_PATH)
    return EXIT_OK


def cmd_status(opts, args):
    opts.no_launch = opts.no_launch or "--launch" not in args
    client = open_client(opts)
    hello = client.hello
    print("Connected to Blender {:s} on {:s}:{:d}".format(hello.get("blender", "?"), client.host, client.port))
    print("Open file : {:s}".format(hello.get("file") or "(unsaved)"))
    print("Workspace : {:s}".format(hello.get("workspace", "?")))
    print("Python    : {:s}".format(hello.get("python", "?")))
    return EXIT_OK


def cmd_shell(opts, args):
    import codeop
    client = open_client(opts)
    try:
        import readline  # noqa: F401  (history and line editing when available)
    except ImportError:
        pass
    print("Python {:s} inside Blender {:s}. bpy, C (context) and D (data) are ready. Ctrl-D quits.".format(
        client.hello.get("python", "?"), client.hello.get("blender", "?")))
    # What Blender's own Python console imports.
    client.run(
        "import bpy\nfrom math import *\ntry:\n    from mathutils import *\nexcept ImportError:\n    pass\n",
        name="<console>", session=True, quiet=True)
    compiler = codeop.CommandCompiler()
    buffer = []
    while True:
        try:
            line = input("... " if buffer else ">>> ")
        except EOFError:
            print()
            return EXIT_OK
        except KeyboardInterrupt:
            print("\nKeyboardInterrupt")
            buffer.clear()
            continue
        buffer.append(line)
        source = "\n".join(buffer)
        try:
            complete = compiler(source, "<console>", "single")
        except (SyntaxError, OverflowError, ValueError):
            complete = True  # let Blender report the error with its traceback
        if complete is None:
            continue
        buffer.clear()
        try:
            client.run(source + "\n", name="<console>", session=True, mode="single")
        except ScriptError as ex:
            sys.stderr.write(str(ex))


def cmd_run(opts, args):
    if not args:
        raise CLIError("usage: blender-cli run SCRIPT [ARGS...]")
    script, script_args = args[0], args[1:]
    if script_args and script_args[0] == "--":
        script_args = script_args[1:]
    return run_blender_args(opts, ["--python", script, "--"] + script_args)


def cmd_exec(opts, args):
    if not args:
        raise CLIError("usage: blender-cli exec CODE")
    code = " ".join(args)
    if code == "-":
        code = sys.stdin.read()
    return run_blender_args(opts, ["--python-expr", code])


def cmd_eval(opts, args):
    if not args:
        raise CLIError("usage: blender-cli eval EXPR")
    client = open_client(opts)
    try:
        reply = client.eval(" ".join(args))
    except ScriptError as ex:
        sys.stderr.write(str(ex))
        return EXIT_SCRIPT_ERROR
    value = reply.get("value")
    # Strings print bare so the output is usable in shell pipelines; the rest as repr().
    print(value if isinstance(value, str) and reply.get("repr") == repr(value) else reply.get("repr", value))
    return EXIT_OK


def cmd_open(opts, args):
    if len(args) != 1:
        raise CLIError("usage: blender-cli open FILE.blend")
    return run_blender_args(opts, [args[0]])


def cmd_save(opts, args):
    return run_blender_args(opts, ["--save"] + args[:1])


def cmd_render(opts, args):
    if not any(a in {"-f", "--render-frame", "-a", "--render-anim"} for a in args):
        args = args + ["-f", "-1"]
    return run_blender_args(opts, args)


def cmd_push(opts, args):
    if not args or len(args) > 2:
        raise CLIError("usage: blender-cli push LOCAL [REMOTE]")
    remote = args[1] if len(args) == 2 else "files/" + os.path.basename(args[0])
    client = open_client(opts)
    print(client.put(os.path.expanduser(args[0]), remote))
    return EXIT_OK


def cmd_pull(opts, args):
    if not args or len(args) > 2:
        raise CLIError("usage: blender-cli pull REMOTE [LOCAL]")
    local = args[1] if len(args) == 2 else os.path.basename(args[0])
    client = open_client(opts)
    print(client.get(args[0], os.path.expanduser(local)))
    return EXIT_OK


def cmd_ls(opts, args):
    client = open_client(opts)
    reply = client.request({"op": "ls", "path": args[0] if args else "."})
    if not reply.get("ok"):
        raise CLIError(reply.get("error", "ls failed"), EXIT_SCRIPT_ERROR)
    for e in reply.get("value", []):
        print("{:>12s}  {:s}{:s}".format("-" if e["dir"] else str(e["size"]), e["name"], "/" if e["dir"] else ""))
    return EXIT_OK


def cmd_rm(opts, args):
    if len(args) != 1:
        raise CLIError("usage: blender-cli rm REMOTE")
    client = open_client(opts)
    print("removed", client.simple("rm", path=args[0]))
    return EXIT_OK


def cmd_launch(opts, args):
    if not launch_blender():
        raise CLIError("could not start Blender with 'am'", EXIT_CONNECTION)
    print("Blender started")
    return EXIT_OK


def cmd_quit(opts, args):
    opts.no_launch = True
    client = open_client(opts)
    client.simple("quit")
    print("Blender is quitting")
    return EXIT_OK


COMMANDS = {
    "pair": cmd_pair,
    "status": cmd_status,
    "ping": cmd_status,
    "shell": cmd_shell,
    "console": cmd_shell,
    "repl": cmd_shell,
    "run": cmd_run,
    "exec": cmd_exec,
    "eval": cmd_eval,
    "open": cmd_open,
    "save": cmd_save,
    "render": cmd_render,
    "push": cmd_push,
    "pull": cmd_pull,
    "ls": cmd_ls,
    "rm": cmd_rm,
    "launch": cmd_launch,
    "quit": cmd_quit,
}


def run_blender_args(opts, args):
    client = open_client(opts)
    try:
        return Runner(client).process(args)
    finally:
        client.close()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if not args or args[0] in {"-h", "--help", "help"}:
            sys.stdout.write(USAGE)
            return EXIT_OK if args else EXIT_USAGE
        opts, args = extract_options(args)
        if args and args[0] in COMMANDS:
            return COMMANDS[args[0]](opts, args[1:]) or EXIT_OK
        if args == ["--cli-version"]:
            print(VERSION)
            return EXIT_OK
        return run_blender_args(opts, args)
    except CLIError as ex:
        err(str(ex))
        return ex.code
    except KeyboardInterrupt:
        err("interrupted (Blender finishes the command it was running)")
        return 130
    except (BrokenPipeError, ConnectionResetError):
        err("lost the connection to Blender")
        return EXIT_CONNECTION


if __name__ == "__main__":
    sys.exit(main())
