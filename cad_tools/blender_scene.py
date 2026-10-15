"""Mesh, color and camera primitives; import only inside Blender."""

from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from cad_tools.preview_config import PreviewView, RenderStyle


def color(hex_value: str) -> tuple[float, float, float, float]:
    values = [int(hex_value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values) + (1,)


def import_mesh(path: Path, name: str, shade: str) -> bpy.types.Object:
    bpy.ops.wm.stl_import(filepath=str(path))
    obj = bpy.context.object
    obj.name = name
    obj.color = color(shade)
    return obj


def view_camera(
    camera: bpy.types.Object,
    eye: tuple[float, float, float],
    target: tuple[float, float, float],
    scale: float,
    up_axis: tuple[float, float, float] = (0, 1, 0),
) -> None:
    camera.location = eye
    forward = (Vector(target) - camera.location).normalized()
    right = forward.cross(Vector(up_axis)).normalized()
    up = right.cross(forward)
    camera.rotation_euler = Matrix((right, up, -forward)).transposed().to_euler()
    camera.data.ortho_scale = scale


def add_material(name: str, color: tuple[float, float, float, float]) -> bpy.types.Material:
    result = bpy.data.materials.new(name)
    result.diffuse_color = color
    return result


def mesh_bounds(
    obj: bpy.types.Object,
) -> tuple[float, float, float, float, float, float]:
    points = [vertex.co for vertex in obj.data.vertices]
    return (
        min(point.x for point in points),
        max(point.x for point in points),
        min(point.y for point in points),
        max(point.y for point in points),
        min(point.z for point in points),
        max(point.z for point in points),
    )


def render_preview(path: Path, view: PreviewView, style: RenderStyle, caption: str = "") -> None:
    """Render a prepared scene with an orthographic camera and lossless WebP."""
    bpy.ops.object.camera_add()
    camera = bpy.context.object
    camera.data.type = "ORTHO"
    camera.data.clip_end = style.clip_end
    view_camera(camera, view.eye, view.target, view.scale, view.up_axis)
    scene = bpy.context.scene
    scene.camera = camera
    if caption:
        if style.banner:
            bpy.ops.mesh.primitive_plane_add(size=2)
            banner = bpy.context.object
            banner.parent = camera
            banner.location = (0, view.scale * 0.456, -90)
            banner.scale = (view.scale * 0.49, view.scale * 0.044, 1)
            banner.color = (0.92, 0.93, 0.95, 1)
        bpy.ops.object.text_add()
        title = bpy.context.object
        title.parent = camera
        title.location = (0, view.scale * 0.45, -80)
        title.data.body = caption
        title.data.align_x = "CENTER"
        title.data.align_y = "TOP_BASELINE"
        title.data.size = view.scale * style.title_size
        title.color = (0.03, 0.04, 0.05, 1)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.color_type = style.color_type
    scene.display.shading.light = style.light
    scene.display.shading.show_cavity = style.cavity
    scene.display.shading.cavity_type = "BOTH"
    scene.display.shading.background_type = "WORLD"
    scene.world.color = (0.92, 0.93, 0.95)
    if style.view_transform:
        scene.view_settings.view_transform = style.view_transform
    scene.view_settings.exposure = style.exposure
    # Dithering noise adds no useful detail and inflates lossless files.
    scene.render.dither_intensity = 0
    scene.render.resolution_x, scene.render.resolution_y = view.resolution
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "WEBP"
    scene.render.image_settings.color_mode = "RGB"
    # Blender chooses lossless WebP (VP8L) at quality 100.
    scene.render.image_settings.quality = 100
    scene.render.image_settings.color_depth = "8"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
