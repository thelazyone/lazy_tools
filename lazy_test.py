bl_info = {
    "name": "Lazy Tests",
    "author": "thelazyone",
    "version": (1, 0, 2),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Testing new utilities in a dedicated panel",
    "warning": "",
    "doc_url": "https://github.com/thelazyone/lazy_tools",
    "category": "Mesh",
}

import bpy
from bpy.types import Operator, Panel



def make_non_manifold(obj):
    # Ensure the 3D Printing Toolbox is enabled
    addon_key = 'mesh_3d_print_toolbox'
    if addon_key not in bpy.context.preferences.addons:
        bpy.ops.preferences.addon_enable(module=addon_key)
    
    # Save the current mode so we can return to it later
    original_mode = obj.mode
    
    # Ensure object is in edit mode for the operation
    if obj.mode != 'EDIT':
        bpy.ops.object.mode_set(mode='EDIT')
    
    try:
        # Perform the make non-manifold operation
        bpy.ops.mesh.print3d_clean_non_manifold()
    except AttributeError:
        print("3D Printing Toolbox addon not installed!")
        return {'CANCELLED'}
    finally:
        # Return to the original mode
        if original_mode != 'EDIT':
            bpy.ops.object.mode_set(mode=original_mode)
    
    return {'FINISHED'}


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


def make_non_manifold_iterate(obj, max_iterations):
    """Keep applying the fix until the mesh is watertight or we run out of tries."""
    for _ in range(max_iterations):
        if is_watertight_mesh(obj):
            break
        make_non_manifold(obj)



class OBJECT_OT_test_make_manifold(Operator):
    bl_idname = "object.test_make_manifold"
    bl_label = "Test Make Manifold"
    bl_description = "Using the 3D print toolbox make manifold."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'
    

    def execute(self, context):
        obj = context.active_object

        make_non_manifold_iterate(obj, 10)

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