import bpy
import bmesh
import mathutils
import random
from bpy.types import Operator

# For profiling only:
import time


def clone_object(context, i_selected_object):
    """
    Clones the selected object.
    """
    object_copy = i_selected_object.copy()
    object_copy.data = i_selected_object.data.copy()
    object_copy.animation_data_clear()
    context.collection.objects.link(object_copy)
    return object_copy


class LAZYCHIP_OP_removedamage(Operator):
    bl_label = "Remove Damage"
    bl_idname = "lazychip.op_removedamage"
    bl_options = {'REGISTER', 'UNDO'}
    
    def remove_damage(self, context, i_selected_object):
        if '_chipped' in i_selected_object.data.name:
            selected_name = i_selected_object.data.name
            selected_name_split = selected_name.split('_chipped', 1)[0]
            if bpy.data.meshes.find(selected_name_split) != -1:
                i_selected_object.data = bpy.data.meshes[selected_name_split]
                bpy.data.meshes.remove(
                    bpy.data.meshes[selected_name], do_unlink=True)
            elif bpy.data.meshes.find(selected_name_split.rsplit('.', 1)[0]) != -1:
                i_selected_object.data = bpy.data.meshes[selected_name_split.rsplit('.', 1)[
                    0]]
                bpy.data.meshes.remove(
                    bpy.data.meshes[selected_name], do_unlink=True)
            else:
                pass
                
    def execute(self, context):
        bpy.ops.object.mode_set(mode='OBJECT')
        scene = context.scene
        all_meshes = [
            curr_object for curr_object in context.selected_objects if curr_object.type == 'MESH']
        for i_selected_object in all_meshes:
            self.remove_damage(context, i_selected_object)
        return {'FINISHED'}
    

class LAZYCHIP_OP_clearstash(Operator):
    bl_label = "Clear Stash"
    bl_idname = "lazychip.op_clearstash"
    bl_options = {'REGISTER', 'UNDO'}
    
    def clear_stash(self, context, i_selected_object):
        if '_chipped' in i_selected_object.data.name:
            selected_name = i_selected_object.data.name
            selected_name_split = selected_name.split('_chipped', 1)[0]
            if bpy.data.meshes.find(selected_name_split) != -1:
                i_selected_object.data.name = selected_name_split + "_applied" 
            elif bpy.data.meshes.find(selected_name_split.rsplit('.', 1)[0]) != -1:
                i_selected_object.data = bpy.data.meshes[selected_name_split.rsplit('.', 1)[
                    0]]
                bpy.data.meshes.remove(
                    bpy.data.meshes[selected_name], do_unlink=True)
            else:
                pass
                
    def execute(self, context):
        bpy.ops.object.mode_set(mode='OBJECT')
        scene = context.scene
        all_meshes = [
            curr_object for curr_object in context.selected_objects if curr_object.type == 'MESH']
        for i_selected_object in all_meshes:
            self.clear_stash(context, i_selected_object)
        return {'FINISHED'}

