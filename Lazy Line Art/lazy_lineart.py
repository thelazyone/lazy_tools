bl_info = {
    "name": "Lazy Line Art",
    "author": "thelazyone",
    "version": (0, 2, 1),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "One-click NPR pipeline: collections, view layers with overrides, "
                   "Grease Pencil Line Art with stroke filtering, and a compositor combine.",
    "warning": "Early prototype targeting Blender 5.1+",
    "doc_url": "https://github.com/thelazyone/lazy_tools",
    "category": "3D View",
}

import math
import json
import os
import sys
import time
import traceback
from datetime import datetime

import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
)

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
VL_GRUNGE = f"{TAG}.vl.grunge"
VL_LINES = f"{TAG}.vl.lines"
MAT_OVERRIDE_SHADOW = f"{TAG}.mat.override_shadow"
MAT_GRUNGE_MASK = f"{TAG}.mat.grunge_mask"
MAT_GP_LINES = f"{TAG}.mat.gp_lines"
WORLD_DARK = f"{TAG}.world.dark"
NG_STROKE_FILTER = f"{TAG}.ng.stroke_filter"
NG_SCREEN_SPACE = f"{TAG}.ng.screen_space"
NG_EDGE_DETECT = f"{TAG}.ng.edge_detect"
NG_COMPOSITOR = f"{TAG}.ng.compositor"
FILE_OUT_NAME = "lzla_file_out"
FILE_OUT_SPLIT_NAME = "lzla_file_out_split"
FILE_OUT_PATHS_NAME = "lzla_file_out_paths"
FILE_OUT_IDS_NAME = "lzla_file_out_collection_ids"

# Retired tags, kept only so scenes built by an older version can be migrated:
# the hard-shadow pass rendered an image identical to the normal shadow pass
# (both are now a plain lighting-luminance map, cut at different thresholds in
# the compositor), and the shadow override used to be called "override_white"
# back when it emitted a stepped white mask rather than luminance.
LEGACY_VL_HARD_SHADOW = f"{TAG}.vl.hard_shadow"
LEGACY_MAT_OVERRIDE_WHITE = f"{TAG}.mat.override_white"
LEGACY_MAT_OVERRIDE_HARD_SHADOW = f"{TAG}.mat.override_hard_shadow"

# The view layers this add-on owns. They are render plumbing, so none of them
# ever counts as the layer whose collection checkboxes describe user intent.
LZLA_VIEW_LAYER_NAMES = frozenset((
    VL_FLAT, VL_SHADOW, VL_GRUNGE, VL_LINES, LEGACY_VL_HARD_SHADOW,
))
# Marks a collection whose Line Art usage we set from its LOD preset (or
# switched to EXCLUDE because it is inactive), so a usage the user set by hand
# ("Occlusion Only", ...) is never overwritten.
AUTO_EXCLUDE_PROP = "lazy_lineart_auto_exclude"
# Stamped on the objects of every collection whose grunge scale isn't 1, and
# read back by an Attribute node inside the grunge override material.
LOD_SCALE_PROP = "lazy_lineart_grunge_scale"
# Marks an object whose per-object crease override we set, so a Contours Only
# collection can be undone without clobbering an override set by hand.
AUTO_CREASE_PROP = "lazy_lineart_auto_crease"

# Per-collection level of detail. Line Art solves occlusion once for the whole
# scene, so these are the four participation modes a single shared modifier can
# vary per collection - splitting into one modifier per collection costs more
# time than it saves and loses cross-collection occlusion.
LOD_USAGE = {
    'HERO': 'INCLUDE',
    'MID': 'NO_INTERSECTION',
    'CONTOUR': 'NO_INTERSECTION',
    'BACKGROUND': 'OCCLUSION_ONLY',
    'OFF': 'EXCLUDE',
}
# Contours Only has no equivalent in lineart_usage - the modifier's edge types
# are global. It is built instead from the per-object crease override, which
# does resolve per object: threshold 0 means no angle is creased at all, so
# only silhouettes survive. (The angle reads the other way round from what you
# might guess - measured: 0 deg = no creases, 179 deg = more creases than the
# 140 deg default.)
LOD_CONTOURS_ONLY = 'CONTOUR'

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
# How far shadow/grunge tints pull flats toward the picked hue after darkening.
# 0 = luminance darken only (keep base hue); 1 = full COLOR replace.
TINT_HUE_MIX = 0.55


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


def _blur_radius_sockets(node):
    """Find a Blur node's X/Y radius sockets, whatever this version calls them.

    Matched by shape rather than a fixed list, because guessing spellings is
    how this went wrong before: any input whose name mentions an axis is a
    radius socket, and a bare "Size" is the multiplier, never the radius."""
    found = []
    for socket in node.inputs:
        name = socket.name.strip().lower().replace("_", " ")
        if name in ("size x", "size y", "sizex", "sizey", "x", "y"):
            found.append(socket)
    return found


def _set_blur_radius(node, radius):
    """Set a compositor Blur node's pixel radius, whatever layout it has.

    Worth spelling out, because getting this wrong is silent and looks exactly
    like "blur does nothing" - which is precisely what happened on 5.x until
    0.1.51. There are two incompatible layouts:

    - Blender 5.x: one 2D **vector** ``Size`` socket that *is* the X/Y radius
      in pixels. Handled first, and returned from directly.
    - Earlier: separate X/Y radius properties or sockets, where the float
      ``Size`` socket is a **multiplier** on them, not the radius itself.
      Writing the radius into Size while X and Y sit at their default of 0
      gives radius * 0 = no blur at all, and nothing reports a problem.

    Order matters. Sockets win, because a version that moved these options to
    sockets may still expose `size_x` as a property that accepts a write and
    then ignores it - which reads back fine and fools any check. Properties are
    the fallback, and the bare Size socket is treated as the radius only when
    the node has no axis inputs at all. Every fallback says so on the console,
    so a wrong guess surfaces instead of quietly flattening the blur."""
    radius = max(0.0, float(radius))
    label = getattr(node, "name", node)

    # Blender 5.x dropped size_x/size_y and the float Size multiplier, and packs
    # the X/Y radius into a single 2D *vector* socket - still in pixels. Being a
    # radius rather than a multiplier, it must not be neutralised to 1.0 after
    # the write, which is why this returns straight out instead of falling
    # through to the shared tail below. Writing a float into it raises
    # TypeError, which is exactly how smoothing came to do nothing at all.
    size_socket = node.inputs.get("Size")
    if size_socket is not None and getattr(size_socket, "type", "") == 'VECTOR':
        for value in ((radius, radius), (radius, radius, 0.0)):
            try:
                size_socket.default_value = value
                return True
            except (TypeError, ValueError):
                continue
        print(f"[Lazy Line Art] Blur node {label!r} has a vector Size socket that "
              f"rejected ({radius}, {radius}) - the pass will not be blurred")
        return False

    axis_sockets = _blur_radius_sockets(node)
    written = False

    if axis_sockets:
        for socket in axis_sockets:
            try:
                socket.default_value = radius
                written = True
            except (TypeError, ValueError):
                pass
    else:
        for attr in ("size_x", "size_y"):
            if not hasattr(node, attr):
                continue
            try:
                setattr(node, attr, int(round(radius)))
            except (TypeError, ValueError):
                continue
            if getattr(node, attr) == int(round(radius)):
                written = True

    if hasattr(node, "use_relative"):
        try:
            node.use_relative = False
        except (TypeError, ValueError):
            pass
    _set_node_input(node, ("Relative", "Use Relative"), False)

    if written:
        # The radius is set above, so the multiplier has to be neutral. Graphs
        # built by an older version have the radius sitting in here instead.
        _set_node_input(node, "Size", 1.0)
        return True

    has_size = node.inputs.get("Size") is not None
    if not axis_sockets and not has_size:
        print(f"[Lazy Line Art] Blur node {label!r} exposes no radius input; "
              f"inputs are {[s.name for s in node.inputs]} - pass not blurred")
        return False

    # No axis target anywhere: on this build Size must itself be the radius.
    if _set_node_input(node, "Size", radius):
        print(f"[Lazy Line Art] Blur node {label!r} has no X/Y radius input, "
              f"using Size as the radius; inputs are {[s.name for s in node.inputs]}")
        return True

    print(f"[Lazy Line Art] Blur radius could not be set on {label!r}; inputs "
          f"are {[s.name for s in node.inputs]} - the pass will not be blurred")
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


def _tint_luminance(rgba):
    """Rec.709 luminance of an RGB(A) tint — used to darken without killing hue."""
    return 0.2126 * rgba[0] + 0.7152 * rgba[1] + 0.0722 * rgba[2]


def _masked_multiply_tint(tree, base, apply_mask, tint_rgba, x, y, prefix):
    """Where apply_mask=1, darken flats then blend in the tint's hue/sat.

    1. Multiply by tint luminance — darkens without killing base chroma.
    2. COLOR-blend the tint at that same value — candidate with the picked hue.
    3. Mix those two — both base and tint contribute (not a full replace).
    """
    alpha = tint_rgba[3] if len(tint_rgba) > 3 else 1.0
    lum = _tint_luminance(tint_rgba)

    mult, m_factor, a, b, darkened = _mix_rgba(tree, 'MULTIPLY')
    mult.name = f"{prefix}_mult"
    mult.location = (x - 480, y)
    m_factor.default_value = 1.0
    b.default_value = (lum, lum, lum, alpha)
    tree.links.new(base, a)

    color, c_factor, a, b, shifted = _mix_rgba(tree, 'COLOR')
    color.name = f"{prefix}_color"
    color.location = (x - 320, y)
    c_factor.default_value = 1.0
    b.default_value = (tint_rgba[0], tint_rgba[1], tint_rgba[2], alpha)
    tree.links.new(darkened, a)

    cast, cast_factor, a, b, combined = _mix_rgba(tree)
    cast.name = f"{prefix}_cast"
    cast.location = (x - 160, y)
    cast_factor.default_value = TINT_HUE_MIX
    tree.links.new(darkened, a)
    tree.links.new(shifted, b)

    pick, factor, a, b, result = _mix_rgba(tree)
    pick.name = f"{prefix}_pick"
    pick.location = (x, y)
    tree.links.new(base, a)
    tree.links.new(combined, b)
    tree.links.new(apply_mask, factor)
    return result



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
    color = (rgba[0], rgba[1], rgba[2], rgba[3] if len(rgba) > 3 else 1.0)
    mult = comp.nodes.get(f"lzla_{key}_mult")
    hue = comp.nodes.get(f"lzla_{key}_color")
    # New graph: luminance darken + COLOR candidate + mix toward tint hue.
    if hue is not None:
        updated = _set_mix_b_color(hue, color)
        if mult is not None:
            lum = _tint_luminance(color)
            updated = _set_mix_b_color(mult, (lum, lum, lum, color[3])) or updated
        return updated
    # Legacy graph: single per-channel multiply.
    if mult is not None and _set_mix_b_color(mult, color):
        return True
    rgb = comp.nodes.get(f"lzla_{key}_tint_color")
    if rgb is not None:
        if hasattr(rgb, "color"):
            rgb.color = color[:3]
        rgb.outputs[0].default_value = color
        return True
    tint = comp.nodes.get(f"lzla_{key}_tint")
    if tint is not None:
        color_inputs = [s for s in tint.inputs if s.type == 'RGBA']
        if color_inputs:
            color_inputs[0].default_value = color
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



def _lineart_modifier():
    """The Line Art modifier on the tagged Grease Pencil object, if built."""
    gp_object = _find_tagged(bpy.data.objects, GP_CONTOURS)
    if gp_object is None:
        return None
    return gp_object.modifiers.get(LINEART_MOD_NAME)


def _apply_lineart_quality(lineart, props, *, full=False):
    """Apply artistic edge choices; preview may additionally skip intersections."""
    lineart.use_contour = props.use_contours
    lineart.use_crease = props.use_creases
    lineart.use_material = props.use_material_boundaries
    lineart.use_edge_mark = props.use_marked_edges
    intersection = props.use_intersections and (full or props.preview_intersections)
    if lineart.use_intersection != intersection:
        lineart.use_intersection = intersection
    if lineart.use_back_face_culling != props.use_back_face_culling:
        lineart.use_back_face_culling = props.use_back_face_culling


