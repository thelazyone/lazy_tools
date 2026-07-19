bl_info = {
    "name": "Lazy Line Art",
    "author": "thelazyone",
    "version": (0, 1, 33),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "One-click NPR pipeline: collections, view layers with overrides, "
                   "Grease Pencil Line Art with stroke filtering, and a compositor combine.",
    "warning": "Early prototype targeting Blender 5.0+",
    "wiki_url": "",
    "category": "3D View",
}

import math
import os
import time
import traceback
from datetime import datetime

import bpy
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty

# Every datablock this add-on creates is tagged twice: with a name prefix
# (readable in the outliner) and with a custom id-property (survives user
# renames). All creation goes through _ensure_* helpers that look the tag up
# first, so re-running Build Pipeline converges to the same scene state
# instead of piling up duplicates.
TAG = "LZLA"
ID_PROP = "lazy_lineart_id"

COL_LINEART = f"{TAG}.col.lineart"
GP_CONTOURS = f"{TAG}.gp.contours"
VL_FLAT = f"{TAG}.vl.flat"
VL_SHADOW = f"{TAG}.vl.shadow"
VL_HARD_SHADOW = f"{TAG}.vl.hard_shadow"
VL_GRUNGE = f"{TAG}.vl.grunge"
VL_LINES = f"{TAG}.vl.lines"
MAT_OVERRIDE_WHITE = f"{TAG}.mat.override_white"
MAT_OVERRIDE_HARD_SHADOW = f"{TAG}.mat.override_hard_shadow"
MAT_GRUNGE_MASK = f"{TAG}.mat.grunge_mask"
MAT_GP_LINES = f"{TAG}.mat.gp_lines"
WORLD_DARK = f"{TAG}.world.dark"
NG_STROKE_FILTER = f"{TAG}.ng.stroke_filter"
NG_SCREEN_SPACE = f"{TAG}.ng.screen_space"
NG_EDGE_DETECT = f"{TAG}.ng.edge_detect"
NG_COMPOSITOR = f"{TAG}.ng.compositor"
FILE_OUT_NAME = "lzla_file_out"
FILE_OUT_SPLIT_NAME = "lzla_file_out_split"

GP_LAYER_NAME = "Lines"
LINEART_MOD_NAME = f"{TAG} Line Art"
THICKNESS_MOD_NAME = f"{TAG} Screen Space"
FILTER_MOD_NAME = f"{TAG} Stroke Filter"
MIN_SCREEN_SPACE_SCALE = 0.01
UTILITY_LAYER_SAMPLES = 16
RUGGED_SEED_OFFSET = 17.3
DOT_SEED_OFFSET = 42.0
GRUNGE_NOISE_DETAIL = 4.0
GRUNGE_NOISE_ROUGHNESS = 0.6
GRUNGE_COORD_ROTATION_MAIN = (
    math.radians(37.3), math.radians(23.7), math.radians(51.1),
)
GRUNGE_COORD_ROTATION_SCATTER = (
    math.radians(19.1), math.radians(41.5), math.radians(67.8),
)
GRUNGE_NOISE_THRESHOLD = 0.5
GRUNGE_BLUR_RADIUS_SCALE = 0.12
OBJ_RANDOM_SEED_SCALE = 997.0


# ----------------------------------------------------------------------------
# Generic idempotent datablock helpers
# ----------------------------------------------------------------------------

def _find_tagged(collection, key):
    for datablock in collection:
        try:
            if datablock.get(ID_PROP) == key:
                return datablock
        except AttributeError:
            continue
    return None


def _ensure_datablock(collection, key, create):
    """Fetch the datablock tagged with `key`, creating (and tagging) it if missing."""
    datablock = _find_tagged(collection, key)
    if datablock is None:
        datablock = create()
        datablock[ID_PROP] = key
    if datablock.name != key:
        datablock.name = key
    return datablock


def _gp_data_collection():
    # Blender 5.0 renamed grease_pencils_v3 (4.3/4.4) to grease_pencils.
    v3 = getattr(bpy.data, "grease_pencils_v3", None)
    return v3 if v3 is not None else bpy.data.grease_pencils


def _set_node_input(node, socket_names, value):
    """Set a node input socket's default value, trying several socket names.
    Blender 5.0 moved most compositor node options from RNA properties to
    input sockets, so option setting needs to tolerate both layouts."""
    if isinstance(socket_names, str):
        socket_names = (socket_names,)
    for name in socket_names:
        socket = node.inputs.get(name)
        if socket is None:
            continue
        try:
            socket.default_value = value
            return True
        except (TypeError, ValueError):
            continue
    return False


def _group_input_socket(group_node, socket_name):
    """Resolve a compositor group node input by its tree interface name."""
    if group_node is None or group_node.node_tree is None:
        return None
    for item in group_node.node_tree.interface.items_tree:
        if getattr(item, 'item_type', None) != 'SOCKET':
            continue
        if item.in_out != 'INPUT' or item.name != socket_name:
            continue
        return group_node.inputs.get(item.identifier)
    return group_node.inputs.get(socket_name)


def _set_group_input(group_node, socket_name, value):
    """Set a group node input default, matching by interface socket name."""
    socket = _group_input_socket(group_node, socket_name)
    if socket is None:
        return False
    try:
        socket.default_value = value
        return True
    except (TypeError, ValueError):
        return False


def _set_node_option(node, attr, socket_names, value_candidates):
    """Set a node option that may be an RNA enum property (pre-5.0) or a menu
    input socket (5.0 unified nodes), trying each candidate spelling."""
    if isinstance(value_candidates, str):
        value_candidates = (value_candidates,)
    if hasattr(node, attr):
        for value in value_candidates:
            try:
                setattr(node, attr, value)
                return True
            except TypeError:
                continue
    for value in value_candidates:
        if _set_node_input(node, socket_names, value):
            return True
    return False


def _mix_rgba(tree, blend_type='MIX'):
    """Create a ShaderNodeMix in RGBA mode and return it with its Factor/A/B
    input sockets and Result output, resolved by socket identifier (the node
    has same-named sockets for every data type)."""
    node = tree.nodes.new('ShaderNodeMix')
    node.data_type = 'RGBA'
    node.blend_type = blend_type
    factor = next(s for s in node.inputs if s.identifier == 'Factor_Float')
    a = next(s for s in node.inputs if s.identifier == 'A_Color')
    b = next(s for s in node.inputs if s.identifier == 'B_Color')
    result = next(s for s in node.outputs if s.identifier == 'Result_Color')
    return node, factor, a, b, result


def _one_minus(tree, value, x, y, name):
    inv = tree.nodes.new('ShaderNodeMath')
    inv.name = name
    inv.operation = 'SUBTRACT'
    inv.location = (x, y)
    inv.inputs[0].default_value = 1.0
    tree.links.new(value, inv.inputs[1])
    return inv.outputs[0]


def _set_mix_b_color(mix_node, rgba):
    """Write an RGBA tint into a Mix node's B_Color input."""
    color = (rgba[0], rgba[1], rgba[2], rgba[3] if len(rgba) > 3 else 1.0)
    for socket in mix_node.inputs:
        if socket.identifier == 'B_Color':
            socket.default_value = color
            return True
    color_inputs = [s for s in mix_node.inputs if s.type == 'RGBA']
    if len(color_inputs) > 1:
        color_inputs[1].default_value = color
        return True
    return False


def _masked_multiply_tint(tree, base, apply_mask, tint_rgba, x, y, prefix):
    """Where apply_mask=1, darken base with a per-channel multiply (keeps hue)."""
    mult, m_factor, a, b, product = _mix_rgba(tree, 'MULTIPLY')
    mult.name = f"{prefix}_mult"
    mult.location = (x - 160, y)
    m_factor.default_value = 1.0
    b.default_value = (
        tint_rgba[0], tint_rgba[1], tint_rgba[2],
        tint_rgba[3] if len(tint_rgba) > 3 else 1.0,
    )
    tree.links.new(base, a)

    pick, factor, a, b, result = _mix_rgba(tree)
    pick.name = f"{prefix}_pick"
    pick.location = (x, y)
    tree.links.new(base, a)
    tree.links.new(product, b)
    tree.links.new(apply_mask, factor)
    return result


def _shadow_apply_mask(tree, lit_mask, x, y, name):
    """Convert a lit=1 mask into a shadow=1 apply mask."""
    return _one_minus(tree, lit_mask, x, y, name)


def _rgb_to_luminance_value(tree, color_socket, x, y, name):
    """Return a scalar luminance socket from an RGB/color image socket."""
    for node_type in ('CompositorNodeRGBToBW', 'ShaderNodeRGBToBW'):
        try:
            node = tree.nodes.new(node_type)
        except RuntimeError:
            continue
        node.name = name
        node.location = (x, y)
        color_in = node.inputs.get("Color") or node.inputs.get("Image") or node.inputs[0]
        tree.links.new(color_socket, color_in)
        return node.outputs.get("Val") or node.outputs.get("Value") or node.outputs[0]
    raise RuntimeError(f"Cannot create an RGB-to-luminance node in {getattr(tree, 'name', tree)!r}")


def _grunge_apply_mask(tree, grunge_white, x, y, prefix):
    """Build a per-pixel mask that is 1 where the grunge pass should tint."""
    lum = _rgb_to_luminance_value(
        tree, grunge_white, x - 180, y, f"{prefix}_lum",
    )
    return _one_minus(tree, lum, x, y, f"{prefix}_apply")


def _set_compositor_tint_color(comp, key, rgba):
    """Push a tint color into the compositor nodes for one overlay pass."""
    mult = comp.nodes.get(f"lzla_{key}_mult")
    if mult is not None and _set_mix_b_color(mult, rgba):
        return True
    rgb = comp.nodes.get(f"lzla_{key}_tint_color")
    if rgb is not None:
        color = (rgba[0], rgba[1], rgba[2], rgba[3] if len(rgba) > 3 else 1.0)
        if hasattr(rgb, "color"):
            rgb.color = color[:3]
        rgb.outputs[0].default_value = color
        return True
    tint = comp.nodes.get(f"lzla_{key}_tint")
    if tint is not None:
        color_inputs = [s for s in tint.inputs if s.type == 'RGBA']
        if color_inputs:
            color_inputs[0].default_value = tuple(rgba)
            return True
    return False


def _link_toon_luminance_step(tree, luminance, threshold_name, threshold_value, x, y=0):
    """Hard-step luminance to black (shadow) / white (lit) at threshold."""
    value = tree.nodes.new('ShaderNodeValue')
    value.name = threshold_name
    value.location = (x, y - 140)
    value.outputs[0].default_value = threshold_value

    compare = tree.nodes.new('ShaderNodeMath')
    compare.name = f"{threshold_name}_compare"
    compare.operation = 'GREATER_THAN'
    compare.location = (x, y)
    tree.links.new(luminance, compare.inputs[0])
    tree.links.new(value.outputs[0], compare.inputs[1])

    mix, factor, a, b, result = _mix_rgba(tree)
    mix.name = f"{threshold_name}_mix"
    mix.location = (x + 180, y)
    a.default_value = (0.0, 0.0, 0.0, 1.0)
    b.default_value = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(compare.outputs[0], factor)
    return result


def _set_material_threshold(mat, value_node_name, ramp_name, value):
    """Write a threshold slider value into an override material node tree."""
    if mat is None or mat.node_tree is None:
        return False
    value_node = mat.node_tree.nodes.get(value_node_name)
    if value_node is not None:
        if value_node.type == 'VALUE':
            value_node.outputs[0].default_value = value
        elif value_node.type == 'MATH':
            value_node.inputs[1].default_value = value
        mat.update_tag()
        return True
    ramp = mat.node_tree.nodes.get(ramp_name)
    if ramp is not None:
        ramp.color_ramp.elements[1].position = value
        mat.update_tag()
        return True
    return False


def _clear_node_tree(tree):
    tree.nodes.clear()
    interface = getattr(tree, "interface", None)
    if interface is not None:
        interface.clear()


def _ensure_modifier_order(obj, ordered_names):
    """Move modifiers so they appear top-to-bottom in `ordered_names`."""
    for target_index, name in enumerate(ordered_names):
        current_index = obj.modifiers.find(name)
        if current_index != -1 and current_index != target_index:
            obj.modifiers.move(current_index, target_index)



def _lineart_base_radius(props):
    """Base Line Art radius at Reference Distance (per-point scaling is in GN)."""
    return max(props.stroke_radius, 0.0)


def _gn_new_node(tree, type_names):
    if isinstance(type_names, str):
        type_names = (type_names,)
    for type_name in type_names:
        try:
            return tree.nodes.new(type_name)
        except RuntimeError:
            continue
    return None


