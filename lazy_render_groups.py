bl_info = {
    "name": "Lazy Render Groups",
    "author": "thelazyone",
    "version": (0, 2, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Organize models into render groups (collections). Switch which "
                   "group is visible/rendered, and batch-render all of them.",
    "warning": "Early prototype",
    "doc_url": "https://github.com/thelazyone/lazy_tools",
    "category": "3D View",
}

import os
import re
from datetime import datetime

import bpy
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

# Tagged parent for all render-group collections. Objects are *moved* into their
# group (unlinked from Scene Collection etc.) so hiding the group actually hides
# them. Shared studio stuff (camera, lights, backdrop) stays outside groups.
TAG = "LZRG"
ID_PROP = "lazy_render_groups_id"
PARENT_KEY = f"{TAG}.render_groups"
PARENT_NAME = "LZRG.Render Groups"
GROUP_PREFIX = f"{TAG}.group."


# ----------------------------------------------------------------------------
# Collection helpers
# ----------------------------------------------------------------------------

def _find_tagged_collection(key):
    for col in bpy.data.collections:
        if col.get(ID_PROP) == key:
            return col
    return None


def _ensure_parent_collection(scene):
    parent = _find_tagged_collection(PARENT_KEY)
    if parent is None:
        parent = bpy.data.collections.new(PARENT_NAME)
        parent[ID_PROP] = PARENT_KEY
    if parent.name not in scene.collection.children:
        try:
            scene.collection.children.link(parent)
        except RuntimeError:
            pass
    parent.hide_viewport = False
    parent.hide_render = False
    return parent


def _unique_group_name(base="Group"):
    existing = {item.name for item in bpy.context.scene.lazy_render_groups_props.groups}
    if base not in existing:
        return base
    i = 1
    while f"{base}.{i:03d}" in existing:
        i += 1
    return f"{base}.{i:03d}"


def _safe_filename(name):
    cleaned = re.sub(r"[^\w\-]+", "_", name.strip())
    return cleaned.strip("_") or "group"


def _group_collection_name(display_name):
    return f"{GROUP_PREFIX}{display_name}"


def _sync_collection_name(item):
    col = item.collection
    if col is None:
        return
    wanted = _group_collection_name(item.name)
    if col.name != wanted:
        col.name = wanted


def _move_objects_into_collection(objects, target_col):
    """Link into target and unlink from every other collection.

    Required so collection hide_viewport / hide_render actually control
    visibility (objects still in Scene Collection would stay visible).
    """
    if target_col is None:
        return 0
    moved = 0
    for obj in objects:
        if obj.name not in target_col.objects:
            target_col.objects.link(obj)
            moved += 1
        for col in list(obj.users_collection):
            if col != target_col:
                col.objects.unlink(obj)
    return moved


def _ensure_group_owns_objects(item):
    """Repair groups created before move-into-collection (link-only)."""
    col = item.collection
    if col is None:
        return
    _move_objects_into_collection(list(col.objects), col)


def _rehome_objects_to_scene(scene, col):
    """Before deleting a group collection, put its objects back on the scene root."""
    if col is None:
        return
    root = scene.collection
    for obj in list(col.objects):
        if obj.name not in root.objects:
            root.objects.link(obj)
        col.objects.unlink(obj)


def _set_collection_visible(collection, visible):
    if collection is None:
        return
    collection.hide_viewport = not visible
    collection.hide_render = not visible


def _apply_visibility(scene):
    """Solo active group, hide all (active_index < 0), or show all."""
    props = scene.lazy_render_groups_props
    if props.show_all:
        for item in props.groups:
            _ensure_group_owns_objects(item)
            _set_collection_visible(item.collection, True)
        return
    for i, item in enumerate(props.groups):
        _ensure_group_owns_objects(item)
        _set_collection_visible(item.collection, i == props.active_index)


def activate_group(scene, index):
    """Solo one group (index >= 0) or hide every group (index < 0)."""
    props = scene.lazy_render_groups_props
    if index >= len(props.groups):
        return None

    props.show_all = False
    if props.active_index != index:
        props.active_index = index
    else:
        # Same index: still re-apply (e.g. after create).
        _apply_visibility(scene)

    if index < 0:
        return None
    return props.groups[index]


def activate_group_by_name(scene, name):
    props = scene.lazy_render_groups_props
    for i, item in enumerate(props.groups):
        if item.name == name:
            return activate_group(scene, i)
    return None


def iter_groups(scene):
    props = scene.lazy_render_groups_props
    for i, item in enumerate(props.groups):
        if item.collection is not None:
            yield i, item


def group_count(scene):
    return sum(1 for _ in iter_groups(scene))


def has_render_groups(scene):
    return hasattr(scene, "lazy_render_groups_props") and group_count(scene) > 0


def prune_missing_groups(scene):
    """Drop list entries whose collection was deleted in the Outliner.

    Must not run from Panel.draw — writing RNA during draw empties the panel.
    """
    props = scene.lazy_render_groups_props
    for i in range(len(props.groups) - 1, -1, -1):
        if props.groups[i].collection is None:
            props.groups.remove(i)
    if props.groups and props.active_index >= len(props.groups):
        props.active_index = len(props.groups) - 1
    elif not props.groups:
        props.active_index = -1


# ----------------------------------------------------------------------------
# Property callbacks
# ----------------------------------------------------------------------------

def _on_group_name_changed(self, context):
    name = (self.name or "").strip() or "Group"
    props = context.scene.lazy_render_groups_props
    for other in props.groups:
        if other != self and other.name == name:
            name = _unique_group_name(name)
            break
    if self.name != name:
        self.name = name
        return
    _sync_collection_name(self)


def _on_visibility_props_changed(self, context):
    _apply_visibility(context.scene)


# ----------------------------------------------------------------------------
# Property groups
# ----------------------------------------------------------------------------

class LazyRenderGroupItem(PropertyGroup):
    name: StringProperty(
        name="Name",
        default="Group",
        update=_on_group_name_changed,
    )
    collection: PointerProperty(
        name="Collection",
        type=bpy.types.Collection,
    )


class LazyRenderGroupsProps(PropertyGroup):
    groups: CollectionProperty(type=LazyRenderGroupItem)
    # -1 = none selected (all groups hidden). >=0 = solo that group.
    active_index: IntProperty(
        name="Active Group",
        default=-1,
        update=_on_visibility_props_changed,
    )
    show_all: BoolProperty(
        name="Show All",
        default=False,
        description="When on, every render group is visible",
        update=_on_visibility_props_changed,
    )
    add_timestamp: BoolProperty(
        name="Timestamp in File Name",
        default=True,
        description=(
            "Append a date/time stamp to rendered file names. Turn off to get "
            "stable names (re-rendering overwrites the previous file)"
        ),
    )


# ----------------------------------------------------------------------------
# Operators
# ----------------------------------------------------------------------------

class LZRG_OT_create_group(Operator):
    bl_idname = "lazyrendergroups.create_group"
    bl_label = "Create Group from Selection"
    bl_description = (
        "Move the selection into a new render group. The new group becomes "
        "active (solo); click it again in the list to hide it"
    )
    bl_options = {'REGISTER', 'UNDO'}

    group_name: StringProperty(name="Name", default="Group")

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and len(context.selected_objects) > 0

    def invoke(self, context, event):
        self.group_name = _unique_group_name("Group")
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "group_name")

    def execute(self, context):
        scene = context.scene
        prune_missing_groups(scene)
        props = scene.lazy_render_groups_props
        parent = _ensure_parent_collection(scene)

        name = _unique_group_name((self.group_name or "Group").strip() or "Group")
        col = bpy.data.collections.new(_group_collection_name(name))
        col[ID_PROP] = f"{GROUP_PREFIX}{name}"
        try:
            parent.children.link(col)
        except RuntimeError:
            pass

        selected = list(context.selected_objects)
        _move_objects_into_collection(selected, col)

        item = props.groups.add()
        item.collection = col
        item.name = name

        activate_group(scene, len(props.groups) - 1)
        self.report({'INFO'}, f"Created render group '{name}' ({len(selected)} objects)")
        return {'FINISHED'}


