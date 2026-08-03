from . import mesh_operators
from . import ui
from . import preset_operators
from . import damage


bl_info = {
    "name": "Lazy Chip",
    "author": "thelazyone",
    "location": "View3D > Sidebar > Lazy Tools",
    "blender": (5, 1, 0),
    "category": "Object",
    "version": (1, 1, 0),
    "description": "A suite of tools for mesh weathering and manipulation.",
    "warning": "",
    "doc_url": "https://github.com/thelazyone/lazy_tools",
}

def register():
    # ui.register() owns WeatheringProps and creates Scene.weathering_props.
    # Do not create that pointer here as well.
    ui.register()
    preset_operators.register()
    mesh_operators.register()
    damage.register()


def unregister():
    # Mirror of register(). ui.unregister() removes Scene.weathering_props;
    # deleting it again here used to raise AttributeError on disable.
    damage.unregister()
    mesh_operators.unregister()
    preset_operators.unregister()
    ui.unregister()


if __name__ == "__main__":
    register()