def _gn_require_node(tree, type_names):
    node = _gn_new_node(tree, type_names)
    if node is None:
        raise RuntimeError(
            f"Cannot create any of {type_names!r} in {getattr(tree, 'name', tree)!r}"
        )
    return node


def _gn_socket(node, names, *, kind='output'):
    sockets = node.outputs if kind == 'output' else node.inputs
    for name in names:
        if isinstance(name, int):
            if -len(sockets) <= name < len(sockets):
                return sockets[name]
            continue
        if name in sockets:
            return sockets[name]
    return sockets[0]


def _gn_local_to_world(tree, local_pos, obj_info, x, y):
    """GP-local point -> world space via Object Info transform matrix."""
    transform = _gn_require_node(tree, 'FunctionNodeTransformPoint')
    transform.location = (x, y)
    tree.links.new(local_pos, _gn_socket(transform, ("Vector",), kind='input'))
    tree.links.new(
        _gn_socket(obj_info, ("Transform",)),
        _gn_socket(transform, ("Transform",), kind='input'),
    )
    return _gn_socket(transform, ("Vector",))


def _build_screen_space_thickness_group(props):
    """Per-point screen-space scaling using camera-space Z (Blender 4.3+ method).

    Matches the official Screen Space Thickness node group: transform each point
    into active-camera space, take |Z| as view depth, then scale the Line Art
    radius by (view_depth / reference_distance).
    """
    tree = _ensure_datablock(
        bpy.data.node_groups, NG_SCREEN_SPACE,
        lambda: bpy.data.node_groups.new(NG_SCREEN_SPACE, 'GeometryNodeTree'),
    )
    _clear_node_tree(tree)

    tree.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    group_in = tree.nodes.new('NodeGroupInput')
    group_in.location = (-1100, 0)
    group_out = tree.nodes.new('NodeGroupOutput')
    group_out.location = (900, 0)

    ref_distance = tree.nodes.new('ShaderNodeValue')
    ref_distance.name = "lzla_ref_distance"
    ref_distance.label = "Reference Distance"
    ref_distance.location = (-1100, -320)
    ref_distance.outputs[0].default_value = props.thickness_reference_distance

    active_cam = _gn_require_node(tree, 'GeometryNodeInputActiveCamera')
    active_cam.location = (-1100, -500)

    cam_info = _gn_require_node(tree, 'GeometryNodeObjectInfo')
    cam_info.location = (-900, -500)
    tree.links.new(
        _gn_socket(active_cam, ("Active Camera",)),
        _gn_socket(cam_info, ("Object",), kind='input'),
    )

    self_info = tree.nodes.new('GeometryNodeObjectInfo')
    self_info.name = "lzla_self_info"
    self_info.label = "GP Object"
    self_info.location = (-1100, -680)

    position = tree.nodes.new('GeometryNodeInputPosition')
    position.location = (-900, -120)

    input_radius = tree.nodes.new('GeometryNodeInputRadius')
    input_radius.location = (-900, -320)

    world_pos = _gn_local_to_world(
        tree, _gn_socket(position, ("Position",)), self_info, -680, -120,
    )

    inv_cam = _gn_require_node(tree, 'FunctionNodeInvertMatrix')
    inv_cam.location = (-460, -500)
    tree.links.new(
        _gn_socket(cam_info, ("Transform",)),
        _gn_socket(inv_cam, ("Matrix",), kind='input'),
    )

    cam_pos = _gn_require_node(tree, 'FunctionNodeTransformPoint')
    cam_pos.location = (-240, -120)
    tree.links.new(world_pos, _gn_socket(cam_pos, ("Vector",), kind='input'))
    tree.links.new(
        _gn_socket(inv_cam, ("Matrix",)),
        _gn_socket(cam_pos, ("Transform",), kind='input'),
    )

    separate = _gn_require_node(tree, 'ShaderNodeSeparateXYZ')
    separate.location = (-20, -120)
    tree.links.new(_gn_socket(cam_pos, ("Vector",)), _gn_socket(separate, ("Vector",), kind='input'))

    depth_abs = _gn_require_node(tree, ('FunctionNodeFloatMath', 'ShaderNodeMath'))
    depth_abs.operation = 'ABSOLUTE'
    depth_abs.location = (180, -120)
    tree.links.new(_gn_socket(separate, ("Z",)), depth_abs.inputs[0])

    depth_scale = _gn_require_node(tree, ('FunctionNodeFloatMath', 'ShaderNodeMath'))
    depth_scale.operation = 'DIVIDE'
    depth_scale.location = (380, -120)
    tree.links.new(depth_abs.outputs[0], depth_scale.inputs[0])
    tree.links.new(ref_distance.outputs[0], depth_scale.inputs[1])

    clamp_scale = _gn_require_node(tree, ('FunctionNodeFloatMath', 'ShaderNodeMath'))
    clamp_scale.operation = 'MAXIMUM'
    clamp_scale.location = (580, -120)
    clamp_scale.inputs[1].default_value = MIN_SCREEN_SPACE_SCALE
    tree.links.new(depth_scale.outputs[0], clamp_scale.inputs[0])

    final_scale = clamp_scale.outputs[0]
    cam_data = _gn_new_node(tree, 'GeometryNodeCameraInfo')
    if cam_data is not None:
        cam_data.name = "lzla_cam_data"
        cam_data.label = "Camera Data"
        cam_data.location = (580, -320)
        tree.links.new(
            _gn_socket(active_cam, ("Active Camera",)),
            _gn_socket(cam_data, ("Camera",), kind='input'),
        )
        persp_switch = _gn_new_node(tree, 'GeometryNodeSwitch')
        if persp_switch is not None:
            persp_switch.input_type = 'FLOAT'
            persp_switch.location = (780, -120)
            tree.links.new(
                _gn_socket(cam_data, ("Is Orthographic",)),
                _gn_socket(persp_switch, ("Switch",), kind='input'),
            )
            tree.links.new(clamp_scale.outputs[0], _gn_socket(persp_switch, ("False",), kind='input'))
            persp_switch.inputs["True"].default_value = 1.0
            final_scale = _gn_socket(persp_switch, ("Output",))

    scaled_radius = _gn_require_node(tree, ('FunctionNodeFloatMath', 'ShaderNodeMath'))
    scaled_radius.operation = 'MULTIPLY'
    scaled_radius.location = (780, -320)
    tree.links.new(_gn_socket(input_radius, ("Radius",)), scaled_radius.inputs[0])
    tree.links.new(final_scale, scaled_radius.inputs[1])

    set_radius = _gn_require_node(tree, 'GeometryNodeSetCurveRadius')
    set_radius.location = (780, 0)
    tree.links.new(group_in.outputs["Geometry"], _gn_socket(set_radius, ("Geometry",), kind='input'))
    tree.links.new(scaled_radius.outputs[0], _gn_socket(set_radius, ("Radius",), kind='input'))
    tree.links.new(_gn_socket(set_radius, ("Geometry",)), group_out.inputs["Geometry"])

    return tree


def _sync_screen_space_thickness(scene, props, gp_object):
    """Push panel/object references into the screen-space node group."""
    if gp_object is None:
        return
    thickness_mod = gp_object.modifiers.get(THICKNESS_MOD_NAME)
    if thickness_mod is None or thickness_mod.node_group is None:
        return

    tree = thickness_mod.node_group
    ref = tree.nodes.get("lzla_ref_distance")
    if ref is not None:
        ref.outputs[0].default_value = max(props.thickness_reference_distance, 1e-4)

    self_node = tree.nodes.get("lzla_self_info")
    if self_node is not None:
        self_node.inputs["Object"].default_value = gp_object


# ----------------------------------------------------------------------------
# Materials and world
# ----------------------------------------------------------------------------

def _ensure_material(key):
    mat = _ensure_datablock(bpy.data.materials, key, lambda: bpy.data.materials.new(key))
    if mat.node_tree is None:
        try:
            mat.use_nodes = True  # deprecated no-op on 5.0, needed pre-5.0
        except AttributeError:
            pass
    return mat


def _build_override_shadow(props):
    """Toon-stepped matte white for the normal shadow view layer.

    Diffuse lighting is hard-stepped at Shadow Threshold. The compositor blurs
    that pass, hard-steps again at Edge Threshold, and tints flats darker."""
    mat = _ensure_material(MAT_OVERRIDE_WHITE)
    tree = mat.node_tree
    tree.nodes.clear()

    diffuse = tree.nodes.new('ShaderNodeBsdfDiffuse')
    diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    diffuse.location = (-500, 0)

    to_rgb = tree.nodes.new('ShaderNodeShaderToRGB')
    to_rgb.location = (-300, 0)
    tree.links.new(diffuse.outputs["BSDF"], to_rgb.inputs["Shader"])

    to_bw = tree.nodes.new('ShaderNodeRGBToBW')
    to_bw.location = (-150, 0)
    tree.links.new(to_rgb.outputs["Color"], to_bw.inputs["Color"])

    mask = _link_toon_luminance_step(
        tree, to_bw.outputs["Val"], "lzla_shadow_threshold", props.shadow_threshold, 50,
    )

    emission = tree.nodes.new('ShaderNodeEmission')
    emission.location = (300, 0)
    tree.links.new(mask, emission.inputs["Color"])

    output = tree.nodes.new('ShaderNodeOutputMaterial')
    output.location = (550, 0)
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return mat


def _build_override_hard_shadow(props):
    """Toon-stepped matte white for the hard-shadow view layer.

    Same pipeline as the normal shadow pass, but stepped at Hard Shadow
    Threshold so only the deepest lit regions register before compositing."""
    mat = _ensure_material(MAT_OVERRIDE_HARD_SHADOW)
    tree = mat.node_tree
    tree.nodes.clear()

    diffuse = tree.nodes.new('ShaderNodeBsdfDiffuse')
    diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    diffuse.location = (-500, 0)

    to_rgb = tree.nodes.new('ShaderNodeShaderToRGB')
    to_rgb.location = (-300, 0)
    tree.links.new(diffuse.outputs["BSDF"], to_rgb.inputs["Shader"])

    to_bw = tree.nodes.new('ShaderNodeRGBToBW')
    to_bw.location = (-150, 0)
    tree.links.new(to_rgb.outputs["Color"], to_bw.inputs["Color"])

    mask = _link_toon_luminance_step(
        tree, to_bw.outputs["Val"], "lzla_hard_shadow_threshold",
        props.hard_shadow_threshold, 50,
    )

    enable = tree.nodes.new('ShaderNodeValue')
    enable.name = "lzla_hard_shadow_enabled"
    enable.location = (50, -140)
    enable.outputs[0].default_value = 1.0 if props.hard_shadow_enabled else 0.0

    enable_mix, factor, a, b, emission_color = _mix_rgba(tree)
    enable_mix.name = "lzla_hard_shadow_enable_mix"
    enable_mix.location = (250, 0)
    a.default_value = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(mask, b)
    tree.links.new(enable.outputs[0], factor)

    emission = tree.nodes.new('ShaderNodeEmission')
    emission.location = (450, 0)
    tree.links.new(emission_color, emission.inputs["Color"])

    output = tree.nodes.new('ShaderNodeOutputMaterial')
    output.location = (650, 0)
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return mat


def _tilted_object_coords(tree, obj_socket, rotation, x, y, name):
    """Tilt object-space coordinates off the mesh axes to avoid linear banding."""
    mapping = tree.nodes.new('ShaderNodeMapping')
    mapping.name = name
    mapping.location = (x, y)
    mapping.inputs['Rotation'].default_value = rotation
    tree.links.new(obj_socket, mapping.inputs['Vector'])
    return mapping.outputs['Vector']


def _configure_base_noise(node, scale, detail, roughness):
    """4D noise without distortion - distortion causes streaky linear artifacts."""
    node.noise_dimensions = '4D'
    _set_node_input(node, "Scale", scale)
    _set_node_input(node, "Detail", detail)
    _set_node_input(node, "Roughness", roughness)
    _set_node_input(node, "Distortion", 0.0)


def _configure_main_grunge_noise(node, scale):
    """Patch/rugged harmonics: 4D noise with scale only, like the committed build.

    Blender's default detail/roughness keeps the summed field smooth enough for
    clean hard-step regions. Forcing high rugged detail made edges speckly."""
    node.noise_dimensions = '4D'
    _set_node_input(node, "Scale", scale)


def _configure_grunge_noise(node, scale):
    """Scatter harmonic: medium detail, no distortion."""
    _configure_base_noise(node, scale, GRUNGE_NOISE_DETAIL, GRUNGE_NOISE_ROUGHNESS)


