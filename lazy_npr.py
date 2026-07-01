bl_info = {
    "name": "Lazy NPR",
    "author": "thelazyone",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Experimental viewport line preview (silhouette, creases, material and open-edge boundaries) for NPR work.",
    "warning": "Early prototype: recomputes on demand, not per-frame.",
    "wiki_url": "",
    "category": "3D View",
}

import math
import traceback

import bpy
import bmesh
import gpu
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty, IntProperty
from bpy_extras import view3d_utils
from mathutils import Vector, geometry
from mathutils.bvhtree import BVHTree
from gpu_extras.batch import batch_for_shader

# Module-level state: the draw handler and the last computed GPU batches.
# Recomputing the line geometry is treated as an explicit, on-demand step
# (button press, or optional auto-update on parameter change) rather than
# something done every viewport redraw - the draw callback below only ever
# draws whatever batches were last built.
#
# Line segments are chained into connected polylines (one LINE_STRIP batch
# per chain) rather than being drawn as one big pile of disconnected 2-point
# segments. That alone doesn't remove the small gap/notch that appears where
# two segments meet at an angle though - Blender's built-in polyline shader
# computes each segment's screen-space thickness from that segment's own
# direction only, with no notion of a "join" between adjacent segments. To
# get clean, rounded corners instead, a small disc is drawn at every point
# where 2+ segments meet, using a tiny custom shader that keeps the disc a
# constant size in pixels (matching the line thickness) regardless of
# distance from the camera - the same trick the built-in polyline shader
# uses internally.
_draw_handler = None
_line_shader = None
_line_batches = []
_joint_shader = None
_joint_batch = None
_status_message = "Not computed yet"


def _get_shader():
    global _line_shader
    if _line_shader is None:
        _line_shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    return _line_shader


_JOINT_VERTEX_SOURCE = """
void main()
{
    vec4 clip_pos = ModelViewProjectionMatrix * vec4(center, 1.0);
    vec2 ndc_offset = dir * radiusPx * 2.0 / viewportSize;
    clip_pos.xy += ndc_offset * clip_pos.w;
    gl_Position = clip_pos;
}
"""

_JOINT_FRAGMENT_SOURCE = """
void main()
{
    fragColor = color;
}
"""


def _get_joint_shader():
    """Build the round-joint disc shader via GPUShaderCreateInfo - the old raw
    GPUShader(vertex_code, fragment_code) string constructor isn't supported
    on the Vulkan backend (Blender 4.5+ default on Windows/most platforms)."""
    global _joint_shader
    if _joint_shader is not None:
        return _joint_shader

    shader_info = gpu.types.GPUShaderCreateInfo()
    shader_info.push_constant('MAT4', "ModelViewProjectionMatrix")
    shader_info.push_constant('VEC2', "viewportSize")
    shader_info.push_constant('FLOAT', "radiusPx")
    shader_info.push_constant('VEC4', "color")
    shader_info.vertex_in(0, 'VEC3', "center")
    shader_info.vertex_in(1, 'VEC2', "dir")
    shader_info.fragment_out(0, 'VEC4', "fragColor")
    shader_info.vertex_source(_JOINT_VERTEX_SOURCE)
    shader_info.fragment_source(_JOINT_FRAGMENT_SOURCE)

    _joint_shader = gpu.shader.create_from_info(shader_info)
    return _joint_shader


def _find_view3d(context):
    """Find a VIEW_3D area/window-region/space, regardless of which region the caller runs from."""
    screen = context.screen
    if screen is None:
        return None, None, None
    for area in screen.areas:
        if area.type != 'VIEW_3D':
            continue
        space = area.spaces.active
        for region in area.regions:
            if region.type == 'WINDOW':
                return area, region, space
    return None, None, None


