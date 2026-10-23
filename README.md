# Lazy Tools

A suite of Blender add-ons for mesh weathering and manipulation, used in my own miniature
sculpting workflow. Every tool lives in the **Lazy Tools** tab of the 3D viewport sidebar.

Targets Blender 5.1.

## Tools

* **Lazy Reloader** — hot-reloads any Lazy Tools add-on from source, without restarting Blender.
* **Lazy Chip** — mesh chipping and weathering, with presets for wood, stone and concrete.
* **Lazy Crack** — volumetric cracks with infill, placed interactively.
* **Lazy Flattening** — crops a mesh at a world-space Z plane and caps the opening.
* **Lazy Decimation** — decimation with an optional remesh pass and manifold cleanup afterwards.
* **Lazy Materials** — material cleanup, UV generation, colour palettes and AO baking.
* **Lazy Transform Tools** — random rotation and scale for selected objects, plus view-aligned posing.
* **Lazy Local View** — isolates the selection while keeping every scene light visible (Shift + /).
* **Lazy Render Groups** — organises models into switchable collections and batch-renders them.
* **Lazy Line Art** — one-click NPR pipeline: line art, shadow and grunge passes, compositor combine.
* **Lazy Tests** — scratch panel for utilities in progress.

## External dependency

**Lazy Chip's Fix Manifold, Apply Damage and Lazy Tests need the
[3D Print Toolbox](https://extensions.blender.org/add-ons/print3d-toolbox/) extension.** It is
not bundled with Blender any more, so install it yourself from *Preferences > Get Extensions*.
Without it those tools report an error and Lazy Chip shows a warning in the panel; everything
else works normally.

## Install

Install each `.py` file (or the `lazy_chip` folder) as a Blender add-on. Point Lazy Reloader at
this repo in its preferences to reload the others in place while editing.

## Lazy Line Art and Path Companion

The complete illustration workflow now lives in **[Lazy Line Art](Lazy%20Line%20Art/README.md)**:
Blender add-on, native companion, launcher, saved defaults, legacy companion and tests.
Open `Lazy Line Art/Path Companion.exe` or its launcher. Lazy Reloader also finds the
add-on in this folder when its source directory is set to the Lazy Tools root.