def _lineart_base_radius(props):
    """Base Line Art radius at Reference Distance (per-point scaling is in GN).

    The global Thickness Multiplier rides here rather than on the slider, so
    the per-pass widths you dialled in stay where you left them and one number
    scales the lot - render a group, change the multiplier, render the next."""
    return max(props.stroke_radius, 0.0) * max(props.thickness_multiplier, 0.0)


def _contour_thickness(props, thickness):
    """A contour pass's line width in pixels, multiplier included.

    The edge detector's Thickness input is a dilate/erode radius and stops
    meaning anything below one pixel, so the multiplier cannot take a line all
    the way to nothing - set the pass's own thickness to 0 for that."""
    return max(thickness * max(props.thickness_multiplier, 0.0), 1.0)


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


def _toon_step_ramp(tree, luminance, threshold, name, x, y):
    """A constant-interpolation Color Ramp: 0 below threshold, 1 above.

    A Color Ramp rather than a Math GREATER_THAN on purpose - it is the node a
    toon shader is expected to have, it shows the step as a visible band you can
    drag in the shader editor, and it updates the 3D viewport live."""
    ramp = tree.nodes.new('ShaderNodeValToRGB')
    ramp.name = name
    ramp.location = (x, y)
    ramp.color_ramp.interpolation = 'CONSTANT'
    elements = ramp.color_ramp.elements
    while len(elements) > 2:
        elements.remove(elements[-1])
    elements[0].position = 0.0
    elements[0].color = (0.0, 0.0, 0.0, 1.0)
    elements[1].position = max(min(threshold, 1.0), 0.0)
    elements[1].color = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(luminance, ramp.inputs["Fac"])
    return ramp.outputs["Color"]


def _link_toon_levels(tree, luminance, props, x, y, to_scalar=None):
    """Two constant ramps on one luminance, summed and halved into three levels.

        0.0 = below Hard Shadow Threshold (deep shadow)
        0.5 = below Shadow Threshold      (normal shadow)
        1.0 = lit

    Built identically in a shader tree or a compositor tree - the only thing
    that changes is where the light gets stepped, which is what Shadow Step
    selects. `to_scalar` converts a ramp's Color output to a value when the
    tree needs that spelled out (compositor); shader trees convert implicitly.

    Luminance is divided by Light Level first, and that division is what makes
    the thresholds mean anything. Lighting is scene-referred and unbounded: a
    100 W area light a few centimetres from a miniature puts the lit surface at
    a luminance of 20, not 1. A Color Ramp stop can only sit between 0 and 1, so
    without the divide every threshold on the slider is far below the darkest
    part of the model, every pixel reads as lit, and the shadow silently does
    nothing. Light Level says which luminance counts as fully lit and rescales
    the signal into the range the ramps can actually address."""
    level_scale = tree.nodes.new('ShaderNodeMath')
    level_scale.name = "lzla_light_level"
    level_scale.operation = 'DIVIDE'
    level_scale.location = (x - 200, y)
    level_scale.inputs[1].default_value = max(props.shadow_light_level, 1e-3)
    tree.links.new(luminance, level_scale.inputs[0])
    luminance = level_scale.outputs[0]

    soft = _toon_step_ramp(
        tree, luminance, props.shadow_threshold, "lzla_shadow_ramp", x, y + 160,
    )
    hard = _toon_step_ramp(
        tree, luminance, props.hard_shadow_threshold,
        "lzla_hard_shadow_ramp", x, y - 160,
    )
    if to_scalar is not None:
        soft = to_scalar(soft, x + 300, y + 160, "lzla_shadow_ramp_val")
        hard = to_scalar(hard, x + 300, y - 160, "lzla_hard_shadow_ramp_val")

    total = tree.nodes.new('ShaderNodeMath')
    total.name = "lzla_shadow_levels_add"
    total.operation = 'ADD'
    total.location = (x + 480, y)
    tree.links.new(soft, total.inputs[0])
    tree.links.new(hard, total.inputs[1])

    level = tree.nodes.new('ShaderNodeMath')
    level.name = "lzla_shadow_levels"
    level.operation = 'MULTIPLY'
    level.location = (x + 660, y)
    level.inputs[1].default_value = 0.5
    tree.links.new(total.outputs[0], level.inputs[0])
    return level.outputs[0]


def _build_override_shadow(props):
    """Override material for the shadow view layer. Two shapes, one per mode.

    SHADER mode steps the light here, with two constant ramps (see
    `_link_toon_levels`). The step is evaluated per render sample and the film
    averages the samples, so the boundary comes back anti-aliased for free, and
    the whole thing previews live in the 3D viewport.

    That only holds when the light is *hard*. EEVEE traces area-light shadows by
    sampling the emitter's surface, so a penumbra pixel is lit in some samples
    and shadowed in others; a per-sample step then averages into grey across the
    entire penumbra, and more samples only make the grey smoother. A sun at zero
    angle has no surface to sample, every sample agrees, and the step stays
    two-valued - which is exactly why a sun looks right and an area light does
    not.

    COMPOSITE mode is the answer for soft lights: emit the raw lighting
    luminance and let the compositor step the *converged* image, where the
    penumbra is a smooth gradient a threshold can cut cleanly."""
    mat = _ensure_material(MAT_OVERRIDE_SHADOW)
    tree = mat.node_tree
    tree.nodes.clear()

    diffuse = tree.nodes.new('ShaderNodeBsdfDiffuse')
    diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    diffuse.location = (-800, 0)

    to_rgb = tree.nodes.new('ShaderNodeShaderToRGB')
    to_rgb.location = (-600, 0)
    tree.links.new(diffuse.outputs["BSDF"], to_rgb.inputs["Shader"])

    to_bw = tree.nodes.new('ShaderNodeRGBToBW')
    to_bw.name = "lzla_light_luminance"
    to_bw.location = (-420, 0)
    tree.links.new(to_rgb.outputs["Color"], to_bw.inputs["Color"])

    if props.shadow_step_mode == 'SHADER':
        emission_source = _link_toon_levels(tree, to_bw.outputs["Val"], props, -240, 0)
        emission_x = 700
    else:
        emission_source = to_bw.outputs["Val"]
        emission_x = -240

    emission = tree.nodes.new('ShaderNodeEmission')
    emission.location = (emission_x, 0)
    tree.links.new(emission_source, emission.inputs["Color"])

    output = tree.nodes.new('ShaderNodeOutputMaterial')
    output.location = (emission_x + 200, 0)
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return mat


def _tilted_object_coords(tree, obj_socket, rotation, x, y, name, scale_socket=None):
    """Tilt object-space coordinates off the mesh axes to avoid linear banding.

    `scale_socket` scales the coordinates themselves, which is the same thing as
    scaling every noise fed by them - that is how a collection's LOD grunge
    scale reaches patch, rugged and scatter at once without relinking the
    per-noise Scale inputs the sliders keep writing."""
    mapping = tree.nodes.new('ShaderNodeMapping')
    mapping.name = name
    mapping.location = (x, y)
    mapping.inputs['Rotation'].default_value = rotation
    tree.links.new(obj_socket, mapping.inputs['Vector'])
    if scale_socket is not None:
        tree.links.new(scale_socket, mapping.inputs['Scale'])
    return mapping.outputs['Vector']


