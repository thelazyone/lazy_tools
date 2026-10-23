# Lazy Line Art — illustration workspace (0.2.1)

Everything for this workflow lives here. Other Lazy Tools add-ons remain in the parent folder.

## Start here

1. Open **Path Companion.exe**, or **Launch Path Companion.bat**.
2. In Blender, install/reload **lazy_lineart.py** from this folder. Existing installations keep
   working; Lazy Reloader can find this source with its directory still set to the parent Lazy Tools folder.
3. Use **Export Path Package** in Blender, then **Open Blender package** in the companion.
   Existing packages work; no new render is needed for the layer controls.

The companion is a native Rust application with an antialiased preview. The preview reduces
subpixel detail at the current zoom to keep dense drawings responsive; SVGs and sessions retain
the full path and thickness data. Blender still handles
rendering. Collection IDs use a separate unlit Workbench pass; your chosen main renderer is preserved.

## Layers

The top list shows a silhouette preview and visibility checkbox for each collection. Select a row
or its preview to edit that layer. Selection keeps other visible layers on the canvas; **Solo selected
layer** isolates the preview. Visibility affects the linework and SVG, while the colour render remains
a reference image. Solo is preview-only: export includes every enabled layer.

Each layer has independent mask threshold, join gap, simplification, minimum length, line variation,
and group styles. The **Line groups** table has visibility, width and opacity columns. Layer opacity
and width multiplier apply on top of those group values. **Copy from layer** copies reconstruction,
variation, group styles, visibility, opacity and width multiplier. Individual hidden-stroke choices
are not copied. Changing reconstruction clears individual stroke hides only in that layer.

## Variation and defaults

**Line variation** collapses into one row. The built-in **Subtle — your preset** uses:

| Effect | Amplitude | Wavelength |
| --- | --- | --- |
| Primary wobble | 1 px | 90 px |
| Secondary wobble | 0.5 px | 20 px |
| Thickness jitter | 5% | 5 px |

New packages start with this variation and **6 px** group widths. **Save as default** beside Line groups
or inside Line variation stores that section for future packages. **Use default** / **Saved default**
applies it to the selected layer. Presets can be saved by name; saving an existing name updates it.
All defaults and user presets live in **settings/defaults.json**, with a backup of the previous save.
Saving defaults does not overwrite other layers in the current package.

## Save and export

**Save SVG + session** writes a grouped vector SVG and a `.paths.json` session containing per-layer
settings, styles, hidden strokes and editable centreline/width data. Thickness jitter exports as filled
vector outlines; uniform-width strokes stay stroked paths. Sessions from earlier Rust builds still
open; new layer-aware sessions require this build. Python sessions use a separate format.

**Save Photoshop ID PNG** writes exact collection colours and a palette. In Photoshop use Magic Wand
with tolerance 0 and Contiguous off. IDs identify visible surfaces, not hidden geometry. Collection
ownership near edges is sampled from that mask. Transparent/material-displaced surfaces can differ
from the Workbench ID pass. Original paths and joins can be shown for inspection.

## Folder contents

- `Path Companion.exe` and `Launch Path Companion.bat`: current app and launcher.
- `lazy_lineart.py`: Blender add-on.
- `settings/`: saved defaults and presets.
- `path_companion_rust/`: Rust source and build cache.
- `path_companion/` and `Launch Legacy Python Companion.bat`: preserved Python version.
- `tests/` and `test_artifacts/`: checks and local test output.

## Development checks

Run from this folder:

```
cargo test --manifest-path path_companion_rust/Cargo.toml --locked --offline
cargo clippy --manifest-path path_companion_rust/Cargo.toml --locked --offline --all-targets -- -D warnings
cargo build --release --manifest-path path_companion_rust/Cargo.toml --locked --offline
```

After building, copy `path_companion_rust/target/release/lazy-path-companion.exe` over
`Path Companion.exe` while it is closed. `Path Companion.exe --check <package-folder>` checks a package
without writing it. `--benchmark` also saves benchmark SVG/session files, and `--smoke` opens the app,
saves `rust_preview.png` into the supplied package folder, and closes automatically.

Blender integration scripts are in `tests/`; run them with Blender's background/factory-startup options.
The legacy Python tests are `python -m unittest discover -s tests -p test_path_core.py`.

## Colour and light

Expand **Colour & light** for the selected layer to adjust luminosity, saturation, hue and contrast.
These affect the rendered colour pixels belonging to that collection; linework stays independent.
Luminosity adjusts HSL lightness, saturation is a multiplier, hue is in degrees, and contrast pivots
around middle lightness. The original image is never overwritten.

Drag **Drag gradient onto image** onto the canvas to place a luminosity ramp. Alternatively click
that button, then drag across the canvas to draw it. Drag either endpoint to change its position
and direction. Use **Gradient light**, **Darken**, **Lighten**, or **Reverse** to tune it. There is
one gradient per layer: drawing or dropping another replaces the previous one. Disable
**Edit gradient on canvas** (or press Escape) to return to selecting paths. **Remove gradient**
keeps the other colour settings. **Reset colour & gradient** clears that layer's colour edits.

While dragging, the colour preview uses reduced resolution for responsiveness; it returns to full
resolution on release. **Save adjusted colour PNG** writes the full-resolution colour plate with its
original alpha and source bit depth (including 16-bit Blender plates), ready to combine with the separately exported linework in Photoshop. Layer visibility
and Solo still control linework, not the colour plate. The session stores colour edits and gradients;
Copy from layer includes them. SVG remains the linework export. Earlier sessions open with neutral
colour settings; new colour-aware sessions require this build.

When an update is staged while the app is open, close the app and use **Launch Path Companion.bat**.
The launcher promotes the staged executable automatically. If the previous app is still open,
it runs the staged version without interrupting that session.
