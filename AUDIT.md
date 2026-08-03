# Lazy Tools — consistency pass

Status of the audit. **Section 1 is done and needs testing. Section 2 is the backlog.**

---

# 1. Done in this pass

Every add-on had its patch version bumped, so the Reloader dropdown reflects the change.

| | Fix |
|---|---|
| **A1** | `lazy_flattening.unregister()` called `register_class` on two classes. Now unregisters, in reverse order, with the Scene pointer dropped first. |
| **A2** | `lazy_chip/__init__.py` no longer creates or deletes `Scene.weathering_props` — `ui.py` is the sole owner. `unregister()` now mirrors `register()` in reverse. |
| **A3** | `LazyChipInfoPanel` no longer collides with `WeatheringPanel`. Renamed `LAZYCHIP_PT_info`, parent `LAZYCHIP_PT_main`, `bl_category` added. **Still not registered** — the panel layout is unchanged, but adding it to `register()` is now safe. |
| **A4** | Deleted the bodyless duplicate `MATERIAL_OT_view_both` in `lazy_materials.py`. |
| **A5** | `lazy_test.make_non_manifold_iterate` now loops until watertight (was inverted — it bailed out while the mesh was still broken). `check_non_manifold` renamed `is_watertight_mesh` to match `lazy_chip`, and the misleading `is_manifold` variable renamed. |
| **A6** | Flattening now cuts at **world Z**, with `z_level` as the height. The object is no longer moved to the origin (that hack only worked for objects with no rotation or scale, and effectively cut through the object's own origin). `mesh.bisect` gets the world-space plane directly; the below-plane vertex test now transforms each vertex through `matrix_world` first. The inner `connect_vertices_within_threshold` closure was hoisted to a module-level `connect_vertices_on_plane(obj, z_level, threshold)` and made plane-aware. |
| **C3** | `lazy_crack`: `operator_crack` → `MESH_OT_crack`, `panel_crack` → `LAZYCRACK_PT_main`, `crack_settings` → `CrackSettings` (the `Scene.crack_settings` *property* name is unchanged, so existing .blend files are unaffected). `lazy_flattening`: the Panel `MESH_flatten_vertices` → `LAZYFLAT_PT_main`, so it no longer shadows the operator `MESH_OT_flatten_vertices`. |
| **"Smart" → "Lazy"** | `Smart Cracks` → **Lazy Crack**, `Smart Decimation` → **Lazy Decimation** (also the operator label, which is the button text). `Lazy Z Flattener` → **Lazy Flattening** to match its panel. All 11 add-ons now read `Lazy *`, and the `bl_info` name matches the panel label everywhere. |
| **D1** | Deleted `lazy_chip/presets.py`. `DamagePresets` was imported by nothing and its values contradicted the live defaults in `ui.py` (`fixed_scale_check` False vs True, `fixed_scale` 1/min 0 vs 4/min 0.1, `attempts_max` 100 vs 5). |
| **D2** | Removed `register_classes_from_module()` from `lazy_chip/__init__.py` — never called, and its per-attribute `print()` would have spammed the console. `import bpy` went with it, as nothing else in that file used it. |
| **D3** | `.gitignore` extended (`__pycache__/`, `*.py[cod]`, editor/OS noise). Added `.gitattributes` normalising everything to LF. Deleted the four tracked `.pyc` files. **Two git commands still need running by hand — see below.** |
| **D4** | README rewritten: one line per tool, 11 tools, NPR and "Dematerial" gone. |
| **B2** | All 11 add-ons now declare `"blender": (5, 1, 0)`. |
| **B3** | `wiki_url` → `doc_url` pointing at the repo, on all 11. |
| **C1** | All versions are 3-tuples and bumped (see table below). |
| **C2** | Panel idnames all follow `X_PT_y`, and **sidebar order is now explicit** — see below. |
| **C4** | `REGISTER, UNDO` added to `mesh.crack`, `mesh.decimate_and_fix`, `mesh.quick_decimate`, `mesh.just_fix`, `mesh.flatten_vertices`. Also gave `quick_decimate.ratio` a `default=0.5` (it was 0.0, below its own `min`). |
| **C5** | `materials`, `crack`, `decimation`, `flattening`, `transform` now delete the `Scene` PointerProperty *before* unregistering the PropertyGroup, and unregister in reverse registration order. |
| **C6** | `lazy_local_view` gained a sidebar panel — so it now appears in the Lazy Tools tab **and in the Reloader dropdown**, which discovers add-ons by scanning panels. |
| **C7** | Keymaps register against the correct space types (`Object Mode` and `Mesh` are `EMPTY`, not `VIEW_3D` — the old code silently created two dead keymaps). `unregister` is now tolerant of already-freed keymaps. |
| **C8** | `lazy_transform`: `math.radians()` instead of `3.14159/180`; `scale_variation` moved to a new `LazyTransformSettings` PropertyGroup so the slider stops resetting on every redraw; `set_local_z_to_view.poll` now checks object mode and `region_data`. |

### Sidebar ordering (C2)

The old `AAA_` idname prefix didn't work because panels with equal `bl_order` fall back to
**registration order**, not alphabetical order. Every panel now carries an explicit `bl_order`:

| order | panel | add-on |
|---|---|---|
| 0 | `LAZYRELOAD_PT_main` | Lazy Reloader |
| 10 | `LAZYCHIP_PT_main` | Lazy Chip |
| 20 | `LAZYCRACK_PT_main` | Lazy Crack |
| 30 | `LAZYFLAT_PT_main` | Lazy Flattening |
| 40 | `LAZYDEC_PT_main` | Lazy Decimation |
| 50 | `MATERIAL_PT_custom_panel` | Lazy Materials |
| 60 | `TRANSFORM_PT_lazy_panel` | Lazy Transform Tools |
| 70 | `LAZYLOCALVIEW_PT_main` | Lazy Local View *(new)* |
| 80 | `LZRG_PT_panel` | Lazy Render Groups |
| 90 | `LINEART_PT_lazy_panel` | Lazy Line Art |
| 100 | `OBJECT_PT_test_make_manifold` | Lazy Tests |

`bl_order` is unsigned, so 0 is as high as a panel can sit. Gaps of 10 leave room to insert.

### Versions after the bump

| Add-on | was | now |
|---|---|---|
| Lazy Line Art | `(0, 1, 37)` | `(0, 1, 38)` |
| Lazy Reloader | `(0, 1, 6)` | `(0, 1, 7)` |
| Lazy Render Groups | `(0, 1, 3)` | `(0, 1, 4)` |
| Lazy Decimation *(was Smart)* | `(1, 0, 7)` | `(1, 0, 9)` |
| Lazy Crack *(was Smart Cracks)* | `(1, 0, 6)` | `(1, 0, 9)` |
| Lazy Chip | `(1, 0, 1)` | `(1, 0, 3)` |
| Lazy Local View | `(1, 1)` | `(1, 1, 1)` |
| Lazy Flattening *(was Lazy Z Flattener)* | `(1, 0)` | `(1, 1, 1)` — middle digit bumped, A6 is a feature |
| Lazy Materials | `(1, 0)` | `(1, 0, 1)` |
| Lazy Tests | `(1, 0)` | `(1, 0, 1)` |
| Lazy Transform Tools | `(1, 0)` | `(1, 0, 1)` |

### Two git commands to run yourself

The sandbox can't write to the git index, so these are left for you. The `.pyc` files are already
deleted from disk, so committing picks that up; the renormalise is what stops every file showing
as fully rewritten in future diffs.

```
git add --renormalize .
git add -A
git commit -m "Lazy Tools: consistency pass, rename Smart->Lazy, normalise line endings"
```

Expect the renormalise commit to touch nearly every file once. After that, diffs should be clean.

### What to test

1. **Enable → disable → re-enable** Lazy Chip and Lazy Z Flattener. Both used to throw; this is the main thing to confirm.
2. **Reloader dropdown**: should list 10 add-ons including Lazy Local View, all with 3-part versions, and the Reloader panel should sit at the top of the sidebar.
3. **Ctrl+Z** after Crack, Lazy Decimation, Quick Decimate, Just Fix, Flatten.
4. **Local View**: sidebar button and Shift+/ in object mode *and* edit mode.
5. **Transform**: the Variation % slider should hold its value now.
6. **Flattening** — the behaviour change worth checking carefully: try it on an object whose
   origin is *not* at world Z=0, and on one that is rotated or scaled. The cut should land at
   the Z coordinate in the panel, not at the object's origin. Set the slider to a non-zero
   value to confirm it's actually read.

---

# 2. Deferred

### B1 — Fix Manifold is broken on Blender 5.x *(highest-value item left)*
`lazy_chip/mesh_operators.py:11-13` and `lazy_test.py:21-23`:
```python
addon_key = 'mesh_3d_print_toolbox'
if addon_key not in bpy.context.preferences.addons:
    bpy.ops.preferences.addon_enable(module=addon_key)
```
3D Print Toolbox was unbundled in Blender 4.2 and now ships from extensions.blender.org, where
the module key is an extension path (`bl_ext.<repo>.print3d_toolbox`), not `mesh_3d_print_toolbox`.
On 5.1 `addon_enable` raises *before* the `try` that was meant to catch the failure.

Intended fix — feature-detect the operator rather than enabling by module name:
```python
if not hasattr(bpy.ops.mesh, "print3d_clean_non_manifold"):
    self.report({'ERROR'}, "3D Print Toolbox extension not installed/enabled")
    return {'CANCELLED'}
```
Affects Lazy Chip's **Fix Manifold** and **Apply Damage**, and Lazy Tests' Make Manifold.
Goal is to make it actually work, not just fail gracefully.

### Known quirk left alone in flattening
`mesh.separate(type='LOOSE')` leaves one island in the original object, and the fill loop skips
that object (`if separated_obj is obj: continue`). So the original island never gets the
`region_to_loop` + connect-verts treatment. This is pre-existing behaviour and was left as-is
rather than silently changed — worth deciding on once you've tested the new Z handling.

### D5 — Unused imports
`lazy_crack` (`bmesh`, `BVHTree`), `lazy_chip/damage` (`bmesh`, `mathutils`), `lazy_materials`
(`bmesh`), `lazy_transform` (`bmesh`), `lazy_test` (`Vector`, `Matrix`), `lazy_decimation`
(`IntProperty`), `lazy_flattening` (`math`).

`lazy_crack.py`'s import block is byte-identical to `lazy_decimation.py`'s including the unused
entries — it was copy-pasted. Its `bl_info` also sits after the imports, unlike every other file.