def _build_lod_grunge_scale(tree, x, y):
    """Per-collection grunge scale, read off the object being shaded.

    Objects only carry LOD_SCALE_PROP when their collection asks for a scale
    other than 1, and an Attribute node returns 0 for a property that isn't
    there - so a LESS_THAN test adds the missing 1.0 back. Object attributes do
    resolve through a view layer's material override, which is what lets this
    ride the single existing grunge pass instead of needing one per LOD."""
    attr = tree.nodes.new('ShaderNodeAttribute')
    attr.name = "lzla_lod_grunge_scale"
    attr.attribute_type = 'OBJECT'
    attr.attribute_name = f'["{LOD_SCALE_PROP}"]'
    attr.location = (x, y)

    missing = tree.nodes.new('ShaderNodeMath')
    missing.name = "lzla_lod_grunge_missing"
    missing.operation = 'LESS_THAN'
    missing.location = (x + 200, y - 120)
    missing.inputs[1].default_value = 1e-6
    tree.links.new(attr.outputs["Fac"], missing.inputs[0])

    total = tree.nodes.new('ShaderNodeMath')
    total.name = "lzla_lod_grunge_total"
    total.operation = 'ADD'
    total.location = (x + 400, y)
    tree.links.new(attr.outputs["Fac"], total.inputs[0])
    tree.links.new(missing.outputs[0], total.inputs[1])
    return total.outputs[0]


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

    lod_scale = _build_lod_grunge_scale(tree, -1500, 150)

    main_coords = _tilted_object_coords(
        tree, coords.outputs["Object"], GRUNGE_COORD_ROTATION_MAIN,
        -850, 0, "lzla_grunge_coords_main", lod_scale,
    )
    scatter_coords = _tilted_object_coords(
        tree, coords.outputs["Object"], GRUNGE_COORD_ROTATION_SCATTER,
        -850, -200, "lzla_grunge_coords_scatter", lod_scale,
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
    lineart.use_loose = False
    _apply_lineart_quality(lineart, props)
    lineart.crease_threshold = props.crease_angle
    lineart.chaining_image_threshold = props.chain_gaps
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


def _set_holdout_tree(layer_collection, holdout):
    """Recursively set holdout on a LayerCollection and all children.

    Exclusion is deliberately not touched here: it mirrors the working view
    layer's checkboxes (see `_sync_view_layer_exclusions`), and forcing it off
    would drag collections the user unchecked back into every render pass."""
    if layer_collection.exclude:
        return
    layer_collection.holdout = holdout
    for child in layer_collection.children:
        _set_holdout_tree(child, holdout)


def _apply_lines_holdouts(root_layer_collection, lineart_collection):
    """Lines pass: keep every active collection in the depsgraph, but hold out
    all mesh content so only Grease Pencil strokes hit the beauty buffer.

    Walks the full LayerCollection tree (not just direct scene children) so
    collections added later - e.g. LZRG.Render Groups and its group
    subcollections - get the same holdout treatment as the default Collection.
    The lineart collection branch is never held out.
    """
    for child in root_layer_collection.children:
        if child.collection == lineart_collection:
            # The strokes are the whole point of this pass: this is the one
            # branch that is force-included, whatever it was left at.
            if child.exclude:
                child.exclude = False
            _set_holdout_tree(child, False)
        else:
            _set_holdout_tree(child, True)


def _ensure_render_transparency(scene):
    """Holdouts become opaque black unless Film Transparent is on. Re-assert
    before every sync/render so a manual toggle or an old .blend can't stick."""
    scene.render.film_transparent = True
    scene.render.use_compositing = True
    try:
        scene.render.image_settings.color_mode = 'RGBA'
        scene.render.image_settings.file_format = 'PNG'
    except (AttributeError, TypeError):
        pass


# ----------------------------------------------------------------------------
# Active collections
#
# Everything the pipeline renders - and, above all, everything Line Art chews
# through on each refresh - is driven by which collections are *active* in the
# user's working view layer. A collection counts as inactive when it is
# unchecked there (`exclude`) or has renders switched off on the collection
# itself (the camera icon, which is what Lazy Render Groups toggles).
#
# The two viewport-only switches are left out on purpose: the eye is temporary
# hiding, and the monitor already drops the collection from the viewport
# depsgraph, so Line Art skips it in the preview for free while a render - the
# one place both switches are meant to be ignored - still includes it.
#
# Inactive collections are then switched off in two places, because one is not
# enough: excluding them from our view layers keeps them out of the render
# passes, while `collection.lineart_usage = 'EXCLUDE'` is what actually stops
# the Line Art modifier from loading their geometry - it walks the scene's
# collection tree itself, so an unchecked collection full of heavy meshes
# still costs minutes per refresh until its usage says otherwise.
# ----------------------------------------------------------------------------

def _reference_view_layer(scene):
    """The view layer whose collection checkboxes drive the pipeline.

    The user unchecks collections in their own working layer, not in ours, so
    prefer the active one when it isn't one of the add-on's, and otherwise fall
    back to the first layer we don't own."""
    try:
        active = bpy.context.view_layer
    except AttributeError:
        active = None
    if (active is not None and active.name not in LZLA_VIEW_LAYER_NAMES
            and scene.view_layers.get(active.name) == active):
        return active
    for view_layer in scene.view_layers:
        if view_layer.name not in LZLA_VIEW_LAYER_NAMES:
            return view_layer
    return scene.view_layers[0] if len(scene.view_layers) else None


def _collection_activity(scene):
    """Map every collection under the reference view layer to active/inactive.

    Inactivity is inherited by child collections, and a collection linked in
    several places is active as soon as one of its branches is."""
    activity = {}
    reference = _reference_view_layer(scene)
    if reference is None:
        return activity

    def walk(layer_collection, parent_active):
        for child in layer_collection.children:
            collection = child.collection
            active = (parent_active
                      and not child.exclude
                      and not collection.hide_render)
            activity[collection] = activity.get(collection, False) or active
            walk(child, active)

    walk(reference.layer_collection, True)
    return activity


def _collection_lod(collection):
    """The per-collection LOD settings, or None on a Blender that never got the
    registration (older file opened mid-upgrade)."""
    return getattr(collection, "lazy_lineart", None)


def _sync_lineart_usage(activity, lineart_collection):
    """Tell Line Art how much of each collection to trace.

    Inactive collections are dropped outright; active ones follow their LOD
    preset. Only usages this add-on wrote are ever reset, so one picked by hand
    on a collection left at Default keeps working."""
    for collection, active in activity.items():
        if collection == lineart_collection:
            continue
        try:
            usage = collection.lineart_usage
        except AttributeError:
            return  # Blender build without per-collection Line Art usage.

        desired = None
        if not active:
            desired = 'EXCLUDE'
        else:
            lod = _collection_lod(collection)
            if lod is not None and lod.preset != 'DEFAULT':
                desired = LOD_USAGE[lod.preset]

        if desired is not None:
            if usage != desired:
                collection.lineart_usage = desired
            if not collection.get(AUTO_EXCLUDE_PROP):
                collection[AUTO_EXCLUDE_PROP] = True
        elif collection.get(AUTO_EXCLUDE_PROP):
            if usage != 'INCLUDE':
                collection.lineart_usage = 'INCLUDE'
            del collection[AUTO_EXCLUDE_PROP]


def _stamp_grunge_scale(obj, scale):
    """Give one object its collection's grunge scale - or take it away again."""
    current = obj.get(LOD_SCALE_PROP)
    if scale == 1.0:
        if current is not None:
            del obj[LOD_SCALE_PROP]
    elif current != scale:
        obj[LOD_SCALE_PROP] = scale


def _stamp_contours_only(obj, contours_only):
    """Switch one object's crease lines off - or hand them back.

    Only overrides this add-on set are ever cleared, so a crease override put on
    an object by hand survives a collection going back to another preset."""
    lineart = getattr(obj, "lineart", None)
    if lineart is None:
        return
    owned = bool(obj.get(AUTO_CREASE_PROP))
    if contours_only:
        if lineart.use_crease_override and not owned:
            return  # Set by hand: same rule as a hand-set usage, leave it be.
        if not lineart.use_crease_override:
            lineart.use_crease_override = True
        if lineart.crease_threshold != 0.0:
            lineart.crease_threshold = 0.0
        if not obj.get(AUTO_CREASE_PROP):
            obj[AUTO_CREASE_PROP] = True
    elif owned:
        if lineart.use_crease_override:
            lineart.use_crease_override = False
        del obj[AUTO_CREASE_PROP]


def _sync_object_lod(scene):
    """Stamp the per-object half of each collection's LOD onto its objects.

    Two things live here because neither has a per-collection setting in
    Blender: the grunge scale (read back by an Attribute node in the override
    material) and crease suppression for Contours Only (the modifier's edge
    types are global, the per-object crease override is not).

    Both inherit down the tree - a child follows its parent until it asks for
    something of its own, which for the grunge scale means any value but 1 and
    for the preset means anything but Default. An object linked into two
    collections takes whichever branch is walked last."""
    reference = _reference_view_layer(scene)
    if reference is None:
        return

    def walk(layer_collection, scale, contours_only):
        for child in layer_collection.children:
            collection = child.collection
            lod = _collection_lod(collection)
            child_scale, child_contours = scale, contours_only
            if lod is not None:
                if lod.grunge_scale != 1.0:
                    child_scale = lod.grunge_scale
                if lod.preset != 'DEFAULT':
                    child_contours = lod.preset == LOD_CONTOURS_ONLY
            for obj in collection.objects:
                _stamp_grunge_scale(obj, child_scale)
                _stamp_contours_only(obj, child_contours)
            walk(child, child_scale, child_contours)

    walk(reference.layer_collection, 1.0, False)


def _sync_collection_lod(scene, activity=None):
    """Push every per-collection setting into the scene: Line Art participation,
    crease suppression and grunge scale. No view layer writes, so it is cheap enough to run before
    each live refresh - which is what keeps a collection that was just
    unchecked, or dropped to Background, from being traced one last time."""
    if activity is None:
        activity = _collection_activity(scene)
    _sync_lineart_usage(activity, _find_tagged(bpy.data.collections, COL_LINEART))
    _sync_object_lod(scene)


def _sync_view_layer_exclusions(view_layer, activity, lineart_collection):
    """Mirror the working layer's checkboxes onto one of our view layers.

    Per-pass exclusions set by hand on the add-on's own view layers are not
    preserved: the working layer is the single source of truth for what the
    pipeline renders. The lineart collection is skipped - its own branch is
    excluded per pass by the caller."""
    def walk(layer_collection):
        for child in layer_collection.children:
            if child.collection == lineart_collection:
                continue
            active = activity.get(child.collection)
            if active is not None and child.exclude == active:
                # Writing `exclude` rebuilds the depsgraph, so only write when
                # it actually differs (True while active, False while not).
                child.exclude = not active
            walk(child)

    walk(view_layer.layer_collection)


def _sync_view_layer_collection_flags(scene, lineart_collection=None):
    """Re-apply exclude/holdout flags on existing Lazy Line Art view layers.

    Safe to call before every render: newly created collections (render groups,
    extra scene collections) are picked up without rebuilding materials, and
    collections the user switched off in the working view layer are dropped
    from every pass as well as from Line Art.
    """
    if lineart_collection is None:
        lineart_collection = _find_tagged(bpy.data.collections, COL_LINEART)
    if lineart_collection is None:
        return False

    _ensure_render_transparency(scene)

    activity = _collection_activity(scene)
    _sync_collection_lod(scene, activity)
    for name in (VL_FLAT, VL_SHADOW, VL_GRUNGE, VL_LINES):
        view_layer = scene.view_layers.get(name)
        if view_layer is not None:
            _sync_view_layer_exclusions(view_layer, activity, lineart_collection)

    mesh_layers = (
        scene.view_layers.get(VL_FLAT),
        scene.view_layers.get(VL_SHADOW),
        scene.view_layers.get(VL_GRUNGE),
    )
    for view_layer in mesh_layers:
        if view_layer is None:
            continue
        lc = _layer_collection_for(view_layer.layer_collection, lineart_collection)
        if lc is not None and not lc.exclude:
            lc.exclude = True
        # Mesh passes must not inherit leftover holdouts from the lines pass
        # setup (holdout is per view layer, but clear explicitly for safety).
        _clear_holdouts(view_layer.layer_collection)

    vl_lines = scene.view_layers.get(VL_LINES)
    if vl_lines is not None:
        _apply_lines_holdouts(vl_lines.layer_collection, lineart_collection)
    return True


def _clear_holdouts(layer_collection):
    """Ensure mesh view layers don't keep holdout flags (transparent film only
    needs holdout on the lines pass)."""
    layer_collection.holdout = False
    for child in layer_collection.children:
        _clear_holdouts(child)


def _retag_datablock(collection, legacy_key, key):
    """Adopt a datablock tagged with an old key under its current one.

    Keeps the user's existing material (and anything pointing at it) instead of
    orphaning it and creating a duplicate next to it. No-op once a datablock
    already carries the current tag."""
    if _find_tagged(collection, key) is not None:
        return None
    legacy = _find_tagged(collection, legacy_key)
    if legacy is None:
        return None
    legacy[ID_PROP] = key
    legacy.name = key
    return legacy


def _retire_hard_shadow_layer(scene, delete=False):
    """Retire the separate hard-shadow view layer left by older versions.

    Both shadow tiers now read the same lighting pass and differ only in where
    the compositor cuts it, so this layer renders an image identical to
    LZLA.vl.shadow. Blender renders every enabled view layer, so leaving it
    active costs a full extra pass per frame for nothing.

    Deleting a view layer reshuffles the UI, so only Build Pipeline does that
    (`delete=True`); the slider paths just switch it off and let the next build
    clear it away."""
    view_layer = scene.view_layers.get(LEGACY_VL_HARD_SHADOW)
    if view_layer is None:
        return False
    view_layer.use = False
    if delete and len(scene.view_layers) > 1:
        try:
            scene.view_layers.remove(view_layer)
        except (RuntimeError, TypeError):
            pass  # Still in use by a window - it is disabled either way.
    return True


def _migrate_legacy_datablocks(scene, delete=False):
    """Bring a scene built by an older version up to the current layout.

    `delete` also frees the datablocks the old layout left orphaned; it is only
    passed from Build Pipeline, since removing IDs from a property update
    callback is not worth the risk."""
    _retag_datablock(bpy.data.materials, LEGACY_MAT_OVERRIDE_WHITE, MAT_OVERRIDE_SHADOW)
    _retire_hard_shadow_layer(scene, delete=delete)

    stale = _find_tagged(bpy.data.materials, LEGACY_MAT_OVERRIDE_HARD_SHADOW)
    if stale is not None and delete and stale.users == 0:
        bpy.data.materials.remove(stale)


def _configure_view_layers(context, lineart_collection, props):
    scene = context.scene
    _migrate_legacy_datablocks(scene, delete=True)

    override_shadow = _build_override_shadow(props)
    grunge_mask = _build_grunge_mask(props)
    dark_world = _build_dark_world()

    vl_flat = _ensure_view_layer(scene, VL_FLAT)
    vl_shadow = _ensure_view_layer(scene, VL_SHADOW)
    vl_grunge = _ensure_view_layer(scene, VL_GRUNGE)
    vl_lines = _ensure_view_layer(scene, VL_LINES)

    # Passes that render meshes exclude the GP line collection; the lines pass
    # keeps everything enabled (Line Art needs the meshes in its depsgraph to
    # compute occlusion) but holds the mesh collections out so only strokes
    # remain in the image. Loose objects sitting directly in the scene root
    # collection can't be held out per-layer - keep scene content in
    # collections for a clean lines pass. Nested trees (LZRG groups, etc.)
    # are walked recursively so holdout applies to the whole branch.
    _sync_view_layer_collection_flags(scene, lineart_collection)

    vl_flat.material_override = None
    vl_shadow.material_override = override_shadow
    vl_shadow.world_override = dark_world
    vl_grunge.material_override = grunge_mask
    vl_grunge.world_override = dark_world
    vl_lines.material_override = None

    for view_layer in (vl_grunge, vl_lines):
        view_layer.samples = UTILITY_LAYER_SAMPLES
    # The shadow pass is the only lit one, so it is the only one whose grain
    # ends up dithering a threshold - it gets its own, higher, sample budget.
    vl_shadow.samples = props.shadow_samples

    return vl_flat, vl_shadow, vl_grunge, vl_lines


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


def _set_alpha_replace(tree, image, alpha, x, y, name):
    """Set Alpha in REPLACE mode - the node's default is APPLY, which multiplies.

    Loud on failure: a silently-APPLY'd Set Alpha multiplies the incoming alpha
    instead of replacing it, and every plate downstream comes out wrong."""
    node = tree.nodes.new('CompositorNodeSetAlpha')
    node.name = name
    node.location = (x, y)
    ok = False
    if hasattr(node, "mode"):
        try:
            node.mode = 'REPLACE_ALPHA'
            ok = True
        except (TypeError, ValueError):
            pass
    if not ok:
        ok = _set_node_input(node, ("Type", "Mode", "Alpha Mode"), 'REPLACE_ALPHA')
    if not ok:
        ok = _set_node_input(node, ("Type", "Mode", "Alpha Mode"), 'Replace Alpha')
    if not ok:
        print("[Lazy Line Art] Set Alpha has no reachable REPLACE mode on "
              f"{name!r}; inputs are {[i.name for i in node.inputs]}")
    image_in = node.inputs.get("Image") or node.inputs[0]
    alpha_in = node.inputs.get("Alpha") or node.inputs[1]
    tree.links.new(image, image_in)
    tree.links.new(alpha, alpha_in)
    return node.outputs.get("Image") or node.outputs[0]


def _premultiplied_line_layer(tree, mask, color, x, y, prefix):
    """One contour pass as a premultiplied RGBA layer: colour * mask, alpha = mask.

    Premultiplied on purpose - it is the form Alpha Over consumes, and it keeps
    a layer's colour and its coverage derived from the same number, so they can
    never disagree about how much of a pixel the line covers."""
    node, factor, a_in, b_in, rgb = _mix_rgba(tree)
    node.name = f"{prefix}_rgb"
    node.location = (x, y)
    a_in.default_value = (0.0, 0.0, 0.0, 1.0)
    b_in.default_value = tuple(color)
    tree.links.new(mask, factor)
    return _set_alpha_replace(tree, rgb, mask, x + 180, y, f"{prefix}_set_alpha")


def _premultiplied_from_white_plate(tree, beauty, alpha, x, y, prefix="lzla"):
    """Recover a premultiplied RGBA layer from a plate composited over white.

        beauty = straight * a + white * (1 - a)
        premultiplied = straight * a = beauty - white * (1 - a)

    Note what is *not* here: a divide by alpha. The old version divided to get
    the straight colour and the consumer multiplied it straight back, a round
    trip whose only lasting effect was the reciprocal it fed through a Mix
    factor - and Mix clamps its factor to 1 by default, so for every partially
    covered pixel the divide quietly did nothing. Premultiplied is what Alpha
    Over wants anyway, so the whole detour comes out."""
    inv_a = tree.nodes.new('ShaderNodeMath')
    inv_a.name = f"{prefix}_alpha_inv"
    inv_a.operation = 'SUBTRACT'
    inv_a.location = (x - 400, y - 120)
    inv_a.inputs[0].default_value = 1.0
    tree.links.new(alpha, inv_a.inputs[1])

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

    return _set_alpha_replace(
        tree, associated_rgb, alpha, x + 200, y, f"{prefix}_set_alpha",
    )


def _shadow_level_masks(tree, shadow_white, props, x, y):
    """Split the three-level shadow pass back into one mask per tier.

    In SHADER mode the render layer already carries the levels. In COMPOSITE
    mode it carries raw lighting luminance and the same pair of ramps is built
    here instead, stepping the converged image - which is what makes soft
    area-light shadows resolve to a clean edge rather than a grey penumbra. The
    ramps stay real, draggable Color Ramp nodes either way; in this mode they
    live in the compositor, where the backdrop updates from the last render as
    you drag them.

    The pass carries 0.0 = deep shadow, 0.5 = normal shadow, 1.0 = lit.
    Recovering the tiers is arithmetic, not a further threshold, and that
    distinction is the whole reason this reads cleanly:

        normal-and-deeper = clamp((1 - level) * 2)
        deep-only         = clamp(1 - level * 2)

    Both are exact on the three levels, and *linear* on the anti-aliased pixels
    in between - so the sample-averaged edge the shader produced arrives intact
    instead of being re-quantised into stair-steps and dither.

    Smoothing is an optional blur ahead of all this. In SHADER mode leave it at
    zero - the shader already anti-aliases. In COMPOSITE mode it is the tool for
    residual render noise: it smooths the lighting *before* the step, so the
    threshold cuts a clean curve instead of chasing grain.

    Returns (normal-tier mask, deep-tier mask) as scalar 0..1 sockets."""
    # Always built, even at radius 0 (a true pass-through), so the slider stays
    # live without forcing a rebuild.
    blur = tree.nodes.new('CompositorNodeBlur')
    blur.name = "lzla_shadow_blur"
    blur.location = (x - 1400, y)
    _set_blur_radius(blur, props.shadow_smoothing)
    tree.links.new(shadow_white, blur.inputs["Image"])

    level = _rgb_to_luminance_value(
        tree, blur.outputs["Image"], x - 1220, y, "lzla_shadow_level",
    )
    if props.shadow_step_mode == 'COMPOSITE':
        def _to_scalar(color, sx, sy, name):
            return _rgb_to_luminance_value(tree, color, sx, sy, name)

        level = _link_toon_levels(tree, level, props, x - 1040, y, _to_scalar)

    inv = tree.nodes.new('ShaderNodeMath')
    inv.name = "lzla_shadow_level_inv"
    inv.operation = 'SUBTRACT'
    inv.location = (x - 20, y + 80)
    inv.inputs[0].default_value = 1.0
    tree.links.new(level, inv.inputs[1])

    soft_mask = tree.nodes.new('ShaderNodeMath')
    soft_mask.name = "lzla_shadow_apply"
    soft_mask.operation = 'MULTIPLY'
    soft_mask.use_clamp = True
    soft_mask.location = (x + 160, y + 80)
    soft_mask.inputs[1].default_value = 2.0
    tree.links.new(inv.outputs[0], soft_mask.inputs[0])

    doubled = tree.nodes.new('ShaderNodeMath')
    doubled.name = "lzla_hard_shadow_level_x2"
    doubled.operation = 'MULTIPLY'
    doubled.location = (x - 20, y - 160)
    doubled.inputs[1].default_value = 2.0
    tree.links.new(level, doubled.inputs[0])

    hard_mask = tree.nodes.new('ShaderNodeMath')
    hard_mask.name = "lzla_hard_shadow_apply"
    hard_mask.operation = 'SUBTRACT'
    hard_mask.use_clamp = True
    hard_mask.location = (x + 160, y - 160)
    hard_mask.inputs[0].default_value = 1.0
    tree.links.new(doubled.outputs[0], hard_mask.inputs[1])

    soft = _sharpen_mask(tree, soft_mask.outputs[0], props.shadow_edge_sharpness,
                         x + 340, y + 80, "lzla_shadow_edge_sharpen")
    hard = _sharpen_mask(tree, hard_mask.outputs[0], props.shadow_edge_sharpness,
                         x + 340, y - 160, "lzla_hard_shadow_edge_sharpen")
    return soft, hard


def _sharpen_mask(tree, mask, sharpness, x, y, name):
    """Tighten a tier mask around its own half-way point.

    A soft light's penumbra reaches the compositor as levels *between* the
    tiers - 0.42, 0.47, 0.49 - because the shader steps per sample and the film
    averages them. The normal tier does not care: 2 * (1 - level) is already
    clamped at 1 across that whole band, which is why its contour looks right.
    The deep tier is the opposite case - 1 - 2 * level spends its entire range
    inside exactly that band, so its fill fades in over the penumbra while the
    contour, a hard binarize, gets drawn at one point part way down. That is the
    black line with lit ground still showing beyond it.

    So the deep tier needs the step the normal tier gets for free. Rescaling the
    mask about 0.5 is that step - `clamp((m - 0.5) * k + 0.5)`, one node - and
    unlike a plain threshold it keeps the ramp, just narrower, so the edge stays
    anti-aliased instead of turning into stair-steps. k = 1 is a pass-through.

    Applied to both tiers, so the two keep matching, and named so the slider can
    drive it without a rebuild."""
    node = tree.nodes.new('ShaderNodeMath')
    node.name = name
    node.operation = 'MULTIPLY_ADD'
    node.use_clamp = True
    node.location = (x, y)
    tree.links.new(mask, node.inputs[0])
    _set_mask_sharpness(node, sharpness)
    return node.outputs[0]


def _set_mask_sharpness(node, sharpness):
    """Write k into a sharpen node: out = clamp(m * k + (0.5 - 0.5 * k))."""
    sharpness = max(1.0, float(sharpness))
    node.inputs[1].default_value = sharpness
    node.inputs[2].default_value = 0.5 - 0.5 * sharpness


def _mask_as_image(tree, mask, x, y, name):
    """Present a 0..1 scalar mask as a black/white image for the edge detector."""
    node, factor, a, b, result = _mix_rgba(tree)
    node.name = name
    node.location = (x, y)
    a.default_value = (0.0, 0.0, 0.0, 1.0)
    b.default_value = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(mask, factor)
    return result


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
        ("flat", VL_FLAT), ("shadow", VL_SHADOW), ("grunge", VL_GRUNGE),
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
    grunge_white = _over_white(tree, render_layers["grunge"], -700, -1050, "lzla_grunge_over_white")

    # One render layer carries both tiers as three levels; splitting them is
    # arithmetic on that image, so the shader's anti-aliased terminator is not
    # re-thresholded on the way through.
    shadow_apply, hard_apply = _shadow_level_masks(
        tree, shadow_white, props, -500, -350,
    )
    shadow_for_edges = _mask_as_image(
        tree, shadow_apply, -180, -270, "lzla_shadow_mask_image",
    )
    hard_shadow_for_edges = _mask_as_image(
        tree, hard_apply, -180, -510, "lzla_hard_shadow_mask_image",
    )

    hard_shadow_enable = tree.nodes.new('ShaderNodeValue')
    hard_shadow_enable.name = "lzla_hard_shadow_enable"
    hard_shadow_enable.location = (-500, -850)
    hard_shadow_enable.outputs[0].default_value = 1.0 if props.hard_shadow_enabled else 0.0

    current = flat_white

    current = _masked_multiply_tint(
        tree, current, shadow_apply, props.shadow_color, -250, -350, "lzla_shadow",
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

    # Lines are assembled as premultiplied RGBA layers stacked with Alpha Over.
    #
    # They used to be painted onto a white plate and then algebraically lifted
    # back out, with the plate's alpha computed *separately* as the max of every
    # contributing mask. Colour and coverage came from two different formulas,
    # and max is not how coverage combines: where a Line Art stroke's
    # anti-aliased fringe crossed a contour line's own fringe, max reported the
    # larger of the two (say 0.5) rather than the union (0.5 + 0.3 * 0.5 = 0.65),
    # so the crossing came out more transparent than either line - a pale seam
    # tracing the stroke exactly where two blacks should have merged solid.
    #
    # Alpha Over derives colour and coverage from one operation, so the two can
    # no longer disagree, and the white-plate round trip disappears with them.
    lines_layer = None
    separate_line_layers = []
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
        _set_group_input(edges, "Thickness", _contour_thickness(props, thickness))
        edge_mask = edges.outputs["Mask"]
        if key == "hard_shadow":
            edge_mask = _gate_mask(
                tree, edge_mask, hard_shadow_enable.outputs[0],
                -300, edge_y, "lzla_hard_shadow_edge_gate",
            )

        layer = _premultiplied_line_layer(
            tree, edge_mask, color, line_x, -1400, f"lzla_split_{key}_line",
        )
        separate_line_layers.append((f"{key}_lines", layer))
        if lines_layer is None:
            lines_layer = layer
        else:
            lines_layer = _composite_over(
                tree, lines_layer, layer, line_x + 400, -1400,
                f"lzla_split_{key}_line_over",
            )

    # The lines render layer's Combined pass is already premultiplied RGBA with
    # the stroke coverage in its alpha, so it stacks straight in.
    lines_final = _composite_over(
        tree, lines_layer, render_layers["lines"].outputs["Image"],
        700, -1400, "lzla_split_lines_gp_over",
    )
    flats_final = _premultiplied_from_white_plate(
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
        (("lines", lines_final), ("flats", flats_final),
         ("geometry_lines", render_layers["lines"].outputs["Image"]),
         *separate_line_layers),
    )
    split_out.mute = True

    paths_out = _make_file_out(
        FILE_OUT_PATHS_NAME, "Path processing package", 850,
        (("flats", flats_final),
         ("geometry_lines", render_layers["lines"].outputs["Image"]),
         ("shadow_mask", shadow_for_edges),
         ("hard_shadow_mask", _mask_as_image(tree, hard_apply, 500, -2100, "lzla_path_hard_mask")),
         ("grunge_mask", _mask_as_image(tree, grunge_apply, 500, -2300, "lzla_path_grunge_mask"))),
    )
    paths_out.mute = True
    # Mask values are data, not display colours. Keep the tracing threshold linear.
    paths_out.format.color_management = 'OVERRIDE'
    paths_out.format.view_settings.view_transform = 'Standard'
    paths_out.format.view_settings.look = 'None'
    paths_out.format.view_settings.exposure = 0
    paths_out.format.view_settings.gamma = 1

    scene.compositing_node_group = tree
    _ensure_render_transparency(scene)
    return tree


def _arm_file_output(scene, split=False, group_name=None, stamp=None):
    """Put a fresh timestamped path on the requested File Output node (next to
    the .blend) and mute the other one so only the requested files get written.

    group_name: optional Lazy Render Groups name inserted into the filename.
    stamp: optional shared timestamp so a batch render keeps one time prefix.
    """
    if not bpy.data.is_saved:
        return None, "Save the .blend first"
    comp = scene.compositing_node_group
    if comp is None or comp.get(ID_PROP) != NG_COMPOSITOR:
        return None, "Run Build Pipeline first"
    _ensure_render_transparency(scene)
    armed_name = FILE_OUT_SPLIT_NAME if split else FILE_OUT_NAME
    paths_node = comp.nodes.get(FILE_OUT_PATHS_NAME)
    if paths_node is not None:
        paths_node.mute = True
    ids_node = comp.nodes.get(FILE_OUT_IDS_NAME)
    if ids_node is not None:
        ids_node.mute = True
    other_name = FILE_OUT_NAME if split else FILE_OUT_SPLIT_NAME
    node = comp.nodes.get(armed_name)
    if node is None:
        return None, "File Output missing - run Build Pipeline"
    other = comp.nodes.get(other_name)
    if other is not None:
        other.mute = True
    node.mute = False

    if stamp is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Absolute path to the .blend's folder - the relative "//" prefix has
    # proven unreliable on the File Output node in some setups.
    blend_dir = os.path.dirname(bpy.path.abspath(bpy.data.filepath))
    node.directory = blend_dir + os.sep
    group_bit = ""
    if group_name:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in group_name.strip())
        safe = safe.strip("_") or "group"
        group_bit = f"{safe}_"
    # Split slots are named "lines"/"flats", so the stamp lands before them:
    # lazy_lineart_<group_><stamp>_lines####.png / _flats####.png
    if split:
        node.file_name = f"lazy_lineart_{group_bit}{stamp}_"
    else:
        node.file_name = f"lazy_lineart_{group_bit}{stamp}"
    fmt = node.format
    if hasattr(fmt, "media_type"):
        fmt.media_type = 'IMAGE'
    fmt.file_format = 'PNG'
    fmt.color_mode = 'RGBA'
    return node.directory + node.file_name, None


def _render_groups_module():
    """Return the Lazy Render Groups module if that addon is loaded."""
    mod = sys.modules.get("lazy_render_groups")
    if mod is not None and hasattr(mod, "render_all_with_callback"):
        return mod
    # Extensions / nested installs: bl_ext.<repo>.lazy_render_groups
    for name, candidate in sys.modules.items():
        if name.rsplit(".", 1)[-1] != "lazy_render_groups":
            continue
        if candidate is not None and hasattr(candidate, "render_all_with_callback"):
            return candidate
    return None


def _render_groups_available(scene):
    """True when the Render Groups addon is loaded (checkbox can show)."""
    return _render_groups_module() is not None


def _render_groups_ready(scene):
    """True when there is at least one valid group to iterate."""
    mod = _render_groups_module()
    if mod is None:
        return False
    return bool(getattr(mod, "has_render_groups", lambda _s: False)(scene))


# ----------------------------------------------------------------------------
# Settings sync (panel props -> existing datablocks, no creation)
# ----------------------------------------------------------------------------

def _ensure_shadow_override(scene):
    """Make sure the shadow view layer still carries its luminance override.

    Returns the material actually assigned to the layer (or None)."""
    props = scene.lazy_lineart_props
    view_layer = scene.view_layers.get(VL_SHADOW)
    mat = _find_tagged(bpy.data.materials, MAT_OVERRIDE_SHADOW)
    if mat is None and view_layer is not None:
        mat = _retag_datablock(
            bpy.data.materials, LEGACY_MAT_OVERRIDE_WHITE, MAT_OVERRIDE_SHADOW,
        ) or _build_override_shadow(props)
    if view_layer is not None and mat is not None:
        if view_layer.material_override != mat:
            view_layer.material_override = mat
        return view_layer.material_override
    return mat


def _shadow_pass_material_is_stale(mat, mode):
    """True when the override material does not match the current Shadow Step.

    SHADER mode needs both ramps in the material; COMPOSITE mode needs them
    gone, so the pass carries raw luminance for the compositor to step. A
    material built by an older version satisfies neither and is caught here
    too."""
    if mat is None or mat.node_tree is None:
        return False
    has_ramps = (
        mat.node_tree.nodes.get("lzla_shadow_ramp") is not None
        and mat.node_tree.nodes.get("lzla_hard_shadow_ramp") is not None
    )
    if mat.node_tree.nodes.get("lzla_light_luminance") is None:
        return True
    if has_ramps and mat.node_tree.nodes.get("lzla_light_level") is None:
        return True
    return has_ramps != (mode == 'SHADER')


def _sync_shadow_samples(scene):
    """Push the sample count onto the shadow view layer.

    Only this pass is lit, so only this pass benefits from samples; grunge and
    lines are emission and Grease Pencil, and stay on the cheap default."""
    view_layer = scene.view_layers.get(VL_SHADOW)
    if view_layer is None:
        return False
    view_layer.samples = scene.lazy_lineart_props.shadow_samples
    return True


def _set_ramp_threshold(owner, ramp_name, value):
    """Move a constant Color Ramp's step to `value`.

    `owner` is either a material (shader tree) or a node group (compositor);
    Shadow Step decides which one actually holds the ramps."""
    if owner is None:
        return False
    tree = getattr(owner, "node_tree", owner)
    if tree is None:
        return False
    ramp = tree.nodes.get(ramp_name)
    if ramp is None or not hasattr(ramp, "color_ramp"):
        return False
    elements = ramp.color_ramp.elements
    if len(elements) < 2:
        return False
    elements[1].position = max(min(value, 1.0), 0.0)
    owner.update_tag()
    return True


def _set_light_level(owner, value):
    """Write the Light Level divisor into whichever tree holds the ramps."""
    if owner is None:
        return False
    tree = getattr(owner, "node_tree", owner)
    if tree is None:
        return False
    node = tree.nodes.get("lzla_light_level")
    if node is None:
        return False
    node.inputs[1].default_value = max(value, 1e-3)
    owner.update_tag()
    return True


def _sync_shadow_thresholds(scene):
    """Push both shadow threshold sliders into the override material's ramps.

    Back in the material, so dragging a threshold updates the 3D viewport
    straight away instead of only showing up once you render."""
    props = scene.lazy_lineart_props

    mat = _ensure_shadow_override(scene)
    if _shadow_pass_material_is_stale(mat, props.shadow_step_mode):
        mat = _build_override_shadow(props)
        view_layer = scene.view_layers.get(VL_SHADOW)
        if view_layer is not None:
            view_layer.material_override = mat

    # Whichever tree holds the ramps this mode gets the write; the other simply
    # has no such nodes, so all three calls are safe to make unconditionally.
    for tree_owner in (mat, _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)):
        _set_ramp_threshold(tree_owner, "lzla_shadow_ramp", props.shadow_threshold)
        _set_ramp_threshold(
            tree_owner, "lzla_hard_shadow_ramp", props.hard_shadow_threshold,
        )
        _set_light_level(tree_owner, props.shadow_light_level)


