import bpy
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import IntProperty, FloatProperty, PointerProperty, BoolProperty
import math
import bmesh
from mathutils.bvhtree import BVHTree
from mathutils import Vector

bl_info = {
    "name": "Smart Decimation",
    "author": "thelazyone",
    "version": (1, 0, 7),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Decimate meshes with extra checks.",
    "warning": "",
    "wiki_url": "",
    "category": "Mesh",
}


# To solve overlapping edges and faces, applying a relaxation logic.
def relax_intersecting_faces(obj, iterations=3, relaxation_strength=0.5):
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    
    # Create a BVH tree and find intersecting pairs
    bvh = BVHTree.FromBMesh(bm)
    intersecting_pairs = bvh.overlap(bvh)
    
    # Extract vertices from intersecting faces
    vertices_to_relax = set()
    for pair in intersecting_pairs:
        face1, face2 = pair
        for face in [bm.faces[face1], bm.faces[face2]]:
            for vert in face.verts:
                vertices_to_relax.add(vert)
    
    # Relaxation process
    for _ in range(iterations):
        for vert in vertices_to_relax:
            avg_pos = Vector((0, 0, 0))
            for edge in vert.link_edges:
                other_vert = edge.other_vert(vert)
                avg_pos += other_vert.co
            avg_pos /= len(vert.link_edges)
            
            # Move the vertex towards the average position of connected vertices
            vert.co = vert.co.lerp(avg_pos, relaxation_strength)
    
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode='OBJECT')


# Fixing the overlapping edges
def fix_intersections_and_recalculate_normals(obj):
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    
    # Recalculate outside normals
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)
    
    # Fix intersecting faces
    bpy.ops.mesh.intersect_boolean(operation='UNION')
    
    bpy.ops.object.mode_set(mode='OBJECT')


# Checks if something is non-manifold.
def check_non_manifold(mesh):
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_non_manifold()
    bpy.ops.object.mode_set(mode='OBJECT')
    return any(v.select for v in mesh.vertices)


# A basic non-manifold fix. Not resolutive.
def fix_non_manifold(mesh):
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_non_manifold()
    bpy.ops.mesh.fill_holes()
    bpy.ops.mesh.intersect(mode='SELECT', separate_mode='CUT')
    bpy.ops.object.mode_set(mode='OBJECT')


# Checks any part of the decimated object that is not connected to the main body 
# and smaller than a certain threhsold
def delete_small_islands(obj, threshold=100):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Make sure we're in object mode
    bpy.ops.object.mode_set(mode='OBJECT')

    # Separate by loose parts
    bpy.ops.mesh.separate(type='LOOSE')
    bpy.context.view_layer.update()  # Ensure the scene is updated

    # Temporarily store the original object's name to avoid renaming issues
    original_name = obj.name

    # Collect objects to delete based on face count threshold
    objects_to_delete = [o for o in bpy.context.selected_objects if len(o.data.polygons) < threshold]

    # Check: if the objects to be deleted are all of them, ending this now.
    if len(objects_to_delete) < len(bpy.context.selected_objects): 

        # Delete objects
        for obj_to_delete in objects_to_delete:
            bpy.data.objects.remove(obj_to_delete, do_unlink=True)

        # Re-select the remaining objects for joining
        for obj in bpy.context.view_layer.objects:
            if obj.name.startswith(original_name):
                obj.select_set(True)
            else:
                obj.select_set(False)

        # Make sure there's more than one object selected for join operation
        if len(bpy.context.selected_objects) > 1:
            bpy.ops.object.join()  # Join the remaining objects

    # Rename the joined object to the original name
    bpy.context.view_layer.objects.active.name = original_name


