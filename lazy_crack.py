import bpy
from bpy.types import Operator, Panel
import math
from mathutils import Vector

import random
from math import radians


# BIG TODO s:
# * wheel rotation should be relative to the viewer's plane, not z! 
# * scale of crack detail should be absolute not relative to the object

bl_info = {
    "name": "Lazy Crack",
    "author": "thelazyone",
    "version": (1, 0, 10),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Apply volumetric cracks with infill",
    "warning": "",
    "doc_url": "https://github.com/thelazyone/lazy_tools",
    "category": "Mesh",
}

class CrackSettings(bpy.types.PropertyGroup):
    fill_crack: bpy.props.BoolProperty(name="Fill Crack", default=False)
    center_geometry: bpy.props.BoolProperty(name="Center Origin", default=True)
    noise_scale: bpy.props.FloatProperty(name="Noise Scale", default=1.5, min=0.1, max=10.0)
    noise_depth: bpy.props.IntProperty(name="Noise Depth", default=3, min=0, max=10)
    noise_resolution: bpy.props.IntProperty(name="Noise Resolution", default=6, min=3, max=7)
    thickness: bpy.props.FloatProperty(name="Thickness", default=0.2, min=0.01, max=0.5)
    noise_intensity: bpy.props.FloatProperty(name="Noise Intensity", default=1.6, min=0.1, max=10.0)


