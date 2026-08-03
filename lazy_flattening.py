bl_info = {
    "name": "Lazy Flattening",
    "author": "thelazyone",
    "version": (1, 1, 3),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Crops meshes along a world-space Z plane",
    "warning": "",
    "doc_url": "https://github.com/thelazyone/lazy_tools",
    "category": "Mesh",
}

import bpy
from bpy.types import Operator, Panel
from bpy.props import FloatProperty
import bmesh

# Tolerance for "is this vertex sitting on the cut plane", in world units.
PLANE_EPSILON = 0.001


class ZFlattenerSettings(bpy.types.PropertyGroup):
    z_level: FloatProperty(
        name="Z coordinate",
        description="World-space Z height to cut at. Everything below it is removed",
        default=0.0,
    )


def connect_vertices_on_plane(obj, z_level, threshold):
    """Connect nearby vertices lying on the cut plane so the opening can be filled.

    Vertex coordinates are object-local, so each one is pushed through
    matrix_world before its height is compared against the world-space plane.
    """
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    matrix = obj.matrix_world
    plane_verts = [
        v for v in bm.verts
        if abs((matrix @ v.co).z - z_level) < PLANE_EPSILON
    ]

    # Connect vertices within the threshold distance
    connected = set()
    for v1 in plane_verts:
        for v2 in plane_verts:
            if v1 is v2:
                continue
            if (v1, v2) in connected or (v2, v1) in connected:
                continue
            if (v1.co - v2.co).length < threshold:
                connected.add((v1, v2))
                bmesh.ops.connect_verts(bm, verts=[v1, v2])

    # Update the mesh
    bm.to_mesh(mesh)
    bm.free()


class LAZYFLAT_PT_main(Panel):
    bl_idname = "LAZYFLAT_PT_main"
    bl_label = "Lazy Flattening"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_context = "objectmode"
    bl_order = 30

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.z_flattener_settings
        layout.prop(settings, "z_level", text="Z coordinate")
        layout.operator(MESH_OT_flatten_vertices.bl_idname)
        

class MESH_OT_flatten_vertices(Operator):
    bl_idname = "mesh.flatten_vertices"
    bl_label = "Flatten Vertices"
    bl_description = (
        "Split faces along the world Z plane, remove everything below it, and fill the area"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data
        z_level = context.scene.z_flattener_settings.z_level

        # The cut plane lives in world space and the object is left where it is.
        # Previously the object was temporarily moved to the origin so that local
        # coordinates happened to match world ones -- which only held for objects
        # with no rotation or scale, and meant the cut always ran through the
        # object's own origin rather than a real Z height.
        plane_co = (0.0, 0.0, z_level)

        # Enter edit mode
        bpy.ops.object.mode_set(mode='EDIT')

        # Bisect along the plane. mesh.bisect takes world-space plane arguments
        # and maps them into object space itself.
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.bisect(plane_co=plane_co, plane_no=(0, 0, 1), use_fill=True)

        # Select everything below the plane. Vertex coordinates are object-local,
        # so each one is transformed to world space before comparing heights.
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')
        matrix = obj.matrix_world
        for v in mesh.vertices:
            if (matrix @ v.co).z < z_level - PLANE_EPSILON:
                v.select = True
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.delete(type='VERT')

        # Separate disconnected parts of the mesh
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.separate(type='LOOSE')

        # Return to object mode
        bpy.ops.object.mode_set(mode='OBJECT')

        # separate() keeps one island in the original object and moves the rest
        # into new ones, so the whole result is in the selection - the original
        # included. It used to be skipped here, which left that island uncapped.
        islands = [o for o in context.selected_objects if o.type == 'MESH']

        # Process each island separately
        for island in islands:
            # Isolate the island first: with several meshes selected, entering
            # edit mode opens them all at once and the operators below would
            # run across every island instead of just this one.
            bpy.ops.object.select_all(action='DESELECT')
            island.select_set(True)
            context.view_layer.objects.active = island

            # Enter edit mode
            bpy.ops.object.mode_set(mode='EDIT')

            # Fill the area
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.object.mode_set(mode='OBJECT')

            connect_vertices_on_plane(island, z_level, 0.1)

        # Hand back every island selected, with the original object active
        bpy.ops.object.select_all(action='DESELECT')
        for island in islands:
            island.select_set(True)
        context.view_layer.objects.active = obj

        return {'FINISHED'}


def register():
    bpy.utils.register_class(ZFlattenerSettings)
    bpy.utils.register_class(MESH_OT_flatten_vertices)
    bpy.utils.register_class(LAZYFLAT_PT_main)

    bpy.types.Scene.z_flattener_settings = bpy.props.PointerProperty(type=ZFlattenerSettings)

def unregister():
    # Drop the Scene pointer before freeing the PropertyGroup it points at.
    del bpy.types.Scene.z_flattener_settings

    bpy.utils.unregister_class(LAZYFLAT_PT_main)
    bpy.utils.unregister_class(MESH_OT_flatten_vertices)
    bpy.utils.unregister_class(ZFlattenerSettings)


if __name__ == "__main__":
    register()