def _tag_redraw(context):
    screen = context.screen
    if screen is None:
        return
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _classify_edge(props, f1, f2, normal_matrix, view_origin, matrix_world):
    """Return a classification string for a manifold (2-face) edge, or None to discard it.

    Silhouette is checked before crease on purpose: an edge that currently sits on the
    outline of the object is very often *also* a sharp crease (e.g. every edge of a cube),
    but conceptually it's the outline, not an "interior" crease line - and if it were
    classified as a crease instead, toggling Silhouette off wouldn't actually remove it as
    long as Creases stayed on, which is confusing. Creases only capture sharp edges that
    aren't already part of the current outline.
    """
    if props.use_material_boundaries and f1.material_index != f2.material_index:
        return 'MATERIAL'

    n1_world = (normal_matrix @ f1.normal).normalized()
    n2_world = (normal_matrix @ f2.normal).normalized()

    if props.use_silhouette:
        c1 = matrix_world @ f1.calc_center_median()
        c2 = matrix_world @ f2.calc_center_median()
        d1 = (c1 - view_origin).normalized()
        d2 = (c2 - view_origin).normalized()
        if n1_world.dot(d1) * n2_world.dot(d2) < 0.0:
            return 'SILHOUETTE'

    if props.use_creases and n1_world.angle(n2_world, 0.0) >= props.crease_angle:
        return 'CREASE'

    return None


def _average_normal(normal_matrix, faces):
    total = Vector((0.0, 0.0, 0.0))
    for face in faces:
        total += (normal_matrix @ face.normal).normalized()
    if total.length < 1e-9:
        return Vector((0.0, 0.0, 1.0))
    return total.normalized()


def _is_point_visible(context, depsgraph, view_origin, point, bias_normal, bias_amount):
    """Ray-cast from the camera towards a single point to check it isn't blocked by other geometry.

    The point is nudged outward along the (averaged) face normal before casting, since testing a
    point that lies exactly on the mesh surface would otherwise re-intersect that same surface
    almost immediately due to floating point precision.
    """
    test_point = point + bias_normal * bias_amount
    direction = test_point - view_origin
    distance = direction.length
    if distance < 1e-8:
        return True
    direction.normalize()

    result, *_rest = context.scene.ray_cast(
        depsgraph, view_origin, direction, distance=max(distance - bias_amount * 0.5, 0.0)
    )
    return not result


def _refine_occlusion_boundary(
    context, depsgraph, view_origin, region, rv3d, world_a, edge_vector,
    t_visible, t_hidden, bias_normal, bias_amount,
    max_iterations=14, pixel_tolerance=0.5,
):
    """Binary-search between a known-visible and a known-hidden point on an edge for the exact
    point where visibility flips, converging until the bracket is within about a pixel on screen
    (rather than just landing on whichever coarse sample happened to be nearby)."""
    point_visible = world_a + edge_vector * t_visible
    point_hidden = world_a + edge_vector * t_hidden

    for _ in range(max_iterations):
        screen_visible = view3d_utils.location_3d_to_region_2d(region, rv3d, point_visible)
        screen_hidden = view3d_utils.location_3d_to_region_2d(region, rv3d, point_hidden)
        if screen_visible is not None and screen_hidden is not None:
            if (screen_visible - screen_hidden).length <= pixel_tolerance:
                break

        t_mid = (t_visible + t_hidden) * 0.5
        point_mid = world_a + edge_vector * t_mid
        if _is_point_visible(context, depsgraph, view_origin, point_mid, bias_normal, bias_amount):
            t_visible, point_visible = t_mid, point_mid
        else:
            t_hidden, point_hidden = t_mid, point_mid

    return point_visible