def _apply_scatter_ramp(ramp):
    """Hard-step scatter at a fixed 0.5 threshold."""
    ramp.color_ramp.interpolation = 'CONSTANT'
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = (1.0, 1.0, 1.0, 1.0)
    ramp.color_ramp.elements[1].position = GRUNGE_NOISE_THRESHOLD
    ramp.color_ramp.elements[1].color = (0.0, 0.0, 0.0, 1.0)


def _scatter_noise_tap(tree, coords, seed_socket, scale, suffix, x, y):
    noise = tree.nodes.new('ShaderNodeTexNoise')
    noise.name = f"lzla_grunge_noise{suffix}"
    noise.location = (x, y)
    _configure_grunge_noise(noise, scale)
    tree.links.new(coords, noise.inputs["Vector"])
    tree.links.new(seed_socket, noise.inputs["W"])
    return noise.outputs["Fac"]


def _build_blurred_scatter_fac(tree, coords, seed_socket, scale, blur, x, y):
    """Box-blur the scatter noise field before thresholding."""
    blur_r = tree.nodes.new('ShaderNodeValue')
    blur_r.name = "lzla_grunge_noise_blur_r"
    blur_r.label = "Scatter Blur"
    blur_r.location = (x - 200, y - 120)
    blur_r.outputs[0].default_value = blur * GRUNGE_BLUR_RADIUS_SCALE

    neg_r = tree.nodes.new('ShaderNodeMath')
    neg_r.operation = 'MULTIPLY'
    neg_r.location = (x - 50, y - 120)
    neg_r.inputs[1].default_value = -1.0
    tree.links.new(blur_r.outputs[0], neg_r.inputs[0])

    taps = []
    tap_specs = (
        (0.0, 0.0, 0.0, ""),
        (1.0, 0.0, 0.0, "_px"),
        (-1.0, 0.0, 0.0, "_nx"),
        (0.0, 1.0, 0.0, "_py"),
        (0.0, -1.0, 0.0, "_ny"),
    )
    for ox, oy, oz, suffix in tap_specs:
        if ox == oy == oz == 0.0:
            sample_coords = coords
        else:
            offset = tree.nodes.new('ShaderNodeCombineXYZ')
            offset.location = (x - 180, y - 180 - 40 * len(taps))
            if ox > 0:
                tree.links.new(blur_r.outputs[0], offset.inputs["X"])
            elif ox < 0:
                tree.links.new(neg_r.outputs[0], offset.inputs["X"])
            else:
                offset.inputs["X"].default_value = 0.0
            if oy > 0:
                tree.links.new(blur_r.outputs[0], offset.inputs["Y"])
            elif oy < 0:
                tree.links.new(neg_r.outputs[0], offset.inputs["Y"])
            else:
                offset.inputs["Y"].default_value = 0.0
            offset.inputs["Z"].default_value = 0.0
            add = tree.nodes.new('ShaderNodeVectorMath')
            add.operation = 'ADD'
            add.location = (x - 20, y - 180 - 40 * len(taps))
            tree.links.new(coords, add.inputs[0])
            tree.links.new(offset.outputs["Vector"], add.inputs[1])
            sample_coords = add.outputs["Vector"]
        taps.append(_scatter_noise_tap(
            tree, sample_coords, seed_socket, scale, suffix, x + 40 * len(taps), y,
        ))

    total = taps[0]
    for fac in taps[1:]:
        add = tree.nodes.new('ShaderNodeMath')
        add.operation = 'ADD'
        add.location = (x + 220, y - 40 * len(taps))
        tree.links.new(total, add.inputs[0])
        tree.links.new(fac, add.inputs[1])
        total = add.outputs[0]

    avg = tree.nodes.new('ShaderNodeMath')
    avg.name = "lzla_grunge_noise_blur_avg"
    avg.operation = 'MULTIPLY'
    avg.location = (x + 360, y)
    avg.inputs[1].default_value = 1.0 / len(taps)
    tree.links.new(total, avg.inputs[0])
    return avg.outputs[0]


def _build_object_seed_modifier(tree, panel_seed_socket, obj_info, x, y):
    """Per-object W seed: panel_seed × (Random×scale + Object Index + 1).

    Random comes from Object Info and is unique per object. The panel seed
    multiplies the whole field so changing Grunge Seed reshuffles every object."""
    random_scale = tree.nodes.new('ShaderNodeMath')
    random_scale.name = "lzla_obj_random_scale"
    random_scale.operation = 'MULTIPLY'
    random_scale.location = (x - 280, y - 80)
    random_scale.inputs[1].default_value = OBJ_RANDOM_SEED_SCALE
    tree.links.new(obj_info.outputs["Random"], random_scale.inputs[0])

    obj_id_base = tree.nodes.new('ShaderNodeMath')
    obj_id_base.name = "lzla_obj_id_base"
    obj_id_base.operation = 'ADD'
    obj_id_base.location = (x - 280, y)
    obj_id_base.inputs[1].default_value = 1.0
    tree.links.new(random_scale.outputs[0], obj_id_base.inputs[0])

    obj_factor = tree.nodes.new('ShaderNodeMath')
    obj_factor.name = "lzla_obj_factor"
    obj_factor.operation = 'ADD'
    obj_factor.location = (x - 120, y)
    tree.links.new(obj_id_base.outputs[0], obj_factor.inputs[0])
    tree.links.new(obj_info.outputs["Object Index"], obj_factor.inputs[1])

    obj_seed = tree.nodes.new('ShaderNodeMath')
    obj_seed.name = "lzla_obj_seed"
    obj_seed.operation = 'MULTIPLY'
    obj_seed.location = (x, y)
    tree.links.new(panel_seed_socket, obj_seed.inputs[0])
    tree.links.new(obj_factor.outputs[0], obj_seed.inputs[1])
    return obj_seed.outputs[0]


def _build_grunge_mask(props):
    """Black/white hard-stepped procedural grunge, used as the grunge view
    layer's override. Emission-based so lighting doesn't affect the mask.
    Patch and rugged are summed and hard-stepped; scatter is a separate
    harmonic, hard-stepped on its own, then unioned in. Coordinates are
    tilted off mesh axes and all noise runs without distortion to avoid
    linear banding. Each object gets panel_seed × (Object Info Random + Pass Index)
    so the global seed reshuffles all objects while keeping them distinct."""
    mat = _ensure_material(MAT_GRUNGE_MASK)
    tree = mat.node_tree
    tree.nodes.clear()

    coords = tree.nodes.new('ShaderNodeTexCoord')
    coords.location = (-1000, 0)

    seed = tree.nodes.new('ShaderNodeValue')
    seed.name = "lzla_seed"
    seed.label = "Grunge Seed"
    seed.outputs[0].default_value = props.grunge_seed
    seed.location = (-1000, -250)

    obj_info = tree.nodes.new('ShaderNodeObjectInfo')
    obj_info.location = (-1000, -450)

    obj_seed_value = _build_object_seed_modifier(
        tree, seed.outputs[0], obj_info, -800, -250,
    )

    main_coords = _tilted_object_coords(
        tree, coords.outputs["Object"], GRUNGE_COORD_ROTATION_MAIN,
        -850, 0, "lzla_grunge_coords_main",
    )
    scatter_coords = _tilted_object_coords(
        tree, coords.outputs["Object"], GRUNGE_COORD_ROTATION_SCATTER,
        -850, -200, "lzla_grunge_coords_scatter",
    )

    rugged_seed = tree.nodes.new('ShaderNodeMath')
    rugged_seed.operation = 'ADD'
    rugged_seed.location = (-650, -350)
    rugged_seed.inputs[1].default_value = RUGGED_SEED_OFFSET
    tree.links.new(obj_seed_value, rugged_seed.inputs[0])

    scatter_seed = tree.nodes.new('ShaderNodeMath')
    scatter_seed.operation = 'ADD'
    scatter_seed.location = (-650, -550)
    scatter_seed.inputs[1].default_value = DOT_SEED_OFFSET
    tree.links.new(obj_seed_value, scatter_seed.inputs[0])

    patch_noise = tree.nodes.new('ShaderNodeTexNoise')
    patch_noise.name = "lzla_patch_noise"
    patch_noise.location = (-750, 200)
    _configure_main_grunge_noise(patch_noise, props.grunge_patch_scale)
    tree.links.new(main_coords, patch_noise.inputs["Vector"])
    tree.links.new(obj_seed_value, patch_noise.inputs["W"])

    patch_amp = tree.nodes.new('ShaderNodeMath')
    patch_amp.name = "lzla_patch_amp"
    patch_amp.operation = 'MULTIPLY'
    patch_amp.location = (-550, 200)
    patch_amp.inputs[1].default_value = props.grunge_patch_amplitude
    tree.links.new(patch_noise.outputs["Fac"], patch_amp.inputs[0])

    rugged_noise = tree.nodes.new('ShaderNodeTexNoise')
    rugged_noise.name = "lzla_rugged_noise"
    rugged_noise.location = (-750, -50)
    _configure_main_grunge_noise(rugged_noise, props.grunge_rugged_scale)
    tree.links.new(main_coords, rugged_noise.inputs["Vector"])
    tree.links.new(rugged_seed.outputs[0], rugged_noise.inputs["W"])

    rugged_amp = tree.nodes.new('ShaderNodeMath')
    rugged_amp.name = "lzla_rugged_amp"
    rugged_amp.operation = 'MULTIPLY'
    rugged_amp.location = (-550, -50)
    rugged_amp.inputs[1].default_value = props.grunge_rugged_amplitude
    tree.links.new(rugged_noise.outputs["Fac"], rugged_amp.inputs[0])

    scatter_fac = _build_blurred_scatter_fac(
        tree, scatter_coords, scatter_seed.outputs[0],
        props.grunge_noise_scale, props.grunge_noise_blur, -750, -300,
    )

    scatter_amp = tree.nodes.new('ShaderNodeMath')
    scatter_amp.name = "lzla_grunge_noise_amp"
    scatter_amp.operation = 'MULTIPLY'
    scatter_amp.location = (-550, -300)
    scatter_amp.inputs[1].default_value = props.grunge_noise_intensity
    tree.links.new(scatter_fac, scatter_amp.inputs[0])

    combine = tree.nodes.new('ShaderNodeMath')
    combine.name = "lzla_combine"
    combine.operation = 'ADD'
    combine.location = (-350, 50)
    tree.links.new(patch_amp.outputs[0], combine.inputs[0])
    tree.links.new(rugged_amp.outputs[0], combine.inputs[1])

    main_ramp = tree.nodes.new('ShaderNodeValToRGB')
    main_ramp.name = "lzla_ramp"
    main_ramp.location = (-150, 50)
    main_ramp.color_ramp.interpolation = 'CONSTANT'
    main_ramp.color_ramp.elements[0].position = 0.0
    main_ramp.color_ramp.elements[0].color = (1.0, 1.0, 1.0, 1.0)
    main_ramp.color_ramp.elements[1].position = props.grunge_threshold
    main_ramp.color_ramp.elements[1].color = (0.0, 0.0, 0.0, 1.0)
    tree.links.new(combine.outputs[0], main_ramp.inputs["Fac"])

    scatter_ramp = tree.nodes.new('ShaderNodeValToRGB')
    scatter_ramp.name = "lzla_grunge_noise_ramp"
    scatter_ramp.location = (-150, -300)
    _apply_scatter_ramp(scatter_ramp)
    tree.links.new(scatter_amp.outputs[0], scatter_ramp.inputs["Fac"])

    union, factor, a, b, union_out = _mix_rgba(tree, 'MULTIPLY')
    union.name = "lzla_grunge_union"
    union.location = (50, 0)
    factor.default_value = 1.0
    tree.links.new(main_ramp.outputs["Color"], a)
    tree.links.new(scatter_ramp.outputs["Color"], b)

    emission = tree.nodes.new('ShaderNodeEmission')
    emission.location = (300, 0)
    tree.links.new(union_out, emission.inputs["Color"])

    output = tree.nodes.new('ShaderNodeOutputMaterial')
    output.location = (500, 0)
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return mat


def _build_gp_line_material(props):
    mat = _ensure_datablock(bpy.data.materials, MAT_GP_LINES,
                            lambda: bpy.data.materials.new(MAT_GP_LINES))
    if not mat.is_grease_pencil:
        bpy.data.materials.create_gpencil_data(mat)
    mat.grease_pencil.color = tuple(props.line_color)
    return mat


