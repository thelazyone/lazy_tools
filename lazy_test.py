bl_info = {
    "name": "Lazy Tests",
    "author": "thelazyone",
    "version": (1, 0, 3),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Testing new utilities in a dedicated panel",
    "warning": "",
    "doc_url": "https://github.com/thelazyone/lazy_tools",
    "category": "Mesh",
}

import sys

import bpy
from bpy.types import Operator, Panel


# The 3D Print Toolbox bridge lives in Lazy Chip, which owns it. This file used
# to carry its own copy; the two drifted apart and this one ended up with an
# inverted loop, so it now borrows the real implementation instead.
def _lazy_chip_mesh_operators():
    """Find lazy_chip.mesh_operators, whatever module path it is installed under."""
    for name, mod in list(sys.modules.items()):
        if mod is None or not name.endswith("lazy_chip.mesh_operators"):
            continue
        if hasattr(mod, "make_manifold"):
            return mod
    return None


def make_manifold(obj):
    mod = _lazy_chip_mesh_operators()
    if mod is None:
        print("[Lazy Tests] Lazy Chip is not enabled - Make Manifold unavailable")
        return {'CANCELLED'}
    return mod.make_manifold(obj)


def is_watertight_mesh(obj):
    """True when the mesh has no non-manifold vertices left."""
    # Ensure the object is a mesh
    if obj.type != 'MESH':
        raise ValueError("Provided object is not a mesh")

    # Store the current mode so we can revert back to it
    original_mode = obj.mode

    # Switch to object mode to safely switch to edit mode
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.objects.active = obj

    # Switch to edit mode to select non-manifold vertices
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_non_manifold()

    # Switch back to object mode to read selection data
    bpy.ops.object.mode_set(mode='OBJECT')

    # A selected vertex means the mesh is still non-manifold there
    has_non_manifold = any(v.select for v in obj.data.vertices)

    # Return to the original mode
    bpy.ops.object.mode_set(mode=original_mode)

    return not has_non_manifold


def make_manifold_iterate(obj, max_iterations):
    """Keep applying the fix until the mesh is watertight or we run out of tries."""
    for _ in range(max_iterations):
        if is_watertight_mesh(obj):
            break
        if make_manifold(obj) == {'CANCELLED'}:
            break



class OBJECT_OT_test_make_manifold(Operator):
    bl_idname = "object.test_make_manifold"
    bl_label = "Test Make Manifold"
    bl_description = (
        "Make the active mesh manifold. Needs Lazy Chip enabled and the "
        "3D Print Toolbox extension installed"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        mod = _lazy_chip_mesh_operators()
        if mod is None:
            self.report({'ERROR'}, "Lazy Chip must be enabled - it owns the toolbox bridge")
            return {'CANCELLED'}
        if mod.get_manifold_operator() is None:
            self.report({'ERROR'}, mod.MISSING_MESSAGE)
            return {'CANCELLED'}

        make_manifold_iterate(context.active_object, 10)
        return {'FINISHED'}


class OBJECT_PT_test_make_manifold(Panel):
    bl_label = "Test Make Manifold"
    bl_idname = "OBJECT_PT_test_make_manifold"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_context = "objectmode"
    bl_order = 100

    def draw(self, context):
        layout = self.layout
        layout.operator(OBJECT_OT_test_make_manifold.bl_idname)

def register():
    bpy.utils.register_class(OBJECT_OT_test_make_manifold)
    bpy.utils.register_class(OBJECT_PT_test_make_manifold)

def unregister():
    bpy.utils.unregister_class(OBJECT_OT_test_make_manifold)
    bpy.utils.unregister_class(OBJECT_PT_test_make_manifold)

if __name__ == "__main__":
    register()