class LAZYCHIP_OP_applydamage(Operator):
    bl_label = "Apply Damage"
    bl_idname = "lazychip.op_applydamage"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Testing watertight. Using https://blender.stackexchange.com/questions/160055/is-there-a-way-to-use-the-terminal-to-check-if-a-mesh-is-watertight
    def is_watertight_mesh(self, object: bpy.types.Object, check_self_intersection=True) -> bool:
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
    
    

    def make_non_manifold(self, object: bpy.types.Object, ):
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


    def make_non_manifold_iterate(self, object: bpy.types.Object, max_iterations):
        self.report({'INFO'}, "Called make non manifold.")
        for i in range(max_iterations):
            if not self.is_watertight_mesh(object):
                self.report({'INFO'}, "Mesh was non-manifold, fixing it (attempt" + str(i+1) + " of " + str(max_iterations) +").")
                self.make_non_manifold(object)
            else:
                self.report({'INFO'}, "all is good!.")
                break
        

    def remove_damage(self, context, i_selected_object):
        if '_chipped' in i_selected_object.data.name:
            selected_name = i_selected_object.data.name
            # assign the original mesh back
            selected_name_split = selected_name.split('_chipped', 1)[0]
            if bpy.data.meshes.find(selected_name_split) != -1:
                i_selected_object.data = bpy.data.meshes[selected_name_split]
                bpy.data.meshes.remove(
                    bpy.data.meshes[selected_name], do_unlink=True)
            elif bpy.data.meshes.find(selected_name_split.rsplit('.', 1)[0]) != -1:
                i_selected_object.data = bpy.data.meshes[selected_name_split.rsplit('.', 1)[0]]
                bpy.data.meshes.remove(
                    bpy.data.meshes[selected_name], do_unlink=True)
            else:
                pass
                
    def apply_damage(self, context, i_selected_object):    

        # TODO profiling only.
        start_t = time.time()

        context.view_layer.objects.active = i_selected_object
        bpy.ops.object.convert(target='MESH')

        self.report({'INFO'}, "SUB_T1: " + str(time.time() - start_t))
        
        scene = context.scene
        curr_resolution_property = scene.weathering_props.resolution_property
        curr_dimensions = i_selected_object.dimensions
        curr_scale = i_selected_object.scale
        all_dimensions_ratio = min(curr_dimensions.x/curr_scale.x, min(
            curr_dimensions.y/curr_scale.y, curr_dimensions.z/curr_scale.z))
        if scene.weathering_props.fixed_scale_check_property:
            all_dimensions_ratio = scene.weathering_props.fixed_scale_property
        rescaled_ratio = all_dimensions_ratio / curr_resolution_property
        new_remesh_modifier = i_selected_object.modifiers.new("Remesh", 'REMESH')
        new_remesh_modifier.voxel_size = rescaled_ratio
        new_remesh_modifier.use_smooth_shade = False
        edge_relax_value = scene.weathering_props.edge_relax_property

        self.report({'INFO'}, "SUB_T2: " + str(time.time() - start_t))

        new_smooth_property = i_selected_object.modifiers.new("Smooth", 'SMOOTH')
        new_smooth_property.iterations = int(curr_resolution_property*edge_relax_value)
        edge_push_value = scene.weathering_props.edge_push_property
        rescaled_noise_value = scene.weathering_props.noise_strength_property * \
            rescaled_ratio
        new_displace_modifier = i_selected_object.modifiers.new(
            "Displace", 'DISPLACE')
        new_displace_modifier.strength = rescaled_noise_value / 2
        new_displace_modifier.mid_level = 1.0 - edge_push_value
        new_displace_modifier.texture_coords = 'GLOBAL'
        random.seed(
            scene.weathering_props.seed_property)
        
        self.report({'INFO'}, "SUB_T3: " + str(time.time() - start_t))

        random1 = random.uniform(-99.9, 99.9)
        random2 = random.uniform(-99.9, 99.9)
        random3 = random.uniform(-99.9, 99.9)
        i_selected_object.location.x += random1
        i_selected_object.location.y += random2
        i_selected_object.location.z += random3
        static_noise_depth = 4

        self.report({'INFO'}, "SUB_T4: " + str(time.time() - start_t))

        rescaled_noise_scale = scene.weathering_props.noise_scale_property / \
            200 * all_dimensions_ratio
        contrast_property = scene.weathering_props.noise_contrast_property
        noise_modifier = bpy.data.textures.new('Clouds', type='CLOUDS')
        noise_modifier.noise_basis = 'IMPROVED_PERLIN'
        noise_modifier.noise_scale = rescaled_noise_scale
        noise_modifier.noise_depth = static_noise_depth
        noise_modifier.contrast = contrast_property
        new_displace_modifier.texture = noise_modifier

        self.report({'INFO'}, "SUB_T5: " + str(time.time() - start_t))

        bpy.ops.object.convert(target='MESH')

        self.report({'INFO'}, "SUB_T6: " + str(time.time() - start_t))

        bpy.data.textures.remove(noise_modifier)
        i_selected_object.location.x -= random1
        i_selected_object.location.y -= random2
        i_selected_object.location.z -= random3

        self.report({'INFO'}, "SUB_T7: " + str(time.time() - start_t))
        

    def clone_object(self, context, i_selected_object):
        object_copy = i_selected_object.copy()
        object_copy.data = i_selected_object.data.copy()
        object_copy.animation_data_clear()
        context.collection.objects.link(object_copy)
        return object_copy
        
    def apply_boolean(self, context, i_selected_object, target_object):
        new_boolean = i_selected_object.modifiers.new(
            "Boolean", 'BOOLEAN')
        new_boolean.operation = 'INTERSECT'
        new_boolean.solver = 'FAST'
        cloned_object = self.clone_object(
            context, target_object)
        new_boolean.object = cloned_object
        bpy.ops.object.convert(target='MESH')
        bpy.data.meshes.remove(cloned_object.data, do_unlink=True)
        
    # Partial operation within execute:
    def apply_damage_one_pass(self, context, scene, all_meshes):
        active_object = context.view_layer.objects.active
        for current_mesh in all_meshes:

            # Updating the seed and removing the current damage
            if scene.weathering_props.random_seed_property:
                scene.weathering_props.seed_property = random.randint(0, 999999)
            self.remove_damage(context, current_mesh)
            context.view_layer.objects.active = current_mesh
            
            # Setting the shading to flat!
            context.object.data.use_auto_smooth = False
            bpy.ops.object.shade_flat()
            
            # Creating a temporary clone and renaming it 
            object_copy = self.clone_object(context, current_mesh)
            object_copy.data.name = current_mesh.data.name + '_chipped'
            copied_matrix_basis = object_copy.matrix_basis.copy()
            if hasattr(object_copy.data, "transform"):
                object_copy.data.transform(copied_matrix_basis)
                
            # TODO profiling only.
            self.report({'INFO'}, "Starting the apply damage performance.")
            start_t = time.time()

            # Applying the damage (and the bool modifier)
            self.apply_damage(context, object_copy)

            self.report({'INFO'}, "T1: " + str(time.time() - start_t))

            copied_matrix_basis.invert()

            self.report({'INFO'}, "T2: " + str(time.time() - start_t))

            if hasattr(object_copy.data, "transform"):
                object_copy.data.transform(copied_matrix_basis)
            
            self.report({'INFO'}, "T3: " + str(time.time() - start_t))

            # TODO maybe to be removed: applying non-manifold before pass
            if scene.weathering_props.fix_between_steps_property:
                self.make_non_manifold(current_mesh)

            self.apply_boolean(context, object_copy, current_mesh)
                
            self.report({'INFO'}, "T3: " + str(time.time() - start_t))
                
            # Tricking into creating this fake object
            current_mesh.data.use_fake_user = True
            object_copy.name = "LazyChip_tempObject"
            current_mesh.data = object_copy.data
            bpy.data.objects.remove(object_copy, do_unlink=True)
            current_mesh.data.use_fake_user = True
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
            current_mesh.select_set(False)

        for current_mesh in all_meshes:
            current_mesh.select_set(True)
        context.view_layer.objects.active = active_object
        
    def execute(self, context):
        self.report({'INFO'}, "Calling Execute. Applying damages...")
        self.report({'INFO'}, "AAA")
        scene = context.scene
        bpy.ops.object.mode_set(mode='OBJECT')
        all_meshes = [
            curr_object for curr_object in context.selected_objects if curr_object.type == 'MESH']
        self.apply_damage_one_pass(context, scene, all_meshes)

        # TODO maybe to be removed: applying non-manifold before pass
        if scene.weathering_props.fix_between_steps_property:
            for curr_object in all_meshes:
                self.make_non_manifold_iterate(curr_object, 5)


    
        # Checking for non watertight
        all_meshes = [
            curr_object for curr_object in all_meshes if not self.is_watertight_mesh(curr_object)]
        watertight_iteration = 0;
        while len(all_meshes) > 0:
            if watertight_iteration >= scene.weathering_props.attempts_property - 1:
                self.report({'ERROR'}, "Reached max level of iterations (" + str(scene.weathering_props.attempts_property) + ")")
                break
                
            watertight_iteration = watertight_iteration + 1
            self.apply_damage_one_pass(context, scene, all_meshes)
        
            all_meshes = [
                curr_object for curr_object in all_meshes if not self.is_watertight_mesh(curr_object)]
            
        return {'FINISHED'}

def register():
    bpy.utils.register_class(LAZYCHIP_OP_applydamage)
    bpy.utils.register_class(LAZYCHIP_OP_clearstash)
    bpy.utils.register_class(LAZYCHIP_OP_removedamage)

def unregister():
    bpy.utils.unregister_class(LAZYCHIP_OP_applydamage)
    bpy.utils.unregister_class(LAZYCHIP_OP_clearstash)
    bpy.utils.unregister_class(LAZYCHIP_OP_removedamage)