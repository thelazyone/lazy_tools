"""Blender integration: actually render and export a small intersecting scene."""
import json
import sys
import os
from pathlib import Path
import bpy

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import lazy_lineart as addon

addon.register()
scene = bpy.context.scene
scene.render.engine = os.environ.get('LZLA_TEST_ENGINE', 'CYCLES')
original_engine = scene.render.engine
scene.cycles.samples = 2
scene.render.resolution_x = 192
scene.render.resolution_y = 192
scene.render.resolution_percentage = 100
bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, location=(1, 0, .4))
sphere = bpy.context.object
sphere.pass_index = 17
detail = bpy.data.collections.new('Sphere details')
scene.collection.children.link(detail)
detail.objects.link(sphere)  # Also belongs to original collection: deterministic tie-break.
child = bpy.data.collections.new('Nested sphere')
detail.children.link(child)
child.objects.link(sphere)  # Deepest collection must win.
cube = bpy.data.objects['Cube']
cube.pass_index = 99
scene.lazy_lineart_props.shadow_samples = 2
scene.lazy_lineart_props.min_stroke_length = 100  # Snapshot must bypass deletion.
assert bpy.ops.lazylineart.build_pipeline() == {'FINISHED'}
destination = root / 'test_artifacts'
destination.mkdir(exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=str(destination / 'path_export_test.blend'))
node = scene.compositing_node_group.nodes[addon.FILE_OUT_PATHS_NAME]
original = (node.mute, node.directory, node.file_name)
original_colors = {obj.name: tuple(obj.color) for obj in scene.objects}
original_scenes = set(bpy.data.scenes.keys())
original_groups = set(bpy.data.node_groups.keys())
captured_folders = []
render_ids = addon._render_collection_ids
def record_ids(*args):
    captured_folders.append(args[3])
    return render_ids(*args)
addon._render_collection_ids = record_ids
assert bpy.ops.lazylineart.export_paths() == {'FINISHED'}
assert scene.render.engine == original_engine
assert set(bpy.data.scenes.keys()) == original_scenes
assert set(bpy.data.node_groups.keys()) == original_groups
assert {obj.name: tuple(obj.color) for obj in scene.objects} == original_colors
assert original == (node.mute, node.directory, node.file_name)
assert sphere.pass_index == 17 and cube.pass_index == 99
package_file = captured_folders[-1] / 'package.json'
data = json.loads(package_file.read_text())
assert data['geometry'], 'No geometry strokes exported'
assert all((package_file.parent / name).is_file() for name in data['files'].values())
assert (data['width'], data['height']) == (192, 192)
assert len(data['collections']) == 2
assert any(c['name'] == 'Nested sphere' for c in data['collections'])
assert len({tuple(c['color']) for c in data['collections']}) == 2
import struct, zlib
png = (package_file.parent / data['files']['collection_ids']).read_bytes()
offset = 8
compressed = b''
while offset < len(png):
    size = struct.unpack('>I', png[offset:offset+4])[0]
    if png[offset+4:offset+8] == b'IDAT':
        compressed += png[offset+8:offset+8+size]
    offset += size + 12
rows = zlib.decompress(compressed)
colors = {tuple(rows[y*(192*3+1)+1+x*3:y*(192*3+1)+4+x*3]) for y in range(192) for x in range(192)}
assert colors == {(0, 0, 0)} | {tuple(c['color']) for c in data['collections']}, colors
print('PASS: real render, masks, raw geometry and state restoration:', package_file)