def _visible_subsegments(context, depsgraph, view_origin, region, rv3d, world_a, world_b,
                          bias_normal, coarse_samples):
    """Split an edge into the sub-segments that are actually visible from the current view.

    A single visible/hidden test on the whole edge can't tell a fully-hidden edge from one that
    only dips behind another surface partway along, so this first coarsely samples the edge to
    find roughly where visibility changes, then binary-searches each such transition down to
    about a pixel of precision instead of leaving the cut wherever the nearest coarse sample was.
    """
    edge_vector = world_b - world_a
    edge_length = edge_vector.length
    if edge_length < 1e-9:
        return []

    bias_amount = max(edge_length * 0.01, 1e-5)
    coarse_samples = max(int(coarse_samples), 2)

    ts = [i / coarse_samples for i in range(coarse_samples + 1)]
    points = [world_a + edge_vector * t for t in ts]
    visible = [
        _is_point_visible(context, depsgraph, view_origin, point, bias_normal, bias_amount)
        for point in points
    ]

    segments = []
    i = 0
    sample_total = len(visible)
    while i < sample_total:
        if not visible[i]:
            i += 1
            continue

        run_start = i
        while i < sample_total and visible[i]:
            i += 1
        run_end = i - 1  # inclusive - last visible coarse sample in this run

        start_point = points[run_start]
        if run_start > 0 and not visible[run_start - 1]:
            start_point = _refine_occlusion_boundary(
                context, depsgraph, view_origin, region, rv3d, world_a, edge_vector,
                ts[run_start], ts[run_start - 1], bias_normal, bias_amount,
            )

        end_point = points[run_end]
        if run_end < sample_total - 1 and not visible[run_end + 1]:
            end_point = _refine_occlusion_boundary(
                context, depsgraph, view_origin, region, rv3d, world_a, edge_vector,
                ts[run_end], ts[run_end + 1], bias_normal, bias_amount,
            )

        segments.append((start_point, end_point))

    return segments


def _maybe_add_segment(segments, region, rv3d, min_length_px, point_a, point_b):
    if min_length_px > 0.0:
        screen_a = view3d_utils.location_3d_to_region_2d(region, rv3d, point_a)
        screen_b = view3d_utils.location_3d_to_region_2d(region, rv3d, point_b)
        if screen_a is None or screen_b is None:
            return
        if (screen_a - screen_b).length < min_length_px:
            return
    segments.append((point_a.copy(), point_b.copy()))


def _build_object_triangle_data(mesh, matrix_world):
    """World-space triangles (+ normal, material) and a BVH for one object, used to find where
    its surface actually crosses another object's surface - a mesh-topology edge can never
    represent that, since the intersection curve doesn't exist in either mesh's own geometry."""
    mesh.calc_loop_triangles()
    verts_world = [matrix_world @ v.co for v in mesh.vertices]

    polygons = []
    triangles = []
    for loop_tri in mesh.loop_triangles:
        i0, i1, i2 = loop_tri.vertices
        polygons.append((i0, i1, i2))

        v0, v1, v2 = verts_world[i0], verts_world[i1], verts_world[i2]
        normal = (v1 - v0).cross(v2 - v0)
        if normal.length > 1e-12:
            normal.normalize()

        material = None
        if 0 <= loop_tri.material_index < len(mesh.materials):
            material = mesh.materials[loop_tri.material_index]

        triangles.append((v0, v1, v2, normal, material))

    if not polygons:
        return None, []

    bvh = BVHTree.FromPolygons(verts_world, polygons, all_triangles=True)
    return bvh, triangles


def _segment_triangle_hit(point_a, point_b, tri):
    """Where a finite edge (point_a -> point_b) crosses a triangle, or None. intersect_ray_tri
    only clips to the triangle's own area, not to the segment length, so that part is checked
    here by hand."""
    ray = point_b - point_a
    ray_len_sq = ray.length_squared
    if ray_len_sq < 1e-12:
        return None

    hit = geometry.intersect_ray_tri(tri[0], tri[1], tri[2], ray, point_a, True)
    if hit is None:
        return None

    t = (hit - point_a).dot(ray) / ray_len_sq
    if t < -1e-6 or t > 1.0 + 1e-6:
        return None
    return hit