def _sync_shadow_sharpness(scene):
    """Push the sharpness slider into both tiers' sharpen nodes."""
    props = scene.lazy_lineart_props
    comp = _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)
    if comp is None:
        return
    for name in ("lzla_shadow_edge_sharpen", "lzla_hard_shadow_edge_sharpen"):
        node = comp.nodes.get(name)
        if node is not None:
            _set_mask_sharpness(node, props.shadow_edge_sharpness)


def _sync_hard_shadow_enabled(scene):
    """Push the enable checkbox into the compositor gate.

    The hard tier is a second cut of the shared shadow pass, so there is nothing
    to disable on the render side - both the fill tint and the contour pass are
    gated in the compositor."""
    props = scene.lazy_lineart_props
    enabled_val = 1.0 if props.hard_shadow_enabled else 0.0

    comp = _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)
    if comp is not None:
        enable_node = comp.nodes.get("lzla_hard_shadow_enable")
        if enable_node is not None:
            enable_node.outputs[0].default_value = enabled_val


def _sync_settings(scene):
    props = scene.lazy_lineart_props

    _sync_collection_lod(scene)

    gp_object = _find_tagged(bpy.data.objects, GP_CONTOURS)
    if gp_object is not None:
        lineart = gp_object.modifiers.get(LINEART_MOD_NAME)
        if lineart is not None:
            lineart.crease_threshold = props.crease_angle
            lineart.chaining_image_threshold = props.chain_gaps
            lineart.radius = _lineart_base_radius(props)
            _apply_lineart_quality(lineart, props)
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

    _sync_shadow_samples(scene)
    _sync_shadow_thresholds(scene)
    _sync_shadow_sharpness(scene)
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
        blur = comp.nodes.get("lzla_shadow_blur")
        if blur is not None:
            _set_blur_radius(blur, props.shadow_smoothing)
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
                _set_group_input(edges, "Thickness",
                                 _contour_thickness(props, line_thickness))
            # Current graph: the colour lives on the premultiply mix. The two
            # older names are kept so a scene built before the rework still
            # takes colour edits until its next rebuild.
            line_mix = comp.nodes.get(f"lzla_split_{key}_line_rgb")
            if line_mix is None:
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


