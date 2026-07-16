# lazy tools
A suite of tools for mesh weathering and manipulation in Blender.
It's tools I'm using for my own workflow, even though they're not game-changers it makes sense for me to keep them all in the same suite.


Current tools are: 
* Chip: Based on PolyDamage, with added functionalities and much needed fixes.
* Decimation: A simple decimation tool, with a round of manifold fix after the operation.
* Flattening: Manipulates the mesh to delete everything below Z = 0. Unlike boolean operations, this one works.
* Dematerial: Simply removes any material from the selected objects.
* NPR: Experimental viewport line preview (silhouette, creases, material and open-edge boundaries) for NPR work. Early prototype: recomputes on demand via an "Update Lines" button rather than every frame.
* Line Art (Blender 5.0+): One-click NPR pipeline. Builds a tagged collection with a Grease Pencil Line Art object (plus a Geometry Nodes min-length stroke filter), view layers with material overrides (flat colors, toon shadow on matte white, grunge mask), and a compositor tree that edge-detects shadow/grunge contours and combines all passes. Re-running Build Pipeline converges instead of duplicating.