# Fixes to be applied to the decimated mesh (or without decimating)
def fix_mesh(obj, settings):
    mesh = obj.data
    
    # Save current selection mode
    bpy.context.view_layer.objects.active = obj
    original_select_mode = bpy.context.tool_settings.mesh_select_mode[:]
    
    # Ensure we're in object mode for certain operations
    bpy.ops.object.mode_set(mode='OBJECT')

    # Switch to vertex select mode for operations
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.context.tool_settings.mesh_select_mode = (True, False, False)
    
    # Fix non-manifold geometry if necessary
    if settings.fix_non_manifold:
        fix_non_manifold(mesh)
    
    # Fix intersecting faces and recalculate normals if enabled
    if settings.fix_intersections:
        fix_intersections_and_recalculate_normals(obj)
        relax_intersecting_faces(obj)
        delete_small_islands(obj)

    # Restore original selection mode and switch back to object mode
    bpy.context.tool_settings.mesh_select_mode = original_select_mode
    bpy.ops.object.mode_set(mode='OBJECT')


class DecimateAndFixSettings(bpy.types.PropertyGroup):
    decimate_ratio: FloatProperty(
        name="Decimate Ratio",
        description="Decimate ratio for the modifier",
        min=0.01,
        max=1.0,
        default=0.1,
    )
    fix_non_manifold: BoolProperty(
        name="Fix Non-Manifold",
        description="Automatically fix non-manifold issues",
        default=False,
    )
    fix_intersections: BoolProperty(
        name="Fix Intersections",
        description="Automatically fix intersecting faces",
        default=False,
    )
    remesh_before_decimation: BoolProperty(
        name="Remesh Before Decimation",
        description="Apply remeshing before decimation",
        default=False,
    )
    apply_scale_before_remesh: BoolProperty(
        name="Apply Scale",
        description="Before remesh, apply object scale if it is not 1,1,1",
        default=True,
    )
    remesh_value: FloatProperty(
        name="Remesh Value",
        description="The value for remeshing",
        min=0.01,
        max=10.0,
        default=1.0,
    )


def _scale_is_unit(obj, abs_tol=1e-6):
    return all(math.isclose(obj.scale[i], 1.0, rel_tol=0.0, abs_tol=abs_tol) for i in range(3))


def _process_decimate_object(obj, context, settings, decimate_ratio, do_remesh):
    """Run remesh (optional), decimate at decimate_ratio, then fix_mesh. Caller sets active/select."""
    original_select_mode = context.tool_settings.mesh_select_mode[:]

    bpy.ops.object.mode_set(mode="OBJECT")

    if do_remesh and settings.remesh_before_decimation:
        if settings.apply_scale_before_remesh and not _scale_is_unit(obj):
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

        bpy.context.object.data.remesh_voxel_size = settings.remesh_value
        bpy.ops.object.voxel_remesh()

    bpy.ops.object.mode_set(mode="EDIT")
    context.tool_settings.mesh_select_mode = (True, False, False)

    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.decimate(ratio=decimate_ratio)

    fix_mesh(obj, settings)

    context.tool_settings.mesh_select_mode = original_select_mode
    bpy.ops.object.mode_set(mode="OBJECT")


# Decimating the mesh, then calling (conditionally) the fix_mesh function.
class MESH_OT_decimate_and_fix(Operator):
    bl_idname = "mesh.decimate_and_fix"
    bl_label = "Smart Decimation"
    bl_description = "Automates the process of adding a Decimate modifier, applying it, and fixing mesh issues"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'
        
    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):

        original_selection = context.selected_objects.copy()
        objects = context.selected_objects
        total_objects = len(objects)
        settings = context.scene.decimate_and_fix_settings

        for index, obj in enumerate(objects, start=1):
            if obj.type != 'MESH':
                continue

            self.report({'INFO'}, f"Processing object {index}/{total_objects}: {obj.name}")

            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)

            _process_decimate_object(
                obj, context, settings, settings.decimate_ratio, do_remesh=True
            )
        
        # Restore the original selection
        bpy.ops.object.select_all(action='DESELECT')
        for obj in original_selection:
            obj.select_set(True)
        
        # Set the first of the originally selected objects as active again
        if original_selection:
            bpy.context.view_layer.objects.active = original_selection[0]

        self.report({'INFO'}, "Finished processing all objects")
        return {'FINISHED'}


