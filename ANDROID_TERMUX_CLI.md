# Controlling Blender for Android from Termux

On a desktop, Blender is scripted from a shell:

```bash
blender scene.blend --python build.py -o //renders/frame_#### -a
```

On Android there is no shell to start Blender from and no `argv` to hand it: Blender
is a library inside an Android activity. This build gives Termux the same reach in
another way. Blender runs a small server on the phone's loopback interface, and
`blender-cli` in Termux talks to it. `blender-cli` takes Blender's own command line
arguments, so desktop scripts and command lines carry over.

## Setup, once

1. Install [Termux](https://termux.dev) (from F-Droid or GitHub, not the old Play Store
   build). The installer adds Python itself if it is missing.
2. In Blender: **Edit > Preferences > System > Remote Control**.
3. Press **Start**, and tick **Start Automatically** if you want it on every launch.
4. Press **Copy Setup Command** and paste the line into Termux. It looks like:

   ```bash
   curl -fsS http://127.0.0.1:8642/install | sh -s -- <token>
   ```

   That downloads `blender-cli` from Blender itself, installs it into Termux's `bin`, and
   pairs it with the token.
5. Check it:

   ```bash
   blender-cli status
   ```

If `curl` is missing: `pkg install curl`. `blender-cli` is also attached to every CI
build and release as a plain file, if you would rather install it by hand:
`cp blender-cli $PREFIX/bin/ && chmod +x $PREFIX/bin/blender-cli && blender-cli pair <token>`.

## Using it

### Desktop-style arguments

Arguments are read in order and acted on one at a time, like Blender's own command
line, so `-o`, `-E` and `-F` apply to the renders after them.

```bash
# Run a script against the open scene
blender-cli --python build.py

# Script arguments go after --, exactly as on the desktop (sys.argv)
blender-cli --python build.py -- --rows 5 --seed 42

# One-liners
blender-cli --python-expr "import bpy; bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 2))"

# Open a file, build, render frames 1 to 10 into Termux's home, save a copy
blender-cli scene.blend -P build.py -E CYCLES -o ~/renders/shot_#### -s 1 -e 10 -a --save ~/scene_built.blend

# Single frames: 1 | 1,5,9 | 10..20
blender-cli -o out/frame_ -f 1,5,10..12
```

| Argument | Does |
| --- | --- |
| `FILE.blend` | Opens the file |
| `-P`, `--python FILE` | Runs a script (read in Termux and sent over) |
| `--python-expr EXPR` | Runs a line of Python |
| `--python-text NAME` | Runs a text block of the open file |
| `--python-exit-code N` | Exit status when a script fails (default 1) |
| `-f`, `--render-frame FRAMES` | Renders frames: `1`, `1,5,9`, `10..20`, `-1` for the current frame |
| `-a`, `--render-anim` | Renders the frame range |
| `-s`, `-e`, `-j` | Frame start, end, step |
| `-o`, `--render-output PATH` | Output path; `#` is the frame number, appended when missing (as on the desktop) |
| `-F`, `--render-format FMT` | `PNG`, `JPEG`, `OPEN_EXR`, `FFMPEG`, ... |
| `-x`, `--use-extension 0/1` | Add the file extension |
| `-E`, `--engine ENGINE` | `CYCLES`, `BLENDER_EEVEE`, `BLENDER_WORKBENCH`; `-E help` lists them |
| `-S`, `--scene NAME` | Switches scene |
| `-t`, `--threads N` | Render threads, 0 for all |
| `--new` | New file from the startup scene |
| `--save [PATH]` | Saves, or saves as PATH |
| `--quit` | Quits Blender at the end |
| `-v`, `--version` | Blender's version |
| `-b`, `--background`, `--factory-startup`, `-noaudio` | Accepted so desktop command lines work unchanged; nothing to do, since Blender is already running with its interface |
| `--` | Everything after it reaches the scripts' `sys.argv` |

Errors print Blender's Python traceback in Termux. After a failed script the rest of the
arguments still run, as on the desktop, and the exit status is 1 (or what
`--python-exit-code` says) so shell scripts and `&&` chains can tell.