def _build_dark_world():
    """Black, zero-strength world for the utility layers: keeps toon shadow
    regions crisp (no ambient fill light) - backgrounds are re-added as flat
    white in the compositor since the scene renders with a transparent film."""
    world = _ensure_datablock(bpy.data.worlds, WORLD_DARK,
                              lambda: bpy.data.worlds.new(WORLD_DARK))
    if world.node_tree is None:
        try:
            world.use_nodes = True
        except AttributeError:
            pass
    for node in world.node_tree.nodes:
        if node.type == 'BACKGROUND':
            node.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
            _set_node_input(node, "Strength", 0.0)
            break
    return world


# ----------------------------------------------------------------------------
# Grease Pencil object + Line Art + screen-space thickness + stroke filter
# ----------------------------------------------------------------------------

def _build_stroke_filter_group(props):
    """Geometry Nodes group that deletes stroke chains shorter than Min Length
    (geometry-space spline length). Runs after the Line Art modifier, directly
    on the Grease Pencil curves."""
    tree = _ensure_datablock(
        bpy.data.node_groups, NG_STROKE_FILTER,
        lambda: bpy.data.node_groups.new(NG_STROKE_FILTER, 'GeometryNodeTree'),
    )
    _clear_node_tree(tree)

    tree.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    min_length = tree.interface.new_socket("Min Length", in_out='INPUT',
                                           socket_type='NodeSocketFloat')
    min_length.default_value = props.min_stroke_length
    min_length.min_value = 0.0
    tree.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    group_in = tree.nodes.new('NodeGroupInput')
    group_in.location = (-500, 0)
    group_out = tree.nodes.new('NodeGroupOutput')
    group_out.location = (300, 0)

    spline_length = tree.nodes.new('GeometryNodeSplineLength')
    spline_length.location = (-500, -200)

    compare = tree.nodes.new('FunctionNodeCompare')
    compare.data_type = 'FLOAT'
    compare.operation = 'LESS_THAN'
    compare.location = (-250, -200)
    tree.links.new(spline_length.outputs["Length"], compare.inputs["A"])
    tree.links.new(group_in.outputs["Min Length"], compare.inputs["B"])

    delete = tree.nodes.new('GeometryNodeDeleteGeometry')
    delete.domain = 'CURVE'
    delete.mode = 'ALL'
    delete.location = (0, 0)
    tree.links.new(group_in.outputs["Geometry"], delete.inputs["Geometry"])
    tree.links.new(compare.outputs["Result"], delete.inputs["Selection"])
    tree.links.new(delete.outputs["Geometry"], group_out.inputs["Geometry"])

    return tree, min_length.identifier


def _build_lineart_collection(context, props):
    scene = context.scene

    collection = _ensure_datablock(bpy.data.collections, COL_LINEART,
                                   lambda: bpy.data.collections.new(COL_LINEART))
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)

    gp_data = _ensure_datablock(_gp_data_collection(), GP_CONTOURS,
                                lambda: _gp_data_collection().new(GP_CONTOURS))

    gp_object = _find_tagged(bpy.data.objects, GP_CONTOURS)
    if gp_object is None:
        gp_object = bpy.data.objects.new(GP_CONTOURS, gp_data)
        gp_object[ID_PROP] = GP_CONTOURS
    gp_object.name = GP_CONTOURS
    if gp_object.name not in collection.objects:
        collection.objects.link(gp_object)

    layer = None
    for candidate in gp_data.layers:
        if candidate.name == GP_LAYER_NAME:
            layer = candidate
            break
    if layer is None:
        layer = gp_data.layers.new(GP_LAYER_NAME)

    line_material = _build_gp_line_material(props)
    if line_material.name not in gp_data.materials:
        gp_data.materials.append(line_material)

    lineart = gp_object.modifiers.get(LINEART_MOD_NAME)
    if lineart is None:
        lineart = gp_object.modifiers.new(LINEART_MOD_NAME, 'LINEART')
    mod_visible = props.live_updates
    lineart.show_viewport = mod_visible
    lineart.show_render = True
    lineart.source_type = 'SCENE'
    lineart.use_contour = True
    lineart.use_crease = True
    lineart.use_intersection = True
    lineart.use_material = True
    lineart.use_loose = False
    lineart.crease_threshold = props.crease_angle
    lineart.radius = _lineart_base_radius(props)
    lineart.target_layer = layer.name
    lineart.target_material = line_material

    thickness_tree = _build_screen_space_thickness_group(props)
    thickness_mod = gp_object.modifiers.get(THICKNESS_MOD_NAME)
    if thickness_mod is None:
        thickness_mod = gp_object.modifiers.new(THICKNESS_MOD_NAME, 'NODES')
    thickness_mod.node_group = thickness_tree
    thickness_mod.show_viewport = mod_visible
    thickness_mod.show_render = True
    _sync_screen_space_thickness(scene, props, gp_object)

    filter_tree, min_length_id = _build_stroke_filter_group(props)
    stroke_filter = gp_object.modifiers.get(FILTER_MOD_NAME)
    if stroke_filter is None:
        stroke_filter = gp_object.modifiers.new(FILTER_MOD_NAME, 'NODES')
    stroke_filter.node_group = filter_tree
    stroke_filter[min_length_id] = props.min_stroke_length
    stroke_filter.show_viewport = mod_visible
    stroke_filter.show_render = True

    _ensure_modifier_order(
        gp_object,
        (LINEART_MOD_NAME, THICKNESS_MOD_NAME, FILTER_MOD_NAME),
    )

    return collection, gp_object


# ----------------------------------------------------------------------------
# View layers
# ----------------------------------------------------------------------------

def _layer_collection_for(layer_collection, collection):
    if layer_collection.collection == collection:
        return layer_collection
    for child in layer_collection.children:
        found = _layer_collection_for(child, collection)
        if found is not None:
            return found
    return None


def _ensure_view_layer(scene, name):
    view_layer = scene.view_layers.get(name)
    if view_layer is None:
        view_layer = scene.view_layers.new(name)
    return view_layer


def _configure_view_layers(context, lineart_collection, props):
    scene = context.scene
    override_shadow = _build_override_shadow(props)
    override_hard_shadow = _build_override_hard_shadow(props)
    grunge_mask = _build_grunge_mask(props)
    dark_world = _build_dark_world()

    vl_flat = _ensure_view_layer(scene, VL_FLAT)
    vl_shadow = _ensure_view_layer(scene, VL_SHADOW)
    vl_hard_shadow = _ensure_view_layer(scene, VL_HARD_SHADOW)
    vl_grunge = _ensure_view_layer(scene, VL_GRUNGE)
    vl_lines = _ensure_view_layer(scene, VL_LINES)

    # Passes that render meshes exclude the GP line collection; the lines pass
    # keeps everything enabled (Line Art needs the meshes in its depsgraph to
    # compute occlusion) but holds the mesh collections out so only strokes
    # remain in the image. Loose objects sitting directly in the scene root
    # collection can't be held out per-layer - keep scene content in
    # collections for a clean lines pass.
    for view_layer in (vl_flat, vl_shadow, vl_hard_shadow, vl_grunge):
        lc = _layer_collection_for(view_layer.layer_collection, lineart_collection)
        if lc is not None:
            lc.exclude = True

    for child in vl_lines.layer_collection.children:
        is_lineart = child.collection == lineart_collection
        child.exclude = False
        child.holdout = not is_lineart

    vl_flat.material_override = None
    vl_shadow.material_override = override_shadow
    vl_shadow.world_override = dark_world
    vl_hard_shadow.material_override = override_hard_shadow
    vl_hard_shadow.world_override = dark_world
    vl_grunge.material_override = grunge_mask
    vl_grunge.world_override = dark_world
    vl_lines.material_override = None

    for view_layer in (vl_shadow, vl_hard_shadow, vl_grunge, vl_lines):
        view_layer.samples = UTILITY_LAYER_SAMPLES

    return vl_flat, vl_shadow, vl_hard_shadow, vl_grunge, vl_lines


# ----------------------------------------------------------------------------
# Compositor
# ----------------------------------------------------------------------------

def _build_edge_detect_group(props):
    """Turn a flat black/white pass into smooth contour lines that match the
    Line Art look: binarize, take a morphological gradient (dilate minus erode
    with a round, distance-based structuring element - thickness in pixels),
    then SMAA anti-aliasing. Unlike a Laplace filter this produces no halo
    speckle and the line weight is controllable per pass."""
    tree = _ensure_datablock(
        bpy.data.node_groups, NG_EDGE_DETECT,
        lambda: bpy.data.node_groups.new(NG_EDGE_DETECT, 'CompositorNodeTree'),
    )
    _clear_node_tree(tree)

    tree.interface.new_socket("Image", in_out='INPUT', socket_type='NodeSocketColor')
    threshold = tree.interface.new_socket("Threshold", in_out='INPUT',
                                          socket_type='NodeSocketFloat')
    threshold.default_value = props.edge_threshold
    threshold.min_value = 0.0
    threshold.max_value = 1.0
    thickness = tree.interface.new_socket("Thickness", in_out='INPUT',
                                          socket_type='NodeSocketFloat')
    thickness.default_value = 4.0
    thickness.min_value = 1.0
    thickness.max_value = 32.0
    tree.interface.new_socket("Mask", in_out='OUTPUT', socket_type='NodeSocketFloat')

    group_in = tree.nodes.new('NodeGroupInput')
    group_in.location = (-900, 0)

    binarize = tree.nodes.new('ShaderNodeMath')
    binarize.operation = 'GREATER_THAN'
    binarize.location = (-650, 100)
    tree.links.new(group_in.outputs["Image"], binarize.inputs[0])
    tree.links.new(group_in.outputs["Threshold"], binarize.inputs[1])

    # Split the thickness across dilation (ceil) and erosion (floor) so odd
    # widths still work and thickness 1 gives a clean 1px outside line.
    half = tree.nodes.new('ShaderNodeMath')
    half.operation = 'DIVIDE'
    half.location = (-650, -150)
    half.inputs[1].default_value = 2.0
    tree.links.new(group_in.outputs["Thickness"], half.inputs[0])

    dilate_size = tree.nodes.new('ShaderNodeMath')
    dilate_size.operation = 'CEIL'
    dilate_size.location = (-450, -100)
    tree.links.new(half.outputs[0], dilate_size.inputs[0])

    erode_size = tree.nodes.new('ShaderNodeMath')
    erode_size.operation = 'FLOOR'
    erode_size.location = (-450, -250)
    tree.links.new(half.outputs[0], erode_size.inputs[0])

    erode_neg = tree.nodes.new('ShaderNodeMath')
    erode_neg.operation = 'MULTIPLY'
    erode_neg.location = (-250, -250)
    erode_neg.inputs[1].default_value = -1.0
    tree.links.new(erode_size.outputs[0], erode_neg.inputs[0])

    dilate = tree.nodes.new('CompositorNodeDilateErode')
    dilate.location = (-250, 150)
    _set_node_input(dilate, "Type", 'Distance')
    tree.links.new(binarize.outputs[0], dilate.inputs["Mask"])
    tree.links.new(dilate_size.outputs[0], dilate.inputs["Size"])

    erode = tree.nodes.new('CompositorNodeDilateErode')
    erode.location = (-250, -50)
    _set_node_input(erode, "Type", 'Distance')
    tree.links.new(binarize.outputs[0], erode.inputs["Mask"])
    tree.links.new(erode_neg.outputs[0], erode.inputs["Size"])

    gradient = tree.nodes.new('ShaderNodeMath')
    gradient.operation = 'SUBTRACT'
    gradient.use_clamp = True
    gradient.location = (0, 0)
    tree.links.new(dilate.outputs["Mask"], gradient.inputs[0])
    tree.links.new(erode.outputs["Mask"], gradient.inputs[1])

    smooth = tree.nodes.new('CompositorNodeAntiAliasing')
    smooth.location = (250, 0)
    tree.links.new(gradient.outputs[0], smooth.inputs["Image"])

    group_out = tree.nodes.new('NodeGroupOutput')
    group_out.location = (500, 0)
    tree.links.new(smooth.outputs["Image"], group_out.inputs["Mask"])
    return tree


def _over_white(tree, rl_node, x, y, name):
    """Composite a transparent-film render layer onto a flat white backdrop.

    Needed for fill/edge maths (ramps and mixes treat missing film as white).
    Final transparency is applied afterward via a combined alpha mask."""
    node, factor, a, b, result = _mix_rgba(tree)
    node.name = name
    node.location = (x, y)
    a.default_value = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(rl_node.outputs["Alpha"], factor)
    tree.links.new(rl_node.outputs["Image"], b)
    return result


