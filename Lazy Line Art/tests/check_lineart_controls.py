"""Run with Blender --background --factory-startup --python this_file."""
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lazy_lineart as addon

addon.register()
scene = bpy.context.scene
props = scene.lazy_lineart_props
assert bpy.ops.lazylineart.build_pipeline() == {'FINISHED'}
modifier = addon._lineart_modifier()

# Preview can be cheaper, but the artistic choice must survive render/bake.
for enabled in (False, True):
    for preview in (False, True):
        props.use_intersections = enabled
        props.preview_intersections = preview
        addon._apply_lineart_quality(modifier, props)
        assert modifier.use_intersection == (enabled and preview)
        addon._lazy_lineart_render_pre(scene)
        assert modifier.use_intersection == enabled
        addon._lazy_lineart_render_post(scene)
        assert modifier.use_intersection == (enabled and preview)
        addon._apply_lineart_quality(modifier, props, full=True)
        assert modifier.use_intersection == enabled

assert bpy.ops.lazylineart.clean_strokes() == {'FINISHED'}
assert modifier.use_contour and modifier.use_edge_mark
assert not any((modifier.use_crease, modifier.use_material, modifier.use_intersection))

# Rebuilding must preserve the selected edge types and separate export wiring.
assert bpy.ops.lazylineart.build_pipeline() == {'FINISHED'}
assert not addon._lineart_modifier().use_intersection
tree = scene.compositing_node_group
output = tree.nodes[addon.FILE_OUT_SPLIT_NAME]
expected = {'lines', 'flats', 'geometry_lines', 'shadow_lines',
            'hard_shadow_lines', 'grunge_lines'}
assert {item.name for item in output.file_output_items} == expected
assert all(output.inputs[name].is_linked for name in expected)
source = output.inputs['geometry_lines'].links[0].from_node
assert source.layer == addon.VL_LINES
assert output.inputs['geometry_lines'].links[0].from_socket.name == 'Image'
assert output.mute
for node in tree.nodes:
    if node.bl_idname == 'CompositorNodeSetAlpha':
        assert node.inputs['Type'].default_value == 'Replace Alpha'
assert len([obj for obj in bpy.data.objects
            if obj.get(addon.ID_PROP) == addon.GP_CONTOURS]) == 1
addon.unregister()
print('PASS: edge choices, render restoration, clean preset, rebuild and split wiring')
