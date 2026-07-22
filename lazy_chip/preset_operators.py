import bpy
from bpy.types import Operator



class LAZYCHIP_OP_woodchipping(Operator):
    bl_label = "Set Wood Chipping"
    bl_idname = "lazychip.op_woodchipping"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 64
        weathering_props.edge_relax_property = 0.5
        weathering_props.edge_push_property = 0.8
        weathering_props.noise_scale_property = 40
        weathering_props.noise_strength_property = 20
        weathering_props.noise_contrast_property = 2
        weathering_props.seed_property = 0  
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = True
        weathering_props.fixed_scale_property = 4.0
        return {'FINISHED'}
    
class LAZYCHIP_OP_woodsmoothing(Operator):
    bl_label = "Set Wood Smoothing"
    bl_idname = "lazychip.op_woodsmoothing"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 64
        weathering_props.edge_relax_property = 1.5
        weathering_props.edge_push_property = 0.9
        weathering_props.noise_scale_property = 60
        weathering_props.noise_strength_property = 8.0
        weathering_props.noise_contrast_property = 0.5
        weathering_props.seed_property = 0
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = True
        weathering_props.fixed_scale_property = 2.0
        return {'FINISHED'}
    
class LAZYCHIP_OP_stonemarbling(Operator):
    bl_label = "Set Stone Marbling"
    bl_idname = "lazychip.op_stonemarbling"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 64
        weathering_props.edge_relax_property = 0.0
        weathering_props.edge_push_property = 0.5
        weathering_props.noise_scale_property = 100
        weathering_props.noise_strength_property = 24
        weathering_props.noise_contrast_property = 0.5
        weathering_props.seed_property = 0  
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = True
        weathering_props.fixed_scale_property = 3.0
        return {'FINISHED'}
    
class LAZYCHIP_OP_stonechipping(Operator):
    bl_label = "Set Stone Chipping"
    bl_idname = "lazychip.op_stonechipping"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 64
        weathering_props.edge_relax_property = 4.0
        weathering_props.edge_push_property = 0.8
        weathering_props.noise_scale_property = 40
        weathering_props.noise_strength_property = 12.0
        weathering_props.noise_contrast_property = 1.0
        weathering_props.seed_property = 0
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = True
        weathering_props.fixed_scale_property = 4.0
        return {'FINISHED'}
    
class LAZYCHIP_OP_stoneweathering(Operator):
    bl_label = "Set Stone Weathering"
    bl_idname = "lazychip.op_stoneweathering"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 64
        weathering_props.edge_relax_property = 0.0
        weathering_props.edge_push_property = 0.9
        weathering_props.noise_scale_property = 160
        weathering_props.noise_strength_property = 24
        weathering_props.noise_contrast_property = 3.5
        weathering_props.seed_property = 0  
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = True
        weathering_props.fixed_scale_property = 2.0
        return {'FINISHED'}
    
class LAZYCHIP_OP_concretechippingsurface(Operator):
    bl_label = "Set Concrete Chipping Surface"
    bl_idname = "lazychip.op_concretechippingsurface"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 64
        weathering_props.edge_relax_property = .5
        weathering_props.edge_push_property = 0.9
        weathering_props.noise_scale_property = 40
        weathering_props.noise_strength_property = 30.0
        weathering_props.noise_contrast_property = 2.2
        weathering_props.seed_property = 0
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = True
        weathering_props.fixed_scale_property = 4.0
        return {'FINISHED'}
    
class LAZYCHIP_OP_concreteheavyweathering(Operator):
    bl_label = "Set Concrete Heavy Weathering"
    bl_idname = "lazychip.op_concreteheavyweathering"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 128
        weathering_props.edge_relax_property = 1.0
        weathering_props.edge_push_property = 0.65
        weathering_props.noise_scale_property = 30
        weathering_props.noise_strength_property = 12.0
        weathering_props.noise_contrast_property = 1.3
        weathering_props.seed_property = 0
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = True
        weathering_props.fixed_scale_property = 4.0
        return {'FINISHED'}
    
class LAZYCHIP_OP_concretechippingedges(Operator):
    bl_label = "Set Concrete Chipping Edges"
    bl_idname = "lazychip.op_concretechippingedges"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 64
        weathering_props.edge_relax_property = 4.0
        weathering_props.edge_push_property = 0.98
        weathering_props.noise_scale_property = 40
        weathering_props.noise_strength_property = 12.0
        weathering_props.noise_contrast_property = 1.0
        weathering_props.seed_property = 0
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = True
        weathering_props.fixed_scale_property = 4.0
        return {'FINISHED'}

class LAZYCHIP_OP_setdefaultsettings(Operator):
    bl_label = "Reset Settings"
    bl_idname = "lazychip.op_setdefaultsettings"
    def execute(self, context):
        weathering_props = context.scene.weathering_props
        weathering_props.resolution_property = 64
        weathering_props.edge_relax_property = 3.0
        weathering_props.edge_push_property = 0.7
        weathering_props.noise_scale_property = 40
        weathering_props.noise_strength_property = 8.0
        weathering_props.noise_contrast_property = 1.0
        weathering_props.seed_property = 0
        weathering_props.random_seed_property = True
        weathering_props.fixed_scale_check_property = False
        weathering_props.fixed_scale_property = 1.0
        weathering_props.attempts_property = 1
        weathering_props.fix_between_steps_property = False
        weathering_props.simplify_damage_ratio_property = 0.2
        return {'FINISHED'}
    
    
def register():
    bpy.utils.register_class(LAZYCHIP_OP_woodsmoothing)
    bpy.utils.register_class(LAZYCHIP_OP_woodchipping)
    bpy.utils.register_class(LAZYCHIP_OP_stonemarbling)
    bpy.utils.register_class(LAZYCHIP_OP_stonechipping)
    bpy.utils.register_class(LAZYCHIP_OP_stoneweathering)
    bpy.utils.register_class(LAZYCHIP_OP_concretechippingsurface)
    bpy.utils.register_class(LAZYCHIP_OP_concreteheavyweathering)
    bpy.utils.register_class(LAZYCHIP_OP_concretechippingedges)

    bpy.utils.register_class(LAZYCHIP_OP_setdefaultsettings)

def unregister():
    bpy.utils.unregister_class(LAZYCHIP_OP_woodsmoothing)
    bpy.utils.unregister_class(LAZYCHIP_OP_woodchipping)
    bpy.utils.unregister_class(LAZYCHIP_OP_stonemarbling)
    bpy.utils.unregister_class(LAZYCHIP_OP_stonechipping)
    bpy.utils.unregister_class(LAZYCHIP_OP_stoneweathering)
    bpy.utils.unregister_class(LAZYCHIP_OP_concretechippingsurface)
    bpy.utils.unregister_class(LAZYCHIP_OP_concreteheavyweathering)
    bpy.utils.unregister_class(LAZYCHIP_OP_concretechippingedges)

    bpy.utils.unregister_class(LAZYCHIP_OP_setdefaultsettings)
