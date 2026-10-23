"""Camera-space path reconstruction. No Blender dependency."""
import json
import hashlib
import math
from pathlib import Path
from xml.etree import ElementTree as ET

import contourpy
import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

GROUPS = ('geometry', 'shadow', 'hard_shadow', 'grunge')


def load_package(folder):
    folder = Path(folder)
    data = json.loads((folder / 'package.json').read_text(encoding='utf-8'))
    if data.get('version') != 1:
        raise ValueError('Unsupported package version')
    data['package_id'] = hashlib.sha256((folder / 'package.json').read_bytes()).hexdigest()
    for name, relative in data['files'].items():
        file = (folder / relative).resolve()
        if not file.is_relative_to(folder.resolve()):
            raise ValueError('Package file is outside its folder')
        with Image.open(file) as image:
            if image.size != (data['width'], data['height']):
                raise ValueError(f'Image dimensions do not match: {name}')
    return data


def mask_paths(file, threshold, encoding='srgb'):
    pixels = np.asarray(Image.open(file).convert('RGB'), dtype=float)[:, :, 0] / 255
    if encoding == 'srgb':
        pixels = np.where(pixels <= .04045, pixels / 12.92, ((pixels + .055) / 1.055) ** 2.4)
    # Sample centres in the same top-left pixel coordinate system as geometry.
    h, w = pixels.shape
    generator = contourpy.contour_generator(x=np.arange(w) + .5, y=np.arange(h) + .5,
                                            z=pixels, name='serial', corner_mask=False)
    return [line.tolist() for line in generator.lines(threshold) if len(line) >= 2]


def length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def simplify(points, tolerance):
    """Iterative RDP; closed loops split at a distant vertex to preserve closure."""
    if len(points) < 3 or tolerance <= 0:
        return points
    if points[0] == points[-1]:
        index = max(range(1, len(points) - 1), key=lambda i: math.dist(points[0], points[i]))
        return simplify(points[:index + 1], tolerance)[:-1] + simplify(points[index:], tolerance)
    p = np.asarray(points, dtype=float)
    keep, stack = {0, len(p) - 1}, [(0, len(p) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        v = p[b] - p[a]
        t = np.clip(((p[a + 1:b] - p[a]) @ v) / max(float(v @ v), 1e-20), 0, 1)
        d = np.linalg.norm(p[a + 1:b] - (p[a] + t[:, None] * v), axis=1)
        index = int(np.argmax(d))
        if d[index] > tolerance:
            index += a + 1
            keep.add(index)
            stack.extend(((a, index), (index, b)))
    return p[sorted(keep)].tolist()


def join_paths(paths, gap, angle=25):
    """Greedy endpoint matching with tangent tests and one join per endpoint.

    Never joins closed paths or different groups. Joins are returned separately
    for inspection. No claim of visibility-aware or semantic reconstruction.
    """
    paths = [[list(p) for p in line] for line in paths if len(line) >= 2]
    if gap <= 0:
        return paths, []
    endpoints, owners, directions = [], [], []
    for i, line in enumerate(paths):
        if math.dist(line[0], line[-1]) < 1e-8:
            continue
        for side in (0, 1):
            oriented = line if side == 0 else line[::-1]
            end = np.array(oriented[0], dtype=float)
            # Avoid estimating direction from coincident points.
            inner = next((np.array(p) for p in oriented[1:] if math.dist(end, p) > .01), None)
            if inner is None:
                continue
            direction = end - inner
            direction /= np.linalg.norm(direction)
            endpoints.append(end)
            owners.append((i, side))
            directions.append(direction)
    if not endpoints:
        return paths, []
    candidates = []
    cosine = math.cos(math.radians(angle))
    for a, b in cKDTree(endpoints).query_pairs(gap):
        if owners[a][0] == owners[b][0]:
            continue
        delta = endpoints[b] - endpoints[a]
        distance = float(np.linalg.norm(delta))
        if float(directions[a] @ -directions[b]) < cosine:
            continue
        if distance > .01:
            direction = delta / distance
            if min(float(directions[a] @ direction), float(directions[b] @ -direction)) < cosine:
                continue
        candidates.append((distance, a, b))
    links, joins = {}, []
    for _, a, b in sorted(candidates):
        ea, eb = owners[a], owners[b]
        if ea in links or eb in links:
            continue
        links[ea], links[eb] = eb, ea
        joins.append([endpoints[a].tolist(), endpoints[b].tolist()])
    visited, result = set(), []
    # Open chains first, then cycles.
    starts = [(i, side) for i in range(len(paths)) for side in (0, 1) if (i, side) not in links]
    starts += [(i, 0) for i in range(len(paths))]
    for i, side in starts:
        if i in visited:
            continue
        line = []
        while i not in visited:
            visited.add(i)
            line.extend(paths[i] if side == 0 else paths[i][::-1])
            following = links.get((i, 1 - side))
            if following is None:
                break
            i, side = following
            if i in visited and line[0] != line[-1]:
                line.append(line[0][:])
        result.append(line)
    return result, joins


def reconstruct(folder, data, threshold=.5, gap=0, tolerance=.6, minimum=0):
    raw = {'geometry': [s['points'] for s in data.get('geometry', [])]}
    for group in GROUPS[1:]:
        raw[group] = mask_paths(Path(folder) / data['files'][group + '_mask'], threshold,
                                data.get('mask_encoding', 'srgb'))
    result, joins = {}, {}
    for group, paths in raw.items():
        # Mask contours are already connected; never bridge unrelated regions.
        chained, joins[group] = join_paths(paths, gap if group == 'geometry' else 0)
        result[group] = [simplify(p, tolerance) for p in chained if length(p) >= minimum]
    return raw, result, joins


def write_svg(file, data, paths, styles, hidden=()):
    root = ET.Element('svg', xmlns='http://www.w3.org/2000/svg',
                      width=str(data['width']), height=str(data['height']),
                      viewBox=f"0 0 {data['width']} {data['height']}")
    for group in GROUPS:
        style = styles[group]
        if not style['enabled']:
            continue
        layer = ET.SubElement(root, 'g', id=group, fill='none', stroke=style['color'],
                              attrib={'stroke-width': str(style['width']),
                                      'stroke-opacity': str(style['opacity']),
                                      'stroke-linecap': 'round', 'stroke-linejoin': 'round'})
        for index, line in enumerate(paths[group]):
            if (group, index) in hidden:
                continue
            commands = 'M ' + ' L '.join(f'{x:.3f},{y:.3f}' for x, y in line)
            if line[0] == line[-1]:
                commands += ' Z'
            ET.SubElement(layer, 'path', id=f'{group}-{index}', d=commands)
    ET.ElementTree(root).write(file, encoding='utf-8', xml_declaration=True)
