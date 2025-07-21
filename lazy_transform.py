bl_info = {
    "name": "Lazy Transform Tools",
    "author": "thelazyone",
    "version": (1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Provides random transformation tools for selected objects",
    "warning": "",
    "wiki_url": "",
    "category": "Object",
}

import bpy
import bmesh
from bpy.types import Operator, Panel
from bpy.props import FloatProperty
import random
import mathutils
from mathutils import Vector, Matrix


class TRANSFORM_OT_random_rotation_z(Operator):
    bl_idname = "transform.random_rotation_z"
    bl_label = "Random Rotation Z"
    bl_description = "Rotates selected objects on their local Z-axis by a random amount between 0 and 360°"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) > 0 and context.mode == 'OBJECT'

    def execute(self, context):
        selected_objects = context.selected_objects
        
        for obj in selected_objects:
            if obj.type == 'MESH':
                # Generate random rotation angle between 0 and 360 degrees
                random_angle = random.uniform(0, 360)
                random_radians = random_angle * (3.14159 / 180)  # Convert to radians
                
                # Create rotation matrix for Z-axis
                rotation_matrix = mathutils.Matrix.Rotation(random_radians, 4, 'Z')
                
                # Apply rotation to the object's local transformation
                obj.matrix_local = obj.matrix_local @ rotation_matrix
                
        self.report({'INFO'}, f"Applied random Z rotation to {len(selected_objects)} objects")
        return {'FINISHED'}


class TRANSFORM_OT_random_rotation_all(Operator):
    bl_idname = "transform.random_rotation_all"
    bl_label = "Random Rotation All Axes"
    bl_description = "Rotates selected objects on all axes by random amounts (x,y,z,x,y,z sequence to prevent anisotropies)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) > 0 and context.mode == 'OBJECT'

    def execute(self, context):
        selected_objects = context.selected_objects
        
        for obj in selected_objects:
            if obj.type == 'MESH':
                # Generate 6 random rotation angles (x,y,z,x,y,z sequence)
                rotation_sequence = ['X', 'Y', 'Z', 'X', 'Y', 'Z']
                
                # Start with identity matrix
                combined_rotation = mathutils.Matrix.Identity(4)
                
                for axis in rotation_sequence:
                    # Generate random angle between 0 and 360 degrees
                    random_angle = random.uniform(0, 360)
                    random_radians = random_angle * (3.14159 / 180)
                    
                    # Create rotation matrix for current axis
                    rotation_matrix = mathutils.Matrix.Rotation(random_radians, 4, axis)
                    
                    # Combine with previous rotations
                    combined_rotation = combined_rotation @ rotation_matrix
                
                # Apply combined rotation to the object's local transformation
                obj.matrix_local = obj.matrix_local @ combined_rotation
                
        self.report({'INFO'}, f"Applied random all-axis rotation to {len(selected_objects)} objects")
        return {'FINISHED'}


class TRANSFORM_OT_random_scale(Operator):
    bl_idname = "transform.random_scale"
    bl_label = "Random Scale"
    bl_description = "Randomly scales selected objects by up to 10% (both up and down)"
    bl_options = {'REGISTER', 'UNDO'}

    scale_variation: FloatProperty(
        name="Scale Variation",
        description="Maximum scale variation percentage",
        default=10.0,
        min=0.1,
        max=50.0,
        unit='NONE'
    )

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) > 0 and context.mode == 'OBJECT'

    def execute(self, context):
        selected_objects = context.selected_objects
        
        for obj in selected_objects:
            if obj.type == 'MESH':
                # Generate random scale factor between (1 - variation/100) and (1 + variation/100)
                variation = self.scale_variation / 100.0
                min_scale = 1.0 - variation
                max_scale = 1.0 + variation
                random_scale_factor = random.uniform(min_scale, max_scale)
                
                # Create scale matrix (uniform scaling)
                scale_matrix = mathutils.Matrix.Scale(random_scale_factor, 4)
                
                # Apply scale to the object's local transformation
                obj.matrix_local = obj.matrix_local @ scale_matrix
                
        self.report({'INFO'}, f"Applied random scale to {len(selected_objects)} objects (±{self.scale_variation}%)")
        return {'FINISHED'}


class OBJECT_OT_set_local_z_to_view(Operator):
    bl_idname = "object.set_local_z_to_view"
    bl_label = "Set Local Z to View"
    bl_description = "Sets the object's local Z-axis to align with the current view direction, without changing its global orientation"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'
    
    def execute(self, context):
        obj = context.active_object

        # Calculate the target view direction, negated to align forwards
        view_direction = context.region_data.view_rotation @ Vector((0.0, 0.0, -1.0))

        # Compute the rotation matrix needed to align the local Z to the view direction
        # This is the current rotation matrix of the object
        align_matrix = obj.rotation_euler.to_matrix().to_4x4()

        # Local Z in world coordinates is the third column of the rotation matrix
        local_z_world = align_matrix @ Vector((0.0, 0.0, 1.0))

        # Find the rotation difference needed to align the local Z with the view direction
        rotation_difference = view_direction.rotation_difference(local_z_world).to_matrix().to_4x4()
        new_matrix = rotation_difference @ align_matrix

        # Convert the rotation difference to a matrix and apply it to the object
        obj.rotation_euler = new_matrix.to_euler()

        # Apply this rotation locally
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)

        # Immediately apply the inverse rotation to revert to the original orientation
        inverse_rotation_matrix = rotation_difference.to_4x4().inverted()
        obj.matrix_world = obj.matrix_world @ inverse_rotation_matrix

        return {'FINISHED'}


class TRANSFORM_PT_lazy_panel(Panel):
    bl_label = "Lazy Transform Tools"
    bl_idname = "TRANSFORM_PT_lazy_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_context = ""

    def draw(self, context):
        layout = self.layout

        # Check if objects are selected
        objects_selected = len(context.selected_objects) > 0 and context.mode == 'OBJECT'
        active_object = context.active_object is not None and context.active_object.type == 'MESH'
        
        layout.label(text="Random Transformations")
        
        col = layout.column()
        col.enabled = objects_selected
        
        col.operator(TRANSFORM_OT_random_rotation_z.bl_idname)
        col.operator(TRANSFORM_OT_random_rotation_all.bl_idname)
        
        # Scale section with property
        col.separator()
        col.label(text="Random Scale:")
        scale_op = col.operator(TRANSFORM_OT_random_scale.bl_idname)
        col.prop(scale_op, "scale_variation", text="Variation %")
        
        layout.separator()
        layout.label(text="Pose Tools")
        
        col = layout.column()
        col.enabled = active_object
        col.operator(OBJECT_OT_set_local_z_to_view.bl_idname)
        
        if not objects_selected and not active_object:
            layout.label(text="Select objects in Object mode", icon='INFO')


def register():
    bpy.utils.register_class(TRANSFORM_OT_random_rotation_z)
    bpy.utils.register_class(TRANSFORM_OT_random_rotation_all)
    bpy.utils.register_class(TRANSFORM_OT_random_scale)
    bpy.utils.register_class(OBJECT_OT_set_local_z_to_view)
    bpy.utils.register_class(TRANSFORM_PT_lazy_panel)


def unregister():
    bpy.utils.unregister_class(TRANSFORM_OT_random_rotation_z)
    bpy.utils.unregister_class(TRANSFORM_OT_random_rotation_all)
    bpy.utils.unregister_class(TRANSFORM_OT_random_scale)
    bpy.utils.unregister_class(OBJECT_OT_set_local_z_to_view)
    bpy.utils.unregister_class(TRANSFORM_PT_lazy_panel)


if __name__ == "__main__":
    register() 