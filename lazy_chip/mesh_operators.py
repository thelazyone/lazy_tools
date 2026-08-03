import bpy
import bmesh

# Mesh utilities.


# Calls the Blender 3D Print Toolbox "make manifold" once.
def make_non_manifold(object: bpy.types.Object, ):
    # Ensure the 3D Printing Toolbox is enabled
    addon_key = 'mesh_3d_print_toolbox'
    if addon_key not in bpy.context.preferences.addons:
        bpy.ops.preferences.addon_enable(module=addon_key)
    
    # Save the current mode so we can return to it later
    original_mode = object.mode
    
    # Ensure object is in edit mode for the operation
    if object.mode != 'EDIT':
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


# Checks if the model is watertight.
# Using https://blender.stackexchange.com/questions/160055/is-there-a-way-to-use-the-terminal-to-check-if-a-mesh-is-watertight
def is_watertight_mesh(object: bpy.types.Object, check_self_intersection=True) -> bool:
    """
    Checks whether the given object is watertight or not
    :param object: Object the inspect
    :return: True if watertight, False otherwise
    """
    
    old_active_object = bpy.context.view_layer.objects.active
    
    old_mode = old_active_object.mode

    bpy.context.view_layer.objects.active = object

    bpy.ops.object.mode_set(mode='EDIT')

    # Store the previous selection mode and switch to vertex selection mode
    previous_select_mode = tuple(bpy.context.tool_settings.mesh_select_mode)
    bpy.context.tool_settings.mesh_select_mode = (True, False, False)

    bpy.ops.mesh.select_non_manifold(extend=False)
    bm = bmesh.from_edit_mesh(object.data)

    is_watertight = True

    for v in bm.verts:
        if v.select:
            is_watertight = False
            break

    bpy.context.view_layer.objects.active = old_active_object
    bpy.ops.object.mode_set(mode=old_mode)

    # Restore the previous selection mode
    bpy.context.tool_settings.mesh_select_mode = previous_select_mode

    bpy.context.view_layer.objects.active = old_active_object
    bpy.ops.object.mode_set(mode=old_mode)

    return is_watertight


# Attempts to fix non-manifold issues, repeating the 3D Print Toolbox command
# Multiple times until there are no more non-manifold issues.
def make_non_manifold_iterate(object: bpy.types.Object, max_iterations):
    for i in range(max_iterations):
        if not is_watertight_mesh(object):
            make_non_manifold(object)
        else:
            break


class LAZYCHIP_OT_fix_manifold(bpy.types.Operator):
    bl_idname = "lazychip.op_fixmanifold"
    bl_label = "Fix Manifold"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                context.view_layer.objects.active = obj
                make_non_manifold(obj)
        return {'FINISHED'}

def register():
    bpy.utils.register_class(LAZYCHIP_OT_fix_manifold)

def unregister():
    bpy.utils.unregister_class(LAZYCHIP_OT_fix_manifold)