class LZRG_OT_add_to_active(Operator):
    bl_idname = "lazyrendergroups.add_to_active"
    bl_label = "Add Selection to Active"
    bl_description = "Move selected objects into the active (solo) render group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = getattr(context.scene, "lazy_render_groups_props", None)
        if props is None or props.show_all:
            return False
        if props.active_index < 0 or props.active_index >= len(props.groups):
            return False
        item = props.groups[props.active_index]
        return (
            context.mode == 'OBJECT'
            and len(context.selected_objects) > 0
            and item.collection is not None
        )

    def execute(self, context):
        props = context.scene.lazy_render_groups_props
        item = props.groups[props.active_index]
        moved = _move_objects_into_collection(list(context.selected_objects), item.collection)
        _apply_visibility(context.scene)
        self.report({'INFO'}, f"Moved {moved} object(s) into '{item.name}'")
        return {'FINISHED'}


class LZRG_OT_remove_group(Operator):
    bl_idname = "lazyrendergroups.remove_group"
    bl_label = "Remove Group"
    bl_description = (
        "Remove the active render group. Objects are moved back to the scene "
        "collection; only the group collection is deleted"
    )
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1)

    @classmethod
    def poll(cls, context):
        props = getattr(context.scene, "lazy_render_groups_props", None)
        return props is not None and len(props.groups) > 0

    def execute(self, context):
        scene = context.scene
        prune_missing_groups(scene)
        props = scene.lazy_render_groups_props

        index = self.index
        if index < 0:
            index = props.active_index
        if index < 0 or index >= len(props.groups):
            # Fall back to last group if nothing is solo-selected.
            index = len(props.groups) - 1
        if index < 0:
            return {'CANCELLED'}

        item = props.groups[index]
        name = item.name
        col = item.collection
        props.groups.remove(index)

        _rehome_objects_to_scene(scene, col)
        if col is not None:
            for parent in list(bpy.data.collections):
                if col.name in parent.children:
                    parent.children.unlink(col)
            if col.name in scene.collection.children:
                scene.collection.children.unlink(col)
            bpy.data.collections.remove(col)

        if not props.groups:
            props.show_all = False
            props.active_index = -1
        elif props.active_index == index:
            props.active_index = -1
            _apply_visibility(scene)
        elif props.active_index > index:
            props.active_index -= 1
        else:
            _apply_visibility(scene)

        self.report({'INFO'}, f"Removed render group '{name}'")
        return {'FINISHED'}


