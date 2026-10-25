# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Touch: remote control from Termux, the Android stand-in for running ``blender`` from a shell.

The server and the protocol live in ``_bpy_internal.remote_control``; this module is the
user-facing half: a Preferences panel (System > Remote Control) to start and stop it, show
the one-line Termux setup command, and replace the token, plus starting it with Blender
when the user asked for that.

Only shown on Android, where there is no other way to script a running Blender from a
terminal. ``BLENDER_REMOTE_CONTROL=1`` shows it on other platforms too, which is how the
feature is tested without a device.
"""

import os
import sys

import bpy
from bpy.props import IntProperty
from bpy.types import Operator, Panel

__all__ = (
    "register",
    "unregister",
)


def _enabled_platform():
    # CPython's own marker for an Android build of the interpreter.
    return hasattr(sys, "getandroidapilevel") or os.environ.get("BLENDER_REMOTE_CONTROL") == "1"


def _rc():
    from _bpy_internal import remote_control
    return remote_control


class WM_OT_remote_control_start(Operator):
    """Start the remote control server so blender-cli in Termux can drive Blender"""
    bl_idname = "wm.remote_control_start"
    bl_label = "Start Remote Control"

    def execute(self, _context):
        error = _rc().start()
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        self.report({'INFO'}, "Remote control listening on 127.0.0.1:{:d}".format(_rc().status()["port"]))
        return {'FINISHED'}


class WM_OT_remote_control_stop(Operator):
    """Stop the remote control server and disconnect every client"""
    bl_idname = "wm.remote_control_stop"
    bl_label = "Stop Remote Control"

    def execute(self, _context):
        _rc().stop()
        return {'FINISHED'}


class WM_OT_remote_control_autostart(Operator):
    """Start the remote control server every time Blender starts"""
    bl_idname = "wm.remote_control_autostart"
    bl_label = "Start Automatically"

    def execute(self, context):
        rc = _rc()
        config = rc.config_load()
        config["autostart"] = not config["autostart"]
        rc.config_save(config)
        if config["autostart"] and not rc.is_running():
            rc.start()
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


class WM_OT_remote_control_copy_setup(Operator):
    """Copy the line to paste into Termux: it installs blender-cli and pairs it with this Blender"""
    bl_idname = "wm.remote_control_copy_setup"
    bl_label = "Copy Setup Command"

    def execute(self, context):
        rc = _rc()
        if not rc.is_running():
            error = rc.start()
            if error:
                self.report({'ERROR'}, error)
                return {'CANCELLED'}
        context.window_manager.clipboard = rc.setup_command()
        self.report({'INFO'}, "Setup command copied: paste it into Termux")
        return {'FINISHED'}


class WM_OT_remote_control_copy_token(Operator):
    """Copy the access token (blender-cli pair TOKEN)"""
    bl_idname = "wm.remote_control_copy_token"
    bl_label = "Copy Token"

    def execute(self, context):
        context.window_manager.clipboard = _rc().config_load()["token"]
        self.report({'INFO'}, "Token copied")
        return {'FINISHED'}


class WM_OT_remote_control_new_token(Operator):
    """Replace the access token. Paired terminals lose access until they are paired again"""
    bl_idname = "wm.remote_control_new_token"
    bl_label = "New Token"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, _context):
        _rc().regenerate_token()
        self.report({'INFO'}, "New token created: copy the setup command again")
        return {'FINISHED'}


class WM_OT_remote_control_set_port(Operator):
    """Change the port the remote control server listens on"""
    bl_idname = "wm.remote_control_set_port"
    bl_label = "Set Port"

    port: IntProperty(name="Port", min=1024, max=65535, default=8642)

    def invoke(self, context, _event):
        self.port = _rc().status()["port"]
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, _context):
        rc = _rc()
        config = rc.config_load()
        config["port"] = self.port
        rc.config_save(config)
        if rc.is_running():
            rc.stop()
            error = rc.start()
            if error:
                self.report({'ERROR'}, error)
                return {'CANCELLED'}
        return {'FINISHED'}


class USERPREF_PT_system_remote_control(Panel):
    bl_space_type = 'PREFERENCES'
    bl_region_type = 'WINDOW'
    bl_context = "system"
    bl_label = "Remote Control"

    @classmethod
    def poll(cls, _context):
        return _enabled_platform()

    def draw(self, _context):
        layout = self.layout
        rc = _rc()
        status = rc.status()

        col = layout.column()
        if status["running"]:
            clients = status["clients"]
            col.label(
                text="Listening on 127.0.0.1:{:d}{:s}".format(
                    status["port"],
                    ", {:d} connected".format(clients) if clients else ""),
                icon='LINKED',
            )
        else:
            col.label(text="Stopped", icon='UNLINKED')
        if status["error"]:
            col.label(text=status["error"], icon='ERROR')

        row = layout.row(align=True)
        if status["running"]:
            row.operator("wm.remote_control_stop", text="Stop", icon='PAUSE')
        else:
            row.operator("wm.remote_control_start", text="Start", icon='PLAY')
        row.operator(
            "wm.remote_control_autostart",
            text="Start Automatically",
            icon='CHECKBOX_HLT' if status["autostart"] else 'CHECKBOX_DEHLT',
            depress=status["autostart"],
        )

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Termux setup: copy this and paste it into Termux.")
        col.operator("wm.remote_control_copy_setup", icon='COPYDOWN')
        row = col.row(align=True)
        row.operator("wm.remote_control_copy_token", icon='KEYINGSET')
        row.operator("wm.remote_control_new_token", icon='FILE_REFRESH')
        row.operator("wm.remote_control_set_port", text="Port {:d}".format(status["port"]))

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Then, in Termux:")
        col.label(text="blender-cli --python build.py")
        col.label(text="blender-cli scene.blend -o out/#### -f 1")
        col.label(text="blender-cli shell")
        col.label(text="Workspace: " + status["workspace"])
        col.label(text="Anyone holding the token can run any Python in Blender.", icon='INFO')


classes = (
    WM_OT_remote_control_start,
    WM_OT_remote_control_stop,
    WM_OT_remote_control_autostart,
    WM_OT_remote_control_copy_setup,
    WM_OT_remote_control_copy_token,
    WM_OT_remote_control_new_token,
    WM_OT_remote_control_set_port,
    USERPREF_PT_system_remote_control,
)


def _shutdown():
    # Close the socket and remove the marker, so the activity drops its foreground service.
    try:
        _rc().stop(tag_redraw=False)
    except Exception:
        pass


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    if not _enabled_platform():
        return
    import atexit
    # unregister() first so a script reload does not stack up copies.
    atexit.unregister(_shutdown)
    atexit.register(_shutdown)
    rc = _rc()
    # Reloading scripts runs register() again; the running server survives that.
    if not bpy.app.timers.is_registered(rc.autostart) and not rc.is_running():
        # Deferred: preferences and the window are not ready while scripts register.
        # Persistent, or loading the startup file would remove it before it fires.
        bpy.app.timers.register(rc.autostart, first_interval=1.0, persistent=True)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
