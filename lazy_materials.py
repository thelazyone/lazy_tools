bl_info = {
    "name": "Lazy Materials",
    "author": "thelazyone",
    "version": (1, 0, 1),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Provides multiple tools for handling materials and UV maps",
    "warning": "",
    "doc_url": "https://github.com/thelazyone/lazy_tools",
    "category": "Mesh",
}

import bpy
import bmesh
from bpy.types import Operator, Panel
from bpy.props import IntProperty
import math as m
import random   # for UV settings
import colorsys  # for Palette


# Setting for the plugin.
class ExtendedMaterialSettings(bpy.types.PropertyGroup):
    palette_rows: bpy.props.IntProperty(
        name="Rows",
        description="Number of rows in the color palette",
        default=6, min=1, max=6
    )
    palette_columns: bpy.props.IntProperty(
        name="Columns",
        description="Number of columns in the color palette",
        default=6, min=1, max=6
    )

def _value_fingerprint(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return ("bool", val)
    if isinstance(val, (int, float)):
        return ("num", float(val))
    if isinstance(val, str):
        return ("str", val)
    if isinstance(val, tuple):
        return ("tuple",) + tuple(_value_fingerprint(v) for v in val)
    return ("repr", repr(val))


def _socket_default_value(socket):
    if socket.is_linked:
        return None
    try:
        val = socket.default_value
    except (AttributeError, TypeError):
        return None
    if hasattr(val, "__len__") and not isinstance(val, str):
        try:
            val = tuple(val)
        except TypeError:
            pass
    return _value_fingerprint(val)


def _image_fingerprint(image):
    if image is None:
        return None
    fp = ("image", image.size[0], image.size[1], image.filepath or "")
    if image.pixels:
        sample_len = min(len(image.pixels), 64)
        fp = fp + (tuple(image.pixels[:sample_len]),)
    return fp


def _node_sort_key(node):
    return repr(_node_fingerprint(node))


def _node_fingerprint(node):
    data = [node.type, node.bl_idname]
    for inp in node.inputs:
        val = _socket_default_value(inp)
        if val is not None:
            data.append((inp.identifier, val))
    if node.type == "TEX_IMAGE":
        data.append(("image", _image_fingerprint(node.image)))
    return tuple(data)


_MATERIAL_SKIP_PROPS = {
    "name", "name_full", "users", "use_fake_user", "library", "tag",
    "is_evaluated", "original", "override_library", "asset_data",
    "preview", "node_tree",
}


def _material_props_fingerprint(mat):
    values = []
    for prop in mat.bl_rna.properties:
        if prop.is_readonly or prop.identifier in _MATERIAL_SKIP_PROPS:
            continue
        if prop.type == "POINTER":
            continue
        try:
            val = getattr(mat, prop.identifier)
            if hasattr(val, "__len__") and not isinstance(val, str):
                val = tuple(val)
            val = _value_fingerprint(val)
            values.append((prop.identifier, val))
        except (AttributeError, TypeError):
            pass
    return tuple(sorted(values))


def material_fingerprint(mat):
    if mat is None:
        return None

    if not mat.use_nodes:
        return ("legacy", _material_props_fingerprint(mat))

    tree = mat.node_tree
    if tree is None:
        return ("empty_nodes", _material_props_fingerprint(mat))

    nodes_sorted = sorted(tree.nodes, key=_node_sort_key)
    node_index = {node: idx for idx, node in enumerate(nodes_sorted)}
    node_fps = [_node_fingerprint(node) for node in nodes_sorted]
    link_fps = sorted(
        (
            node_index[link.from_node],
            link.from_socket.identifier,
            node_index[link.to_node],
            link.to_socket.identifier,
        )
        for link in tree.links
    )
    return ("nodes", _material_props_fingerprint(mat), tuple(node_fps), tuple(link_fps))


def _replace_material(old_mat, new_mat):
    if old_mat == new_mat:
        return
    old_mat.user_remap(new_mat)
    bpy.data.materials.remove(old_mat, do_unlink=True)


def _selected_mesh_objects(context):
    return [obj for obj in context.selected_objects if obj.type == 'MESH']


def _object_has_no_material(obj):
    materials = obj.data.materials
    return len(materials) == 0 or not any(mat is not None for mat in materials)


def _material_matching_name(name):
    exact = bpy.data.materials.get(name)
    if exact:
        return exact

    name_lower = name.lower()
    matches = [mat for mat in bpy.data.materials if mat.name.lower() == name_lower]
    if not matches:
        return None
    for mat in matches:
        if mat.name == name:
            return mat
    return matches[0]


def _collection_matching_name(name):
    exact = bpy.data.collections.get(name)
    if exact:
        return exact

    name_lower = name.lower()
    matches = [coll for coll in bpy.data.collections if coll.name.lower() == name_lower]
    if not matches:
        return None
    for coll in matches:
        if coll.name == name:
            return coll
    return matches[0]


def _get_or_create_collection(context, name):
    coll = _collection_matching_name(name)
    if coll:
        return coll
    coll = bpy.data.collections.new(name)
    context.scene.collection.children.link(coll)
    return coll


def _object_primary_material(obj):
    materials = obj.data.materials
    if not materials:
        return None
    idx = obj.active_material_index
    if 0 <= idx < len(materials) and materials[idx] is not None:
        return materials[idx]
    for mat in materials:
        if mat is not None:
            return mat
    return None


def remove_unused_material_slots(obj):
    mesh = obj.data
    if not hasattr(mesh, "materials") or not hasattr(mesh, "polygons"):
        return 0

    old_count = len(mesh.materials)
    if old_count == 0:
        return 0

    if mesh.polygons:
        used_indices = set()
        for poly in mesh.polygons:
            idx = poly.material_index
            if idx >= old_count:
                idx = 0
            used_indices.add(idx)
    else:
        used_indices = set()

    if len(used_indices) == old_count:
        return 0

    used_sorted = sorted(used_indices)
    index_map = {old_idx: new_idx for new_idx, old_idx in enumerate(used_sorted)}

    for poly in mesh.polygons:
        old_idx = poly.material_index
        if old_idx >= old_count:
            old_idx = 0
        poly.material_index = index_map.get(old_idx, 0)

    new_materials = [mesh.materials[i] for i in used_sorted]
    mesh.materials.clear()
    for mat in new_materials:
        mesh.materials.append(mat)

    if mesh.materials:
        if obj.active_material_index in index_map:
            obj.active_material_index = index_map[obj.active_material_index]
        else:
            obj.active_material_index = min(obj.active_material_index, len(mesh.materials) - 1)
    else:
        obj.active_material_index = 0

    return old_count - len(new_materials)


class MATERIAL_OT_remove_unused_materials(Operator):
    bl_idname = "material.remove_unused"
    bl_label = "Remove Unused Materials"
    bl_description = "Remove materials in the file that are not used by any object"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        removed = 0
        for mat in list(bpy.data.materials):
            if mat.users == 0 and not mat.use_fake_user:
                bpy.data.materials.remove(mat, do_unlink=True)
                removed += 1

        if removed:
            self.report({"INFO"}, f"Removed {removed} unused material(s)")
        else:
            self.report({"INFO"}, "No unused materials found")
        return {"FINISHED"}


class MATERIAL_OT_merge_duplicate_materials(Operator):
    bl_idname = "material.merge_duplicates"
    bl_label = "Merge Duplicate Materials"
    bl_description = (
        "Find materials with identical settings and node trees, "
        "keep the alphabetically first name, and reassign all users"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        groups = {}
        for mat in bpy.data.materials:
            groups.setdefault(material_fingerprint(mat), []).append(mat)

        merged = 0
        for fingerprint, materials in groups.items():
            if len(materials) < 2:
                continue

            keeper = min(materials, key=lambda m: m.name.lower())
            for mat in materials:
                if mat == keeper:
                    continue
                _replace_material(mat, keeper)
                merged += 1

        if merged:
            self.report({"INFO"}, f"Merged {merged} duplicate material(s)")
        else:
            self.report({"INFO"}, "No duplicate materials found")
        return {"FINISHED"}


class MATERIAL_OT_delete_materials(Operator):
    bl_idname = "material.delete_materials"
    bl_label = "Remove Materials"
    bl_description = "Remove all material slots from selected mesh objects"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(_selected_mesh_objects(context))

    def execute(self, context):
        count = 0
        for obj in _selected_mesh_objects(context):
            obj.data.materials.clear()
            obj.active_material_index = 0
            count += 1

        if count == 1:
            self.report({'INFO'}, "Removed materials from selected object")
        else:
            self.report({'INFO'}, f"Removed materials from {count} selected objects")
        return {'FINISHED'}


class MATERIAL_OT_assign_from_collection(Operator):
    bl_idname = "material.assign_from_collection"
    bl_label = "Material as Collection"
    bl_description = (
        "For selected objects with no material, assign a material whose name "
        "matches one of the object's collection names (case insensitive)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(_selected_mesh_objects(context))

    def execute(self, context):
        assigned = 0
        skipped_has_material = 0
        skipped_no_match = 0

        for obj in _selected_mesh_objects(context):
            if not _object_has_no_material(obj):
                skipped_has_material += 1
                continue

            material = None
            for collection in obj.users_collection:
                material = _material_matching_name(collection.name)
                if material:
                    break

            if material is None:
                skipped_no_match += 1
                continue

            obj.data.materials.append(material)
            obj.active_material_index = 0
            assigned += 1

        if assigned:
            self.report({'INFO'}, f"Assigned material from collection name on {assigned} object(s)")
        elif skipped_has_material and not skipped_no_match:
            self.report({'INFO'}, "Selected objects already have materials")
        else:
            self.report({'INFO'}, "No matching collection/material names found")
        return {'FINISHED'}


class MATERIAL_OT_move_to_collection_from_material(Operator):
    bl_idname = "material.move_to_collection_from_material"
    bl_label = "Collection from Material"
    bl_description = (
        "For selected objects, move each object into a collection whose name "
        "matches the object's material name"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(_selected_mesh_objects(context))

    def execute(self, context):
        moved = 0
        skipped_no_material = 0

        for obj in _selected_mesh_objects(context):
            material = _object_primary_material(obj)
            if material is None:
                skipped_no_material += 1
                continue

            target = _get_or_create_collection(context, material.name)
            current_collections = list(obj.users_collection)

            if current_collections == [target]:
                continue

            for coll in current_collections:
                coll.objects.unlink(obj)
            target.objects.link(obj)
            moved += 1

        if moved:
            self.report({'INFO'}, f"Moved {moved} object(s) to material-named collection(s)")
        elif skipped_no_material:
            self.report({'INFO'}, "Selected objects have no material to match")
        else:
            self.report({'INFO'}, "Objects already in matching collection(s)")
        return {'FINISHED'}


class MATERIAL_OT_remove_unused_from_selected(Operator):
    bl_idname = "material.remove_unused_from_selected"
    bl_label = "Remove Unused From Selected"
    bl_description = (
        "Remove material slots from selected objects that are not used by any face"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(_selected_mesh_objects(context))

    def execute(self, context):
        objects = _selected_mesh_objects(context)
        removed_slots = 0
        changed_objects = 0

        for obj in objects:
            removed = remove_unused_material_slots(obj)
            if removed:
                removed_slots += removed
                changed_objects += 1

        if removed_slots:
            if changed_objects == 1:
                self.report(
                    {'INFO'},
                    f"Removed {removed_slots} unused material slot(s) from selected object",
                )
            else:
                self.report(
                    {'INFO'},
                    f"Removed {removed_slots} unused material slot(s) from {changed_objects} objects",
                )
        else:
            self.report({'INFO'}, "No unused material slots found on selected objects")
        return {'FINISHED'}
    
class MATERIAL_OT_generate_materials(Operator):
    bl_idname = "uv.generate_materials"
    bl_label = "Generate Materials and UVs"
    bl_description = "Generates Materials, UVs and Textures for color and AO"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH' and \
            len(context.selected_objects) == 1 and context.mode == 'OBJECT'
    
    def execute(self, context):
        selected_objects = context.selected_objects
        for obj in selected_objects:
            if obj.type == 'MESH':
                
                # Apply transformations
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

                # Define unique names based on the object name
                texture_color_name = f"{obj.name}_texture_color"
                texture_ao_name = f"{obj.name}_texture_ao"
                material_color_name = f"{obj.name}_Material_Color"
                material_ao_name = f"{obj.name}_Material_AO"

                # Ensure UV maps are added if they do not exist
                uv_layers = obj.data.uv_layers
                if "uv_color" not in uv_layers:
                    uv_layers.new(name="uv_color")
                if "uv_ao" not in uv_layers:
                    uv_layers.new(name="uv_ao")

                # Switch to edit mode to perform UV operations
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='EDIT')

                # Unwrap each UV map separately with adjusted settings
                bpy.ops.object.mode_set(mode='EDIT')
                for uv_name in ["uv_color", "uv_ao"]:
                    uv_layers.active = uv_layers[uv_name]
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.uv.smart_project(
                        angle_limit=m.radians(66),
                        island_margin=0.00,
                        area_weight=0.00,
                        margin_method='SCALED',
                        correct_aspect=True,
                        scale_to_bounds=False
                )
                    
                # Switch back to object mode after unwrapping
                bpy.ops.object.mode_set(mode='OBJECT')

                # Manage existing textures
                old_texture_color = bpy.data.images.get(texture_color_name)
                if old_texture_color:
                    if old_texture_color.users == 1:
                        bpy.data.images.remove(old_texture_color)
                    else:
                        old_texture_color.user_clear()  # Clear users if more than one

                old_texture_ao = bpy.data.images.get(texture_ao_name)
                if old_texture_ao:
                    if old_texture_ao.users == 1:
                        bpy.data.images.remove(old_texture_ao)
                    else:
                        old_texture_ao.user_clear()

                # Create or find textures
                if texture_color_name not in bpy.data.images:
                    texture_color = bpy.data.images.new(name=texture_color_name, width=1024, height=1024, alpha=True)
                    # Fill the image with green color
                    fill_color = (0.0, 1.0, 0.0, 1.0)  # RGBA for green
                    texture_color.pixels = [val for _ in range(texture_color.size[0] * texture_color.size[1]) for val in fill_color]
                else:
                    texture_color = bpy.data.images[texture_color_name]

                if texture_ao_name not in bpy.data.images:
                    texture_ao = bpy.data.images.new(name=texture_ao_name, width=1024, height=1024, alpha=True)
                    # Fill the image with white color
                    fill_color = (1.0, 1.0, 1.0, 1.0)  # RGBA for white
                    texture_ao.pixels = [val for _ in range(texture_ao.size[0] * texture_ao.size[1]) for val in fill_color]
                else:
                    texture_ao = bpy.data.images[texture_ao_name]

                # Create or find materials
                mat_color = bpy.data.materials.get(material_color_name) or bpy.data.materials.new(name=material_color_name)
                mat_ao = bpy.data.materials.get(material_ao_name) or bpy.data.materials.new(name=material_ao_name)

                # Setup material node tree for color
                if mat_color.use_nodes:
                    bsdf_color = mat_color.node_tree.nodes.get('Principled BSDF')
                else:
                    mat_color.use_nodes = True
                    bsdf_color = mat_color.node_tree.nodes.get('Principled BSDF')
                tex_image_color = mat_color.node_tree.nodes.new('ShaderNodeTexImage')
                tex_image_color.image = texture_color
                uv_map_color = mat_color.node_tree.nodes.new('ShaderNodeUVMap')
                uv_map_color.uv_map = "uv_color"
                mat_color.node_tree.links.new(uv_map_color.outputs['UV'], tex_image_color.inputs['Vector'])
                mat_color.node_tree.links.new(bsdf_color.inputs['Base Color'], tex_image_color.outputs['Color'])

                # Setup material node tree for AO
                if mat_ao.use_nodes:
                    bsdf_ao = mat_ao.node_tree.nodes.get('Principled BSDF')
                else:
                    mat_ao.use_nodes = True
                    bsdf_ao = mat_ao.node_tree.nodes.get('Principled BSDF')
                tex_image_ao = mat_ao.node_tree.nodes.new('ShaderNodeTexImage')
                tex_image_ao.image = texture_ao
                uv_map_ao = mat_ao.node_tree.nodes.new('ShaderNodeUVMap')
                uv_map_ao.uv_map = "uv_ao"
                mat_ao.node_tree.links.new(uv_map_ao.outputs['UV'], tex_image_ao.inputs['Vector'])
                mat_ao.node_tree.links.new(bsdf_ao.inputs['Base Color'], tex_image_ao.outputs['Color'])

                # Assign materials to the object
                obj.data.materials.clear()
                obj.data.materials.append(mat_color)
                obj.data.materials.append(mat_ao)

        self.report({'INFO'}, "UV maps, textures, and materials uniquely created or updated for each object")
        return {'FINISHED'}
    
class MATERIAL_OT_view_color(bpy.types.Operator):
    """Switches to the color material for viewing."""
    bl_idname = "material.view_color"
    bl_label = "View Color Material"
    
    def execute(self, context):
        obj = context.object
        mat_color_name = f"{obj.name}_Material_Color"
        mat_color = bpy.data.materials.get(mat_color_name)

        if mat_color and mat_color.name not in [mat.name for mat in obj.data.materials]:
            obj.data.materials.append(mat_color)

        # Set as active material for viewing
        obj.active_material = mat_color
        self.report({'INFO'}, "Viewing Color Material")
        return {'FINISHED'}

class MATERIAL_OT_view_ao(bpy.types.Operator):
    """Switches to the AO material for viewing."""
    bl_idname = "material.view_ao"
    bl_label = "View AO Material"
    
    def execute(self, context):
        obj = context.object
        mat_ao_name = f"{obj.name}_Material_AO"
        mat_ao = bpy.data.materials.get(mat_ao_name)

        if mat_ao and mat_ao.name not in [mat.name for mat in obj.data.materials]:
            obj.data.materials.append(mat_ao)

        # Set as active material for viewing
        obj.active_material = mat_ao
        self.report({'INFO'}, "Viewing AO Material")
        return {'FINISHED'}

    

class MATERIAL_OT_view_both(bpy.types.Operator):
    bl_idname = "material.view_both"
    bl_label = "View Combined Material"
    bl_description = "Creates and switches to a material that combines color and AO"

    def execute(self, context):
        obj = context.object
        combined_mat_name = f"{obj.name}_Combined_Material"

        # Check if the combined material already exists, otherwise create it
        combined_mat = bpy.data.materials.get(combined_mat_name)
        if not combined_mat:
            combined_mat = bpy.data.materials.new(name=combined_mat_name)

        # Use nodes in the combined material and clear existing nodes
        combined_mat.use_nodes = True
        nodes = combined_mat.node_tree.nodes
        nodes.clear()  # Clear any existing nodes to avoid duplicates

        # Create nodes for the color and AO textures
        color_tex_node = nodes.new('ShaderNodeTexImage')
        color_tex_node.image = bpy.data.images.get(f"{obj.name}_texture_color")
        ao_tex_node = nodes.new('ShaderNodeTexImage')
        ao_tex_node.image = bpy.data.images.get(f"{obj.name}_texture_ao")

        # Create UV Map nodes for each texture
        uv_color_node = nodes.new('ShaderNodeUVMap')
        uv_color_node.uv_map = 'uv_color'
        uv_ao_node = nodes.new('ShaderNodeUVMap')
        uv_ao_node.uv_map = 'uv_ao'

        # Create a mix node to blend the two textures
        mix_node = nodes.new('ShaderNodeMixRGB')
        mix_node.blend_type = 'MULTIPLY'

        # Output node
        output_node = nodes.new('ShaderNodeOutputMaterial')

        # Connect the nodes
        links = combined_mat.node_tree.links
        links.new(uv_color_node.outputs['UV'], color_tex_node.inputs['Vector'])
        links.new(uv_ao_node.outputs['UV'], ao_tex_node.inputs['Vector'])
        links.new(color_tex_node.outputs['Color'], mix_node.inputs[1])
        links.new(ao_tex_node.outputs['Color'], mix_node.inputs[2])
        links.new(mix_node.outputs['Color'], output_node.inputs['Surface'])

        # Apply the combined material to the object if not already applied
        if combined_mat_name not in [mat.name for mat in obj.data.materials]:
            obj.data.materials.append(combined_mat)

        obj.active_material = combined_mat

        self.report({'INFO'}, "Combined material applied")
        return {'FINISHED'}

# FACES MANIPULATION!

def color_index_to_xy(context, index):  
    settings = context.scene.extended_material_settings
    palette_columns = settings.palette_columns
    tile_x = (index % palette_columns)
    tile_y = (index // palette_columns)
    return (tile_x, tile_y)

def random_color():
    """Generate a random color."""
    hue = random.random()  # Random hue
    saturation = 0.75  # Fixed saturation
    value = 1.0  # Fixed value
    return colorsys.hsv_to_rgb(hue, saturation, value)  # Returns an RGB tuple


def initialize_all_faces_random(context):
    obj = context.object
    mesh = obj.data
    settings = context.scene.extended_material_settings
    num_colors = settings.palette_columns * settings.palette_rows

    if num_colors == 0:
        return  # Avoid division by zero if no colors are defined

    # Store the current mode, and switch to Edit Mode to ensure full access to the mesh
    current_mode = bpy.context.object.mode
    bpy.ops.object.mode_set(mode='EDIT')

    # Collect indices of all faces
    all_faces = [p.index for p in mesh.polygons]
    random.shuffle(all_faces)  # Shuffle to randomize distribution

    # Split into groups for each color
    face_groups = {i: [] for i in range(num_colors)}
    for i, face_index in enumerate(all_faces):
        color_group_index = i % num_colors
        face_groups[color_group_index].append(face_index)

    # Assign each group of faces to a corresponding color
    for color_index, faces in face_groups.items():
        assign_faces_to_color(context, faces, color_index)

    # Restore the original mode
    bpy.ops.object.mode_set(mode=current_mode)


def assign_faces_to_color(context, faces, color_index):
    obj = context.object
    mesh = obj.data

    # Store the current mode and switch to object mode to manipulate UVs safely
    current_mode = bpy.context.object.mode
    bpy.ops.object.mode_set(mode='OBJECT')

    if "uv_color" not in mesh.uv_layers:
        mesh.uv_layers.new(name="uv_color")
    
    uv_layer = mesh.uv_layers["uv_color"].data

    settings = context.scene.extended_material_settings
    palette_rows = settings.palette_rows
    palette_columns = settings.palette_columns
    
    tile_width = 1 / palette_columns
    tile_height = 1 / palette_rows
    
    row = color_index // palette_columns
    col = color_index % palette_columns
    tile_x = (col + 0.5) * tile_width
    tile_y = (row + 0.5) * tile_height

    # Apply UV modifications
    for face_idx in faces:
        poly = mesh.polygons[face_idx]
        for loop_index in poly.loop_indices:
            loop_uv = uv_layer[loop_index]
            loop_uv.uv = (tile_x, tile_y)

    # Restore the original mode
    bpy.ops.object.mode_set(mode=current_mode)


class MATERIAL_OT_create_palette(Operator):
    bl_idname = "material.create_palette"
    bl_label = "Create Color Palette"
    bl_description = "Creates a color palette based on specified grid size"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and \
               "uv_color" in context.object.data.uv_layers.keys() and \
               context.mode == 'OBJECT'
    
    def execute(self, context):
        obj = context.active_object
        texture_name = f"{obj.name}_texture_color"
        texture = bpy.data.images.get(texture_name)
        if not texture:
            self.report({'ERROR'}, "Texture not found")
            return {'CANCELLED'}

        settings = context.scene.extended_material_settings
        palette_rows = settings.palette_rows
        palette_columns = settings.palette_columns

        width, height = texture.size
        pixels = [0] * (width * height * 4)

        # Calculate the size of each grid cell
        cell_width = int(width / palette_columns)
        cell_height = int(height / palette_rows)

        # Iterate over each cell in the grid
        for row in range(palette_rows):
            for col in range(palette_columns):
                color = random_color()
                for y in range(row * cell_height, (row + 1) * cell_height):
                    for x in range(col * cell_width, (col + 1) * cell_width):
                        index = (y * width + x) * 4
                        pixels[index:index+3] = color
                        pixels[index+3] = 1.0

        # Update the texture
        texture.pixels = pixels
        texture.update()

        self.report({'INFO'}, "Color palette created and faces initialized")
        return {'FINISHED'}
    
class MATERIAL_OT_assign_faces_random(Operator):
    bl_idname = "material.assign_faces_random"
    bl_label = "Assign Faces At Random"
    bl_description = "Assigns faces to random color tiles"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):

        # Initialize all faces randomly and position UVs
        initialize_all_faces_random(context)

        return {'FINISHED'}

class MATERIAL_OT_default_palette(Operator):
    bl_idname = "material.default_palette"
    bl_label = "Default Palette"
    bl_description = "Loads a color palette from default"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Color palette defined as (R, G, B) tuples
        DEFAULT_PALETTE =  [
        # 6 tones of cool grey
        (0.5, 0.5, 0.6), (0.48, 0.5, 0.55), (0.46, 0.52, 0.55), (0.5, 0.5, 0.55), (0.4, 0.4, 0.45), (0.3, 0.3, 0.35), 
        # 6 tones of warm grey
        (0.5, 0.5, 0.4), (0.45, 0.45, 0.35), (0.4, 0.4, 0.3), (0.35, 0.35, 0.25), (0.3, 0.3, 0.2), (0.25, 0.25, 0.15),
        # 3 of warm brown, 3 of yellow brown
        (0.45, 0.3, 0.1), (0.4, 0.25, 0.05), (0.35, 0.2, 0), (0.55, 0.45, 0.1), (0.5, 0.4, 0.05), (0.45, 0.35, 0),
        # 3 of red, 3 of blue
        (0.7, 0.2, 0.2), (0.6, 0.1, 0.1), (0.5, 0, 0), (0.2, 0.2, 0.7), (0.1, 0.1, 0.6), (0, 0, 0.5),
        # 6 tones of beige
        (0.90, 0.75, 0.65), (0.87, 0.70, 0.55), (0.83, 0.65, 0.50), (0.95, 0.87, 0.76), (0.80, 0.72, 0.60), (0.86, 0.80, 0.68),
        # 3 of warm green, 3 of hard green 
        (0.3, 0.5, 0.3), (0.2, 0.45, 0.25), (0.1, 0.4, 0.2), (0.05, 0.55, 0.25), (0.02, 0.5, 0.2), (0, 0.45, 0.15)
        ]
        
        obj = context.active_object
        texture_name = f"{obj.name}_texture_color"
        texture = bpy.data.images.get(texture_name)
        if not texture:
            self.report({'ERROR'}, "Texture not found")
            return {'CANCELLED'}

        settings = context.scene.extended_material_settings
        settings.palette_rows = 6
        settings.palette_columns = 6
        palette_rows = settings.palette_rows
        palette_columns = settings.palette_columns

        width, height = texture.size
        pixels = [0] * (width * height * 4)

        # Calculate the size of each grid cell
        cell_width = int(width / palette_columns)
        cell_height = int(height / palette_rows)

        # Iterate over each cell in the grid, assigning colors from the STANDARD_PALETTE
        color_index = 0
        for row in range(palette_rows):
            for col in range(palette_columns):
                if color_index >= len(DEFAULT_PALETTE):  # Prevent going out of bounds
                    break
                color = DEFAULT_PALETTE[color_index]
                for y in range(row * cell_height, (row + 1) * cell_height):
                    for x in range(col * cell_width, (col + 1) * cell_width):
                        index = (y * width + x) * 4
                        pixels[index:index+3] = [color[0], color[1], color[2]]  # Set RGB
                        pixels[index+3] = 1.0  # Set Alpha
                color_index += 1

        # Update the texture
        texture.pixels = pixels
        texture.update()

        self.report({'INFO'}, "Standard color palette created and applied")
        return {'FINISHED'}
    

class MATERIAL_OT_set_face_color(Operator):
    bl_idname = "material.set_face_color"
    bl_label = "Set Face Color"
    bl_description = "Sets the selected faces to a specific color in the palette"
    bl_options = {'REGISTER', 'UNDO'}

    color_index: bpy.props.IntProperty(
        name="Color Index",
        description="Index of the color to apply",
        default=0,
        min=0
    )

    @classmethod
    def poll(cls, context):
        return context.object is not None and \
            "uv_color" in context.object.data.uv_layers.keys() and \
            context.mode == 'EDIT_MESH' and \
            context.object.data.total_face_sel > 0

    def execute(self, context):
        obj = context.object

        # Forcing update of the mesh.
        # # TODO there should be a better way?
        # bpy.ops.object.mode_set(mode='OBJECT')  # Switch to object mode
        # bpy.ops.object.mode_set(mode='EDIT')    # Switch back to edit mode

        # Collect indices of selected faces
        obj.update_from_editmode()
        mesh = obj.data
        selected_faces = [p.index for p in mesh.polygons if p.select]

        # Call the function to assign UV coordinates based on the color index
        assign_faces_to_color(context, selected_faces, self.color_index)

        self.report({'INFO'}, f"Faces color set to index {self.color_index}")
        return {'FINISHED'}
    

class MATERIAL_OT_bake_ao(Operator):
    bl_idname = "material.bake_ao"
    bl_label = "Bake AO"
    bl_description = "Bakes ambient occlusion for the current model"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and \
               "uv_ao" in obj.data.uv_layers.keys() and \
               obj.type == 'MESH' and \
               len(context.selected_objects) == 1 and \
               context.mode == 'OBJECT'

    def execute(self, context):
        # Ensure the scene's renderer is set to Cycles
        context.scene.render.engine = 'CYCLES'

        obj = context.active_object

        # Define unique material and texture names based on object name
        material_ao_name = f"{obj.name}_Material_AO"
        texture_ao_name = f"{obj.name}_texture_ao"

        # Find or create the AO material and image
        mat_ao = bpy.data.materials.get(material_ao_name)
        if not mat_ao:
            self.report({'ERROR'}, "AO Material not found: " + material_ao_name)
            return {'CANCELLED'}
        
        image_ao = bpy.data.images.get(texture_ao_name)
        if not image_ao:
            self.report({'ERROR'}, "AO Texture not found: " + texture_ao_name)
            return {'CANCELLED'}
        
        # Prepare the object for baking by selecting it and assigning the AO material
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        obj.data.materials.clear()
        obj.data.materials.append(mat_ao)

        # Create a new image node for the AO texture and set it as active for baking
        node_tree = mat_ao.node_tree
        bake_node = node_tree.nodes.new(type='ShaderNodeTexImage')
        bake_node.image = image_ao
        node_tree.nodes.active = bake_node

        # Configure bake settings
        context.scene.cycles.bake_type = 'AO'
        context.scene.render.bake.use_selected_to_active = False
        context.scene.render.bake.use_clear = True
        context.scene.render.bake.margin = 2  # Adjust based on your needs

        # Perform the bake
        bpy.ops.object.bake(type='AO')

        # Clean up: remove the temporary image node
        node_tree.nodes.remove(bake_node)

        self.report({'INFO'}, "AO baked successfully")
        return {'FINISHED'}
    

### Color picking

def sample_texture_color(image, x, y):
    """ Samples the color from the image at normalized coordinates (x, y). """
    width, height = image.size
    x_pixel = int(x * width)
    y_pixel = int(y * height)

    # Ensure we do not go out of bounds
    x_pixel = max(0, min(x_pixel, width - 1))
    y_pixel = max(0, min(y_pixel, height - 1))

    # Calculate the index in the flat pixel array
    index = (y_pixel * width + x_pixel) * 4  # 4 for RGBA
    r, g, b, a = image.pixels[index:index+4]

    return (r, g, b)


def get_palette_colors(obj, num_columns, num_rows):
    """Retrieve colors from an image based on a grid layout."""
    texture_color_name = f"{obj.name}_texture_color"  # Adjust based on actual usage
    image = bpy.data.images.get(texture_color_name)
    if not image:
        print(f"Image {texture_color_name} not found!")
        return []

    width, height = image.size
    colors = []
    for row in range(num_rows):
        row_colors = []
        for col in range(num_columns):
            x = (col + 0.5) / num_columns
            y = (row + 0.5) / num_rows
            x_pixel = int(x * width)
            y_pixel = int(y * height)

            # Clamp to image bounds
            x_pixel = max(0, min(x_pixel, width - 1))
            y_pixel = max(0, min(y_pixel, height - 1))

            index = (y_pixel * width + x_pixel) * 4  # 4 for RGBA
            r, g, b, a = image.pixels[index:index+4]
            row_colors.append((r, g, b))
        colors.append(row_colors)
    return colors


class MATERIAL_PT_custom_panel(Panel):
    bl_label = "Lazy Materials"
    bl_idname = "MATERIAL_PT_custom_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_context = "" # If nothing is set here it'll work in edit mode too.
    bl_order = 50


    def draw(self, context):
        layout = self.layout
        settings = context.scene.extended_material_settings
        obj = context.active_object
        mesh = obj.data if obj and obj.type == 'MESH' else None

        layout.operator(MATERIAL_OT_delete_materials.bl_idname)
        layout.operator(MATERIAL_OT_assign_from_collection.bl_idname)
        layout.operator(MATERIAL_OT_move_to_collection_from_material.bl_idname)
        layout.operator(MATERIAL_OT_remove_unused_from_selected.bl_idname)

        layout.separator()
        layout.label(text="Scene Cleanup")
        layout.operator(MATERIAL_OT_merge_duplicate_materials.bl_idname)
        layout.operator(MATERIAL_OT_remove_unused_materials.bl_idname)

        layout.separator()
        layout.label(text="UV Mapping")
        layout.operator(MATERIAL_OT_generate_materials.bl_idname)

        layout.separator()
        layout.label(text="Color Palette")
        col = layout.column()
        col.enabled = mesh is not None and "uv_color" in mesh.uv_layers.keys() and context.mode == 'OBJECT'
        row = col.row()
        row.prop(settings, "palette_rows", text="Rows")
        row.prop(settings, "palette_columns", text="Columns")
        col.operator(MATERIAL_OT_create_palette.bl_idname)
        col.operator(MATERIAL_OT_default_palette.bl_idname)
        col.operator(MATERIAL_OT_assign_faces_random.bl_idname)

        layout.separator()
        layout.label(text="Set Face Color")
        col = layout.column()
        col.enabled = context.mode == 'EDIT_MESH' and mesh is not None and mesh.total_face_sel > 0
        if obj:
            colors = get_palette_colors(obj, settings.palette_columns, settings.palette_rows)
            for row_index, row_colors in enumerate(colors):
                row = layout.row()
                for col_index, color in enumerate(row_colors):
                    # Format the color tuple into a readable string
                    index = row_index * settings.palette_columns + col_index
                    color_str = str(index)
                    operator = row.operator("material.set_face_color", text=color_str, icon='COLOR')
                    operator.color_index = index
        else:
            layout.label(text="No active object.")

        layout.separator()
        layout.label(text="AO Baking")
        col = layout.column()
        col.enabled = mesh is not None and "uv_ao" in mesh.uv_layers.keys() and context.mode == 'OBJECT'
        col.operator(MATERIAL_OT_bake_ao.bl_idname)

        layout.separator()
        layout.label(text="View Materials")
        col = layout.column()
        row = col.row()
        row.operator("material.view_color", text="View Color")
        row.operator("material.view_ao", text="View AO")
        col.operator("material.view_both", text="View Both")

def register():

    bpy.utils.register_class(ExtendedMaterialSettings)
    bpy.types.Scene.extended_material_settings = bpy.props.PointerProperty(type=ExtendedMaterialSettings)

    bpy.utils.register_class(MATERIAL_OT_remove_unused_materials)
    bpy.utils.register_class(MATERIAL_OT_merge_duplicate_materials)
    bpy.utils.register_class(MATERIAL_OT_remove_unused_from_selected)
    bpy.utils.register_class(MATERIAL_OT_delete_materials)
    bpy.utils.register_class(MATERIAL_OT_assign_from_collection)
    bpy.utils.register_class(MATERIAL_OT_move_to_collection_from_material)
    bpy.utils.register_class(MATERIAL_OT_generate_materials)
    bpy.utils.register_class(MATERIAL_OT_bake_ao)
    bpy.utils.register_class(MATERIAL_OT_create_palette)
    bpy.utils.register_class(MATERIAL_OT_assign_faces_random)
    bpy.utils.register_class(MATERIAL_OT_default_palette)
    bpy.utils.register_class(MATERIAL_OT_set_face_color)
    bpy.utils.register_class(MATERIAL_OT_view_color)
    bpy.utils.register_class(MATERIAL_OT_view_ao)
    bpy.utils.register_class(MATERIAL_OT_view_both)
    bpy.utils.register_class(MATERIAL_PT_custom_panel)

def unregister():
    # Drop the Scene pointer before freeing the PropertyGroup it points at.
    del bpy.types.Scene.extended_material_settings

    bpy.utils.unregister_class(MATERIAL_OT_remove_unused_materials)
    bpy.utils.unregister_class(MATERIAL_OT_merge_duplicate_materials)
    bpy.utils.unregister_class(MATERIAL_OT_remove_unused_from_selected)
    bpy.utils.unregister_class(MATERIAL_OT_delete_materials)
    bpy.utils.unregister_class(MATERIAL_OT_assign_from_collection)
    bpy.utils.unregister_class(MATERIAL_OT_move_to_collection_from_material)
    bpy.utils.unregister_class(MATERIAL_OT_generate_materials)
    bpy.utils.unregister_class(MATERIAL_OT_bake_ao)
    bpy.utils.unregister_class(MATERIAL_OT_create_palette)
    bpy.utils.unregister_class(MATERIAL_OT_assign_faces_random)
    bpy.utils.unregister_class(MATERIAL_OT_default_palette)
    bpy.utils.unregister_class(MATERIAL_OT_set_face_color)
    bpy.utils.unregister_class(MATERIAL_OT_view_color)
    bpy.utils.unregister_class(MATERIAL_OT_view_ao)
    bpy.utils.unregister_class(MATERIAL_OT_view_both)
    bpy.utils.unregister_class(MATERIAL_PT_custom_panel)
    bpy.utils.unregister_class(ExtendedMaterialSettings)

if __name__ == "__main__":
    register()
