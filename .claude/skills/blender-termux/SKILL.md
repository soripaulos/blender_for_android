---
name: blender-termux
description: Build, edit, inspect and render 3D scenes in Blender for Android from Termux with blender-cli. Use whenever the task is to model, script, render or export something in Blender on this phone, or to read what is in the open Blender scene.
---

# Building in Blender from Termux

Blender runs as an Android app. You reach it from Termux through `blender-cli`, which
sends Python to a server inside the running Blender. Everything you know about Blender's
Python API (`bpy`) applies. The difference from desktop scripting is one thing, and it
matters more than anything else here:

**Blender is a live, long-running session, not a fresh process per command.** Every
command acts on the scene the previous one left behind, and on whatever the user has
been doing by hand in the app. Running a script twice builds it twice. Design for that
(see "Write scripts that can run again").

## 1. Check the connection first

```bash
blender-cli status
```

Prints the Blender version, the open file and the **workspace** directory. Exit codes
you will see from any command:

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0 | Done | |
| 1 | Your Python raised (traceback on stderr) | Read the traceback, fix the script, run again |
| 2 | Bad `blender-cli` arguments, or a local file missing | Fix the command |
| 3 | Cannot reach Blender, or the token was refused | Below |

On exit 3 do not retry in a loop. `blender-cli` already tries to start Blender with `am`
and waits up to 90 s. If it still fails, stop and ask the user to do the part you
cannot: open Blender, go to **Preferences > System > Remote Control**, press **Start**
(and tick **Start Automatically**), press **Copy Setup Command**, and paste that line
into Termux. "authentication failed" means the token changed: same steps. Never invent,
print, log or commit the token; it grants full control of Blender.

If `blender-cli` itself is missing, the same setup line installs it.

## 2. The loop

1. Write the script to a file in Termux (e.g. `~/blender/build_table.py`).
2. Run it: `blender-cli run ~/blender/build_table.py -- --legs 4`
3. Check the result as data (section 5), not by assuming it worked.
4. Render a small preview and look at the image when the shape matters (section 6).
5. Adjust and repeat. Save when the user wants to keep it (section 8).

Prefer script files over long `--python-expr` strings: they are readable, re-runnable,
and tracebacks point at real line numbers.

## 3. Commands

```bash
blender-cli run SCRIPT.py [-- args]      # run a script; args land in sys.argv after "--"
blender-cli --python-expr "CODE"         # a one-liner (statements allowed)
blender-cli eval "EXPR"                  # ONE expression; prints its value (strings bare)
blender-cli exec "CODE"                  # same as --python-expr
blender-cli shell                        # interactive console; variables persist (humans mostly)
blender-cli open FILE.blend | save [PATH] | --new
blender-cli push LOCAL [REMOTE] | pull REMOTE [LOCAL] | ls [DIR] | rm REMOTE
blender-cli --help                       # everything, including all render flags
```

Desktop-style arguments also work and run in order, left to right:

```bash
blender-cli --new -P build.py -E BLENDER_EEVEE -o ~/renders/shot_ -f 1 --save ~/scene.blend
```