def _refresh_gp_usage():
    """Re-read the collection checkboxes and LOD presets before a recompute."""
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        _sync_collection_lod(scene)


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
    _refresh_gp_usage()
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


def _compositor_needs_hue_tint_rebuild(comp):
    """True when the compositor lacks the darken + COLOR + mix tint graph."""
    return (
        comp.nodes.get("lzla_shadow_mult") is not None
        and comp.nodes.get("lzla_shadow_cast") is None
    )


def _compositor_needs_shadow_step_rebuild(comp, mode):
    """True when the compositor does not match the current shadow graph.

    Three ways to be out of date: no level splitter at all (a build old enough
    to threshold here, which re-quantised the shader's anti-aliased edge into
    dither), a hard-shadow render layer (the hard tier used to be its own
    render), or ramps on the wrong side of the render for the current Shadow
    Step."""
    if comp.nodes.get("lzla_shadow_over_white") is None:
        return False
    has_ramps = comp.nodes.get("lzla_shadow_ramp") is not None
    return (
        comp.nodes.get("lzla_shadow_level") is None
        or comp.nodes.get("lzla_rl_hard_shadow") is not None
        or has_ramps != (mode == 'COMPOSITE')
        or (has_ramps and comp.nodes.get("lzla_light_level") is None)
        # Lines used to be lifted off a white plate with a separately-max'd
        # alpha; they are premultiplied Alpha Over layers now.
        or comp.nodes.get("lzla_split_lines_gp_over") is None
        # Built before the tier masks got their terminator step.
        or comp.nodes.get("lzla_hard_shadow_edge_sharpen") is None
    )


def _rebuild_compositor_if_stale(context):
    """Rebuild the compositor - and retire what it left behind - when out of date."""
    comp = _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)
    mode = context.scene.lazy_lineart_props.shadow_step_mode
    if comp is None or not _compositor_needs_shadow_step_rebuild(comp, mode):
        return
    try:
        _build_compositor(context, context.scene.lazy_lineart_props)
        _migrate_legacy_datablocks(context.scene)
    except Exception:
        traceback.print_exc()


def _on_tint_color_changed(self, context):
    if context is None:
        return
    comp = _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)
    if comp is not None and _compositor_needs_hue_tint_rebuild(comp):
        try:
            _build_compositor(context, context.scene.lazy_lineart_props)
        except Exception:
            traceback.print_exc()
    _sync_compositor_tint_colors(context.scene)