def _triangle_intersection_segment(tri_a, tri_b):
    """The (point, point) segment where two triangles cross, or None if they don't.

    Two triangles in general position cross along a single segment, found by testing each edge
    of one against the other's area and vice versa - a proper crossing produces exactly two hits
    among those six tests, which become the segment's endpoints.
    """
    a0, a1, a2 = tri_a
    b0, b1, b2 = tri_b

    candidates = []
    for point_a, point_b in ((a0, a1), (a1, a2), (a2, a0)):
        hit = _segment_triangle_hit(point_a, point_b, tri_b)
        if hit is not None:
            candidates.append(hit)
    for point_a, point_b in ((b0, b1), (b1, b2), (b2, b0)):
        hit = _segment_triangle_hit(point_a, point_b, tri_a)
        if hit is not None:
            candidates.append(hit)

    if len(candidates) < 2:
        return None

    best_pair, best_dist = None, -1.0
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            dist = (candidates[i] - candidates[j]).length
            if dist > best_dist:
                best_dist, best_pair = dist, (candidates[i], candidates[j])

    if best_pair is None or best_dist < 1e-9:
        return None
    return best_pair


def _add_visibility_aware_segment(context, depsgraph, region, rv3d, view_origin, props,
                                   point_a, point_b, bias_normal, segments):
    if props.use_hidden_line_removal:
        for sub_a, sub_b in _visible_subsegments(
            context, depsgraph, view_origin, region, rv3d, point_a, point_b,
            bias_normal, props.visibility_samples
        ):
            _maybe_add_segment(segments, region, rv3d, props.min_length_px, sub_a, sub_b)
    else:
        _maybe_add_segment(segments, region, rv3d, props.min_length_px, point_a, point_b)


def _collect_object_boundary_segments(context, depsgraph, region, rv3d, view_origin,
                                       object_triangle_data, props):
    """Lines where two *different* objects' surfaces actually cross each other in 3D - not where
    one merely occludes the other (that's already covered by each object's own Silhouette)."""
    segments = []
    want_object = props.use_object_boundaries
    want_material = props.use_material_boundaries

    count = len(object_triangle_data)
    for i in range(count):
        bvh_a, tris_a = object_triangle_data[i]
        if bvh_a is None:
            continue
        for j in range(i + 1, count):
            bvh_b, tris_b = object_triangle_data[j]
            if bvh_b is None:
                continue

            for idx_a, idx_b in bvh_a.overlap(bvh_b):
                v0a, v1a, v2a, normal_a, material_a = tris_a[idx_a]
                v0b, v1b, v2b, normal_b, material_b = tris_b[idx_b]

                if not (want_object or (want_material and material_a != material_b)):
                    continue

                hit = _triangle_intersection_segment((v0a, v1a, v2a), (v0b, v1b, v2b))
                if hit is None:
                    continue

                bias_normal = normal_a + normal_b
                bias_normal = bias_normal.normalized() if bias_normal.length > 1e-9 else normal_a

                _add_visibility_aware_segment(
                    context, depsgraph, region, rv3d, view_origin, props,
                    hit[0], hit[1], bias_normal, segments
                )

    return segments


