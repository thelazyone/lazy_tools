import addon_utils
import bpy
import bmesh

# Mesh utilities.


# ---------------------------------------------------------------------------
# 3D Print Toolbox bridge
#
# This add-on borrows "Make Manifold" from the 3D Print Toolbox, which is an
# OPTIONAL dependency the user has to install themselves.
#
# Nothing about it can be hardcoded any more:
#   * It was unbundled in Blender 4.2, so the module key is no longer
#     "mesh_3d_print_toolbox" - as an extension it is "bl_ext.<repo>.<id>",
#     and <repo> depends on where the user installed it from.
#   * The extension is a rewrite by a different author, so the operator name is
#     not guaranteed to stay "mesh.print3d_clean_non_manifold" either.
#
# So both the add-on and the operator are discovered at runtime, and we let
# Blender's own poll() decide which mode the operator wants to run in.
# ---------------------------------------------------------------------------

PRINT3D_ADDON_NAME = "3D Print Toolbox"
PRINT3D_URL = "https://extensions.blender.org/add-ons/print3d-toolbox/"
MISSING_MESSAGE = (
    f"{PRINT3D_ADDON_NAME} not found. Install and enable it from "
    "Preferences > Get Extensions, then try again"
)

# Substrings that identify the toolbox, in module names and operator names.
_PRINT3D_HINTS = ("print3d", "print_3d", "3d_print")

# Operator groups worth scanning, most likely first.
_OP_GROUPS = ("mesh", "print3d", "object")


def _looks_like_print3d(name):
    low = name.lower()
    return any(hint in low for hint in _PRINT3D_HINTS)


# Resolving the operator means walking dir(bpy.ops.*), which is far too slow to
# repeat on every panel redraw. None = never looked up, False = looked up and
# missing, otherwise the operator itself.
_manifold_op_cache = None


def _scan_for_manifold_operator():
    """Locate the toolbox's 'make manifold' operator, whatever it is called.

    Returns the bpy.ops operator, or None when the toolbox is not available.
    """
    for group_name in _OP_GROUPS:
        group = getattr(bpy.ops, group_name, None)
        if group is None:
            continue
        for op_name in dir(group):
            low = op_name.lower()
            if "manifold" not in low:
                continue
            # "check"/"select" variants only report, they do not repair.
            if "check" in low or "select" in low:
                continue
            # Inside the print3d group the name is enough; elsewhere insist on
            # a print3d marker so Blender's own mesh.*_non_manifold operators
            # are not mistaken for the repair tool.
            if group_name != "print3d" and not _looks_like_print3d(op_name):
                continue
            return getattr(group, op_name)
    return None


def find_manifold_operator(refresh=False):
    """Cached lookup of the repair operator. Never has side effects."""
    global _manifold_op_cache
    if refresh or _manifold_op_cache is None:
        _manifold_op_cache = _scan_for_manifold_operator() or False
    return _manifold_op_cache or None


def _enable_print3d_addon():
    """Enable the toolbox under whatever module key it is installed as."""
    for mod in addon_utils.modules():
        name = getattr(mod, "__name__", "")
        if not _looks_like_print3d(name):
            continue
        try:
            if addon_utils.enable(name, default_set=False, persistent=True):
                return True
        except Exception as exc:  # noqa: BLE001 - never break the caller
            print(f"[Lazy Chip] Could not enable {name}: {exc}")
    return False


def get_manifold_operator():
    """Return the repair operator, enabling the toolbox first if it is off.

    May enable an add-on, so only call this from operator execute(), never from
    draw() - Blender does not allow that kind of state change during a redraw.
    """
    op = find_manifold_operator(refresh=True)
    if op is None and _enable_print3d_addon():
        op = find_manifold_operator(refresh=True)
    return op


def print3d_available():
    """True when Make Manifold looks runnable. Cheap and safe to call in draw()."""
    return find_manifold_operator() is not None