def _math_maximum(tree, socket_a, socket_b, x, y, name):
    node = tree.nodes.new('ShaderNodeMath')
    node.name = name
    node.operation = 'MAXIMUM'
    node.location = (x, y)
    tree.links.new(socket_a, node.inputs[0])
    tree.links.new(socket_b, node.inputs[1])
    return node.outputs[0]


def _gate_mask(tree, mask, enable_socket, x, y, name):
    """Zero a mask when enable is off (for contour lines)."""
    gate = tree.nodes.new('ShaderNodeMath')
    gate.name = name
    gate.operation = 'MULTIPLY'
    gate.location = (x, y)
    tree.links.new(mask, gate.inputs[0])
    tree.links.new(enable_socket, gate.inputs[1])
    return gate.outputs[0]


def _mask_passthrough_when_off(tree, mask, enable_socket, x, y, name_prefix):
    """When enable is off, output 1 so the tint mix passes through unchanged."""
    inv_enable = tree.nodes.new('ShaderNodeMath')
    inv_enable.name = f"{name_prefix}_inv_enable"
    inv_enable.operation = 'SUBTRACT'
    inv_enable.location = (x - 120, y)
    inv_enable.inputs[0].default_value = 1.0
    tree.links.new(enable_socket, inv_enable.inputs[1])

    scaled = tree.nodes.new('ShaderNodeMath')
    scaled.name = f"{name_prefix}_scaled"
    scaled.operation = 'MULTIPLY'
    scaled.location = (x - 40, y)
    tree.links.new(mask, scaled.inputs[0])
    tree.links.new(enable_socket, scaled.inputs[1])

    result = tree.nodes.new('ShaderNodeMath')
    result.name = f"{name_prefix}_passthrough"
    result.operation = 'ADD'
    result.location = (x, y)
    tree.links.new(scaled.outputs[0], result.inputs[0])
    tree.links.new(inv_enable.outputs[0], result.inputs[1])
    return result.outputs[0]


def _composite_over(tree, background, foreground, x, y, name):
    """Composite foreground over background with standard alpha-over."""
    node = tree.nodes.new('CompositorNodeAlphaOver')
    node.name = name
    node.location = (x, y)
    bg_in = node.inputs.get("Background") or node.inputs.get("Image") or node.inputs[1]
    fg_in = node.inputs.get("Foreground") or node.inputs.get("Image_001") or node.inputs[2]
    tree.links.new(background, bg_in)
    tree.links.new(foreground, fg_in)
    return node.outputs.get("Image") or node.outputs[0]


def _lift_from_white_plate(tree, beauty, alpha, x, y, prefix="lzla"):
    """Undo 'composite over white' using a soft coverage alpha.

    beauty = fg * a + white * (1 - a)  →  fg = (beauty - white * (1 - a)) / a
    Without this, Set Alpha alone leaves a white halo in AA'd line edges.
    """
    inv_a = tree.nodes.new('ShaderNodeMath')
    inv_a.name = f"{prefix}_alpha_inv"
    inv_a.operation = 'SUBTRACT'
    inv_a.location = (x - 400, y - 120)
    inv_a.inputs[0].default_value = 1.0
    tree.links.new(alpha, inv_a.inputs[1])

    # white * (1 - a) as an RGB color
    white_part, factor, a_in, b_in, white_rgb = _mix_rgba(tree)
    white_part.name = f"{prefix}_white_part"
    white_part.location = (x - 220, y - 120)
    a_in.default_value = (0.0, 0.0, 0.0, 1.0)
    b_in.default_value = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(inv_a.outputs[0], factor)

    associated, factor, a_in, b_in, associated_rgb = _mix_rgba(tree, 'SUBTRACT')
    associated.name = f"{prefix}_lift_subtract"
    associated.location = (x - 40, y)
    factor.default_value = 1.0
    tree.links.new(beauty, a_in)
    tree.links.new(white_rgb, b_in)

    safe_a = tree.nodes.new('ShaderNodeMath')
    safe_a.name = f"{prefix}_alpha_safe"
    safe_a.operation = 'MAXIMUM'
    safe_a.location = (x - 400, y - 280)
    tree.links.new(alpha, safe_a.inputs[0])
    safe_a.inputs[1].default_value = 1e-4

    # 1 / safe_a, broadcast to RGB, multiply → straight color
    inv_safe = tree.nodes.new('ShaderNodeMath')
    inv_safe.name = f"{prefix}_alpha_rcp"
    inv_safe.operation = 'DIVIDE'
    inv_safe.location = (x - 220, y - 280)
    inv_safe.inputs[0].default_value = 1.0
    tree.links.new(safe_a.outputs[0], inv_safe.inputs[1])

    scale, factor, a_in, b_in, scale_rgb = _mix_rgba(tree)
    scale.name = f"{prefix}_alpha_scale_rgb"
    scale.location = (x - 40, y - 280)
    a_in.default_value = (0.0, 0.0, 0.0, 1.0)
    b_in.default_value = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(inv_safe.outputs[0], factor)

    straight, factor, a_in, b_in, straight_rgb = _mix_rgba(tree, 'MULTIPLY')
    straight.name = f"{prefix}_lift_straight"
    straight.location = (x + 160, y)
    factor.default_value = 1.0
    tree.links.new(associated_rgb, a_in)
    tree.links.new(scale_rgb, b_in)

    set_alpha = tree.nodes.new('CompositorNodeSetAlpha')
    set_alpha.name = f"{prefix}_set_alpha"
    set_alpha.location = (x + 360, y)
    if hasattr(set_alpha, "mode"):
        set_alpha.mode = 'REPLACE_ALPHA'
    else:
        _set_node_input(set_alpha, ("Type", "Mode"), 'REPLACE_ALPHA')
    image_in = set_alpha.inputs.get("Image") or set_alpha.inputs[0]
    alpha_in = set_alpha.inputs.get("Alpha") or set_alpha.inputs[1]
    tree.links.new(straight_rgb, image_in)
    tree.links.new(alpha, alpha_in)
    return set_alpha.outputs.get("Image") or set_alpha.outputs[0]


def _shadow_hard_mask(tree, shadow_white, edge_threshold, blur_radius, x, y,
                      prefix="lzla_shadow"):
    """Blur a toon shadow pass, hard-step at Edge Threshold, return a B&W mask.

    The same mask drives fill tinting and contour edge detection."""
    blur = tree.nodes.new('CompositorNodeBlur')
    blur.name = f"{prefix}_blur"
    blur.location = (x - 200, y)
    _set_node_input(blur, ("Size", "Size_X"), blur_radius)
    _set_node_input(blur, "Size_Y", blur_radius)
    tree.links.new(shadow_white, blur.inputs["Image"])

    binarize = tree.nodes.new('ShaderNodeMath')
    binarize.name = f"{prefix}_fill_mask"
    binarize.operation = 'GREATER_THAN'
    binarize.location = (x, y)
    tree.links.new(blur.outputs["Image"], binarize.inputs[0])
    binarize.inputs[1].default_value = edge_threshold

    hard, factor, a, b, hard_image = _mix_rgba(tree)
    hard.name = f"{prefix}_fill_hard"
    hard.location = (x + 180, y)
    a.default_value = (0.0, 0.0, 0.0, 1.0)
    b.default_value = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(binarize.outputs[0], factor)

    return hard_image, blur.outputs["Image"], binarize.outputs[0]


def _build_compositor(context, props):
    scene = context.scene
    if not hasattr(scene, "compositing_node_group"):
        raise RuntimeError("This Blender version has no scene.compositing_node_group "
                           "(Blender 5.0+ is required)")

    edge_group = _build_edge_detect_group(props)
    tree = _ensure_datablock(
        bpy.data.node_groups, NG_COMPOSITOR,
        lambda: bpy.data.node_groups.new(NG_COMPOSITOR, 'CompositorNodeTree'),
    )
    _clear_node_tree(tree)
    tree.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')

    render_layers = {}
    for row, (key, vl_name) in enumerate((
        ("flat", VL_FLAT), ("shadow", VL_SHADOW),
        ("hard_shadow", VL_HARD_SHADOW), ("grunge", VL_GRUNGE),
        ("lines", VL_LINES),
    )):
        node = tree.nodes.new('CompositorNodeRLayers')
        node.name = f"lzla_rl_{key}"
        node.scene = scene
        node.layer = vl_name
        node.location = (-1100, -350 * row)
        render_layers[key] = node

    # Beauty is still built over white (ramps/mixes need that). Flats and lines
    # are lifted to straight RGBA plates separately, then alpha-over combines them.
    flat_white = _over_white(tree, render_layers["flat"], -700, 0, "lzla_flat_over_white")
    shadow_white = _over_white(tree, render_layers["shadow"], -700, -350, "lzla_shadow_over_white")
    hard_shadow_white = _over_white(
        tree, render_layers["hard_shadow"], -700, -700, "lzla_hard_shadow_over_white",
    )
    grunge_white = _over_white(tree, render_layers["grunge"], -700, -1050, "lzla_grunge_over_white")
    _, shadow_for_edges, shadow_lit = _shadow_hard_mask(
        tree, shadow_white, props.edge_threshold, props.shadow_blur, -500, -350,
    )
    _, hard_shadow_for_edges, hard_shadow_lit = _shadow_hard_mask(
        tree, hard_shadow_white, props.edge_threshold, props.hard_shadow_blur,
        -500, -700, prefix="lzla_hard_shadow",
    )

    hard_shadow_enable = tree.nodes.new('ShaderNodeValue')
    hard_shadow_enable.name = "lzla_hard_shadow_enable"
    hard_shadow_enable.location = (-500, -850)
    hard_shadow_enable.outputs[0].default_value = 1.0 if props.hard_shadow_enabled else 0.0

    current = flat_white

    shadow_apply = _shadow_apply_mask(tree, shadow_lit, -350, -350, "lzla_shadow_apply")
    current = _masked_multiply_tint(
        tree, current, shadow_apply, props.shadow_color, -250, -350, "lzla_shadow",
    )

    hard_apply = _shadow_apply_mask(
        tree, hard_shadow_lit, -350, -700, "lzla_hard_shadow_apply",
    )
    hard_apply = _gate_mask(
        tree, hard_apply, hard_shadow_enable.outputs[0],
        -320, -700, "lzla_hard_shadow_apply_gate",
    )
    current = _masked_multiply_tint(
        tree, current, hard_apply, props.hard_shadow_color, -250, -700, "lzla_hard_shadow",
    )

    grunge_apply = _grunge_apply_mask(tree, grunge_white, -350, -1050, "lzla_grunge")
    current = _masked_multiply_tint(
        tree, current, grunge_apply, props.grunge_color, -250, -1050, "lzla_grunge",
    )

    # Everything up to here is the "flats" side: fills + shadow/grunge tints,
    # still composited over white. Kept for the split export.
    flats_white = current

    edge_masks = []
    lines_only = None  # GP strokes + contour lines on a white plate (split export)
    edge_passes = (
        ("shadow", shadow_for_edges, props.shadow_line_thickness,
         props.shadow_line_color, -350, -50),
        ("hard_shadow", hard_shadow_for_edges, props.hard_shadow_line_thickness,
         props.hard_shadow_line_color, -700, 50),
        ("grunge", grunge_white, props.grunge_line_thickness,
         props.grunge_line_color, -1050, 150),
    )
    for key, source, thickness, color, edge_y, line_x in edge_passes:
        edges = tree.nodes.new('CompositorNodeGroup')
        edges.node_tree = edge_group
        edges.name = f"lzla_{key}_edges"
        edges.location = (-450, edge_y)
        image_in = _group_input_socket(edges, "Image")
        if image_in is not None:
            tree.links.new(source, image_in)
        _set_group_input(edges, "Threshold", props.edge_threshold)
        _set_group_input(edges, "Thickness", thickness)
        edge_mask = edges.outputs["Mask"]
        if key == "hard_shadow":
            edge_mask = _gate_mask(
                tree, edge_mask, hard_shadow_enable.outputs[0],
                -300, edge_y, "lzla_hard_shadow_edge_gate",
            )
        edge_masks.append(edge_mask)

        split_mix, factor, a, b, split_out = _mix_rgba(tree)
        split_mix.name = f"lzla_split_{key}_line_mix"
        split_mix.location = (line_x, -1400)
        b.default_value = tuple(color)
        tree.links.new(edge_mask, factor)
        if lines_only is None:
            a.default_value = (1.0, 1.0, 1.0, 1.0)
        else:
            tree.links.new(lines_only, a)
        lines_only = split_out

    split_gp_mix, factor, a, b, split_lines_out = _mix_rgba(tree)
    split_gp_mix.name = "lzla_split_lines_gp_mix"
    split_gp_mix.location = (350, -1400)
    tree.links.new(render_layers["lines"].outputs["Alpha"], factor)
    tree.links.new(lines_only, a)
    tree.links.new(render_layers["lines"].outputs["Image"], b)

    lines_alpha = render_layers["lines"].outputs["Alpha"]
    for i, mask in enumerate(edge_masks):
        lines_alpha = _math_maximum(
            tree, lines_alpha, mask, 350, -1250 - 80 * i, f"lzla_lines_alpha_edge_{i}",
        )

    lines_final = _lift_from_white_plate(
        tree, split_lines_out, lines_alpha, 700, -1400, prefix="lzla_lines",
    )
    flats_final = _lift_from_white_plate(
        tree, flats_white, render_layers["flat"].outputs["Alpha"], 700, -1700,
        prefix="lzla_flats",
    )
    final = _composite_over(tree, flats_final, lines_final, 900, 0, "lzla_final_over")

    group_out = tree.nodes.new('NodeGroupOutput')
    group_out.location = (1100, 0)
    tree.links.new(final, group_out.inputs["Image"])

    viewer = tree.nodes.new('CompositorNodeViewer')
    viewer.location = (1100, -200)
    tree.links.new(final, viewer.inputs["Image"])

    def _make_file_out(name, label, y, sources):
        node = tree.nodes.new('CompositorNodeOutputFile')
        node.name = name
        node.label = label
        node.location = (1100, y)
        node.directory = "//"
        node.file_name = "lazy_lineart"
        while len(node.file_output_items) > 0:
            node.file_output_items.remove(node.file_output_items[0])
        # Link slots by name: the node keeps a virtual "add socket" input at
        # the end of node.inputs, so positional indexing silently links to it.
        for item_name, source in sources:
            item = node.file_output_items.new('RGBA', item_name)
            tree.links.new(source, node.inputs[item.name])
        fmt = node.format
        if hasattr(fmt, "media_type"):
            fmt.media_type = 'IMAGE'
        fmt.file_format = 'PNG'
        fmt.color_mode = 'RGBA'
        return node

    # Same image the Viewer sees — File Output writes it when you render.
    _make_file_out(FILE_OUT_NAME, "Lazy Line Art PNG", 200, (("Image", final),))
    # Split export: lines (GP + shadow/grunge contours) and flats (fills +
    # tints), each on its own transparent plate. Muted unless armed.
    split_out = _make_file_out(
        FILE_OUT_SPLIT_NAME, "Lazy Line Art Split PNGs", 500,
        (("lines", lines_final), ("flats", flats_final)),
    )
    split_out.mute = True

    scene.compositing_node_group = tree
    scene.render.use_compositing = True
    scene.render.film_transparent = True
    return tree


