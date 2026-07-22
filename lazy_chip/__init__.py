import bpy
from . import mesh_operators
from . import ui
from . import preset_operators
from . import damage


bl_info = {
    "name": "Lazy Chip",
    "author": "thelazyone",
    "location": "View3d > Toolbar > Lazy Tools",
    "blender": (4, 0, 0),
    "category": "Object",
    "version": (1, 0, 1),
    "description": "A suite of tools for mesh weathering and manipulation."
}

# Function to automatically register all classes from the modules
def register_classes_from_module(module):
    for attr_name in dir(module):
        print(f"Trying register {attr_name}")
        attr = getattr(module, attr_name)
        if isinstance(attr, type) and (issubclass(attr, bpy.types.Operator) or issubclass(attr, bpy.types.Panel)):
            try:
                bpy.utils.register_class(attr)
            except ValueError as e:
                print(f"Skipping registration for {attr.__name__}: {e}")
                

def register():

    # Registering the other files first:
    ui.register()
    preset_operators.register()
    mesh_operators.register()
    damage.register()

    # Creating local variable
    bpy.types.Scene.weathering_props = bpy.props.PointerProperty(type=ui.WeatheringProps)


def unregister():
    ui.unregister()
    preset_operators.unregister()
    mesh_operators.unregister()
    damage.unregister()

    del bpy.types.Scene.weathering_props


if __name__ == "__main__":
    register()