def _run_in_supported_mode(op, obj):
    """Run op, letting its own poll() choose the mode. True when it ran."""
    if op.poll():
        op()
        return True

    for mode in ('EDIT', 'OBJECT'):
        if obj.mode == mode:
            continue
        try:
            bpy.ops.object.mode_set(mode=mode)
        except RuntimeError:
            continue
        if op.poll():
            op()
            return True
    return False


# Calls the 3D Print Toolbox "make manifold" once.
def make_manifold(object: bpy.types.Object):
    op = get_manifold_operator()
    if op is None:
        print(f"[Lazy Chip] {MISSING_MESSAGE}")
        return {'CANCELLED'}

    # Save the current mode so we can return to it later
    original_mode = object.mode

    try:
        if not _run_in_supported_mode(op, object):
            print("[Lazy Chip] Make Manifold could not run on this selection")
            return {'CANCELLED'}
    except RuntimeError as exc:
        print(f"[Lazy Chip] Make Manifold failed: {exc}")
        return {'CANCELLED'}
    finally:
        # Return to the original mode
        if object.mode != original_mode:
            try:
                bpy.ops.object.mode_set(mode=original_mode)
            except RuntimeError:
                pass

    return {'FINISHED'}


# Checks if the model is watertight.
# Using https://blender.stackexchange.com/questions/160055/is-there-a-way-to-use-the-terminal-to-check-if-a-mesh-is-watertight
def is_watertight_mesh(object: bpy.types.Object, check_self_intersection=True) -> bool:
    """
    Checks whether the given object is watertight or not
    :param object: Object the inspect
    :return: True if watertight, False otherwise
    """
    
    old_active_object = bpy.context.view_layer.objects.active
    
    old_mode = old_active_object.mode

    bpy.context.view_layer.objects.active = object

    bpy.ops.object.mode_set(mode='EDIT')

    # Store the previous selection mode and switch to vertex selection mode
    previous_select_mode = tuple(bpy.context.tool_settings.mesh_select_mode)
    bpy.context.tool_settings.mesh_select_mode = (True, False, False)

    bpy.ops.mesh.select_non_manifold(extend=False)
    bm = bmesh.from_edit_mesh(object.data)

    is_watertight = True

    for v in bm.verts:
        if v.select:
            is_watertight = False
            break

    bpy.context.view_layer.objects.active = old_active_object
    bpy.ops.object.mode_set(mode=old_mode)

    # Restore the previous selection mode
    bpy.context.tool_settings.mesh_select_mode = previous_select_mode

    bpy.context.view_layer.objects.active = old_active_object
    bpy.ops.object.mode_set(mode=old_mode)

    return is_watertight


# Attempts to fix non-manifold issues, repeating the 3D Print Toolbox command
# multiple times until there are no more non-manifold issues.
def make_manifold_iterate(object: bpy.types.Object, max_iterations):
    for _ in range(max_iterations):
        if is_watertight_mesh(object):
            break
        if make_manifold(object) == {'CANCELLED'}:
            break


class LAZYCHIP_OT_fix_manifold(bpy.types.Operator):
    bl_idname = "lazychip.op_fixmanifold"
    bl_label = "Fix Manifold"
    bl_description = (
        "Make selected meshes manifold using the 3D Print Toolbox extension, "
        "which must be installed separately"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        if get_manifold_operator() is None:
            self.report({'ERROR'}, MISSING_MESSAGE)
            return {'CANCELLED'}

        fixed = 0
        failed = []
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            context.view_layer.objects.active = obj
            if make_manifold(obj) == {'FINISHED'}:
                fixed += 1
            else:
                failed.append(obj.name)

        if failed:
            self.report(
                {'WARNING'},
                f"Made {fixed} mesh(es) manifold; failed on {', '.join(failed[:3])}",
            )
        else:
            self.report({'INFO'}, f"Made {fixed} mesh(es) manifold")
        return {'FINISHED'}

def register():
    bpy.utils.register_class(LAZYCHIP_OT_fix_manifold)

def unregister():
    bpy.utils.unregister_class(LAZYCHIP_OT_fix_manifold)