def _arm_file_output(scene, split=False):
    """Put a fresh timestamped path on the requested File Output node (next to
    the .blend) and mute the other one so only the requested files get written."""
    if not bpy.data.is_saved:
        return None, "Save the .blend first"
    comp = scene.compositing_node_group
    if comp is None or comp.get(ID_PROP) != NG_COMPOSITOR:
        return None, "Run Build Pipeline first"
    armed_name = FILE_OUT_SPLIT_NAME if split else FILE_OUT_NAME
    other_name = FILE_OUT_NAME if split else FILE_OUT_SPLIT_NAME
    node = comp.nodes.get(armed_name)
    if node is None:
        return None, "File Output missing - run Build Pipeline"
    other = comp.nodes.get(other_name)
    if other is not None:
        other.mute = True
    node.mute = False

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Absolute path to the .blend's folder - the relative "//" prefix has
    # proven unreliable on the File Output node in some setups.
    blend_dir = os.path.dirname(bpy.path.abspath(bpy.data.filepath))
    node.directory = blend_dir + os.sep
    # Split slots are named "lines"/"flats", so the stamp lands before them:
    # lazy_lineart_<stamp>_lines####.png / _flats####.png
    node.file_name = f"lazy_lineart_{stamp}_" if split else f"lazy_lineart_{stamp}"
    fmt = node.format
    if hasattr(fmt, "media_type"):
        fmt.media_type = 'IMAGE'
    fmt.file_format = 'PNG'
    fmt.color_mode = 'RGBA'
    return node.directory + node.file_name, None


# ----------------------------------------------------------------------------
# Settings sync (panel props -> existing datablocks, no creation)
# ----------------------------------------------------------------------------

def _sync_shadow_thresholds(scene):
    """Push shadow threshold sliders into the override material node trees."""
    props = scene.lazy_lineart_props

    shadow_mat = _find_tagged(bpy.data.materials, MAT_OVERRIDE_WHITE)
    if shadow_mat is None and scene.view_layers.get(VL_SHADOW) is not None:
        shadow_mat = _build_override_shadow(props)

    vl_shadow = scene.view_layers.get(VL_SHADOW)
    if vl_shadow is not None and shadow_mat is not None:
        if vl_shadow.material_override != shadow_mat:
            vl_shadow.material_override = shadow_mat

    mat = shadow_mat
    if vl_shadow is not None and vl_shadow.material_override is not None:
        mat = vl_shadow.material_override

    _set_material_threshold(
        mat, "lzla_shadow_threshold", "lzla_shadow_ramp", props.shadow_threshold,
    )

    hard_shadow_mat = _find_tagged(bpy.data.materials, MAT_OVERRIDE_HARD_SHADOW)
    if hard_shadow_mat is None and scene.view_layers.get(VL_HARD_SHADOW) is not None:
        hard_shadow_mat = _build_override_hard_shadow(props)

    vl_hard_shadow = scene.view_layers.get(VL_HARD_SHADOW)
    if vl_hard_shadow is not None and hard_shadow_mat is not None:
        if vl_hard_shadow.material_override != hard_shadow_mat:
            vl_hard_shadow.material_override = hard_shadow_mat

    hard_mat = hard_shadow_mat
    if vl_hard_shadow is not None and vl_hard_shadow.material_override is not None:
        hard_mat = vl_hard_shadow.material_override

    _set_material_threshold(
        hard_mat, "lzla_hard_shadow_threshold", "lzla_hard_shadow_ramp",
        props.hard_shadow_threshold,
    )


def _sync_hard_shadow_enabled(scene):
    """Push the enable checkbox into the hard-shadow material and compositor."""
    props = scene.lazy_lineart_props
    enabled_val = 1.0 if props.hard_shadow_enabled else 0.0

    hard_shadow_mat = _find_tagged(bpy.data.materials, MAT_OVERRIDE_HARD_SHADOW)
    if hard_shadow_mat is None and scene.view_layers.get(VL_HARD_SHADOW) is not None:
        hard_shadow_mat = _build_override_hard_shadow(props)

    vl_hard_shadow = scene.view_layers.get(VL_HARD_SHADOW)
    if vl_hard_shadow is not None and hard_shadow_mat is not None:
        if vl_hard_shadow.material_override != hard_shadow_mat:
            vl_hard_shadow.material_override = hard_shadow_mat

    hard_mat = hard_shadow_mat
    if vl_hard_shadow is not None and vl_hard_shadow.material_override is not None:
        hard_mat = vl_hard_shadow.material_override

    if hard_mat is not None and hard_mat.node_tree is not None:
        enable = hard_mat.node_tree.nodes.get("lzla_hard_shadow_enabled")
        if enable is None:
            hard_mat = _build_override_hard_shadow(props)
            if vl_hard_shadow is not None:
                vl_hard_shadow.material_override = hard_mat
            _set_material_threshold(
                hard_mat, "lzla_hard_shadow_threshold", "lzla_hard_shadow_ramp",
                props.hard_shadow_threshold,
            )
            enable = hard_mat.node_tree.nodes.get("lzla_hard_shadow_enabled")
        if enable is not None:
            enable.outputs[0].default_value = enabled_val
            hard_mat.update_tag()

    comp = _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)
    if comp is not None:
        enable_node = comp.nodes.get("lzla_hard_shadow_enable")
        if enable_node is not None:
            enable_node.outputs[0].default_value = enabled_val


def _sync_settings(scene):
    props = scene.lazy_lineart_props

    gp_object = _find_tagged(bpy.data.objects, GP_CONTOURS)
    if gp_object is not None:
        lineart = gp_object.modifiers.get(LINEART_MOD_NAME)
        if lineart is not None:
            lineart.crease_threshold = props.crease_angle
            lineart.radius = _lineart_base_radius(props)
        _sync_screen_space_thickness(scene, props, gp_object)
        stroke_filter = gp_object.modifiers.get(FILTER_MOD_NAME)
        if stroke_filter is not None and stroke_filter.node_group is not None:
            for item in stroke_filter.node_group.interface.items_tree:
                if item.item_type == 'SOCKET' and item.name == "Min Length":
                    stroke_filter[item.identifier] = props.min_stroke_length
                    break
        if props.live_updates:
            gp_object.update_tag()

    line_material = _find_tagged(bpy.data.materials, MAT_GP_LINES)
    if line_material is not None and line_material.is_grease_pencil:
        line_material.grease_pencil.color = tuple(props.line_color)

    _sync_shadow_thresholds(scene)
    _sync_hard_shadow_enabled(scene)

    grunge = _find_tagged(bpy.data.materials, MAT_GRUNGE_MASK)
    if grunge is not None and grunge.node_tree is not None:
        seed = grunge.node_tree.nodes.get("lzla_seed")
        if seed is not None:
            seed.outputs[0].default_value = props.grunge_seed
        patch_noise = grunge.node_tree.nodes.get("lzla_patch_noise")
        if patch_noise is not None:
            _configure_main_grunge_noise(patch_noise, props.grunge_patch_scale)
        patch_amp = grunge.node_tree.nodes.get("lzla_patch_amp")
        if patch_amp is not None:
            patch_amp.inputs[1].default_value = props.grunge_patch_amplitude
        rugged_noise = grunge.node_tree.nodes.get("lzla_rugged_noise")
        if rugged_noise is not None:
            _configure_main_grunge_noise(rugged_noise, props.grunge_rugged_scale)
        rugged_amp = grunge.node_tree.nodes.get("lzla_rugged_amp")
        if rugged_amp is not None:
            rugged_amp.inputs[1].default_value = props.grunge_rugged_amplitude
        blur_r = grunge.node_tree.nodes.get("lzla_grunge_noise_blur_r")
        if blur_r is not None:
            blur_r.outputs[0].default_value = props.grunge_noise_blur * GRUNGE_BLUR_RADIUS_SCALE
        scatter_noise = grunge.node_tree.nodes.get("lzla_grunge_noise")
        if scatter_noise is not None:
            _configure_grunge_noise(scatter_noise, props.grunge_noise_scale)
        for suffix in ("_px", "_nx", "_py", "_ny"):
            tap = grunge.node_tree.nodes.get(f"lzla_grunge_noise{suffix}")
            if tap is not None:
                _configure_grunge_noise(tap, props.grunge_noise_scale)
        scatter_amp = grunge.node_tree.nodes.get("lzla_grunge_noise_amp")
        if scatter_amp is not None:
            scatter_amp.inputs[1].default_value = props.grunge_noise_intensity
        scatter_ramp = grunge.node_tree.nodes.get("lzla_grunge_noise_ramp")
        if scatter_ramp is not None:
            _apply_scatter_ramp(scatter_ramp)
        ramp = grunge.node_tree.nodes.get("lzla_ramp")
        if ramp is not None:
            ramp.color_ramp.elements[1].position = props.grunge_threshold

    comp = _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)
    if comp is not None:
        for prefix, blur_radius in (
            ("lzla_shadow", props.shadow_blur),
            ("lzla_hard_shadow", props.hard_shadow_blur),
        ):
            blur = comp.nodes.get(f"{prefix}_blur")
            if blur is not None:
                _set_node_input(blur, ("Size", "Size_X"), blur_radius)
                _set_node_input(blur, "Size_Y", blur_radius)
            fill_mask = comp.nodes.get(f"{prefix}_fill_mask")
            if fill_mask is not None:
                fill_mask.inputs[1].default_value = props.edge_threshold
        for key, tint_color, line_thickness, line_color in (
            ("shadow", props.shadow_color, props.shadow_line_thickness,
             props.shadow_line_color),
            ("hard_shadow", props.hard_shadow_color, props.hard_shadow_line_thickness,
             props.hard_shadow_line_color),
            ("grunge", props.grunge_color, props.grunge_line_thickness,
             props.grunge_line_color),
        ):
            edges = comp.nodes.get(f"lzla_{key}_edges")
            if edges is not None:
                _set_group_input(edges, "Threshold", props.edge_threshold)
                _set_group_input(edges, "Thickness", line_thickness)
            line_mix = comp.nodes.get(f"lzla_split_{key}_line_mix")
            if line_mix is None:
                line_mix = comp.nodes.get(f"lzla_{key}_line_mix")
            if line_mix is not None:
                for socket in line_mix.inputs:
                    if socket.identifier == 'B_Color':
                        socket.default_value = tuple(line_color)
                        break
        _sync_compositor_tint_colors(scene)


