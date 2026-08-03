import bpy
from bpy.types import Panel, PropertyGroup
from bpy.props import IntProperty, FloatProperty

class WeatheringProps(PropertyGroup):
    resolution_property: IntProperty(name="Resolution", default=64, min=16, max=4096)
    edge_relax_property: FloatProperty(name="Edge Relax", default=3.0, min=0, max=2048)
    edge_push_property: FloatProperty(name="Edge Push", default=0.7, min=0.0, max=1.0)
    noise_scale_property: FloatProperty(name="Noise Scale", default=40, min=0, max=8192)
    noise_strength_property: FloatProperty(name="Noise Strength", default=8.0, min=0, max=8192)
    noise_contrast_property: FloatProperty(name="Noise Contrast", default=1.0, min=0, max=1024)
    seed_property: IntProperty(name="Seed", default=0, min=0, max=999999)
    random_seed_property: bpy.props.BoolProperty(name="Random Seed", default=True)
    fixed_scale_check_property: bpy.props.BoolProperty(name="Use Fixed Scale", default=True)
    fixed_scale_property: FloatProperty(name="Noise Scale", default=4, min=0.1, max=20)
    attempts_property: IntProperty(name="Attempts", default=1, min=1, max=5, description="Number of times the script attempts to apply its logic before giving up")
    fix_between_steps_property: bpy.props.BoolProperty(name="Fix Between Steps", default=False)
    simplify_damage_ratio_property: FloatProperty(name="Simplify Damage Ratio",default=0.2,min=0.05,max=1.0,description="Ratio for mesh decimation to simplify damage (1 = no decimation)")


class WeatheringPanel(Panel):
    bl_label = "Lazy Chip"
    bl_idname = "LAZYCHIP_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_order = 10

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        scene_pointer = scene.weathering_props

        # Header
        curr_column = layout.column()
        curr_column.label(text="Lazy Chip", icon='CUBE')

        # Settings
        curr_column = layout.column(align=True)
        curr_column.label(text="Settings:")
        curr_column.prop(scene_pointer, "resolution_property")
        curr_column.prop(scene_pointer, "edge_relax_property")
        curr_column.prop(scene_pointer, "edge_push_property")
        curr_column.prop(scene_pointer, "noise_scale_property")
        curr_column.prop(scene_pointer, "noise_strength_property")
        curr_column.prop(scene_pointer, "noise_contrast_property")
        curr_column.prop(scene_pointer, "seed_property")
        curr_column.prop(scene_pointer, "random_seed_property")
        curr_column.operator("lazychip.op_setdefaultsettings")

        # Proportions
        curr_column.separator()
        curr_column.label(text="Proportions:")
        curr_column.prop(scene_pointer, "fixed_scale_check_property")
        curr_column.prop(scene_pointer, "fixed_scale_property")

        # Attempts and Simplify Damage Ratio
        curr_column.separator()
        curr_column.prop(scene_pointer, "attempts_property")
        curr_column.prop(scene_pointer, "simplify_damage_ratio_property")

        # Fix Between Steps
        curr_column.separator()
        curr_column.prop(scene_pointer, "fix_between_steps_property")

        # Operators with increased scale
        curr_column.separator()
        operator_column = layout.column(align=True)
        operator_column.scale_y = 1.5
        operator_column.operator("lazychip.op_removedamage")
        operator_column.operator("lazychip.op_applydamage")
        operator_column.operator("lazychip.op_clearstash")

        curr_column.separator()
        curr_column.operator("lazychip.op_fixmanifold")

        # Selected Objects
        curr_column.separator()
        curr_column.label(text="Selected objects: " + str(
            [curr_object.name for curr_object in context.selected_objects if curr_object.type == 'MESH']))

        # Material Operators
        curr_column.separator()
        curr_column.label(text="Wood:")
        curr_column.operator("lazychip.op_woodsmoothing")
        curr_column.operator("lazychip.op_woodchipping")
        curr_column.label(text="Stone:")
        curr_column.operator("lazychip.op_stonemarbling")
        curr_column.operator("lazychip.op_stonechipping")
        curr_column.operator("lazychip.op_stoneweathering")
        curr_column.label(text="Concrete:")
        curr_column.operator("lazychip.op_concretechippingsurface")
        curr_column.operator("lazychip.op_concretechippingedges")
        curr_column.operator("lazychip.op_concreteheavyweathering")

# NOT REGISTERED YET — deliberately left out of register() so the panel layout is
# unchanged. The idnames below are now correct, so adding it to register() is safe:
# it used to declare bl_idname "LazyChip", which collided with WeatheringPanel and
# made the panel its own parent.
class LazyChipInfoPanel(Panel):
    bl_label = "Info"
    bl_idname = "LAZYCHIP_PT_info"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_parent_id = 'LAZYCHIP_PT_main'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        curr_column = layout.column()
        curr_column.label(text="About Lazy Chip", icon='INFO')
        curr_column.label(text="Lazy Chip by The Lazy Forger")
        curr_separator = layout.separator()
        curr_column = layout.column(align=True)
        curr_column.label(text="https://github.com/thelazyone/lazy-chip")
        curr_column.label(text="https://www.myminifactory.com/users/TheLazyForger")

# This module owns WeatheringProps and the Scene.weathering_props pointer.
# __init__.py must not create or delete that pointer as well.
def register():
    # Adding the params sliders:
    try:
        bpy.utils.register_class(WeatheringProps)
    except ValueError:
        pass  # Class already registered, ignore
    bpy.types.Scene.weathering_props = bpy.props.PointerProperty(type=WeatheringProps)
    try:
        bpy.utils.register_class(WeatheringPanel)
    except ValueError:
        pass  # Class already registered, ignore

def unregister():
    try:
        bpy.utils.unregister_class(WeatheringPanel)
    except ValueError:
        pass  # Class wasn't registered, ignore
    # Drop the Scene pointer before freeing the PropertyGroup it points at.
    if hasattr(bpy.types.Scene, 'weathering_props'):
        del bpy.types.Scene.weathering_props
    try:
        bpy.utils.unregister_class(WeatheringProps)
    except ValueError:
        pass  # Class wasn't registered, ignore

if __name__ == "__main__":
    register()