class MESH_OT_crack(Operator):
    bl_idname = "mesh.crack"
    bl_label = "Crack Volumes"
    bl_description = "Performs a volumetric crack on a given volume with infill"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'
    
    def modal(self, context, event):
        bpy.ops.object.editmode_toggle()
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.editmode_toggle()

        # Dragging:
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self.dragging = True
            self.mouse_x_init = event.mouse_x  # Update initial positions on drag start
            self.mouse_y_init = event.mouse_y
        elif self.dragging and event.type == 'MOUSEMOVE':
            dx = event.mouse_x - self.mouse_x_init
            dy = event.mouse_y - self.mouse_y_init
            self.displace_cracker(context, dx, dy)
            self.mouse_x_init, self.mouse_y_init = event.mouse_x, event.mouse_y 
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self.dragging = False

        elif event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            self.rotate_cracker(context, event.type)

        elif event.type in {'RET', 'NUMPAD_ENTER'}:
            return self.apply_crack(context)

        elif event.type in {'RIGHTMOUSE', 'ESC'}:
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

        
    def invoke(self, context, event):
        self.dragging = False 
        self.mouse_x_init = event.mouse_x
        self.mouse_y_init = event.mouse_y
        self.create_cracker(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def center_geometry_origin(self, context):
        obj = context.active_object
        # Calculate the center of the bounding box
        local_bbox_center = 0.125 * sum((Vector(b) for b in obj.bound_box), Vector())
        global_bbox_center = obj.matrix_world @ local_bbox_center

        # Move the object so that the bounding box center goes to the origin
        obj.location = global_bbox_center

        # Set the origin to the geometry's bounding box center
        bpy.ops.object.origin_set(type='ORIGIN_CENTER_OF_MASS')  # OR 'ORIGIN_CENTER_OF_VOLUME'

        # Adjust the object's location to compensate for the origin shift
        obj.location -= global_bbox_center - obj.location
    
    def create_cracker(self, context):
        
        # Copying the settings
        settings = context.scene.crack_settings

        # If requested, centering the geometry here
        if settings.center_geometry:
            bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')

        # Ensure the texture exists before using it
        if "cracker_texture" not in bpy.data.textures:
            cracker_texture = bpy.data.textures.new(name="cracker_texture", type='CLOUDS')
            cracker_texture.noise_basis = 'BLENDER_ORIGINAL'
        else:
            cracker_texture = bpy.data.textures["cracker_texture"]
        cracker_texture.noise_scale = settings.noise_scale
        cracker_texture.noise_depth = settings.noise_depth

        # Store target obj
        targetName = bpy.context.object.name
        targetObj = bpy.data.objects[targetName]

        dimx = bpy.context.object.dimensions.x
        dimy = bpy.context.object.dimensions.y
        dimz = bpy.context.object.dimensions.z

        dimensions = (dimx, dimy, dimz)
        dimMax = max(dimensions)
        randomLoc = random.uniform(dimMax/-4, dimMax/4)

        locX = bpy.data.objects[targetName].location[0] + randomLoc/2
        locY = bpy.data.objects[targetName].location[1] 
        locZ = bpy.data.objects[targetName].location[2] + randomLoc/2

        crackerLoc = (locX, locY, locZ)
        crackerRot = math.pi * random.uniform(-30, 30)

        # empty creation
        bpy.ops.object.empty_add(type='PLAIN_AXES', align='VIEW', location=(crackerLoc))
        bpy.context.object.name = "cracker_empty"
        bpy.ops.object.hide_view_set(unselected=False)

        # Cracker obj creation
        bpy.ops.mesh.primitive_plane_add(size=dimMax*2.5, enter_editmode=False, align='VIEW',location = (crackerLoc), scale=(1, 1, 1))
        bpy.context.object.rotation_euler[0] += 1.5708
        bpy.context.object.rotation_euler.rotate_axis('Y', radians(crackerRot))

        bpy.ops.object.shade_smooth()
        bpy.ops.object.hide_view_set(unselected=False)

        bpy.context.object.name = "cracker"
        crackerName = bpy.context.object.name
        crackerObj = bpy.data.objects["cracker"]
        crackerEmpty = bpy.data.objects["cracker_empty"]

        crackerObj.parent = crackerEmpty
        crackerObj.matrix_parent_inverse = crackerEmpty.matrix_world.inverted()

        bpy.data.objects[crackerName].modifiers.new("Cracker_Subd", type='SUBSURF')
        bpy.data.objects[crackerName].modifiers["Cracker_Subd"].subdivision_type = 'SIMPLE'
        bpy.data.objects[crackerName].modifiers["Cracker_Subd"].levels = settings.noise_resolution

        bpy.data.objects[targetName].modifiers.new("cracker", type='BOOLEAN')
        bpy.data.objects[targetName].modifiers["cracker"].solver = 'FAST'
        bpy.data.objects[targetName].modifiers["cracker"].object = crackerObj
        bpy.data.objects[targetName].modifiers["cracker"].operation = 'DIFFERENCE'

        bpy.data.objects['cracker'].modifiers.new("Cracker_Bend", type='SIMPLE_DEFORM')
        bpy.data.objects['cracker'].modifiers["Cracker_Bend"].deform_method = 'BEND'
        bpy.data.objects['cracker'].modifiers["Cracker_Bend"].deform_axis = 'Z'
        bpy.data.objects['cracker'].modifiers["Cracker_Bend"].angle = 0
        bpy.data.objects['cracker'].modifiers["Cracker_Bend"].origin = crackerEmpty

        bpy.data.objects['cracker'].modifiers.new("Cracker_Displace", type='DISPLACE')
        bpy.data.objects['cracker'].modifiers["Cracker_Displace"].texture = cracker_texture
        bpy.data.objects['cracker'].modifiers["Cracker_Displace"].strength = settings.noise_intensity

        bpy.data.objects['cracker'].modifiers.new("Cracker_Solid", type='SOLIDIFY')
        bpy.data.objects['cracker'].modifiers["Cracker_Solid"].thickness = settings.thickness
        bpy.data.objects['cracker'].modifiers["Cracker_Solid"].offset = 0

        bpy.data.objects[targetName].select_set(True)
        bpy.context.view_layer.objects.active = targetObj

        pass

    

    # EXPERIMENT - CONDENSED FUNCTION
    def add_crack_filler(self, context, crack_obj, main_obj):
        filler_obj = self.duplicate_and_modify_crack(context, crack_obj)
        shrunken_obj = self.duplicate_and_shrink_original(context, main_obj)
        out_obj = self.intersect_filler_and_shrunken(context, filler_obj, shrunken_obj, main_obj.name)
        
        # Link the filler object to the scene
        #if out_obj.name not in context.collection.objects:
        if out_obj.name not in context.collection.objects:
            context.collection.objects.link(out_obj)
            # TODO FIX: THIS SOUNDS LIKE A GREAT WAY TO MAKE BUGS


    def clean_duplicate(self, context, target_obj):
        # Store the original list of objects before duplication
        original_objects = set(context.scene.objects)

        # Ensure target_obj is the only selected object for accurate duplication
        bpy.ops.object.select_all(action='DESELECT')
        target_obj.select_set(True)
        context.view_layer.objects.active = target_obj
        
        # Duplicate the object
        new_object = target_obj.copy()
        new_object.data = target_obj.data.copy()
        bpy.context.scene.collection.objects.link(new_object)
        new_object.location = target_obj.matrix_world.translation
        return new_object
    
        
    # EXPERIMENT 1/3 TODO COMMENT
    def duplicate_and_modify_crack(self, context, crack_obj):
        # Ensure crack_obj is the only selected object for accurate duplication
        filler_obj = self.clean_duplicate(context, crack_obj)
        filler_obj.name = "filler_obj__"
        filler_obj.modifiers["Cracker_Solid"].thickness += 0.01

        filler_obj.select_set(True)
        context.view_layer.objects.active = filler_obj
        bpy.ops.object.convert(target='MESH')

        return filler_obj
    
    # EXPERIMENT 2/3 TODO COMMENT
    def duplicate_and_shrink_original(self, context, original_obj):

        shrunken_obj = self.clean_duplicate(context, original_obj)
        shrunken_obj.name = "lazy_shrunken"

        # Apply remesh and shrink
        # TODO

        return shrunken_obj

    
    # EXPERIMENT 3/3 TODO COMMENT
    def intersect_filler_and_shrunken(self, context, filler_obj, shrunken_obj, original_name):

       
        # Ensure the filler object is selected and active
        bpy.ops.object.select_all(action='DESELECT')
        if not filler_obj:
            return None  # One of the objects doesn't exist
        
        if not shrunken_obj:
            return None  # One of the objects doesn't exist

        filler_obj.select_set(True)
        context.view_layer.objects.active = filler_obj

        # Apply Boolean modifier to intersect with the shrunken object
        bpy.ops.object.modifier_add(type='BOOLEAN')

        boolean_modifier = filler_obj.modifiers[-1]
        boolean_modifier.operation = 'INTERSECT'
        boolean_modifier.solver = 'FAST'
        boolean_modifier.object = shrunken_obj
        bpy.ops.object.modifier_apply(modifier=boolean_modifier.name)

        # Optionally, rename the filler object to indicate it's the final filler
        final_filler_name = original_name + "_filler"
        filler_obj.name = final_filler_name

        return filler_obj

    

    def displace_cracker(self, context, dx, dy):
        if self.dragging:
            # Find the 3D View area to get its view matrix
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    view_matrix = area.spaces.active.region_3d.view_matrix
                    break
            else:
                return  # No 3D View found, cannot proceed

            # Convert screen displacement dx, dy into 3D space displacement
            # Adjust the displacement scale factor as needed
            scale_factor = 0.01  # Example scale factor, adjust based on your needs
            displacement_vector = view_matrix.inverted().to_3x3() @ Vector((dx * scale_factor, dy * scale_factor, 0))
            
            # Apply the displacement to the cracker_empty object
            cracker_empty = bpy.data.objects.get("cracker_empty")
            if cracker_empty:
                cracker_empty.location += displacement_vector


    def rotate_cracker(self, context, wheel_direction):
        rotation_amount = radians(5) if wheel_direction == 'WHEELUPMOUSE' else radians(-5)
        bpy.data.objects['cracker'].rotation_euler.rotate_axis('Y', rotation_amount)


    def apply_crack(self, context):
        original_active_object = context.view_layer.objects.active  # Store the original active object
        original_active_object_name = original_active_object.name  # Store the original active object's name for comparison

        # If "fill crack" is set, creating here the other element too.
        if context.scene.crack_settings.fill_crack:
            cloned_cracker = bpy.data.objects.get('cracker')
            cloned_cracker.name = "cloned_cracker"
            self.add_crack_filler(context, cloned_cracker, original_active_object)

        # Apply the crack modifier
        for mod in original_active_object.modifiers:
            if mod.name == "cracker":
                bpy.ops.object.modifier_apply(modifier=mod.name)

        # Remove the cracker and empty objects
        object_to_delete = bpy.data.objects.get('cracker')
        empty_to_delete = bpy.data.objects.get('cracker_empty')
        if object_to_delete:
            bpy.data.objects.remove(object_to_delete, do_unlink=True)
        if empty_to_delete:
            bpy.data.objects.remove(empty_to_delete, do_unlink=True)

        # Ensure we're in Object mode
        if bpy.context.active_object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.object.select_all(action='DESELECT')  # Deselect all objects

        # Reselect the original object and separate it into loose parts
        original_active_object.select_set(True)
        context.view_layer.objects.active = original_active_object
        bpy.ops.object.mode_set(mode='EDIT')  # Switch to Edit Mode
        bpy.ops.mesh.select_all(action='SELECT')  # Select all mesh parts
        bpy.ops.mesh.separate(type='LOOSE')  # Separate by loose parts
        bpy.ops.object.mode_set(mode='OBJECT')  # Switch back to Object Mode

        # Iterate through the objects to find new ones and set them to shade flat
        for obj in bpy.data.objects:
            if obj.name != original_active_object_name and obj.type == 'MESH':
                obj.select_set(True)
                context.view_layer.objects.active = obj  # Make the object active
                bpy.ops.object.shade_flat()  # Set shading to flat

        

        # Cleanup: Deselect all and reselect the original object
        bpy.ops.object.select_all(action='DESELECT')
        original_active_object.select_set(True)
        context.view_layer.objects.active = original_active_object

        return {'FINISHED'}  # or return {'CANCELLED'} as needed



class LAZYCRACK_PT_main(Panel):
    bl_idname = "LAZYCRACK_PT_main"
    bl_label = "Lazy Crack"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_context = "objectmode"
    bl_order = 20

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.crack_settings

        # Settings
        layout.prop(settings, "fill_crack")
        layout.prop(settings, "center_geometry")
        layout.prop(settings, "noise_scale")
        layout.prop(settings, "noise_depth")
        layout.prop(settings, "noise_resolution")
        layout.prop(settings, "thickness")
        layout.prop(settings, "noise_intensity")

        # Apply Crack button
        layout.operator("mesh.crack", text="Apply Crack")



def register():
    bpy.utils.register_class(CrackSettings)
    bpy.utils.register_class(MESH_OT_crack)
    bpy.utils.register_class(LAZYCRACK_PT_main)
    bpy.types.Scene.crack_settings = bpy.props.PointerProperty(type=CrackSettings)

def unregister():
    # Drop the Scene pointer before freeing the PropertyGroup it points at.
    del bpy.types.Scene.crack_settings
    bpy.utils.unregister_class(LAZYCRACK_PT_main)
    bpy.utils.unregister_class(MESH_OT_crack)
    bpy.utils.unregister_class(CrackSettings)

if __name__ == "__main__":
    register()