### Commands

```bash
blender-cli run build.py a b c       # same as --python build.py -- a b c
blender-cli exec "print(len(bpy.data.objects))"
blender-cli eval "bpy.context.scene.frame_end"      # prints the value
blender-cli shell                    # Python console inside Blender (bpy, C, D ready)
blender-cli open scene.blend
blender-cli save ~/scene.blend
blender-cli render -o out/#### -a    # render; with no -f/-a renders the current frame
blender-cli push model.obj           # copy into Blender's workspace (files/model.obj)
blender-cli pull renders/x.png .     # copy back
blender-cli ls                       # list the workspace
blender-cli rm files/model.obj
blender-cli launch                   # start Blender
blender-cli quit                     # quit Blender
```

`blender-cli --help` lists everything. If Blender is not running, `blender-cli` starts
it (with Termux's `am`) and waits for the server, which needs **Start Automatically**
ticked. `--no-launch` turns that off.

### Where files live

Termux's home directory is private to Termux and Blender cannot read it. `blender-cli`
handles that for you:

- A **.blend or other input** in Termux's home is copied into Blender's workspace first.
- **Renders** aimed at a Termux path are rendered into the workspace and copied back
  when the render finishes.
- **`--save PATH`** to a Termux path saves in the workspace and copies the file back.
- Paths in **shared storage** (`/sdcard/...`, `~/storage/shared/...` after
  `termux-setup-storage`) are used directly, once Blender has "All files access".
- `//` paths are relative to the open .blend, as always.

Scripts run with the workspace as their current directory, so `open("out.txt", "w")` in
a script and `blender-cli pull out.txt` afterwards work together. `blender-cli status`
shows where the workspace is (`<app files>/remote`).

### What a script sees

The same as a `--python` script on the desktop with a window open:

- `bpy.context` has the window, its screen and the largest 3D Viewport, so operators
  that need a viewport (`bpy.ops.view3d.*`, many `mesh` and `object` operators) work.
- `sys.argv` is `["blender", "--python", <script>, "--", <your arguments>...]`.
- `__name__` is `"__main__"`, `__file__` is the script's Termux path.
- `print()` output streams to Termux as it happens.
- Each command is one undo step, so **Edit > Undo** takes a script back.
- The viewport redraws when the command finishes.

Commands run on Blender's main thread one at a time, between frames of the interface.
A long render blocks the interface just as it would with a script run inside Blender.
Pressing Ctrl+C in Termux stops waiting, but Blender finishes the command.

## Security

- The server listens on `127.0.0.1` only; nothing off the phone can reach it.
- It is **off** until you start it.
- Every connection must present the token, a random 32 character string that is shown
  only inside Blender. Any app on the phone can open a loopback connection, so the token
  is what keeps other apps out: **whoever has it can run any Python as Blender**.
  If you think it leaked, press **New Token** and copy the setup command again.
- The token is stored in Blender's config directory and in
  `~/.config/blender-cli/config.json` in Termux, both readable only by their own app.
- The installer and the client script are served without a token; they contain no
  secrets.

## While you are in Termux

Android freezes apps in the background within seconds. While the server runs, Blender
holds a foreground service (the "Blender remote control is on" notification) so it keeps
answering while you type in Termux. Stopping the server removes the notification. On
Android 13 and later the notification only shows if Blender is allowed to post
notifications; the service works either way.

Split screen (Termux and Blender side by side) is the comfortable way to watch a script
build something.

## Protocol

For writing your own client (any language with sockets will do):

- TCP to `127.0.0.1:8642` (the port is configurable in the panel).
- Each message is one line of UTF-8 JSON. A message with a `size` key is followed by
  exactly that many raw bytes.
- First message: `{"op": "hello", "token": "..."}`. Reply:
  `{"type": "hello", "ok": true, "protocol": 1, "blender": "5.3.0", "workspace": "...", "file": "..."}`.
- Requests carry an `id` and an `op`:

  | op | Fields | Reply |
  | --- | --- | --- |
  | `exec` | `code`, `name`, `argv`, `session` (keep variables between requests), `mode` (`exec` or `single`), `context` (`view3d`, `window`, `none`) | `stdout`/`stderr` messages while it runs, then `result` with `ok`, `error` (the traceback), and `value` (the script's `__result__`, if it set one) |
  | `eval` | `code` | `result` with `value` (JSON, or the repr) and `repr` |
  | `put` | `path`, `size` + bytes | `result`, `value` is the absolute path |
  | `get` | `path` | `file` with `size` + bytes, or a failed `result` |
  | `ls`, `rm`, `mkdir` | `path` | `result` |
  | `ping` | | `result`, `value` `"pong"` |
  | `quit` | | `result`, then Blender quits |

- Relative paths are relative to the workspace.
- `GET /install` and `GET /blender-cli` over HTTP on the same port return the
  installer and the client.

## Where it lives in the tree

| Part | File |
| --- | --- |
| Server: sockets, framing, token check, file transfer (no `bpy`) | [server.py](scripts/modules/_bpy_internal/remote_control/server.py) |
| Executor: runs requests on the main thread with a `--python`-like context, streams output; config, token, marker file, installer page | [\_\_init\_\_.py](scripts/modules/_bpy_internal/remote_control/__init__.py) |
| The Termux client | [blender_cli.py](scripts/modules/_bpy_internal/remote_control/blender_cli.py) |
| Preferences panel, operators, start with Blender | [bl_android_remote_control.py](scripts/startup/bl_android_remote_control.py) |
| Foreground service while serving | [RemoteControlService.java](build_files/android/apk/app/src/main/java/org/blender/blender/RemoteControlService.java), `updateRemoteControlService()` in [BlenderActivity.java](build_files/android/apk/app/src/main/java/org/blender/blender/BlenderActivity.java), the service and permissions in [AndroidManifest.xml](build_files/android/apk/app/src/main/AndroidManifest.xml) |
| End-to-end test with a stand-in `bpy` | [test_remote_control.py](build_files/android/termux/test_remote_control.py) |

The server and the activity talk through a file, `<files>/remote_control.active`, which
holds the server's PID while it runs: the activity polls it every two seconds and starts
or stops the service. That keeps Python free of JNI, and a marker left behind by a
killed process carries the wrong PID and is ignored.

Python scripts are part of the runtime payload, so a local build needs
`build.py full --repackage` for changes here to reach the phone (see the
`android-build` skill).

## Building the APK in CI

[.github/workflows/android-apk.yml](.github/workflows/android-apk.yml) builds the APK on
GitHub's hosted runners on every push, and publishes a release for a `v*` tag (or a manual
run with **release** ticked). Get the APK from the run's **Artifacts**
(`blender-android-full`), next to `blender-cli` and `SHA256SUMS`.

| Job | Does |
| --- | --- |
| Checks | The remote control tests, `blender-cli` on Python 3.9, Java compile and manifest link. A few minutes |
| Dependencies (stages 1 to 5) | `deps/build.sh` in time-boxed stages ([deps_stage.sh](build_files/android/ci/deps_stage.sh)), because the ~70 libraries take longer than one job may run. Progress is cached between stages, and the finished prefix is cached by the hash of the recipes, so later runs skip this in a minute |
| APK (stages 1 and 2) | `build_apk.sh` through ccache ([build_stage.sh](build_files/android/ci/build_stage.sh)); a stage that runs out of time hands over to the next, which replays the cache |
| Release | On a tag: a GitHub release with the APK and `blender-cli` |

The first run takes many hours of runner time. On a private repository that counts
against the account's Actions minutes. A run that stops in the middle keeps its progress
in the cache: **Re-run** carries on from there.

Signing: CI signs with a key generated once and kept in the Actions cache, so successive
CI builds install over each other. To sign with your own key, add a repository secret
`ANDROID_KEYSTORE_BASE64` holding a base64 keystore with store and key password `android`
and alias `androiddebugkey` (what `package.sh` uses). An APK signed with another key has
to be uninstalled before a CI build installs.

Manual run: **Actions > Android APK > Run workflow**, choosing `full` or `lite`.