`-b`, `--background` and `--factory-startup` are accepted but do nothing: the interface
is already running. To start clean use `--new` (loads the user's startup scene), and
only when the user has agreed that the current scene can be discarded.

## 4. What a script gets

- `bpy` imported; `C` = `bpy.context`, `D` = `bpy.data` are predefined.
- A context with the window, its screen and the largest 3D Viewport, so `bpy.ops` that
  need a viewport work. Still prefer `bpy.data` / mesh / `bmesh` APIs over `bpy.ops`:
  they need no context, no selection state, and are faster and deterministic.
- `print()` streams back to you live. `sys.argv[sys.argv.index("--") + 1:]` are your args.
- The current directory is Blender's workspace (shown by `status`), so relative paths in
  a script read and write there; `blender-cli pull` fetches them afterwards.
- Each command is one undo step and the viewport redraws after it.
- `mathutils` (`Vector`, `Matrix`, `Euler`) and `bmesh` are available; `numpy` is too.
- A fresh namespace per command: variables do not survive between commands. Store state
  in the scene (objects, custom properties), not in Python globals.
- Commands run on Blender's main thread and freeze its UI while they run. Keep each one
  short; a long render is fine but is the user's phone being busy.

## 5. Inspect, don't assume

One expression with `eval`:

```bash
blender-cli eval "len(D.objects)"
blender-cli eval "[o.name for o in D.objects]"
blender-cli eval "D.objects['Table'].dimensions[:]"
blender-cli eval "C.scene.render.engine"
```

Anything bigger: a script that prints JSON, then parse it.

```python
# ~/blender/inspect.py
import bpy, json
print(json.dumps([
    {"name": o.name, "type": o.type, "loc": [round(v, 3) for v in o.location],
     "dims": [round(v, 3) for v in o.dimensions],
     "collection": [c.name for c in o.users_collection],
     "verts": len(o.data.vertices) if o.type == 'MESH' else None}
    for o in bpy.data.objects
], indent=1))
```

```bash
blender-cli run ~/blender/inspect.py
```

## 6. Look at it: preview renders

Render small and fast, into Termux, then open the image (read the PNG to see it):

```bash
blender-cli --python-expr "
import bpy; r = bpy.context.scene.render
r.resolution_x, r.resolution_y, r.resolution_percentage = 640, 480, 100
r.image_settings.file_format = 'PNG'" \
  -E BLENDER_WORKBENCH -o ~/blender/previews/view_ -f -1
```

`blender-cli` prints `Saved: <path>` for each image it copied back. `-f -1` renders the
current frame and, as on the desktop, appends the frame number (`view_0001.png`).

- Engine names differ between Blender versions (`BLENDER_EEVEE` vs `BLENDER_EEVEE_NEXT`):
  `blender-cli -E help` lists the valid ones.
- Workbench for shape checks (seconds). EEVEE for materials and light. Cycles is slow on
  a phone: only when asked, with low samples (`C.scene.cycles.samples = 16`).
- No camera means no render. Add one if `C.scene.camera` is `None`, and frame the
  objects you built rather than guessing:

```python
import bpy
from mathutils import Vector

def frame_objects(objs, cam_name="Agent Camera", margin=1.3):
    pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    lo = Vector(map(min, *pts)); hi = Vector(map(max, *pts))
    center, size = (lo + hi) / 2, max((hi - lo).length, 0.001)
    cam = bpy.data.objects.get(cam_name)
    if cam is None:
        cam = bpy.data.objects.new(cam_name, bpy.data.cameras.new(cam_name))
        bpy.context.scene.collection.objects.link(cam)
    cam.location = center + Vector((1, -1, 0.7)).normalized() * size * margin * 1.6
    cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()
    bpy.context.scene.camera = cam
```

Frame **your** collection: `frame_objects(list(bpy.data.collections["Agent: Table"].objects))`.
Framing every mesh pulls in whatever else is in the scene, and the startup file's 2 m
default `Cube` at the origin swallows anything smaller built there: a preview that shows
only a grey cube means exactly that. Check for it first
(`blender-cli eval "[o.name for o in D.objects if o.type == 'MESH']"`), then build
elsewhere, or set `hide_render`/`hide_viewport` on it; delete it only if the user agrees.

Restore any render settings you changed for a preview if the user cares about theirs, or
set them in a script that saves and restores them.

## 7. Write scripts that can run again

Because the session persists, every build script should own its output and replace it:

```python
import bpy

COLL = "Agent: Table"  # one collection per thing you build

def fresh_collection(name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    # Read what is needed before removing: a removed object cannot be touched again.
    owned = [(obj.type, obj.data) for obj in coll.objects]
    for obj in list(coll.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    # Drop the orphaned data too, so reruns do not pile up unused meshes.
    pools = {'MESH': bpy.data.meshes, 'CURVE': bpy.data.curves,
             'CAMERA': bpy.data.cameras, 'LIGHT': bpy.data.lights}
    for kind, data in owned:
        if data is not None and data.users == 0 and kind in pools:
            pools[kind].remove(data)
    return coll

def add_box(coll, name, size, location):
    import bmesh
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=size, verts=bm.verts)
    bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    coll.objects.link(obj)
    return obj

coll = fresh_collection(COLL)
top = add_box(coll, "Top", (1.2, 0.8, 0.05), (0, 0, 0.75))
for i, (x, y) in enumerate([(-0.55, -0.35), (0.55, -0.35), (-0.55, 0.35), (0.55, 0.35)]):
    add_box(coll, f"Leg {i}", (0.05, 0.05, 0.725), (x, y, 0.3625))
print("built", len(coll.objects), "objects in", COLL)
```

Rules that follow from this:

- Only touch what you created: your collections, objects named by you. Never delete,
  rename or move the user's objects unless asked.
- Reuse materials by name (`bpy.data.materials.get(name) or bpy.data.materials.new(name)`).
- Parametrise with arguments after `--` instead of editing numbers in the file.
- If something must be applied in Edit Mode, return to Object Mode at the end; leaving
  Blender in Edit Mode breaks the next command's `bpy.data` edits.

## 8. Files: Termux and Blender see different disks

Termux's home is private to Termux. `blender-cli` bridges it:

| You give | What happens |
| --- | --- |
| `blender-cli ~/model.blend` or `open ~/model.blend` | Copied into the workspace, then opened from there |
| `-o ~/renders/x_####` | Rendered in the workspace, copied back when done |
| `--save ~/scene.blend` / `save ~/scene.blend` | Saved in the workspace, copied back |
| `/sdcard/...` or `~/storage/shared/...` | Used directly (needs Blender's "All files access") |
| A relative path inside a script | The workspace |

Consequence: a file opened from a Termux path is the workspace copy. A plain
`blender-cli save` writes that copy; to update the file in Termux, `save ~/model.blend`
again. Save to `/sdcard/...` when the user wants to open the result in the Blender app
themselves later.

Exports: write into the workspace in the script, then pull.

```bash
blender-cli --python-expr "import bpy; bpy.ops.export_scene.gltf(filepath='exports/table.glb')"
blender-cli pull exports/table.glb ~/blender/table.glb
```

(`bpy.ops.wm.obj_export(filepath=...)`, `bpy.ops.wm.stl_export(...)` likewise.)

Helper modules: `push` them to the workspace and import from there.

```bash
blender-cli push ~/blender/lib/agentlib.py lib/agentlib.py
```
```python
import os, sys; sys.path.insert(0, os.path.join(os.getcwd(), "lib"))
import importlib, agentlib; importlib.reload(agentlib)   # reload: the session persists
```

## 9. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `RuntimeError: Operator bpy.ops.X.poll() failed, context is incorrect` | The operator needs a mode, selection or editor. Use the data API instead, or set the state first (`bpy.context.view_layer.objects.active = obj`, `obj.select_set(True)`, `bpy.ops.object.mode_set(mode='OBJECT')`) |
| Objects doubled after a rerun | Script is not idempotent: section 7 |
| `KeyError` on `D.objects['X']` | The user renamed or deleted it; inspect first and use `.get()` |
| Render command "succeeds" but no image | No camera, or the object is outside its view: `frame_objects` |
| Preview shows a grey cube, not your model | The startup `Cube` is in front of or around it: section 6 |
| Exit 3 while Blender is open | Remote Control was stopped, or Blender was closed by Android: ask the user to press Start (or enable Start Automatically) |
| A command hangs | Blender is busy (a long render, or the user is in a modal tool). Ctrl+C stops waiting but Blender finishes the command. Wait, do not resend |
| Changes not visible in the app | They are; the viewport may be scrolled away. `frame_objects` and a preview render confirm |

## 10. Do not

- Do not run `blender-cli quit`, `--new`, `open`, or `rm` on something you did not create
  without the user asking: each can throw away unsaved work in their session.
- Do not save over the user's file (`save` with no path) unless asked; save to a new path.
- Do not loop `status` waiting for Blender; one attempt, then ask the user.
- Do not touch Remote Control settings or the token.
- Do not start heavy Cycles renders or huge geometry without saying so: it is a phone,
  and Blender's UI is frozen while a command runs.

For the full reference (every flag, the protocol, security) see
`ANDROID_TERMUX_CLI.md` in the Blender for Android repository.
