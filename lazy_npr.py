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

import bpy
import bmesh
import gpu
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

# Module-level state: the draw handler and the last computed GPU batch.
# Recomputing the line geometry is treated as an explicit, on-demand step
# (button press, or optional auto-update on parameter change) rather than
# something done every viewport redraw - the draw callback below only ever
# draws whatever batch was last built.
_draw_handler = None
_line_shader = None
_line_batch = None


def _get_shader():
    global _line_shader
    if _line_shader is None:
        _line_shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    return _line_shader


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
    """Return a classification string for a manifold (2-face) edge, or None to discard it."""
    if props.use_material_boundaries and f1.material_index != f2.material_index:
        return 'MATERIAL'

    n1_world = (normal_matrix @ f1.normal).normalized()
    n2_world = (normal_matrix @ f2.normal).normalized()

    if props.use_creases and n1_world.angle(n2_world, 0.0) >= props.crease_angle:
        return 'CREASE'

    if props.use_silhouette:
        c1 = matrix_world @ f1.calc_center_median()
        c2 = matrix_world @ f2.calc_center_median()
        d1 = (c1 - view_origin).normalized()
        d2 = (c2 - view_origin).normalized()
        if n1_world.dot(d1) * n2_world.dot(d2) < 0.0:
            return 'SILHOUETTE'

    return None


def _collect_line_segments(context, region, rv3d):
    props = context.scene.lazy_npr_props
    view_origin = rv3d.view_matrix.inverted().translation
    min_length_px = props.min_length_px

    depsgraph = context.evaluated_depsgraph_get()
    segments = []

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

        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bm.normal_update()

            matrix_world = obj_eval.matrix_world
            normal_matrix = matrix_world.to_3x3()

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

                if min_length_px > 0.0:
                    screen_a = view3d_utils.location_3d_to_region_2d(region, rv3d, world_a)
                    screen_b = view3d_utils.location_3d_to_region_2d(region, rv3d, world_b)
                    if screen_a is None or screen_b is None:
                        continue
                    if (screen_a - screen_b).length < min_length_px:
                        continue

                segments.append((world_a.copy(), world_b.copy()))
        finally:
            bm.free()
            obj_eval.to_mesh_clear()

    return segments


def _rebuild_batch(context, region, rv3d):
    global _line_batch
    segments = _collect_line_segments(context, region, rv3d)

    coords = []
    for point_a, point_b in segments:
        coords.append(point_a)
        coords.append(point_b)

    _line_batch = batch_for_shader(_get_shader(), 'LINES', {"pos": coords}) if coords else None
    return len(segments)


def _clear_batch():
    global _line_batch
    _line_batch = None


def _update_lines(context):
    """Recompute the line batch for the current view. Returns segment count, or None if no 3D view exists."""
    _, region, space = _find_view3d(context)
    if region is None or space is None or space.region_3d is None:
        return None
    return _rebuild_batch(context, region, space.region_3d)


def _on_geometry_settings_changed(self, context):
    if self.enabled and self.auto_update:
        _update_lines(context)
    _tag_redraw(context)


def _on_style_settings_changed(self, context):
    _tag_redraw(context)


class LazyNPRProps(PropertyGroup):
    enabled: BoolProperty(name="NPR Preview Enabled", default=False)
    auto_update: BoolProperty(
        name="Auto Update on Change",
        description="Recompute lines automatically whenever a source or parameter below changes "
                    "(can get slow on heavy scenes - leave off and use Update Lines instead)",
        default=False,
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
        description="Edges where adjacent faces use different materials",
        update=_on_geometry_settings_changed,
    )
    use_open_edges: BoolProperty(
        name="Open Edges", default=False,
        description="Edges with only one adjacent face (mesh boundaries)",
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
    if _line_batch is None:
        return

    props = bpy.context.scene.lazy_npr_props
    shader = _get_shader()

    shader.bind()
    shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    shader.uniform_float("lineWidth", props.line_thickness)
    shader.uniform_float("color", props.line_color)

    gpu.state.blend_set('ALPHA')
    _line_batch.draw(shader)
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

        layout.separator()
        layout.label(text="Line Sources:")
        col = layout.column(align=True)
        col.enabled = props.enabled
        col.prop(props, "use_silhouette")
        col.prop(props, "use_creases")
        col.prop(props, "use_material_boundaries")
        col.prop(props, "use_open_edges")

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


def unregister():
    global _draw_handler, _line_batch, _line_shader

    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None
    _line_batch = None
    _line_shader = None

    del bpy.types.Scene.lazy_npr_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
