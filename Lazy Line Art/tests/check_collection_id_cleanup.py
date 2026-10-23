"""Run with Blender --background --factory-startup --python-exit-code 1 --python."""
import sys
from pathlib import Path
import bpy

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import lazy_lineart as addon

addon.register()
scene = bpy.context.scene
scene.render.engine = 'BLENDER_EEVEE'
scene.render.resolution_x = scene.render.resolution_y = 64
scene.render.resolution_percentage = 100
assert bpy.ops.lazylineart.build_pipeline() == {'FINISHED'}
assignments, entries = addon._collection_id_assignments(scene)
colors = {obj.name: tuple(obj.color) for obj in scene.objects}
scenes = set(bpy.data.scenes.keys())
groups = set(bpy.data.node_groups.keys())
shading = (scene.display.shading.light, scene.display.shading.color_type,
           scene.display.shading.show_shadows, scene.display.render_aa)
folder = root / 'test_artifacts' / 'id_failure_cleanup'
folder.mkdir(parents=True, exist_ok=True)
original_writer = addon._write_collection_id_png
def fail_write(*args):
    raise RuntimeError('Injected output failure')
addon._write_collection_id_png = fail_write
try:
    addon._render_collection_ids(scene, assignments, entries, folder, 64, 64)
    raise AssertionError('Expected injected failure')
except RuntimeError as error:
    assert str(error) == 'Injected output failure'
finally:
    addon._write_collection_id_png = original_writer
assert scene.render.engine == 'BLENDER_EEVEE'
assert colors == {obj.name: tuple(obj.color) for obj in scene.objects}
assert scenes == set(bpy.data.scenes.keys())
assert groups == set(bpy.data.node_groups.keys())
assert shading == (scene.display.shading.light, scene.display.shading.color_type,
                   scene.display.shading.show_shadows, scene.display.render_aa)
print('PASS: collection ID output failure restores colours, renderer, display and temporary datablocks')
