bl_info = {
    "name": "Lazy Reloader",
    "author": "thelazyone",
    "version": (0, 1, 6),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Lazy Tools",
    "description": "Hard-reload addons that register panels in the Lazy Tools sidebar",
    "category": "Development",
}

import importlib
import os
import shutil
import sys

import addon_utils
import bpy
from bpy.props import EnumProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup


LAZY_CATEGORY = "Lazy Tools"
_SELF_MODULE_MARKERS = ("lazy_reload",)
# Blender requires dynamic EnumProperty item tuples to stay referenced.
_enum_item_cache = []


def _iter_subclasses(cls):
    stack = list(cls.__subclasses__())
    seen = set()
    while stack:
        sub = stack.pop()
        if sub in seen:
            continue
        seen.add(sub)
        yield sub
        stack.extend(sub.__subclasses__())


def _is_self_module(module_name):
    parts = module_name.split(".")
    return any(marker in parts for marker in _SELF_MODULE_MARKERS)


def _short_module_name(module_name):
    return module_name.rsplit(".", 1)[-1]


def _addon_module_for(class_module):
    """Map a panel's __module__ to the installed addon module key."""
    if not class_module or class_module == "__main__":
        return None

    matches = [
        addon
        for addon in bpy.context.preferences.addons.keys()
        if class_module == addon or class_module.startswith(addon + ".")
    ]
    if matches:
        return max(matches, key=len)

    parts = class_module.split(".")
    if parts[0] == "bl_ext" and len(parts) >= 3:
        return ".".join(parts[:3])
    return parts[0]


def _addon_display_name(module_name):
    info = _addon_bl_info(module_name)
    if info and info.get("name"):
        return info["name"]
    return _short_module_name(module_name)


def _addon_bl_info(module_name):
    mod = sys.modules.get(module_name)
    if mod is not None:
        info = getattr(mod, "bl_info", None)
        if isinstance(info, dict):
            return info

    for mod in addon_utils.modules():
        if getattr(mod, "__name__", None) == module_name:
            return addon_utils.module_bl_info(mod)
    return None


def _format_version(info):
    if not info:
        return "?"
    version = info.get("version")
    if not version:
        return "?"
    return ".".join(str(part) for part in version)


def _installed_addon_path(module_name):
    """Return (path, is_directory) for the installed addon, or (None, False)."""
    for mod in addon_utils.modules():
        if getattr(mod, "__name__", None) != module_name:
            continue
        filepath = getattr(mod, "__file__", None)
        if not filepath or not os.path.exists(filepath):
            return None, False
        if os.path.basename(filepath) == "__init__.py":
            return os.path.dirname(filepath), True
        return filepath, False
    return None, False


_updating_source_directory = False


def _addon_prefs():
    """Find Lazy Reloader preferences even when installed as an extension."""
    prefs = bpy.context.preferences.addons
    for key in (__name__, __package__ or ""):
        if key and key in prefs:
            return prefs[key].preferences
    for key, addon in prefs.items():
        if _is_self_module(key):
            return addon.preferences
    return None


def _save_userpref():
    """Persist addon preferences so the folder survives new .blend files."""
    try:
        bpy.ops.wm.save_userpref()
    except Exception as exc:
        print(f"[Lazy Reloader] Could not save user preferences: {exc}")


def _absolutize_directory(path):
    path = (path or "").strip()
    if not path:
        return ""
    return os.path.normpath(bpy.path.abspath(path))


def _update_source_directory(self, context):
    """Store an absolute path and write it into userpref.blend."""
    global _updating_source_directory
    if _updating_source_directory:
        return

    abs_path = _absolutize_directory(self.source_directory)
    if abs_path != self.source_directory:
        _updating_source_directory = True
        try:
            self.source_directory = abs_path
        finally:
            _updating_source_directory = False

    _save_userpref()


def _source_directory():
    """Folder where you edit Lazy Tools sources (git checkout, etc.)."""
    addon_prefs = _addon_prefs()
    if addon_prefs is not None:
        path = _absolutize_directory(addon_prefs.source_directory)
        if path:
            return path

    # Last resort: folder that contains this reloader file.
    return os.path.dirname(os.path.realpath(__file__))