def _collect_line_segments(context, region, rv3d):
    props = context.scene.lazy_npr_props
    view_origin = rv3d.view_matrix.inverted().translation
    min_length_px = props.min_length_px
    use_hlr = props.use_hidden_line_removal
    visibility_samples = props.visibility_samples
    want_cross_object = props.use_object_boundaries or props.use_material_boundaries

    depsgraph = context.evaluated_depsgraph_get()
    segments = []
    object_triangle_data = []

    for obj in context.visible_objects:
        if obj.type != 'MESH':
            continue

        obj_eval = obj.evaluated_get(depsgraph)
        try:
            mesh = obj_eval.to_mesh()
        except RuntimeError:
            continue
        if mesh is None:
            continue

        matrix_world = obj_eval.matrix_world
        normal_matrix = matrix_world.to_3x3()

        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bm.normal_update()

            for edge in bm.edges:
                faces = edge.link_faces
                classification = None

                if len(faces) == 1:
                    if props.use_open_edges:
                        classification = 'OPEN'
                elif len(faces) == 2:
                    classification = _classify_edge(
                        props, faces[0], faces[1], normal_matrix, view_origin, matrix_world
                    )

                if classification is None:
                    continue

                world_a = matrix_world @ edge.verts[0].co
                world_b = matrix_world @ edge.verts[1].co

                if use_hlr:
                    bias_normal = _average_normal(normal_matrix, faces)
                    for sub_a, sub_b in _visible_subsegments(
                        context, depsgraph, view_origin, region, rv3d, world_a, world_b,
                        bias_normal, visibility_samples
                    ):
                        _maybe_add_segment(segments, region, rv3d, min_length_px, sub_a, sub_b)
                else:
                    _maybe_add_segment(segments, region, rv3d, min_length_px, world_a, world_b)

            if want_cross_object:
                object_triangle_data.append(_build_object_triangle_data(mesh, matrix_world))
        finally:
            bm.free()
            obj_eval.to_mesh_clear()

    if want_cross_object and len(object_triangle_data) > 1:
        segments.extend(
            _collect_object_boundary_segments(
                context, depsgraph, region, rv3d, view_origin, object_triangle_data, props
            )
        )

    return segments


_JOINT_QUANTIZE_PRECISION = 4
_JOINT_CIRCLE_SEGMENTS = 16


def _quantize_point(point):
    """Round a world-space point to a small grid so that geometrically-coincident
    endpoints coming from different sources (mesh edges, HLR splits, cross-object
    intersections...) reliably land on the same dictionary key."""
    return (
        round(point.x, _JOINT_QUANTIZE_PRECISION),
        round(point.y, _JOINT_QUANTIZE_PRECISION),
        round(point.z, _JOINT_QUANTIZE_PRECISION),
    )


def _build_chains_and_joints(segments):
    """Group independent (a, b) segments into connected polylines wherever they
    share an endpoint, and collect every point where 2+ segments meet (a "joint")
    so a rounding disc can be drawn there. Chains only walk through points with
    exactly two segment-ends meeting (a simple pass-through) - true branch points
    (3+ segments, e.g. a corner where three mesh edges meet) are left as separate
    chains and get covered by their own joint disc instead."""
    adjacency = {}
    for seg_idx, (a, b) in enumerate(segments):
        adjacency.setdefault(_quantize_point(a), []).append((seg_idx, 0))
        adjacency.setdefault(_quantize_point(b), []).append((seg_idx, 1))

    joint_points = []
    for key, refs in adjacency.items():
        if len(refs) >= 2:
            seg_idx, end_idx = refs[0]
            joint_points.append(segments[seg_idx][end_idx])

    used = [False] * len(segments)
    chains = []

    def unused_ref_at(key):
        # Only ever walk through a point where exactly two segment-ends meet -
        # anything else is either a dead end (1) or a branch point (3+) and
        # should stop the chain there.
        refs = adjacency.get(key, [])
        if len(refs) != 2:
            return None
        for seg_idx, end_idx in refs:
            if not used[seg_idx]:
                return seg_idx, end_idx
        return None

    for seg_idx in range(len(segments)):
        if used[seg_idx]:
            continue
        used[seg_idx] = True
        chain = [segments[seg_idx][0], segments[seg_idx][1]]

        while True:
            ref = unused_ref_at(_quantize_point(chain[-1]))
            if ref is None:
                break
            next_idx, next_end = ref
            used[next_idx] = True
            chain.append(segments[next_idx][1 - next_end])

        while True:
            ref = unused_ref_at(_quantize_point(chain[0]))
            if ref is None:
                break
            next_idx, next_end = ref
            used[next_idx] = True
            chain.insert(0, segments[next_idx][1 - next_end])

        chains.append(chain)

    return chains, joint_points