class LZRG_OT_toggle_group(Operator):
    bl_idname = "lazyrendergroups.toggle_group"
    bl_label = "Toggle Group"
    bl_description = (
        "Solo this group (hides the others). Click the active group again to "
        "hide all groups"
    )
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=0)

    def execute(self, context):
        scene = context.scene
        props = scene.lazy_render_groups_props
        if self.index < 0 or self.index >= len(props.groups):
            self.report({'WARNING'}, "Invalid group index")
            return {'CANCELLED'}

        if props.show_all:
            props.show_all = False
            props.active_index = self.index
            name = props.groups[self.index].name
            self.report({'INFO'}, f"Active: {name}")
        elif props.active_index == self.index:
            props.active_index = -1
            self.report({'INFO'}, "No group active (all hidden)")
        else:
            props.active_index = self.index
            name = props.groups[self.index].name
            self.report({'INFO'}, f"Active: {name}")
        return {'FINISHED'}


class LZRG_OT_activate_group(Operator):
    bl_idname = "lazyrendergroups.activate_group"
    bl_label = "Activate Group"
    bl_description = "Show this group and hide the others from viewport and render"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=0)

    def execute(self, context):
        item = activate_group(context.scene, self.index)
        if item is None and self.index >= 0:
            self.report({'WARNING'}, "Invalid group index")
            return {'CANCELLED'}
        if item is None:
            self.report({'INFO'}, "No group active (all hidden)")
        else:
            self.report({'INFO'}, f"Active render group: {item.name}")
        return {'FINISHED'}


class LZRG_OT_show_all(Operator):
    bl_idname = "lazyrendergroups.show_all"
    bl_label = "Show All Groups"
    bl_description = "Make every render group visible in viewport and render"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return has_render_groups(context.scene)

    def execute(self, context):
        props = context.scene.lazy_render_groups_props
        props.show_all = True
        self.report({'INFO'}, "All render groups visible")
        return {'FINISHED'}


