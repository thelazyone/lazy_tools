bl_info = {
    "name": "Lazy Local View",
    "author": "thelazyone",
    "version": (1, 1),
    "blender": (4, 0, 0),
    "location": "Shift+/ in the 3D Viewport",
    "description": "Local view on selection while keeping all scene lights visible",
    "warning": "",
    "wiki_url": "",
    "category": "View",
}

import bpy
from bpy.types import Operator

addon_keymaps = []

SHORTCUT_KEY = 'SLASH'
SHORTCUT_MODIFIERS = dict(shift=True)


def _view3d_context(context):
    area = context.area if context.area and context.area.type == 'VIEW_3D' else None
    region = context.region if area is context.area else None
    if area is None:
        for candidate in context.screen.areas:
            if candidate.type == 'VIEW_3D':
                area = candidate
                break
    if area is None:
        return None, None, None
    if region is None:
        for candidate in area.regions:
            if candidate.type == 'WINDOW':
                region = candidate
                break
    return area, region, area.spaces.active


class VIEW3D_OT_local_view_with_lights(Operator):
    bl_idname = "view3d.local_view_with_lights"
    bl_label = "Local View with Lights"
    bl_description = "Isolate selection in local view while keeping all lights. Press again to exit"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        area, _, space = _view3d_context(context)
        return area is not None and (space.local_view or len(context.selected_objects) > 0)

    def execute(self, context):
        area, region, space = _view3d_context(context)
        if area is None or region is None:
            self.report({'WARNING'}, "No 3D Viewport found")
            return {'CANCELLED'}

        with bpy.context.temp_override(
            window=context.window,
            screen=context.screen,
            area=area,
            region=region,
            space_data=space,
        ):
            if space.local_view:
                bpy.ops.view3d.localview(frame_selected=False)
                return {'FINISHED'}

            saved = list(context.selected_objects)
            saved_active = context.view_layer.objects.active
            lights = [obj for obj in context.scene.objects if obj.type == 'LIGHT']

            for obj in context.view_layer.objects:
                obj.select_set(False)
            for obj in saved:
                obj.select_set(True)
            for obj in lights:
                obj.select_set(True)

            bpy.ops.view3d.localview(frame_selected=False)

            for obj in context.view_layer.objects:
                obj.select_set(False)
            for obj in saved:
                obj.select_set(True)
            if saved_active in saved:
                context.view_layer.objects.active = saved_active

            bpy.ops.view3d.view_selected()

        return {'FINISHED'}


def _register_keymap(kc, km_name, key_type):
    km = kc.keymaps.new(name=km_name, space_type='VIEW_3D')
    kmi = km.keymap_items.new(
        VIEW3D_OT_local_view_with_lights.bl_idname,
        type=key_type,
        value='PRESS',
        head=True,
        **SHORTCUT_MODIFIERS,
    )
    addon_keymaps.append((km, kmi))


def register():
    bpy.utils.register_class(VIEW3D_OT_local_view_with_lights)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return

    for km_name in ('3D View Generic', 'Object Mode', 'Mesh'):
        for key_type in (SHORTCUT_KEY, 'NUMPAD_SLASH'):
            _register_keymap(kc, km_name, key_type)


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
    bpy.utils.unregister_class(VIEW3D_OT_local_view_with_lights)


if __name__ == "__main__":
    register()