def _build_joint_triangle_data(joint_points):
    """Build a flat list of independent triangles (as center/direction pairs)
    approximating a filled circle at every joint point. The actual on-screen
    radius is applied later in the shader via a uniform, so tweaking the line
    thickness only needs a redraw, not a full recompute."""
    centers = []
    dirs = []
    ring = [
        (math.cos(2.0 * math.pi * k / _JOINT_CIRCLE_SEGMENTS),
         math.sin(2.0 * math.pi * k / _JOINT_CIRCLE_SEGMENTS))
        for k in range(_JOINT_CIRCLE_SEGMENTS)
    ]

    for center in joint_points:
        for k in range(_JOINT_CIRCLE_SEGMENTS):
            d0 = ring[k]
            d1 = ring[(k + 1) % _JOINT_CIRCLE_SEGMENTS]
            centers.extend((center, center, center))
            dirs.extend(((0.0, 0.0), d0, d1))

    return centers, dirs


def _rebuild_batch(context, region, rv3d):
    global _line_batches, _joint_batch, _status_message
    try:
        segments = _collect_line_segments(context, region, rv3d)
    except Exception as exc:
        traceback.print_exc()
        _status_message = f"Error: {exc}"
        _line_batches = []
        _joint_batch = None
        return 0

    chains, joint_points = _build_chains_and_joints(segments)

    shader = _get_shader()
    _line_batches = [
        batch_for_shader(shader, 'LINE_STRIP', {"pos": chain})
        for chain in chains if len(chain) >= 2
    ]

    centers, dirs = _build_joint_triangle_data(joint_points)
    if centers:
        _joint_batch = batch_for_shader(_get_joint_shader(), 'TRIS', {"center": centers, "dir": dirs})
    else:
        _joint_batch = None

    _status_message = f"{len(segments)} segments -> {len(chains)} lines, {len(joint_points)} joints"
    return len(segments)


def _clear_batch():
    global _line_batches, _joint_batch
    _line_batches = []
    _joint_batch = None


def _update_lines(context):
    """Recompute the line batch for the current view. Returns segment count, or None if no 3D view exists."""
    _, region, space = _find_view3d(context)
    if region is None or space is None or space.region_3d is None:
        return None
    return _rebuild_batch(context, region, space.region_3d)


# Recomputes are debounced through a single pending timer rather than done
# immediately every time something changes - this matters both for scene edits
# (a depsgraph_update_post handler can fire many times per second during an
# interactive drag) and for this add-on's own sliders (dragging Crease Angle,
# for instance, would otherwise re-run hidden-line removal on every tick).
# Changing any of this panel's own settings always schedules a recompute
# whenever the preview is enabled, regardless of "Auto Update on Change" -
# that toggle only gates whether *external* scene edits (moving/editing
# objects elsewhere, animation, etc.) also trigger a recompute, since those
# can be far more frequent and expensive to chase on a heavy scene.
_auto_update_pending = False


def _run_pending_auto_update():
    global _auto_update_pending
    _auto_update_pending = False

    context = bpy.context
    scene = context.scene
    props = getattr(scene, "lazy_npr_props", None)
    if props is not None and props.enabled:
        _update_lines(context)
        _tag_redraw(context)
    return None  # do not repeat this timer


def _schedule_auto_update():
    global _auto_update_pending
    if _auto_update_pending:
        return
    _auto_update_pending = True
    bpy.app.timers.register(_run_pending_auto_update, first_interval=0.2)


def _on_depsgraph_update(scene, depsgraph):
    props = getattr(scene, "lazy_npr_props", None)
    if props is not None and props.enabled and props.auto_update:
        _schedule_auto_update()


def _on_geometry_settings_changed(self, context):
    if self.enabled:
        _schedule_auto_update()
    _tag_redraw(context)