class LZRG_OT_hide_all(Operator):
    bl_idname = "lazyrendergroups.hide_all"
    bl_label = "Hide All Groups"
    bl_description = "Hide every render group (same as deselecting the active one)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return has_render_groups(context.scene)

    def execute(self, context):
        activate_group(context.scene, -1)
        self.report({'INFO'}, "All render groups hidden")
        return {'FINISHED'}


def _push_transparent_png_settings(scene):
    """Force Film Transparent + PNG RGBA for write_still batch renders.

    F12 / Line Art File Output can look fine while write_still still bakes
    transparency to black if image_settings.color_mode is RGB.
    Returns a dict to pass to _pop_transparent_png_settings.
    """
    image = scene.render.image_settings
    saved = {
        "filepath": scene.render.filepath,
        "film_transparent": scene.render.film_transparent,
        "file_format": image.file_format,
        "color_mode": image.color_mode,
    }
    scene.render.film_transparent = True
    image.file_format = 'PNG'
    image.color_mode = 'RGBA'
    return saved


def _pop_transparent_png_settings(scene, saved):
    if not saved:
        return
    scene.render.filepath = saved["filepath"]
    scene.render.film_transparent = saved["film_transparent"]
    image = scene.render.image_settings
    image.file_format = saved["file_format"]
    image.color_mode = saved["color_mode"]


def _render_groups(op, context, groups):
    """Solo each group in turn, write a transparent PNG, restore visibility.

    groups: sequence of (index, item) as yielded by iter_groups().
    """
    scene = context.scene
    if not bpy.data.is_saved:
        op.report({'ERROR'}, "Save the .blend first")
        return {'CANCELLED'}

    props = scene.lazy_render_groups_props
    previous_index = props.active_index
    previous_show_all = props.show_all
    saved_output = _push_transparent_png_settings(scene)

    blend_dir = os.path.dirname(bpy.path.abspath(bpy.data.filepath))
    stamp = datetime.now().strftime("_%Y%m%d_%H%M%S") if props.add_timestamp else ""
    rendered = 0

    try:
        for index, item in groups:
            activate_group(scene, index)
            context.view_layer.update()

            safe = _safe_filename(item.name)
            scene.render.filepath = os.path.join(blend_dir, f"{safe}{stamp}")
            bpy.ops.render.render(write_still=True)
            rendered += 1
    finally:
        _pop_transparent_png_settings(scene, saved_output)
        props.show_all = previous_show_all
        if previous_show_all:
            _apply_visibility(scene)
        elif 0 <= previous_index < len(props.groups):
            activate_group(scene, previous_index)
        else:
            activate_group(scene, -1)

    op.report({'INFO'}, f"Rendered {rendered} group(s) → {blend_dir}")
    return {'FINISHED'}