def _on_shadow_sharpness_changed(self, context):
    """Scenes built before 0.1.51 have no sharpen node - rebuild, then sync."""
    if context is None:
        return
    _rebuild_compositor_if_stale(context)
    _sync_shadow_sharpness(context.scene)


def _on_setting_changed(self, context):
    if not context.scene.lazy_lineart_props.live_updates:
        return
    _sync_state["deadline"] = time.monotonic() + SYNC_DEBOUNCE_SECONDS
    if not _sync_state["scheduled"]:
        _sync_state["scheduled"] = True
        bpy.app.timers.register(_run_sync_timer, first_interval=0.05)


def _on_collection_lod_changed(self, context):
    """A collection's LOD is scene state, not a panel slider: write it through
    even with live updates off, so the next Generate or render is already
    correct."""
    if context is None:
        return
    scene = context.scene
    _sync_collection_lod(scene)
    if scene.lazy_lineart_props.live_updates:
        _refresh_gp_lines()


def _copy_collection_lod(source, target, include_children=False):
    """Copy one collection's LOD onto another (and optionally its children)."""
    src = _collection_lod(source)
    dst = _collection_lod(target)
    if src is None or dst is None:
        return 0
    dst.preset = src.preset
    dst.grunge_scale = src.grunge_scale
    count = 1
    if include_children:
        for child in target.children:
            count += _copy_collection_lod(source, child, True)
    return count


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


def _on_shadow_step_mode_changed(self, context):
    """Shadow Step moves the ramps between the material and the compositor, so
    both have to be rebuilt - and the render layer re-rendered, since the pass
    itself now carries something different."""
    if context is None:
        return
    props = context.scene.lazy_lineart_props
    mat = _build_override_shadow(props)
    view_layer = context.scene.view_layers.get(VL_SHADOW)
    if view_layer is not None:
        view_layer.material_override = mat
    try:
        _build_compositor(context, props)
    except Exception:
        traceback.print_exc()
    _sync_shadow_thresholds(context.scene)
    _sync_hard_shadow_enabled(context.scene)


def _sync_contour_thickness(scene):
    """Push the three contour widths into their edge groups."""
    props = scene.lazy_lineart_props
    comp = _find_tagged(bpy.data.node_groups, NG_COMPOSITOR)
    if comp is None:
        return
    for key, thickness in (
        ("shadow", props.shadow_line_thickness),
        ("hard_shadow", props.hard_shadow_line_thickness),
        ("grunge", props.grunge_line_thickness),
    ):
        edges = comp.nodes.get(f"lzla_{key}_edges")
        if edges is not None:
            _set_group_input(edges, "Thickness", _contour_thickness(props, thickness))


def _on_thickness_multiplier_changed(self, context):
    """Write it through immediately, live updates or not - it exists to be
    changed between one manual render and the next."""
    if context is None:
        return
    _apply_lineart_radius(context)
    _sync_contour_thickness(context.scene)


def _on_shadow_samples_changed(self, context):
    if context is None:
        return
    _sync_shadow_samples(context.scene)


def _on_shadow_threshold_changed(self, context):
    if context is None:
        return
    _rebuild_compositor_if_stale(context)
    _sync_shadow_thresholds(context.scene)


def _on_hard_shadow_threshold_changed(self, context):
    if context is None:
        return
    _rebuild_compositor_if_stale(context)
    _sync_shadow_thresholds(context.scene)


def _on_hard_shadow_enabled_changed(self, context):
    if context is None:
        return
    _rebuild_compositor_if_stale(context)
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
        # The scene edit we debounced may have been a collection being
        # unchecked - skip it now, before paying for one more full trace.
        _refresh_gp_usage()
        for mod in modifiers:
            mod.show_viewport = True
            mod.show_render = True
        _defer_state["suspended"] = False
        _refresh_gp_lines()
    _defer_state["scheduled"] = False
    return None


@persistent
def _on_depsgraph_update(scene, depsgraph):
    if scene.get("lazy_lineart_id_pass"):
        return
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


_render_quality_state = {"intersection": None}


@persistent
def _lazy_lineart_render_pre(scene, *_args):
    """Use the chosen edge types for F12 and panel renders alike."""
    if scene.get("lazy_lineart_id_pass"):
        return
    lineart = _lineart_modifier()
    if lineart is None:
        return
    _render_quality_state["intersection"] = lineart.use_intersection
    _apply_lineart_quality(lineart, scene.lazy_lineart_props, full=True)


@persistent
def _lazy_lineart_render_post(scene, *_args):
    """Hand the preview back its cheap settings once the render is done."""
    if scene.get("lazy_lineart_id_pass"):
        return
    saved = _render_quality_state["intersection"]
    _render_quality_state["intersection"] = None
    if saved is None:
        return
    lineart = _lineart_modifier()
    if lineart is not None and lineart.use_intersection != saved:
        lineart.use_intersection = saved


@persistent
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


def _harden_light(light):
    """Make one light cast a crisp, deterministic shadow. Returns what changed.

    EEVEE Next traces shadows stochastically: a light with a physical radius
    spreads its penumbra over a ray distribution that only converges as samples
    accumulate, and jittered shadows re-sample the shadow map every pass. Both
    are the right call for realism and exactly wrong for a toon step, which
    reads the terminator through a hard ramp - the unconverged penumbra becomes
    dithering along the very edge the ramp is trying to cut.

    Zero radius costs nothing in render time and removes the noise at source."""
    changed = []
    # Sun lights size their penumbra by angular diameter; the rest by radius.
    for attr in ("angle", "shadow_soft_size", "size", "size_y"):
        if not hasattr(light, attr):
            continue
        try:
            if getattr(light, attr) != 0.0:
                setattr(light, attr, 0.0)
                changed.append(attr)
        except (AttributeError, TypeError, ValueError):
            pass
    # Spelling varies across versions; try both and take whichever exists.
    for attr in ("use_shadow_jitter", "use_shadow_jittering"):
        if not hasattr(light, attr):
            continue
        try:
            if getattr(light, attr):
                setattr(light, attr, False)
                changed.append(attr)
        except (AttributeError, TypeError, ValueError):
            pass
    return changed


class LINEART_OT_harden_lights(Operator):
    bl_idname = "lazylineart.harden_lights"
    bl_label = "Harden Light Shadows"
    bl_description = ("Set every light in the scene to zero radius and turn off "
                      "jittered shadows, so the shadow pass renders crisp and "
                      "noise-free instead of dithering along the toon step. "
                      "Undoable, and it only touches lights")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        touched = 0
        for light in bpy.data.lights:
            if _harden_light(light):
                touched += 1
        total = len(bpy.data.lights)
        if not total:
            self.report({'WARNING'}, "No lights in this file")
        elif touched:
            self.report({'INFO'}, f"Hardened {touched} of {total} lights")
        else:
            self.report({'INFO'}, f"All {total} lights already cast hard shadows")
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
        _sync_view_layer_collection_flags(scene)
        _restore_lineart_modifiers(force=True)
        gp_object.update_tag()
        context.view_layer.update()

        if props.bake_strokes:
            # Baked strokes are what ends up in the render, so bake at render
            # quality even while the preview is running without intersections.
            lineart = gp_object.modifiers.get(LINEART_MOD_NAME)
            _apply_lineart_quality(lineart, props, full=True)
            gp_object.update_tag()
            context.view_layer.update()

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
            _apply_lineart_quality(lineart, props)
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
        props = scene.lazy_lineart_props
        _sync_settings(scene)
        _sync_view_layer_collection_flags(scene)

        if props.render_all_groups and _render_groups_available(scene):
            if not _render_groups_ready(scene):
                self.report({'ERROR'}, "Render All Groups is on, but no groups exist")
                return {'CANCELLED'}
            return self._render_all_groups(context)

        file_name, err = _arm_file_output(scene)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        scene.render.use_compositing = True
        # Blocking EXEC, not INVOKE_DEFAULT: an async render job can execute the
        # compositor before the freshly armed file name reaches the evaluated
        # node tree, so every render would overwrite the same PNG. This is the
        # same call the Render All Groups path uses, which stamps correctly.
        bpy.ops.render.render(write_still=False)
        self.report({'INFO'}, f"File Output → {file_name}Image.png")
        return {'FINISHED'}

    def _render_all_groups(self, context):
        scene = context.scene
        mod = _render_groups_module()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        last_path = None

        def render_one(ctx, group_name):
            nonlocal last_path
            _sync_view_layer_collection_flags(ctx.scene)
            file_name, err = _arm_file_output(
                ctx.scene, split=False, group_name=group_name, stamp=stamp,
            )
            if err:
                return err
            ctx.scene.render.use_compositing = True
            # File Output node writes the PNG; write_still would also dump to
            # scene.render.filepath and can flatten alpha to black if color_mode
            # is RGB.
            bpy.ops.render.render(write_still=False)
            last_path = file_name
            return None

        count, err = mod.render_all_with_callback(context, render_one)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        self.report({'INFO'}, f"Rendered {count} group(s) → {last_path or ''}")
        return {'FINISHED'}


class LINEART_OT_render_split(Operator):
    bl_idname = "lazylineart.render_split"
    bl_label = "Render Split (Lines / Flats)"
    bl_description = (
        "Write transparent PNGs next to the .blend: combined lines, flats, "
        "geometry lines, and separate shadow, hard-shadow and grunge lines. "
        "Use either combined lines or the individual line plates, not both"
    )

    def execute(self, context):
        scene = context.scene
        props = scene.lazy_lineart_props
        _sync_settings(scene)
        _sync_view_layer_collection_flags(scene)

        if props.render_all_groups and _render_groups_available(scene):
            if not _render_groups_ready(scene):
                self.report({'ERROR'}, "Render All Groups is on, but no groups exist")
                return {'CANCELLED'}
            return self._render_all_groups(context)

        file_name, err = _arm_file_output(scene, split=True)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        scene.render.use_compositing = True
        # Blocking EXEC - see LINEART_OT_render_composite for why INVOKE_DEFAULT
        # loses the timestamped file name.
        bpy.ops.render.render(write_still=False)
        self.report({'INFO'}, f"File Output → {file_name} (lines, flats and separate line plates)")
        return {'FINISHED'}

    def _render_all_groups(self, context):
        scene = context.scene
        mod = _render_groups_module()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        last_path = None

        def render_one(ctx, group_name):
            nonlocal last_path
            _sync_view_layer_collection_flags(ctx.scene)
            file_name, err = _arm_file_output(
                ctx.scene, split=True, group_name=group_name, stamp=stamp,
            )
            if err:
                return err
            ctx.scene.render.use_compositing = True
            bpy.ops.render.render(write_still=False)
            last_path = file_name
            return None

        count, err = mod.render_all_with_callback(context, render_one)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        self.report(
            {'INFO'},
            f"Rendered {count} group(s) split → {last_path or ''}lines + flats",
        )
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Properties and panel
# ----------------------------------------------------------------------------

