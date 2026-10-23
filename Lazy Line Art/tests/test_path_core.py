import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'path_companion'))
from core import join_paths, simplify, mask_paths, write_svg, GROUPS


class PathTests(unittest.TestCase):
    def test_join_and_reject_parallel(self):
        paths = [[[0, 0], [5, 0]], [[6, 0], [10, 0]], [[0, 1], [5, 1]]]
        joined, joins = join_paths(paths, 2)
        self.assertEqual(len(joined), 2)
        self.assertEqual(len(joins), 1)
        self.assertIn([[0, 0], [5, 0], [6, 0], [10, 0]], joined)

    def test_disabled_joins(self):
        paths = [[[0, 0], [1, 0]], [[2, 0], [3, 0]]]
        self.assertEqual(join_paths(paths, 0), (paths, []))

    def test_mask_hole_and_closed_loop(self):
        mask = np.zeros((30, 30), dtype=np.uint8)
        mask[3:27, 3:27] = 255
        mask[10:20, 10:20] = 0
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'mask.png'
            Image.fromarray(mask).save(file)
            lines = mask_paths(file, .5)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(line[0], line[-1])
            reduced = simplify(line, .6)
            self.assertEqual(reduced[0], reduced[-1])
            self.assertGreaterEqual(len(reduced), 4)

    def test_svg_groups_hide_and_opacity(self):
        paths = {group: [[[0, 0], [10, 10]]] for group in GROUPS}
        styles = {group: dict(enabled=True, width=2, opacity=.4, color='#000000') for group in GROUPS}
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'out.svg'
            write_svg(file, dict(width=20, height=20), paths, styles, {('geometry', 0)})
            root = ET.parse(file).getroot()
        ns = {'s': 'http://www.w3.org/2000/svg'}
        self.assertEqual(len(root.findall('s:g', ns)), 4)
        self.assertEqual(len(root.findall('.//s:path', ns)), 3)
        self.assertEqual(root[0].get('stroke-opacity'), '0.4')


if __name__ == '__main__':
    unittest.main()