# ----------------------------------------------------------------------------
# Deferred updates
#
# On heavy scenes the Line Art modifier is far too slow to recompute on every
# depsgraph tick, which is exactly what happens while dragging an object (or
# one of this panel's sliders - each drag tick used to write the modifier).
# Two debounce mechanisms fix that, both using the same "push a deadline
# forward, act once it stops moving" pattern:
#
# - Panel sliders don't touch any datablock until the value has been stable
#   for a beat, so a drag costs exactly one recompute, at the end.
# - A depsgraph watcher reacts to *external* scene edits (moving meshes, the
#   camera, edit mode...) by turning the Line Art modifiers' viewport
#   visibility off, then back on once the scene has been quiet for the
#   configured delay - again one recompute, when the interaction ends.
#   Strokes vanish while dragging; that's the trade-off.
#
# Updates on this add-on's own tagged datablocks (and on data Line Art cannot
# see: materials, worlds, lights) are ignored by the watcher, both to avoid
# pointless recomputes and to keep our own writes from re-triggering it.
# ----------------------------------------------------------------------------

_sync_state = {"deadline": 0.0, "scheduled": False}
_defer_state = {"deadline": 0.0, "scheduled": False, "suspended": False}
_live_updates_paused = False

SYNC_DEBOUNCE_SECONDS = 0.35
DEFER_POLL_SECONDS = 0.15


def _gp_line_modifiers():
    """Line Art + stroke-filter modifiers on the tagged GP object."""
    gp_object = _find_tagged(bpy.data.objects, GP_CONTOURS)
    if gp_object is None:
        return []
    return list(gp_object.modifiers)


def _refresh_gp_lines():
    gp_object = _find_tagged(bpy.data.objects, GP_CONTOURS)
    if gp_object is not None:
        gp_object.update_tag()


def _set_line_modifiers_visible(viewport, render):
    for mod in _gp_line_modifiers():
        mod.show_viewport = viewport
        mod.show_render = render


def _suspend_lineart_modifiers(*, live_off=False):
    global _live_updates_paused
    _live_updates_paused = live_off
    _set_line_modifiers_visible(False, True)
    _defer_state["suspended"] = True
    _defer_state["scheduled"] = False
    _defer_state["deadline"] = 0.0


def _restore_lineart_modifiers(*, force=False):
    global _live_updates_paused
    if _live_updates_paused and not force:
        return
    _live_updates_paused = False
    _set_line_modifiers_visible(True, True)
    _defer_state["suspended"] = False
    _defer_state["scheduled"] = False
    _defer_state["deadline"] = 0.0
    _refresh_gp_lines()


def _cancel_pending_sync():
    _sync_state["scheduled"] = False
    _sync_state["deadline"] = 0.0


def _on_live_updates_changed(self, context):
    if context is None:
        return
    if self.live_updates:
        _restore_lineart_modifiers(force=True)
        _cancel_pending_sync()
    else:
        _cancel_pending_sync()
        _suspend_lineart_modifiers(live_off=True)


def _run_sync_timer():
    if time.monotonic() < _sync_state["deadline"]:
        return DEFER_POLL_SECONDS
    _sync_state["scheduled"] = False
    props = bpy.context.scene.lazy_lineart_props
    if not props.live_updates:
        return None
    try:
        _sync_settings(bpy.context.scene)
    except Exception:
        traceback.print_exc()
    return None


def _sync_compositor_tint_colors(scene):
    props = scene.lazy_lineart_props
    comp = _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)
    if comp is None:
        return
    for key, tint_color in (
        ("shadow", props.shadow_color),
        ("hard_shadow", props.hard_shadow_color),
        ("grunge", props.grunge_color),
    ):
        _set_compositor_tint_color(comp, key, tuple(tint_color))


def _on_tint_color_changed(self, context):
    if context is None:
        return
    _sync_compositor_tint_colors(context.scene)


def _on_setting_changed(self, context):
    if not context.scene.lazy_lineart_props.live_updates:
        return
    _sync_state["deadline"] = time.monotonic() + SYNC_DEBOUNCE_SECONDS
    if not _sync_state["scheduled"]:
        _sync_state["scheduled"] = True
        bpy.app.timers.register(_run_sync_timer, first_interval=0.05)


def _apply_lineart_radius(context):
    """Push stroke/thickness settings onto the Line Art modifier."""
    scene = context.scene
    props = scene.lazy_lineart_props
    gp_object = _find_tagged(bpy.data.objects, GP_CONTOURS)
    if gp_object is None:
        return

    lineart = gp_object.modifiers.get(LINEART_MOD_NAME)
    if lineart is not None:
        lineart.radius = _lineart_base_radius(props)
    _sync_screen_space_thickness(scene, props, gp_object)

    if not props.live_updates:
        return

    gp_object.update_tag()
    view_layer = getattr(context, "view_layer", None)
    if view_layer is not None:
        view_layer.update()


def _on_stroke_radius_changed(self, context):
    if context is None:
        return
    _apply_lineart_radius(context)


def _on_reference_distance_changed(self, context):
    if context is None:
        return
    _apply_lineart_radius(context)


def _on_shadow_threshold_changed(self, context):
    if context is None:
        return
    _sync_shadow_thresholds(context.scene)


def _on_hard_shadow_threshold_changed(self, context):
    if context is None:
        return
    _sync_shadow_thresholds(context.scene)


def _on_hard_shadow_enabled_changed(self, context):
    if context is None:
        return
    _sync_hard_shadow_enabled(context.scene)


def _is_external_update(depsgraph):
    """True if this depsgraph event contains a change Line Art would recompute
    for: anything except our own tagged datablocks, and except data that can't
    move geometry in front of the camera (materials, worlds, lights)."""
    for update in depsgraph.updates:
        id_block = update.id
        if isinstance(id_block, (bpy.types.Scene, bpy.types.Material,
                                 bpy.types.World, bpy.types.Light)):
            continue
        if isinstance(id_block, bpy.types.Object) and id_block.type == 'LIGHT':
            continue
        try:
            if id_block.get(ID_PROP):
                continue
        except AttributeError:
            pass
        return True
    return False


def _run_defer_timer():
    props = bpy.context.scene.lazy_lineart_props
    if not props.live_updates or _live_updates_paused:
        _defer_state["scheduled"] = False
        return None

    modifiers = _gp_line_modifiers()
    if not modifiers:
        _defer_state["scheduled"] = False
        _defer_state["suspended"] = False
        return None

    if time.monotonic() < _defer_state["deadline"]:
        if not _defer_state["suspended"]:
            for mod in modifiers:
                mod.show_viewport = False
            _defer_state["suspended"] = True
        return DEFER_POLL_SECONDS

    if _defer_state["suspended"]:
        for mod in modifiers:
            mod.show_viewport = True
            mod.show_render = True
        _defer_state["suspended"] = False
        _refresh_gp_lines()
    _defer_state["scheduled"] = False
    return None


def _on_depsgraph_update(scene, depsgraph):
    props = getattr(scene, "lazy_lineart_props", None)
    if props is None or not props.live_updates or not props.use_deferred_updates:
        return
    if not _is_external_update(depsgraph):
        return
    _defer_state["deadline"] = time.monotonic() + props.update_delay
    if not _defer_state["scheduled"]:
        _defer_state["scheduled"] = True
        # Don't toggle modifier visibility from inside the handler - defer
        # even the suspension itself to a timer tick on the main loop.
        bpy.app.timers.register(_run_defer_timer, first_interval=0.0)


def _lazy_lineart_load_post(_dummy):
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return
    props = getattr(scene, "lazy_lineart_props", None)
    if props is not None and not props.live_updates:
        _suspend_lineart_modifiers(live_off=True)


# ----------------------------------------------------------------------------
# Operators
# ----------------------------------------------------------------------------

class LINEART_OT_toggle_updates(Operator):
    bl_idname = "lazylineart.toggle_updates"
    bl_label = "Toggle Live Updates"
    bl_description = ("Enable or disable all automatic Line Art updates: panel setting "
                      "sync and deferred scene-edit refresh")
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.lazy_lineart_props
        props.live_updates = not props.live_updates

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        return {'FINISHED'}


class LINEART_OT_build_pipeline(Operator):
    bl_idname = "lazylineart.build_pipeline"
    bl_label = "Build Pipeline"
    bl_description = ("Create (or converge) the Lazy Line Art scaffolding: collection, "
                      "Grease Pencil Line Art object, view layers with overrides, and "
                      "the compositor combine. Safe to re-run - tagged datablocks are "
                      "rewritten in place, never duplicated")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.lazy_lineart_props
        try:
            lineart_collection, _ = _build_lineart_collection(context, props)
            _configure_view_layers(context, lineart_collection, props)
            _build_compositor(context, props)
            _sync_settings(context.scene)
            if not props.live_updates:
                _suspend_lineart_modifiers(live_off=True)
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, f"Build failed: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, "Lazy Line Art pipeline ready - set materials, lights "
                              "and seeds, then Generate")
        return {'FINISHED'}


class LINEART_OT_generate(Operator):
    bl_idname = "lazylineart.generate"
    bl_label = "Generate Line Art"
    bl_description = ("Sync panel settings into the pipeline and refresh the Line Art "
                      "strokes (optionally baking them to keyframes)")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        props = scene.lazy_lineart_props

        if scene.camera is None:
            self.report({'ERROR'}, "The scene needs an active camera for Line Art")
            return {'CANCELLED'}

        gp_object = _find_tagged(bpy.data.objects, GP_CONTOURS)
        if gp_object is None or gp_object.modifiers.get(LINEART_MOD_NAME) is None:
            self.report({'ERROR'}, "Pipeline not built yet - run Build Pipeline first")
            return {'CANCELLED'}

        _sync_settings(scene)
        _restore_lineart_modifiers(force=True)
        gp_object.update_tag()
        context.view_layer.update()

        if props.bake_strokes:
            baked = False
            with context.temp_override(active_object=gp_object, object=gp_object,
                                       selected_objects=[gp_object]):
                for op_module, op_name in (("object", "lineart_bake_strokes"),
                                           ("grease_pencil", "lineart_bake_strokes")):
                    op = getattr(getattr(bpy.ops, op_module), op_name, None)
                    if op is None:
                        continue
                    try:
                        op()
                        baked = True
                        break
                    except (RuntimeError, AttributeError):
                        continue
            if not baked:
                self.report({'WARNING'}, "Strokes refreshed, but baking failed - "
                                         "bake manually from the Line Art modifier")
                if not props.live_updates:
                    _suspend_lineart_modifiers(live_off=True)
                return {'FINISHED'}
            self.report({'INFO'}, "Line Art baked to keyframes (modifier deactivated)")
            return {'FINISHED'}

        if not props.live_updates:
            _suspend_lineart_modifiers(live_off=True)

        self.report({'INFO'}, "Line Art refreshed")
        return {'FINISHED'}


class LINEART_OT_render_composite(Operator):
    bl_idname = "lazylineart.render_composite"
    bl_label = "Render Composite"
    bl_description = (
        "Render all view layers, combine through the compositor, and write a "
        "timestamped PNG next to the .blend via File Output"
    )

    def execute(self, context):
        scene = context.scene
        _sync_settings(scene)
        file_name, err = _arm_file_output(scene)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        scene.render.use_compositing = True
        bpy.ops.render.render('INVOKE_DEFAULT')
        self.report({'INFO'}, f"File Output → {file_name}Image.png")
        return {'FINISHED'}


class LINEART_OT_render_split(Operator):
    bl_idname = "lazylineart.render_split"
    bl_label = "Render Split (Lines / Flats)"
    bl_description = (
        "Render all view layers and write two timestamped transparent PNGs next "
        "to the .blend: one with all the lines (Line Art strokes plus shadow and "
        "grunge contours) and one with the flat colors and tints"
    )

    def execute(self, context):
        scene = context.scene
        _sync_settings(scene)
        file_name, err = _arm_file_output(scene, split=True)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        scene.render.use_compositing = True
        bpy.ops.render.render('INVOKE_DEFAULT')
        self.report({'INFO'}, f"File Output → {file_name}lines.png + flats")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Properties and panel