class MESH_OT_quick_decimate(Operator):
    """Decimate at a fixed ratio: ignores ratio UI and remesh-before-decimation."""

    bl_idname = "mesh.quick_decimate"
    bl_label = "Quick Decimate"
    bl_description = (
        "Decimate at the chosen ratio without remesh; UI ratio and remesh options are ignored"
    )

    ratio: FloatProperty(
        name="Ratio",
        description="Decimate ratio for this operation only",
        min=0.01,
        max=1.0,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        original_selection = context.selected_objects.copy()
        objects = context.selected_objects
        total_objects = len(objects)
        settings = context.scene.decimate_and_fix_settings

        for index, obj in enumerate(objects, start=1):
            if obj.type != "MESH":
                continue

            self.report(
                {"INFO"},
                f"Quick decimate {index}/{total_objects}: {obj.name} (ratio={self.ratio})",
            )

            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)

            _process_decimate_object(obj, context, settings, self.ratio, do_remesh=False)

        bpy.ops.object.select_all(action="DESELECT")
        for o in original_selection:
            o.select_set(True)

        if original_selection:
            bpy.context.view_layer.objects.active = original_selection[0]

        self.report({"INFO"}, "Finished quick decimate")
        return {"FINISHED"}


# Just Fix Operator
class MESH_OT_just_fix(Operator):
    bl_idname = "mesh.just_fix"
    bl_label = "Just Fix"
    bl_description = "Fix mesh issues without decimation"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'
        
    def execute(self, context):
        settings = context.scene.decimate_and_fix_settings
        objects = context.selected_objects
        for obj in objects:
            if obj.type == 'MESH':
                fix_mesh(obj, settings)
                pass

        return {'FINISHED'}
    

class MESH_PT_decimate_and_fix(Panel):
    bl_idname = "LazyDecimation"
    bl_label = "Lazy Decimation"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_context = "objectmode"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.decimate_and_fix_settings

        row = layout.row()
        row.operator("mesh.quick_decimate", text=".5 decimation").ratio = 0.5
        row.operator("mesh.quick_decimate", text=".1 decimation").ratio = 0.1

        layout.separator(factor=0.5)

        # Decimation ratio and remesh settings
        layout.prop(settings, "decimate_ratio")
        layout.prop(settings, "remesh_before_decimation")

        if settings.remesh_before_decimation:
            layout.prop(settings, "apply_scale_before_remesh")
            layout.prop(settings, "remesh_value")

        # Options for the Decimation
        layout.prop(settings, "fix_non_manifold")
        layout.prop(settings, "fix_intersections")

        # Decimate button
        layout.operator(MESH_OT_decimate_and_fix.bl_idname)
        
        # Just Fix button
        layout.operator("mesh.just_fix", text="Just Fix")


def register():
    bpy.utils.register_class(MESH_OT_decimate_and_fix)
    bpy.utils.register_class(MESH_OT_quick_decimate)
    bpy.utils.register_class(MESH_PT_decimate_and_fix)
    bpy.utils.register_class(MESH_OT_just_fix)
    bpy.utils.register_class(DecimateAndFixSettings)
    bpy.types.Scene.decimate_and_fix_settings = bpy.props.PointerProperty(type=DecimateAndFixSettings)

def unregister():
    bpy.utils.unregister_class(MESH_OT_decimate_and_fix)
    bpy.utils.unregister_class(MESH_OT_quick_decimate)
    bpy.utils.unregister_class(MESH_PT_decimate_and_fix)
    bpy.utils.unregister_class(MESH_OT_just_fix)
    bpy.utils.unregister_class(DecimateAndFixSettings)
    del bpy.types.Scene.decimate_and_fix_settings

if __name__ == "__main__":
    register()