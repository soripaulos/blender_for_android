#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
End-to-end test of the Termux remote control without a device or a Blender build.

Runs the real server (``_bpy_internal.remote_control``) against a stand-in ``bpy`` that
has just enough of the API for the executor, pumps its timer on this thread the way
Blender's main loop would, and drives it with the real ``blender-cli`` in a subprocess.

    python3 build_files/android/termux/test_remote_control.py
"""

import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MODULES = os.path.join(REPO, "scripts", "modules")
CLI = os.path.join(MODULES, "_bpy_internal", "remote_control", "blender_cli.py")


def install_fake_bpy(root):
    """The slice of ``bpy`` the executor uses, with no window (as early in startup)."""
    timers = []

    class Timers:
        @staticmethod
        def register(fn, first_interval=0.0, persistent=False):
            timers.append(fn)

        @staticmethod
        def is_registered(fn):
            return fn in timers

    class Context:
        window_manager = None
        window = None

        @staticmethod
        def temp_override(**_kwargs):
            return contextlib.nullcontext()

    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(version_string="5.3.0 (stub)", timers=Timers())
    bpy.context = Context()
    bpy.data = types.SimpleNamespace(filepath="/stub/scene.blend")
    bpy.ops = types.SimpleNamespace(
        ed=types.SimpleNamespace(undo_push=lambda **_kw: {'FINISHED'}),
        wm=types.SimpleNamespace(quit_blender=lambda: {'FINISHED'}),
    )
    bpy.utils = types.SimpleNamespace(
        user_resource=lambda _kind: os.path.join(root, "config"),
        resource_path=lambda _kind: root,
    )
    sys.modules["bpy"] = bpy
    return timers


class RemoteControlTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        os.environ["BLENDER_ANDROID_FILES_DIR"] = os.path.join(root, "files")
        cls.timers = install_fake_bpy(root)
        sys.path.insert(0, MODULES)
        from _bpy_internal import remote_control
        cls.rc = remote_control

        # Port 0: any free port. The server reports the one it got.
        error = remote_control.start(port=0)
        assert not error, error
        cls.token = remote_control.config_load()["token"]
        cls.port = remote_control.status()["port"]

        cls.home = os.path.join(root, "termux-home")
        os.makedirs(cls.home)

        # Blender's main loop: keep running the timer.
        cls.stop_pump = threading.Event()

        def pump():
            while not cls.stop_pump.is_set():
                interval = remote_control._timer()
                time.sleep(interval or 0.01)
        cls.pump_thread = threading.Thread(target=pump, daemon=True)
        cls.pump_thread.start()

        cls.cli("pair", cls.token, "--port", str(cls.port))

    @classmethod
    def tearDownClass(cls):
        cls.stop_pump.set()
        cls.pump_thread.join(2)
        cls.rc.stop()
        assert not os.path.exists(cls.rc.marker_path()), "marker left behind"
        cls.tmp.cleanup()

    @classmethod
    def cli(cls, *args, stdin=None, check=True, cwd=None):
        env = dict(os.environ, HOME=cls.home, XDG_CONFIG_HOME=os.path.join(cls.home, ".config"))
        proc = subprocess.run(
            [sys.executable, CLI, "--no-launch", *args],
            input=stdin, capture_output=True, text=True, env=env, timeout=60, cwd=cwd or cls.home,
        )
        if check and proc.returncode != 0:
            raise AssertionError("blender-cli {!r} exited {:d}\nstdout:\n{:s}\nstderr:\n{:s}".format(
                args, proc.returncode, proc.stdout, proc.stderr))
        return proc

    def test_status(self):
        out = self.cli("status").stdout
        self.assertIn("5.3.0 (stub)", out)
        self.assertIn("/stub/scene.blend", out)

    def test_python_expr_streams_output(self):
        out = self.cli("--python-expr", "print('hello from blender'); print(bpy.app.version_string)").stdout
        self.assertEqual(out, "hello from blender\n5.3.0 (stub)\n")

    def test_python_file_gets_script_args(self):
        script = os.path.join(self.home, "args.py")
        with open(script, "w") as fh:
            fh.write("import sys\nprint(sys.argv[sys.argv.index('--') + 1:])\nprint(__file__)\n")
        out = self.cli("--python", script, "--", "--size", "2").stdout.splitlines()
        self.assertEqual(out[0], "['--size', '2']")
        self.assertEqual(out[1], script)
        out = self.cli("run", script, "a", "b").stdout.splitlines()
        self.assertEqual(out[0], "['a', 'b']")

    def test_arguments_run_in_order(self):
        out = self.cli("--python-expr", "print(1)", "--python-expr", "print(2)", "--python-expr", "print(3)").stdout
        self.assertEqual(out, "1\n2\n3\n")

    def test_script_error(self):
        proc = self.cli("--python-expr", "print('before')\nraise ValueError('boom')", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("before", proc.stdout)
        self.assertIn("ValueError: boom", proc.stderr)
        self.assertNotIn("remote_control", proc.stderr, "server frames leaked into the traceback")
        proc = self.cli("--python-exit-code", "7", "--python-expr", "1/0", check=False)
        self.assertEqual(proc.returncode, 7)

    def test_sys_exit(self):
        self.assertEqual(self.cli("--python-expr", "import sys; sys.exit(0)", check=False).returncode, 0)
        self.assertEqual(self.cli("--python-expr", "import sys; sys.exit(3)", check=False).returncode, 1)

    def test_eval(self):
        self.assertEqual(self.cli("eval", "6 * 7").stdout, "42\n")
        self.assertEqual(self.cli("eval", "'text'").stdout, "text\n")
        self.assertEqual(self.cli("eval", "[1, 'a']").stdout, "[1, 'a']\n")

    def test_shell_keeps_state(self):
        out = self.cli("shell", stdin="x = 20\nfor i in range(2):\n    x += 1\n\nx * 2\n").stdout
        self.assertIn("44", out)

    def test_file_transfer(self):
        payload = os.urandom(3 * 1024 * 1024 + 17)
        local = os.path.join(self.home, "blob.bin")
        with open(local, "wb") as fh:
            fh.write(payload)
        remote = self.cli("push", local, "nested/blob.bin").stdout.strip()
        self.assertTrue(remote.endswith("nested/blob.bin"))
        listing = self.cli("ls", "nested").stdout
        self.assertIn(str(len(payload)), listing)
        back = os.path.join(self.home, "back.bin")
        self.cli("pull", "nested/blob.bin", back)
        with open(back, "rb") as fh:
            self.assertEqual(fh.read(), payload)
        # Scripts run with the workspace as their current directory.
        self.assertEqual(self.cli("eval", "len(open('nested/blob.bin', 'rb').read())").stdout.strip(),
                         str(len(payload)))
        self.cli("rm", "nested")
        self.assertNotIn("nested", self.cli("ls").stdout)
        self.assertEqual(self.cli("pull", "missing.bin", check=False).returncode, 1)

    def test_wrong_token(self):
        proc = self.cli("--token", "not-the-token", "status", check=False)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("authentication failed", proc.stderr)

    def test_unknown_argument(self):
        proc = self.cli("--frobnicate", check=False)
        self.assertEqual(proc.returncode, 2)

    def test_http_pages(self):
        base = "http://127.0.0.1:{:d}".format(self.port)
        with urllib.request.urlopen(base + "/blender-cli", timeout=10) as resp:
            body = resp.read()
        with open(CLI, "rb") as fh:
            self.assertEqual(body, fh.read())
        with urllib.request.urlopen(base + "/install", timeout=10) as resp:
            script = resp.read().decode()
        self.assertIn("PORT={:d}".format(self.port), script)
        self.assertNotIn(self.token, script, "the token must never be served")

    def test_marker_holds_pid(self):
        with open(self.rc.marker_path()) as fh:
            self.assertEqual(fh.read().strip(), str(os.getpid()))

    def test_parse_frames(self):
        sys.path.insert(0, os.path.dirname(CLI))
        import blender_cli
        self.assertEqual(blender_cli.parse_frames("1,3,5..7"), [1, 3, 5, 6, 7])
        self.assertEqual(blender_cli.parse_frames("3..1"), [3, 2, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