def _on_style_settings_changed(self, context):
    _tag_redraw(context)


class LazyNPRProps(PropertyGroup):
    enabled: BoolProperty(name="NPR Preview Enabled", default=False)
    auto_update: BoolProperty(
        name="Auto Update on Change",
        description="Also recompute lines automatically when the scene changes elsewhere "
                    "(moving/editing objects, animation, etc.) - changes made in this panel "
                    "always refresh regardless of this setting. Can get slow on heavy scenes; "
                    "leave off and use Update Lines instead if so",
        default=False,
        update=_on_geometry_settings_changed,
    )

    use_silhouette: BoolProperty(
        name="Silhouette", default=True,
        description="Edges where the surface turns away from the current view",
        update=_on_geometry_settings_changed,
    )
    use_creases: BoolProperty(
        name="Creases", default=True,
        description="Edges where adjacent faces meet at a sharp angle",
        update=_on_geometry_settings_changed,
    )
    use_material_boundaries: BoolProperty(
        name="Material Boundaries", default=False,
        description="Edges where adjacent faces use different materials - within one object's "
                    "own mesh, and where two different objects' surfaces actually cross with "
                    "different materials assigned",
        update=_on_geometry_settings_changed,
    )
    use_open_edges: BoolProperty(
        name="Open Edges", default=False,
        description="Edges with only one adjacent face (mesh boundaries)",
        update=_on_geometry_settings_changed,
    )
    use_object_boundaries: BoolProperty(
        name="Object Boundaries", default=False,
        description="Lines where two different objects' surfaces actually cross in 3D space. "
                    "Only finds true intersections (objects poking into each other) - if one "
                    "object simply sits in front of/on another without overlapping surfaces, "
                    "that boundary is already covered by Silhouette",
        update=_on_geometry_settings_changed,
    )

    use_hidden_line_removal: BoolProperty(
        name="Hidden Line Removal", default=True,
        description="Ray-cast each candidate line against the whole scene and discard the parts "
                    "that are blocked by other geometry from the current view",
        update=_on_geometry_settings_changed,
    )
    visibility_samples: IntProperty(
        name="Visibility Samples", default=8, min=2, max=32,
        description="Coarse points sampled along each line to detect where it dips behind "
                    "another surface. Each transition found is then refined down to about a "
                    "pixel of precision automatically - raise this only if a short occluder "
                    "in the middle of a long line is being missed entirely",
        update=_on_geometry_settings_changed,
    )

    crease_angle: FloatProperty(
        name="Crease Angle", subtype='ANGLE',
        default=math.radians(35.0), min=0.0, max=math.pi,
        description="Minimum angle between face normals to count as a crease",
        update=_on_geometry_settings_changed,
    )
    min_length_px: FloatProperty(
        name="Min Length (px)", default=3.0, min=0.0, max=500.0,
        description="Discard lines shorter than this, in screen pixels",
        update=_on_geometry_settings_changed,
    )
    line_thickness: FloatProperty(
        name="Thickness (px)", default=2.0, min=0.5, max=20.0,
        update=_on_style_settings_changed,
    )
    line_color: FloatVectorProperty(
        name="Color", subtype='COLOR', size=4,
        default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0,
        update=_on_style_settings_changed,
    )


def _draw_callback():
    props = bpy.context.scene.lazy_npr_props

    if _line_batches:
        shader = _get_shader()
        shader.bind()
        shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
        shader.uniform_float("lineWidth", props.line_thickness)
        shader.uniform_float("color", props.line_color)

        gpu.state.blend_set('ALPHA')
        for batch in _line_batches:
            batch.draw(shader)
        gpu.state.blend_set('NONE')

    if _joint_batch is not None:
        joint_shader = _get_joint_shader()
        joint_shader.bind()
        mvp_matrix = gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()
        joint_shader.uniform_float("ModelViewProjectionMatrix", mvp_matrix)
        joint_shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
        joint_shader.uniform_float("radiusPx", props.line_thickness * 0.5)
        joint_shader.uniform_float("color", props.line_color)

        gpu.state.blend_set('ALPHA')
        _joint_batch.draw(joint_shader)
        gpu.state.blend_set('NONE')