def _source_addon_path(module_name):
    """Resolve the development copy of an addon under the source directory."""
    root = _source_directory()
    short = _short_module_name(module_name)

    package_dir = os.path.join(root, short)
    if os.path.isdir(package_dir) and os.path.isfile(os.path.join(package_dir, "__init__.py")):
        return package_dir, True

    py_file = os.path.join(root, f"{short}.py")
    if os.path.isfile(py_file):
        return py_file, False

    return None, False


def _path_in_addon_search_paths(path):
    path = os.path.realpath(path)
    parent = path if os.path.isdir(path) else os.path.dirname(path)
    for addon_path in addon_utils.paths():
        try:
            if os.path.samefile(parent, addon_path):
                return True
        except OSError:
            if os.path.normcase(os.path.realpath(addon_path)) == os.path.normcase(parent):
                return True
    return False


def _copy_tree(src, dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    elif os.path.isfile(dst) or os.path.islink(dst):
        os.remove(dst)
    shutil.copytree(src, dst)


def _reinstall_from_source(module_name, source_path, source_is_dir):
    """
    Replace the installed addon files with the source copy.
    Returns a short description of what happened.
    """
    installed_path, installed_is_dir = _installed_addon_path(module_name)

    # Already running directly from the source folder — nothing to copy.
    if installed_path and os.path.exists(installed_path):
        try:
            if os.path.samefile(installed_path, source_path):
                return f"live at {source_path}"
        except OSError:
            if os.path.normcase(os.path.realpath(installed_path)) == os.path.normcase(
                os.path.realpath(source_path)
            ):
                return f"live at {source_path}"

    # Prefer Blender's installer when the source is outside addon search paths.
    if not source_is_dir and not _path_in_addon_search_paths(source_path):
        result = bpy.ops.preferences.addon_install(
            filepath=source_path,
            overwrite=True,
            enable_on_install=False,
        )
        if "FINISHED" in result:
            return f"reinstalled from {source_path}"

    # Manual overwrite (packages, or source already under a scripts path).
    path_addons = bpy.utils.user_resource("SCRIPTS", path="addons", create=True)
    if source_is_dir:
        dest = os.path.join(path_addons, os.path.basename(source_path))
        if installed_path and installed_is_dir:
            dest = installed_path
        _copy_tree(source_path, dest)
        return f"copied package to {dest}"

    dest = os.path.join(path_addons, os.path.basename(source_path))
    if installed_path and not installed_is_dir:
        dest = installed_path
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(source_path, dest)
    return f"copied file to {dest}"


def discover_lazy_tools_addons():
    """Find enabled addons that own a View3D panel in the Lazy Tools tab."""
    found = {}

    for cls in _iter_subclasses(bpy.types.Panel):
        if getattr(cls, "bl_category", None) != LAZY_CATEGORY:
            continue
        if getattr(cls, "bl_space_type", None) != "VIEW_3D":
            continue

        addon_module = _addon_module_for(getattr(cls, "__module__", ""))
        if not addon_module or _is_self_module(addon_module):
            continue
        if addon_module not in bpy.context.preferences.addons:
            continue

        if addon_module not in found:
            found[addon_module] = {
                "module": addon_module,
                "name": _addon_display_name(addon_module),
                "panels": [],
            }
        label = getattr(cls, "bl_label", cls.__name__)
        if label not in found[addon_module]["panels"]:
            found[addon_module]["panels"].append(label)

    return sorted(found.values(), key=lambda item: item["name"].lower())


def _enum_items(self, context):
    addons = discover_lazy_tools_addons()
    if not addons:
        items = [("NONE", "No Lazy Tools addons found", "")]
    else:
        items = []
        for addon in addons:
            info = _addon_bl_info(addon["module"])
            version = _format_version(info)
            panels = ", ".join(addon["panels"])
            items.append((
                addon["module"],
                f"{addon['name']} ({version})",
                f"Panels: {panels}" if panels else addon["module"],
            ))

    _enum_item_cache.clear()
    _enum_item_cache.extend(items)
    return _enum_item_cache


def _purge_modules(module_name):
    names = [
        name
        for name in list(sys.modules)
        if name == module_name or name.startswith(module_name + ".")
    ]
    names.sort(key=lambda name: name.count("."), reverse=True)
    for name in names:
        del sys.modules[name]


def hard_reload_addon(module_name):
    """
    Disable, reinstall from the source directory when needed, purge modules,
    refresh addon meta-data, then enable again.
    """
    if module_name in ("", "NONE"):
        raise ValueError("No addon selected")
    if _is_self_module(module_name):
        raise ValueError("Refusing to reload Lazy Reloader from itself")

    was_enabled = module_name in bpy.context.preferences.addons
    source_path, source_is_dir = _source_addon_path(module_name)
    errors = []
    actions = []

    if was_enabled:
        addon_utils.disable(module_name, default_set=False)

    # Drop cached modules before replacing files so nothing keeps old code.
    _purge_modules(module_name)
    importlib.invalidate_caches()

    if source_path:
        try:
            actions.append(_reinstall_from_source(module_name, source_path, source_is_dir))
        except Exception as exc:
            errors.append(f"reinstall: {exc}")
    else:
        actions.append(f"no source file for {_short_module_name(module_name)} in {_source_directory()}")

    # Force Preferences / addon_utils to re-read bl_info from disk.
    try:
        addon_utils.modules_refresh()
    except Exception as exc:
        errors.append(f"modules_refresh: {exc}")

    bpy.utils.refresh_script_paths()

    if was_enabled:
        mod = addon_utils.enable(module_name, default_set=False, persistent=True)
        if mod is None:
            errors.append(f"Failed to enable {module_name}")

    info = _addon_bl_info(module_name)
    version = _format_version(info)
    return errors, version, actions


class LazyReloadPreferences(AddonPreferences):
    bl_idname = __name__

    source_directory: StringProperty(
        name="Folder",
        description=(
            "Folder where you edit Lazy Tools .py files / packages "
            "(e.g. C:\\Projects\\lazy_tools). Saved in Blender preferences "
            "so it persists across new files"
        ),
        subtype="DIR_PATH",
        default="",
        update=_update_source_directory,
    )

    def draw(self, context):
        self.layout.prop(self, "source_directory", text="Folder")


class LazyReloadProps(PropertyGroup):
    target_addon: EnumProperty(
        name="Addon",
        description="Lazy Tools addon to hard-reload",
        items=_enum_items,
    )


class LAZYRELOAD_OT_reload(Operator):
    bl_idname = "lazyreload.reload"
    bl_label = "Reload Addon"
    bl_description = (
        "Disable the selected addon, reinstall it from the source directory, "
        "purge Python modules, refresh bl_info, then enable it again"
    )
    bl_options = {"REGISTER"}

    module: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        module_name = self.module or context.scene.lazy_reload_props.target_addon
        display = _addon_display_name(module_name) if module_name not in ("", "NONE") else module_name

        try:
            errors, version, actions = hard_reload_addon(module_name)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        for action in actions:
            print(f"[Lazy Reloader] {action}")

        if errors:
            self.report({"WARNING"}, f"Reloaded {display} v{version} with issues: {errors[0]}")
            for err in errors:
                print(f"[Lazy Reloader] {err}")
            return {"FINISHED"}

        self.report({"INFO"}, f"Reloaded {display} v{version}")
        return {"FINISHED"}


class LAZYRELOAD_PT_panel(Panel):
    # Leading "AAA_" wins alphabetical tie-break among bl_order == 0 panels.
    bl_idname = "AAA_PT_lazy_reload"
    bl_label = "Lazy Reloader"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = LAZY_CATEGORY
    # Must be >= 0 (negative values get clamped/wrapped and end up at the bottom).
    bl_order = 0



    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        props = context.scene.lazy_reload_props
        addon_prefs = _addon_prefs()

        row = layout.row(align=True)
        if addon_prefs is not None:
            row.prop(addon_prefs, "source_directory", text="Folder")
        else:
            row.label(text="Enable addon to save Folder", icon="ERROR")

        row = layout.row(align=True)
        row.prop(props, "target_addon", text="")
        op = row.operator(LAZYRELOAD_OT_reload.bl_idname, text="", icon="FILE_REFRESH")
        op.module = props.target_addon


classes = (
    LazyReloadPreferences,
    LazyReloadProps,
    LAZYRELOAD_OT_reload,
    LAZYRELOAD_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.lazy_reload_props = bpy.props.PointerProperty(type=LazyReloadProps)


def unregister():
    del bpy.types.Scene.lazy_reload_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