class LZRG_OT_render_all(Operator):
    bl_idname = "lazyrendergroups.render_all"
    bl_label = "Render All Groups"
    bl_description = (
        "Activate each group in turn and render a transparent PNG still next "
        "to the .blend, named with the group name (and a timestamp, if enabled)"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return has_render_groups(context.scene)

    def execute(self, context):
        scene = context.scene
        prune_missing_groups(scene)
        groups = list(iter_groups(scene))
        if not groups:
            self.report({'WARNING'}, "No render groups")
            return {'CANCELLED'}
        return _render_groups(self, context, groups)


class LZRG_OT_render_selected(Operator):
    bl_idname = "lazyrendergroups.render_selected"
    bl_label = "Render Selected Group"
    bl_description = (
        "Render only the group highlighted in the list, using the same naming "
        "rules as Render All Groups"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return has_render_groups(context.scene)

    def execute(self, context):
        scene = context.scene
        prune_missing_groups(scene)
        props = scene.lazy_render_groups_props

        index = props.active_index
        if not (0 <= index < len(props.groups)):
            self.report({'ERROR'}, "No group selected — click a group in the list")
            return {'CANCELLED'}

        item = props.groups[index]
        if item.collection is None:
            self.report({'ERROR'}, f"Group '{item.name}' has no collection")
            return {'CANCELLED'}

        return _render_groups(self, context, [(index, item)])


# ----------------------------------------------------------------------------
# Public helpers for sibling addons (Lazy Line Art)
# ----------------------------------------------------------------------------

def render_all_with_callback(context, render_one_fn):
    """Activate each group, call render_one_fn(context, group_name), restore.

    render_one_fn should perform a blocking render (write_still=True style).
    Returns (count, error_message_or_None).
    """
    scene = context.scene
    if not has_render_groups(scene):
        return 0, "No render groups"
    prune_missing_groups(scene)
    groups = list(iter_groups(scene))
    if not groups:
        return 0, "No render groups"

    props = scene.lazy_render_groups_props
    previous_index = props.active_index
    previous_show_all = props.show_all
    count = 0
    try:
        for index, item in groups:
            activate_group(scene, index)
            context.view_layer.update()
            err = render_one_fn(context, item.name)
            if err:
                return count, err
            count += 1
    finally:
        props.show_all = previous_show_all
        if previous_show_all:
            _apply_visibility(scene)
        elif 0 <= previous_index < len(props.groups):
            activate_group(scene, previous_index)
        else:
            activate_group(scene, -1)
    return count, None


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

class LZRG_PT_panel(Panel):
    bl_label = "Lazy Render Groups"
    bl_idname = "LZRG_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_order = 80

    def draw(self, context):
        layout = self.layout
        props = getattr(context.scene, "lazy_render_groups_props", None)
        if props is None:
            layout.label(text="Props not registered — reload the addon", icon='ERROR')
            return

        col = layout.column()
        col.scale_y = 1.4
        col.operator(LZRG_OT_create_group.bl_idname, icon='COLLECTION_NEW')

        layout.separator()
        layout.label(text="Groups (click to solo, again to hide):")

        if not props.groups:
            layout.label(text="No groups yet", icon='INFO')
        else:
            for i, item in enumerate(props.groups):
                row = layout.row(align=True)
                is_solo = (not props.show_all) and (props.active_index == i)
                icon = 'RADIOBUT_ON' if is_solo else 'RADIOBUT_OFF'
                op = row.operator(
                    LZRG_OT_toggle_group.bl_idname,
                    text="",
                    icon=icon,
                    depress=is_solo,
                )
                op.index = i
                row.prop(item, "name", text="")
                count = len(item.collection.objects) if item.collection else 0
                row.label(text=f"({count})")
                rm = row.operator(
                    LZRG_OT_remove_group.bl_idname,
                    text="",
                    icon='TRASH',
                )
                rm.index = i

        row = layout.row(align=True)
        row.operator(LZRG_OT_add_to_active.bl_idname, icon='ADD')
        row.operator(LZRG_OT_show_all.bl_idname, icon='HIDE_OFF')
        row.operator(LZRG_OT_hide_all.bl_idname, icon='HIDE_ON')

        box = layout.box()
        if props.show_all:
            box.label(text="Mode: Show All", icon='HIDE_OFF')
        elif props.active_index < 0 or props.active_index >= len(props.groups):
            box.label(text="Mode: None (groups hidden)", icon='HIDE_ON')
        else:
            item = props.groups[props.active_index]
            box.label(text=f"Mode: Solo — {item.name}", icon='CHECKMARK')
            if item.collection is not None:
                box.label(
                    text=f"{len(item.collection.objects)} object(s)",
                    icon='OBJECT_DATA',
                )

        layout.separator()
        layout.prop(props, "add_timestamp")
        col = layout.column()
        col.scale_y = 1.4
        sel = col.column()
        sel.enabled = 0 <= props.active_index < len(props.groups)
        sel.operator(LZRG_OT_render_selected.bl_idname, icon='RENDER_STILL')
        col.operator(LZRG_OT_render_all.bl_idname, icon='RENDER_ANIMATION')
        layout.label(text="Camera/lights stay outside groups.")


# ----------------------------------------------------------------------------
# Register
# ----------------------------------------------------------------------------

classes = (
    LazyRenderGroupItem,
    LazyRenderGroupsProps,
    LZRG_OT_create_group,
    LZRG_OT_add_to_active,
    LZRG_OT_remove_group,
    LZRG_OT_toggle_group,
    LZRG_OT_activate_group,
    LZRG_OT_show_all,
    LZRG_OT_hide_all,
    LZRG_OT_render_all,
    LZRG_OT_render_selected,
    LZRG_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.lazy_render_groups_props = PointerProperty(type=LazyRenderGroupsProps)


def unregister():
    del bpy.types.Scene.lazy_render_groups_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