# ----------------------------------------------------------------------------

class LazyLineartProps(PropertyGroup):
    crease_angle: FloatProperty(
        name="Crease Threshold", subtype='ANGLE',
        default=math.radians(120.0), min=0.0, max=math.pi,
        description="Edges whose faces meet at less than this angle become crease lines",
        update=_on_setting_changed,
    )
    min_stroke_length: FloatProperty(
        name="Min Stroke Length", default=0.01, min=0.0, subtype='DISTANCE',
        description="Delete generated stroke chains shorter than this (scene units)",
        update=_on_setting_changed,
    )
    stroke_radius: FloatProperty(
        name="Stroke Radius", default=0.01, min=0.0, max=1.0, precision=4,
        description="Base Line Art thickness at the Reference Distance",
        update=_on_stroke_radius_changed,
    )
    thickness_reference_distance: FloatProperty(
        name="Reference Distance", default=10.0, min=0.01, subtype='DISTANCE',
        description="Camera-axis depth where Stroke Radius applies as-is; each stroke "
                    "point scales by (its view depth / this value)",
        update=_on_reference_distance_changed,
    )
    line_color: FloatVectorProperty(
        name="Line Color", subtype='COLOR', size=4,
        default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0,
        description="Color for Grease Pencil strokes and compositor contour lines",
        update=_on_setting_changed,
    )
    shadow_threshold: FloatProperty(
        name="Shadow Threshold", default=0.35, min=0.001, max=1.0,
        description="Diffuse light level below which the normal shadow pass darkens "
                    "flats before compositor blur and hardening at Edge Threshold",
        update=_on_shadow_threshold_changed,
    )
    shadow_blur: FloatProperty(
        name="Shadow Blur", default=2.0, min=0.0, max=32.0, subtype='PIXEL',
        description="Pixel blur applied to the normal shadow pass before the hard Edge "
                    "Threshold cut (fill and contour lines share this boundary)",
        update=_on_setting_changed,
    )
    shadow_color: FloatVectorProperty(
        name="Shadow Color", subtype='COLOR', size=4,
        default=(0.55, 0.55, 0.62, 1.0), min=0.0, max=1.0,
        description="Multiplier on flat colors in shaded regions (per-channel, preserves hue)",
        update=_on_tint_color_changed,
    )
    shadow_line_thickness: FloatProperty(
        name="Shadow Line Width", default=4.0, min=1.0, max=32.0, subtype='PIXEL',
        description="Thickness of the shadow contour lines, in pixels",
        update=_on_setting_changed,
    )
    shadow_line_color: FloatVectorProperty(
        name="Shadow Line Color", subtype='COLOR', size=4,
        default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0,
        description="Color of the shadow contour lines",
        update=_on_setting_changed,
    )
    hard_shadow_enabled: BoolProperty(
        name="Enable Hard Shadow", default=True,
        description="Apply the hard-shadow tint and contour pass on top of normal shadow",
        update=_on_hard_shadow_enabled_changed,
    )
    hard_shadow_threshold: FloatProperty(
        name="Hard Shadow Threshold", default=0.12, min=0.001, max=1.0,
        description="Diffuse light level below which the hard-shadow pass pushes flats "
                    "to black before compositor blur and hardening at Edge Threshold",
        update=_on_hard_shadow_threshold_changed,
    )
    hard_shadow_blur: FloatProperty(
        name="Hard Shadow Blur", default=1.0, min=0.0, max=32.0, subtype='PIXEL',
        description="Pixel blur applied to the hard-shadow pass before the hard Edge "
                    "Threshold cut (fill and contour lines share this boundary)",
        update=_on_setting_changed,
    )
    hard_shadow_color: FloatVectorProperty(
        name="Hard Shadow Color", subtype='COLOR', size=4,
        default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0,
        description="Multiplier on flat colors in the deepest shadow (per-channel)",
        update=_on_tint_color_changed,
    )
    hard_shadow_line_thickness: FloatProperty(
        name="Hard Shadow Line Width", default=4.0, min=1.0, max=32.0, subtype='PIXEL',
        description="Thickness of the hard-shadow contour lines, in pixels",
        update=_on_setting_changed,
    )
    hard_shadow_line_color: FloatVectorProperty(
        name="Hard Shadow Line Color", subtype='COLOR', size=4,
        default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0,
        description="Color of the hard-shadow contour lines",
        update=_on_setting_changed,
    )
    grunge_color: FloatVectorProperty(
        name="Grunge Color", subtype='COLOR', size=4,
        default=(0.8, 0.78, 0.72, 1.0), min=0.0, max=1.0,
        description="Tint multiplied onto the flat colors inside grunge regions",
        update=_on_tint_color_changed,
    )
    grunge_line_thickness: FloatProperty(
        name="Grunge Line Width", default=4.0, min=1.0, max=32.0, subtype='PIXEL',
        description="Thickness of the grunge contour lines, in pixels",
        update=_on_setting_changed,
    )
    grunge_line_color: FloatVectorProperty(
        name="Grunge Line Color", subtype='COLOR', size=4,
        default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0,
        description="Color of the grunge contour lines",
        update=_on_setting_changed,
    )
    grunge_threshold: FloatProperty(
        name="Grunge Threshold", default=0.52, min=0.0, max=10.0,
        description="Hard-step on the patch + rugged sum - lower covers more area",
        update=_on_setting_changed,
    )
    grunge_seed: FloatProperty(
        name="Grunge Seed", default=1.0,
        description="Global multiplier on each object's noise seed "
                    "(Random-based id + optional Pass Index)",
        update=_on_setting_changed,
    )
    grunge_patch_scale: FloatProperty(
        name="Grunge Patch Scale", default=1.0, min=0.0,
        description="Feature scale of the coarse patch noise",
        update=_on_setting_changed,
    )
    grunge_patch_amplitude: FloatProperty(
        name="Grunge Patch Amplitude", default=0.7, min=0.0, max=10.0,
        description="Weight of the patch noise in the summed field",
        update=_on_setting_changed,
    )
    grunge_rugged_scale: FloatProperty(
        name="Grunge Rugged Scale", default=15.0, min=0.0,
        description="Feature scale of the fine rugged noise",
        update=_on_setting_changed,
    )
    grunge_rugged_amplitude: FloatProperty(
        name="Grunge Rugged Amplitude", default=0.18, min=0.0, max=10.0,
        description="Weight of the rugged noise in the summed field",
        update=_on_setting_changed,
    )
    grunge_noise_scale: FloatProperty(
        name="Grunge Noise Scale", default=10.0, min=0.0,
        description="Scale of the scatter harmonic (higher than patch = smaller, more "
                    "isolated islands between clusters)",
        update=_on_setting_changed,
    )
    grunge_noise_intensity: FloatProperty(
        name="Grunge Noise Intensity", default=0.79, min=0.0, max=10.0,
        description="Weight of the scatter harmonic before its hard-step at 0.5",
        update=_on_setting_changed,
    )
    grunge_noise_blur: FloatProperty(
        name="Grunge Noise Blur", default=0.16, min=0.0, max=0.5,
        description="Box-blur radius applied to the scatter noise field before "
                    "the hard threshold at 0.5",
        update=_on_setting_changed,
    )
    edge_threshold: FloatProperty(
        name="Edge Threshold", default=0.5, min=0.0, max=1.0,
        description="Luminance cut for shadow/hard-shadow fill hardening and contour "
                    "detection on the shadow/grunge passes",
        update=_on_setting_changed,
    )
    bake_strokes: BoolProperty(
        name="Bake Strokes", default=False,
        description="Bake Line Art to keyframes on Generate. Baking deactivates the "
                    "modifier - leave off while iterating on stills",
    )
    live_updates: BoolProperty(
        name="Live Updates", default=False,
        description="When off, panel sliders and scene edits no longer refresh Line Art "
                    "until you run Generate or turn updates back on",
        update=_on_live_updates_changed,
    )
    use_deferred_updates: BoolProperty(
        name="Deferred Updates", default=True,
        description="While moving/editing objects or the camera, temporarily disable "
                    "the Line Art modifier and recompute once, after the scene has "
                    "been still for the Update Delay. Essential on heavy scenes",
    )
    update_delay: FloatProperty(
        name="Update Delay", default=0.8, min=0.1, max=5.0, subtype='TIME_ABSOLUTE',
        description="Quiet time (seconds) after the last scene change before Line Art "
                    "recomputes",
    )


class LINEART_PT_lazy_panel(Panel):
    bl_label = "Lazy Line Art"
    bl_idname = "LINEART_PT_lazy_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'

    def draw(self, context):
        layout = self.layout
        props = context.scene.lazy_lineart_props

        col = layout.column()
        col.scale_y = 1.4
        col.operator(LINEART_OT_build_pipeline.bl_idname, icon='MOD_BUILD')
        col.operator(LINEART_OT_generate.bl_idname, icon='MOD_LINEART')
        col.operator(LINEART_OT_render_composite.bl_idname, icon='RENDER_STILL')
        col.operator(LINEART_OT_render_split.bl_idname, icon='RENDERLAYERS')

        layout.separator()
        layout.label(text="Strokes:")
        col = layout.column(align=True)
        col.prop(props, "crease_angle")
        col.prop(props, "min_stroke_length")
        col.prop(props, "stroke_radius")
        col.prop(props, "thickness_reference_distance")
        col.prop(props, "line_color")
        col.prop(props, "bake_strokes")

        layout.separator()
        layout.label(text="Shadow Pass:")
        col = layout.column(align=True)
        col.prop(props, "shadow_threshold")
        col.prop(props, "shadow_blur")
        col.prop(props, "shadow_color")
        col.prop(props, "shadow_line_thickness")
        col.prop(props, "shadow_line_color")

        layout.separator()
        layout.label(text="Hard Shadow Pass:")
        col = layout.column(align=True)
        col.prop(props, "hard_shadow_enabled")
        sub = col.column(align=True)
        sub.enabled = props.hard_shadow_enabled
        sub.prop(props, "hard_shadow_threshold")
        sub.prop(props, "hard_shadow_blur")
        sub.prop(props, "hard_shadow_color")
        sub.prop(props, "hard_shadow_line_thickness")
        sub.prop(props, "hard_shadow_line_color")

        layout.separator()
        layout.label(text="Grunge Pass:")
        col = layout.column(align=True)
        col.prop(props, "grunge_seed")
        col.prop(props, "grunge_threshold")
        col.label(text="Patch Noise:")
        col.prop(props, "grunge_patch_scale")
        col.prop(props, "grunge_patch_amplitude")
        col.label(text="Rugged Noise:")
        col.prop(props, "grunge_rugged_scale")
        col.prop(props, "grunge_rugged_amplitude")
        col.label(text="Grunge Noise:")
        col.prop(props, "grunge_noise_scale")
        col.prop(props, "grunge_noise_intensity")
        col.prop(props, "grunge_noise_blur")
        col.prop(props, "grunge_color")
        col.prop(props, "grunge_line_thickness")
        col.prop(props, "grunge_line_color")

        layout.separator()
        layout.label(text="Contour Detection:")
        col = layout.column(align=True)
        col.prop(props, "edge_threshold")

        layout.separator()
        layout.label(text="Performance:")
        toggle_text = "Live Updates: ON" if props.live_updates else "Live Updates: OFF"
        row = layout.row()
        row.scale_y = 1.6
        row.operator(
            LINEART_OT_toggle_updates.bl_idname,
            text=toggle_text,
            depress=props.live_updates,
            icon='PLAY' if props.live_updates else 'PAUSE',
        )
        col = layout.column(align=True)
        col.enabled = props.live_updates
        col.prop(props, "use_deferred_updates")
        sub = col.column(align=True)
        sub.enabled = props.use_deferred_updates
        sub.prop(props, "update_delay")


classes = (
    LazyLineartProps,
    LINEART_OT_toggle_updates,
    LINEART_OT_build_pipeline,
    LINEART_OT_generate,
    LINEART_OT_render_composite,
    LINEART_OT_render_split,
    LINEART_PT_lazy_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.lazy_lineart_props = bpy.props.PointerProperty(type=LazyLineartProps)

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _lazy_lineart_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_lazy_lineart_load_post)
    _lazy_lineart_load_post(None)


def unregister():
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _lazy_lineart_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_lazy_lineart_load_post)
    _restore_lineart_modifiers(force=True)
    _cancel_pending_sync()

    del bpy.types.Scene.lazy_lineart_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