class NPR_OT_toggle_preview(Operator):
    bl_idname = "lazynpr.toggle_preview"
    bl_label = "Toggle NPR Preview"
    bl_description = "Enable or disable the Lazy NPR viewport line preview"

    def execute(self, context):
        global _draw_handler
        props = context.scene.lazy_npr_props
        props.enabled = not props.enabled

        if props.enabled:
            if _draw_handler is None:
                _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                    _draw_callback, (), 'WINDOW', 'POST_VIEW'
                )
            if _update_lines(context) is None:
                self.report({'WARNING'}, "No 3D Viewport found to preview into")
        else:
            if _draw_handler is not None:
                bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
                _draw_handler = None
            _clear_batch()

        _tag_redraw(context)
        return {'FINISHED'}


class NPR_OT_update_lines(Operator):
    bl_idname = "lazynpr.update_lines"
    bl_label = "Update Lines"
    bl_description = "Recompute the NPR line preview for the current view"

    def execute(self, context):
        count = _update_lines(context)
        if count is None:
            self.report({'WARNING'}, "No 3D Viewport found")
            return {'CANCELLED'}

        _tag_redraw(context)
        self.report({'INFO'}, f"Lazy NPR: {count} lines")
        return {'FINISHED'}


class NPR_PT_lazy_panel(Panel):
    bl_label = "Lazy NPR"
    bl_idname = "NPR_PT_lazy_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Lazy Tools'

    def draw(self, context):
        layout = self.layout
        props = context.scene.lazy_npr_props

        toggle_text = "Disable NPR Preview" if props.enabled else "Enable NPR Preview"
        row = layout.row()
        row.scale_y = 1.5
        row.operator(NPR_OT_toggle_preview.bl_idname, text=toggle_text, depress=props.enabled)

        col = layout.column()
        col.enabled = props.enabled
        col.operator(NPR_OT_update_lines.bl_idname, icon='FILE_REFRESH')
        col.prop(props, "auto_update")

        layout.box().label(text=_status_message, icon='INFO')

        layout.separator()
        layout.label(text="Line Sources:")
        col = layout.column(align=True)
        col.enabled = props.enabled
        col.prop(props, "use_silhouette")
        col.prop(props, "use_creases")
        col.prop(props, "use_material_boundaries")
        col.prop(props, "use_open_edges")
        col.prop(props, "use_object_boundaries")

        layout.separator()
        layout.label(text="Visibility:")
        col = layout.column(align=True)
        col.enabled = props.enabled
        col.prop(props, "use_hidden_line_removal")
        sub = col.column(align=True)
        sub.enabled = props.enabled and props.use_hidden_line_removal
        sub.prop(props, "visibility_samples")

        layout.separator()
        layout.label(text="Parameters:")
        col = layout.column(align=True)
        col.enabled = props.enabled
        col.prop(props, "crease_angle")
        col.prop(props, "min_length_px")
        col.prop(props, "line_thickness")
        col.prop(props, "line_color")


classes = (
    LazyNPRProps,
    NPR_OT_toggle_preview,
    NPR_OT_update_lines,
    NPR_PT_lazy_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.lazy_npr_props = bpy.props.PointerProperty(type=LazyNPRProps)

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister():
    global _draw_handler, _line_batches, _line_shader, _joint_batch, _joint_shader
    global _status_message, _auto_update_pending

    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    _auto_update_pending = False

    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None
    _line_batches = []
    _line_shader = None
    _joint_batch = None
    _joint_shader = None
    _status_message = "Not computed yet"

    del bpy.types.Scene.lazy_npr_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