class LazyLineartProps(PropertyGroup):
    use_contours: BoolProperty(
        name="Silhouettes", default=True, update=_on_setting_changed,
        description="Trace visible outlines and occluding contours",
    )
    use_creases: BoolProperty(
        name="Creases", default=True, update=_on_setting_changed,
        description="Trace face-angle changes using the Crease Threshold",
    )
    use_material_boundaries: BoolProperty(
        name="Material Boundaries", default=True, update=_on_setting_changed,
        description="Trace borders between materials; can add unwanted detail",
    )
    use_marked_edges: BoolProperty(
        name="Marked Edges", default=False, update=_on_setting_changed,
        description="Include manually marked Freestyle mesh edges",
    )
    use_intersections: BoolProperty(
        name="Intersections", default=True, update=_on_setting_changed,
        description="Trace surface intersections in renders and bakes. Disable "
                    "to reduce clutter and tracing cost on overlapping geometry",
    )
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
    chain_gaps: FloatProperty(
        name="Chain Gaps", default=0.001, min=0.0, max=0.5, precision=4,
        description="How far apart two stroke ends may be, in image space, and still "
                    "be joined into one stroke. Raise it when contours come back as "
                    "dashes or loops that do not close - measured here, going from "
                    "0.001 to 0.05 merged 38 fragments into 32 longer strokes. Too "
                    "high and lines that should stay separate join up",
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
    shadow_step_mode: EnumProperty(
        name="Shadow Step",
        items=(
            ('SHADER', "Shader (hard lights)",
             "Step the light in the override material. Previews live in the 3D "
             "viewport and anti-aliases itself. Correct for suns and zero-radius "
             "lights; an area light's traced penumbra averages into grey here"),
            ('COMPOSITE', "Composite (soft lights)",
             "Step the converged render in the compositor instead. Handles the "
             "soft penumbra of area and radius lights, at the cost of previewing "
             "in the compositor backdrop rather than the 3D viewport"),
        ),
        default='SHADER',
        description="Where the two toon ramps live. Match it to your key light: "
                    "hard light in Shader, soft/area light in Composite",
        update=_on_shadow_step_mode_changed,
    )
    shadow_light_level: FloatProperty(
        name="Light Level", default=1.0, min=0.001, soft_max=50.0,
        description="The lighting luminance that counts as fully lit. Both thresholds "
                    "are fractions of it. Raise it when the shadows stop responding to "
                    "the sliders - that means your lights put the surface well above 1.0 "
                    "and every threshold now sits under the darkest pixel",
        update=_on_shadow_threshold_changed,
    )
    shadow_samples: IntProperty(
        name="Shadow Samples", default=64, min=1, max=1024,
        description="Render samples for the shadow pass. It is the only pass that "
                    "depends on lighting, so it is the only one that comes back grainy "
                    "- raise this (or Shadow Blur) when the shadow boundary breaks up "
                    "into speckle and the contour pass turns messy",
        update=_on_shadow_samples_changed,
    )
    shadow_threshold: FloatProperty(
        name="Shadow Threshold", default=0.35, min=0.001, max=1.0,
        description="Diffuse light level below which the shadow pass reads as shadow. "
                    "Cut in the compositor, after Shadow Blur has smoothed the pass, "
                    "so the result is a single clean black/white boundary",
        update=_on_shadow_threshold_changed,
    )
    shadow_smoothing: FloatProperty(
        name="Smoothing", default=0.0, min=0.0, max=32.0, subtype='PIXEL',
        description="Blur applied to the shadow pass before the tiers are split "
                    "apart. In Shader step mode the terminator is already "
                    "anti-aliased, so this only rounds off a jagged edge. In "
                    "Composite mode it smooths the lighting before the thresholds "
                    "cut it, which is the tool for a grainy or noisy terminator - "
                    "and cheaper than raising Shadow Samples",
        update=_on_setting_changed,
    )
    shadow_edge_sharpness: FloatProperty(
        name="Edge Sharpness", default=6.0, min=1.0, max=64.0,
        description="Tightens both shadow tiers around their own boundary before "
                    "they are tinted and outlined. A soft light's penumbra reaches "
                    "the compositor as greys between the tiers, which the deep tier "
                    "spreads its whole range across - that is what puts a contour "
                    "line part way down a gradient with lit surface still showing "
                    "past it. 1 leaves the edge exactly as rendered",
        update=_on_shadow_sharpness_changed,
    )
    shadow_color: FloatVectorProperty(
        name="Shadow Color", subtype='COLOR', size=4,
        default=(0.55, 0.55, 0.62, 1.0), min=0.0, max=1.0,
        description="Shadow tint: darkens by luminance, then blends this hue/sat into the flat",
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
        description="Apply the hard-shadow tint and contour pass on top of normal "
                    "shadow. Gated in the compositor - toggling costs no render time "
                    "either way, since both tiers cut the same shadow pass",
        update=_on_hard_shadow_enabled_changed,
    )
    hard_shadow_threshold: FloatProperty(
        name="Hard Shadow Threshold", default=0.12, min=0.001, max=1.0,
        description="Diffuse light level below which the deep-shadow tier pushes flats "
                    "to black. A second cut of the same shadow pass, below Shadow "
                    "Threshold, taken after Hard Shadow Blur has smoothed it",
        update=_on_hard_shadow_threshold_changed,
    )
    hard_shadow_color: FloatVectorProperty(
        name="Hard Shadow Color", subtype='COLOR', size=4,
        default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0,
        description="Deep-shadow tint: darkens by luminance, then blends this hue/sat into the flat",
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
        description="Grunge tint: darkens by luminance, then blends this hue/sat into the flat",
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
        description="Luminance cut the contour detector uses to find region borders. "
                    "The shadow masks reach it already black/white, so this mainly "
                    "shapes contours on the grunge pass",
        update=_on_setting_changed,
    )
    bake_strokes: BoolProperty(
        name="Bake Strokes", default=False,
        description="Bake Line Art to keyframes on Generate. Baking deactivates the "
                    "modifier - leave off while iterating on stills",
    )
    thickness_multiplier: FloatProperty(
        name="Thickness Multiplier", default=1.0, min=0.0, max=10.0, precision=3,
        description="Scales every line width at once - Line Art strokes, shadow, "
                    "hard shadow and grunge contours - without touching the "
                    "individual sliders. Meant for rendering one group lighter than "
                    "another: set it, render, set it again for the next group",
        update=_on_thickness_multiplier_changed,
    )
    render_all_groups: BoolProperty(
        name="Render All Groups", default=False,
        description="When Lazy Render Groups is active, Render Composite / Split "
                    "cycles through every group (blocking stills) instead of only "
                    "the currently visible one",
    )
    use_back_face_culling: BoolProperty(
        name="Back-Face Culling", default=True,
        description="Ignore faces pointing away from the camera. Measured ~2x faster "
                    "with an identical stroke count on both closed and open meshes - "
                    "turn it off only if lines go missing on single-sided geometry",
        update=_on_setting_changed,
    )
    preview_intersections: BoolProperty(
        name="Intersections in Preview", default=False,
        description="Trace intersection lines in the viewport too. They are the most "
                    "expensive edge type by far (5-10x on overlapping geometry); "
                    "only used when Intersections is enabled above",
        update=_on_setting_changed,
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


class LazyLineartCollectionProps(PropertyGroup):
    """Per-collection level of detail.

    Line Art solves occlusion once for the whole scene, so these ride the one
    shared modifier rather than adding one modifier per collection - which
    measured slower *and* lost the occlusion between collections."""

    preset: EnumProperty(
        name="Line Art",
        items=(
            ('DEFAULT', "Default", "Trace normally, and leave any Line Art usage set "
                                   "by hand on this collection alone"),
            ('HERO', "Hero", "Contours, creases and intersection lines"),
            ('MID', "Mid", "Contours and creases, no intersection lines - roughly "
                           "3x cheaper on overlapping geometry"),
            ('CONTOUR', "Contours Only", "Silhouettes and holes only: no crease "
                                         "lines anywhere on the surface, and no "
                                         "intersection lines. Keeps the edge "
                                         "between an object and what is inside or "
                                         "behind it, drops the surface detail"),
            ('BACKGROUND', "Background", "Draw no lines at all, but still hide "
                                         "whatever stands behind it"),
            ('OFF', "Off", "Ignore this collection completely"),
        ),
        default='DEFAULT',
        update=_on_collection_lod_changed,
    )
    grunge_scale: FloatProperty(
        name="Grunge Scale", default=1.0, min=0.05, max=20.0,
        description="Scales the grunge pattern on this collection's objects. Below 1 "
                    "gives bigger, sparser patches - what an object that is far away "
                    "but large on screen wants. 1 inherits from the parent collection",
        update=_on_collection_lod_changed,
    )


class LINEART_OT_copy_collection_lod(Operator):
    bl_idname = "lazylineart.copy_collection_lod"
    bl_label = "Copy LOD From"
    bl_description = ("Copy another collection's Line Art preset and grunge scale onto "
                      "the active collection")
    bl_options = {'REGISTER', 'UNDO'}

    source: bpy.props.StringProperty(
        name="From", description="Collection to copy the settings from",
    )
    include_children: BoolProperty(
        name="Include Children", default=False,
        description="Also write the copied settings onto every collection nested "
                    "inside the active one",
    )

    @classmethod
    def poll(cls, context):
        return context.collection is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop_search(self, "source", bpy.data, "collections", text="From")
        layout.prop(self, "include_children")

    def execute(self, context):
        target = context.collection
        source = bpy.data.collections.get(self.source)
        if source is None:
            self.report({'ERROR'}, "Pick a collection to copy from")
            return {'CANCELLED'}
        if source == target:
            self.report({'WARNING'}, "That is already the active collection")
            return {'CANCELLED'}
        count = _copy_collection_lod(source, target, self.include_children)
        if not count:
            self.report({'ERROR'}, "Collections carry no Lazy Line Art settings")
            return {'CANCELLED'}
        _sync_collection_lod(context.scene)
        self.report({'INFO'}, f"Copied {source.name} onto {count} collection(s)")
        return {'FINISHED'}


def _path_stroke_snapshot(context, width, height):
    """Project evaluated visible curves, before the short-chain deletion filter."""
    from bpy_extras.object_utils import world_to_camera_view
    scene = context.scene
    obj = _find_tagged(bpy.data.objects, GP_CONTOURS)
    states = [(m, m.show_viewport) for m in obj.modifiers]
    lineart = obj.modifiers.get(LINEART_MOD_NAME)
    intersection = lineart.use_intersection
    try:
        for m in obj.modifiers:
            m.show_viewport = m.name != FILTER_MOD_NAME
        _apply_lineart_quality(lineart, scene.lazy_lineart_props, full=True)
        obj.update_tag()
        context.view_layer.update()
        evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
        paths = []
        for layer in evaluated.data.layers:
            frame = layer.current_frame()
            if frame is None or layer.hide:
                continue
            matrix = evaluated.matrix_world @ layer.matrix_local
            for stroke in frame.drawing.strokes:
                if stroke.hide_stroke:
                    continue
                points = []
                for point in stroke.points:
                    p = world_to_camera_view(scene, scene.camera, matrix @ point.position)
                    if p.z <= 0:
                        raise RuntimeError("A stroke crosses behind the camera; move the camera before exporting")
                    points.append([p.x * width, (1 - p.y) * height])
                if len(points) >= 2:
                    if stroke.cyclic:
                        points.append(points[0][:])
                    paths.append({'id': f'geometry-{len(paths)}', 'points': points,
                                  'closed': bool(stroke.cyclic)})
        return paths
    finally:
        for modifier, visible in states:
            modifier.show_viewport = visible
        lineart.use_intersection = intersection
        obj.update_tag()
        context.view_layer.update()


def _collection_id_assignments(scene):
    """Deepest visible collection wins; ties use full paths, never iteration order."""
    candidates = {}
    def visit(layer_collection, parents=()):
        if layer_collection.exclude or layer_collection.collection.hide_render:
            return
        collection = layer_collection.collection
        if collection.get(ID_PROP) == COL_LINEART:
            return
        path = parents + (collection.name,)
        for obj in collection.objects:
            if obj.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'VOLUME', 'POINTCLOUD'} and not obj.hide_render:
                candidates.setdefault(obj, []).append(path)
        for child in layer_collection.children:
            visit(child, path)
    visit(scene.view_layers[VL_FLAT].layer_collection)
    owners = {obj: sorted(paths, key=lambda p: (-len(p), p))[0] for obj, paths in candidates.items()}
    paths = sorted(set(owners.values()))
    if len(paths) > 0xFFFFFF:
        raise RuntimeError('Too many collections for RGB IDs')
    entries = []
    for index, path in enumerate(paths, 1):
        # A bijection in 24 bits: unique, repeatable, pseudorandom colours; black is background.
        rgb = (index * 0x9E3779) & 0xFFFFFF
        entries.append({'id': index, 'name': path[-1], 'path': ' / '.join(path),
                        'color': [(rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255]})
    ids = {path: index for index, path in enumerate(paths, 1)}
    return [(obj, ids[path]) for obj, path in owners.items()], entries


def _render_collection_ids(scene, assignments, entries, folder, width, height):
    """Isolated Workbench pass: no lighting, sampling noise or Object Index pass.

    Copy the scene/view-layer exclusions, sharing geometry for correct render
    modifiers. Object display colours are restored even if rendering fails.
    """
    temporary = None
    colors = [(obj, tuple(obj.color)) for obj in scene.objects]
    try:
        temporary = scene.copy()
        temporary.name = 'LZLA Temporary Collection IDs'
        temporary['lazy_lineart_id_pass'] = True
        for layer in list(temporary.view_layers):
            if layer.name != VL_FLAT:
                temporary.view_layers.remove(layer)
        temporary.render.engine = 'BLENDER_WORKBENCH'
        temporary.render.use_compositing = False
        temporary.render.use_sequencer = False
        temporary.compositing_node_group = None
        temporary.render.film_transparent = True
        temporary.render.use_freestyle = False
        temporary.render.dither_intensity = 0
        temporary.display.render_aa = 'OFF'
        shading = temporary.display.shading
        shading.light = 'FLAT'
        shading.color_type = 'OBJECT'
        shading.background_type = 'VIEWPORT'
        shading.background_color = (0, 0, 0)
        shading.show_shadows = False
        shading.show_cavity = False
        shading.show_specular_highlight = False
        shading.show_object_outline = False
        shading.show_xray = False
        for obj, _ in colors:
            obj.color = (0, 0, 0, 1)
        for obj, index in assignments:
            obj.color = tuple(c / 255 for c in entries[index - 1]['color']) + (1,)
        temporary.render.image_settings.file_format = 'OPEN_EXR'
        temporary.render.image_settings.color_depth = '32'
        temporary.render.image_settings.color_mode = 'RGBA'
        file = folder / 'collection_colors.exr'
        temporary.render.filepath = str(file)
        bpy.ops.render.render(scene=temporary.name, write_still=True)
        _write_collection_id_png(file, folder / 'collection_ids.png', entries, width, height)
    finally:
        for obj, color in colors:
            obj.color = color
        if temporary is not None:
            bpy.data.scenes.remove(temporary)


def _write_collection_id_png(index_file, destination, entries, width, height):
    """Write exact RGB bytes with no display transform or antialias colour blending."""
    import numpy as np
    import struct
    import zlib
    image = bpy.data.images.load(str(index_file), check_existing=False)
    try:
        if tuple(image.size) != (width, height):
            raise RuntimeError('Collection ID render dimensions do not match package')
        values = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        rgba = values.reshape(height, width, 4)[::-1]
        rgb = np.rint(rgba[:, :, :3] * 255).clip(0, 255).astype(np.uint8)
        rgb[rgba[:, :, 3] < 0.5] = 0
        palette = np.zeros((len(entries) + 1, 3), dtype=np.uint8)
        for entry in entries:
            palette[entry['id']] = entry['color']
        # Snap only numerical roundoff, never antialiased/blended colours.
        unique, inverse = np.unique(rgb.reshape(-1, 3), axis=0, return_inverse=True)
        snapped = []
        for color in unique:
            distances = np.sum((palette.astype(np.int32) - color.astype(np.int32)) ** 2, axis=1)
            nearest = int(np.argmin(distances))
            if distances[nearest] > 3:
                raise RuntimeError(f'Unexpected colour in unlit collection render: {color}')
            snapped.append(palette[nearest])
        pixels = np.asarray(snapped, dtype=np.uint8)[inverse].reshape(height, width, 3)
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        raw = b''.join(b'\0' + row.tobytes() for row in pixels)
        destination.write_bytes(b'\x89PNG\r\n\x1a\n'
                                + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                                + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    finally:
        bpy.data.images.remove(image)


class LINEART_OT_export_paths(Operator):
    bl_idname = "lazylineart.export_paths"
    bl_label = "Export for Path Processing"
    bl_description = "Render the current scene to a new package folder for the Path Companion"

    def execute(self, context):
        from pathlib import Path
        scene = context.scene
        if not bpy.data.is_saved or scene.camera is None:
            self.report({'ERROR'}, "Save the blend and select an active camera first")
            return {'CANCELLED'}
        if scene.render.use_border or scene.camera.data.type == 'PANO':
            self.report({'ERROR'}, "Prototype supports full-frame perspective/orthographic cameras; disable Render Region")
            return {'CANCELLED'}
        comp = scene.compositing_node_group
        node = comp.nodes.get(FILE_OUT_PATHS_NAME) if comp else None
        if node is None:
            self.report({'ERROR'}, "Run Build Pipeline first to add the package exporter")
            return {'CANCELLED'}
        outputs = [(n, n.mute) for n in comp.nodes if n.bl_idname == 'CompositorNodeOutputFile']
        old_directory, old_name = node.directory, node.file_name
        old_compositing = scene.render.use_compositing
        folder = Path(bpy.data.filepath).parent / ('lineart_paths_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        try:
            _sync_settings(scene)
            _sync_view_layer_collection_flags(scene)
            assignments, collections = _collection_id_assignments(scene)
            width = max(1, scene.render.resolution_x * scene.render.resolution_percentage // 100)
            height = max(1, scene.render.resolution_y * scene.render.resolution_percentage // 100)
            with context.temp_override(view_layer=scene.view_layers[VL_LINES]):
                strokes = _path_stroke_snapshot(context, width, height)
            folder.mkdir()
            _render_collection_ids(scene, assignments, collections, folder, width, height)
            for output, _ in outputs:
                output.mute = output != node
            node.directory = str(folder) + os.sep
            node.file_name = ''
            scene.render.use_compositing = True
            bpy.ops.render.render(write_still=False)
            files = {}
            for key in ('flats', 'geometry_lines', 'shadow_mask', 'hard_shadow_mask', 'grunge_mask'):
                matches = list(folder.glob(key + '*.png'))
                if len(matches) != 1:
                    raise RuntimeError(f"Missing or ambiguous rendered plate: {key}")
                files[key] = matches[0].name
            files['collection_ids'] = 'collection_ids.png'
            (folder / 'collection_palette.json').write_text(json.dumps(collections, indent=2), encoding='utf-8')
            package = {'version': 1, 'width': width, 'height': height,
                       'frame': scene.frame_current, 'camera': scene.camera.name,
                       'camera_matrix': [list(row) for row in scene.camera.matrix_world],
                       'pixel_aspect': [scene.render.pixel_aspect_x, scene.render.pixel_aspect_y],
                       'mask_encoding': 'srgb', 'files': files, 'geometry': strokes,
                       'collections': collections,
                       'collection_assignment': 'Deepest visible collection; full path breaks ties. IDs describe frontmost rendered surfaces, not hidden membership. IDs use a separate solid, unlit Workbench render; transparency and material displacement are not represented.',
                       'notes': 'Visible paths only; no inferred hidden geometry or source-face IDs. Geometry exported before short-stroke filtering.'}
            (folder / 'package.json').write_text(json.dumps(package), encoding='utf-8')
            self.report({'INFO'}, f"Open in Path Companion: {folder}")
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, f"Package export failed: {exc}")
            return {'CANCELLED'}
        finally:
            for output, muted in outputs:
                output.mute = muted
            node.directory, node.file_name = old_directory, old_name
            scene.render.use_compositing = old_compositing
        return {'FINISHED'}


class LINEART_OT_clean_strokes(Operator):
    bl_idname = "lazylineart.clean_strokes"
    bl_label = "Start with Clean Outlines"
    bl_description = ("Keep silhouettes and manually marked edges; disable creases, "
                      "material borders and intersections. Generate to refresh strokes")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.lazy_lineart_props
        props.use_contours = True
        props.use_creases = False
        props.use_material_boundaries = False
        props.use_marked_edges = True
        props.use_intersections = False
        _sync_settings(context.scene)
        self.report({'INFO'}, "Clean outlines selected. Generate to refresh; use geometry_lines for the clean plate")
        return {'FINISHED'}


class LINEART_PT_lazy_panel(Panel):
    bl_label = "Lazy Line Art"
    bl_idname = "LINEART_PT_lazy_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'
    bl_order = 90

    def draw(self, context):
        layout = self.layout
        props = context.scene.lazy_lineart_props

        col = layout.column()
        col.scale_y = 1.4
        col.operator(LINEART_OT_build_pipeline.bl_idname, icon='MOD_BUILD')
        col.operator(LINEART_OT_generate.bl_idname, icon='MOD_LINEART')
        col.operator(LINEART_OT_render_composite.bl_idname, icon='RENDER_STILL')
        col.operator(LINEART_OT_render_split.bl_idname, icon='RENDERLAYERS')
        col.operator(LINEART_OT_export_paths.bl_idname, icon='EXPORT')
        if _render_groups_available(context.scene):
            layout.prop(props, "render_all_groups")
        layout.prop(props, "thickness_multiplier")

        layout.separator()
        layout.label(text="Strokes:")
        col = layout.column(align=True)
        col.operator(LINEART_OT_clean_strokes.bl_idname, icon='MOD_LINEART')
        col.prop(props, "use_contours")
        col.prop(props, "use_creases")
        sub = col.column(align=True)
        sub.enabled = props.use_creases
        sub.prop(props, "crease_angle")
        col.prop(props, "use_material_boundaries")
        col.prop(props, "use_marked_edges")
        col.prop(props, "use_intersections")
        col.prop(props, "min_stroke_length")
        col.prop(props, "chain_gaps")
        col.prop(props, "stroke_radius")
        col.prop(props, "thickness_reference_distance")
        col.prop(props, "line_color")
        col.prop(props, "bake_strokes")

        layout.separator()
        layout.label(text="Shadow Pass:")
        col = layout.column(align=True)
        col.prop(props, "shadow_step_mode")
        col.prop(props, "shadow_samples")
        col.prop(props, "shadow_light_level")
        col.operator(LINEART_OT_harden_lights.bl_idname, icon='LIGHT')
        col.prop(props, "shadow_threshold")
        col.prop(props, "shadow_smoothing")
        col.prop(props, "shadow_edge_sharpness")
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
        layout.label(text="Level of Detail:")
        collection = context.collection
        lod = _collection_lod(collection) if collection is not None else None
        if lod is None:
            layout.label(text="No active collection", icon='INFO')
        else:
            box = layout.box()
            box.label(text=collection.name, icon='OUTLINER_COLLECTION')
            # The add-on's own collection and the scene root are driven by the
            # pipeline, not by a preset.
            box.enabled = (collection.get(ID_PROP) != COL_LINEART
                           and collection != context.scene.collection)
            box.prop(lod, "preset")
            box.prop(lod, "grunge_scale")
            box.operator(LINEART_OT_copy_collection_lod.bl_idname, icon='DUPLICATE')
        layout.label(text="Unchecked collections are skipped", icon='INFO')

        layout.separator()
        layout.label(text="Performance:")
        col = layout.column(align=True)
        col.prop(props, "use_back_face_culling")
        col.prop(props, "preview_intersections")
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
    LazyLineartCollectionProps,
    LazyLineartProps,
    LINEART_OT_toggle_updates,
    LINEART_OT_harden_lights,
    LINEART_OT_build_pipeline,
    LINEART_OT_generate,
    LINEART_OT_render_composite,
    LINEART_OT_render_split,
    LINEART_OT_copy_collection_lod,
    LINEART_OT_clean_strokes,
    LINEART_OT_export_paths,
    LINEART_PT_lazy_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.lazy_lineart_props = bpy.props.PointerProperty(type=LazyLineartProps)
    bpy.types.Collection.lazy_lineart = bpy.props.PointerProperty(
        type=LazyLineartCollectionProps,
    )

    # All of these are @persistent: without it Blender drops them on the next
    # file load, which would silently strand live updates and - worse - leave
    # renders running with the preview's reduced settings.
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _lazy_lineart_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_lazy_lineart_load_post)
    if _lazy_lineart_render_pre not in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.append(_lazy_lineart_render_pre)
    for handler_list in (bpy.app.handlers.render_post,
                         bpy.app.handlers.render_cancel):
        if _lazy_lineart_render_post not in handler_list:
            handler_list.append(_lazy_lineart_render_post)
    _lazy_lineart_load_post(None)


def unregister():
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _lazy_lineart_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_lazy_lineart_load_post)
    if _lazy_lineart_render_pre in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.remove(_lazy_lineart_render_pre)
    for handler_list in (bpy.app.handlers.render_post,
                         bpy.app.handlers.render_cancel):
        if _lazy_lineart_render_post in handler_list:
            handler_list.remove(_lazy_lineart_render_post)
    _restore_lineart_modifiers(force=True)
    _cancel_pending_sync()

    del bpy.types.Scene.lazy_lineart_props
    del bpy.types.Collection.lazy_lineart
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
