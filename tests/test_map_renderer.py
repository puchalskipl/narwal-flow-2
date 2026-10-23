"""Testy narwal_client.map_renderer: obraz bazowy, nakładka, etykiety, meble."""

from __future__ import annotations

import io
import zlib

from narwal_client.map_renderer import (
    MAP_MARGIN_CELLS,
    MAP_RENDER_SCALE,
    _decode_packed_varints,
    _transform_point,
    decompress_map,
    lookup_room_at_grid,
    render_base_map,
    render_map_png,
    render_overlay,
    room_label_points,
)
from narwal_client.models import ObstacleInfo


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _compressed(width: int, height: int, cell) -> bytes:
    """Siatka width × height, `cell(x, y)` zwraca wartość piksela."""
    body = b"".join(_varint(cell(x, y)) for y in range(height) for x in range(width))
    return zlib.compress(b"\x0a" + _varint(len(body)) + body)


def _two_rooms(width: int = 40, height: int = 30) -> bytes:
    """Dwa pokoje obok siebie z pasem ściany dookoła, reszta poza mapą."""

    def cell(x: int, y: int) -> int:
        if 2 <= x < 38 and 2 <= y < 28:
            if x < 4 or x >= 36 or y < 4 or y >= 26:
                return (1 << 8) | 0x08  # ściana
            room = 1 if x < 20 else 2
            return (room << 8) | 0x01
        return 0

    return _compressed(width, height, cell)


class TestDecode:
    def test_decompress_and_decode(self) -> None:
        compressed = _two_rooms()
        pixels = _decode_packed_varints(decompress_map(compressed))
        assert len(pixels) == 40 * 30
        assert pixels[10 * 40 + 10] == (1 << 8) | 0x01
        assert pixels[10 * 40 + 30] == (2 << 8) | 0x01

    def test_empty(self) -> None:
        assert decompress_map(b"") == b""
        assert _decode_packed_varints(b"") == []

    def test_lookup_room(self) -> None:
        compressed = _two_rooms()
        assert lookup_room_at_grid(compressed, 40, 30, 10, 10) == (1, "room_1")
        assert lookup_room_at_grid(compressed, 40, 30, 30, 10) == (2, "room_2")
        assert lookup_room_at_grid(compressed, 40, 30, 2, 2)[1] == "room_1 wall"
        assert lookup_room_at_grid(compressed, 40, 30, 0, 0) == (0, "(empty)")
        assert lookup_room_at_grid(compressed, 40, 30, 99, 0)[0] == -1


class TestRenderBaseMap:
    def test_returns_cropped_image_with_placement_info(self) -> None:
        img = render_base_map(_two_rooms(), 40, 30)
        assert img is not None
        assert img.mode == "RGBA"
        # Zawartość zajmuje komórki 2–37 × 2–27, margines nie wychodzi poza siatkę.
        assert 36 * MAP_RENDER_SCALE <= img.width <= 40 * MAP_RENDER_SCALE
        assert 26 * MAP_RENDER_SCALE <= img.height <= 30 * MAP_RENDER_SCALE
        assert img.info["scale"] == MAP_RENDER_SCALE
        assert img.info["grid_height"] == 30
        assert img.info["crop_origin"] == (0, 0)
        assert MAP_MARGIN_CELLS > 0

    def test_rooms_have_distinct_pastel_colors_and_walls_are_dark(self) -> None:
        img = render_base_map(_two_rooms(), 40, 30)
        assert img is not None
        ox, oy = img.info["crop_origin"]
        s = MAP_RENDER_SCALE
        room1 = img.getpixel((int((10 + 0.5) * s) - ox, int((30 - 1 - 10 + 0.5) * s) - oy))
        room2 = img.getpixel((int((30 + 0.5) * s) - ox, int((30 - 1 - 10 + 0.5) * s) - oy))
        wall = img.getpixel((int((3 + 0.5) * s) - ox, int((30 - 1 - 10 + 0.5) * s) - oy))
        assert room1[3] == 255 and room2[3] == 255
        assert room1[:3] != room2[:3]
        assert sum(room1[:3]) > 600 and sum(room2[:3]) > 600
        assert sum(wall[:3]) < 300
        # Poza mapą przezroczyste.
        assert img.getpixel((0, 0))[3] == 0

    def test_empty_and_zero_dimensions(self) -> None:
        assert render_base_map(b"", 100, 100) is None
        assert render_base_map(_two_rooms(), 0, 30) is None
        assert render_base_map(_two_rooms(), 40, 0) is None

    def test_furniture_changes_image(self) -> None:
        compressed = _two_rooms()
        plain = render_base_map(compressed, 40, 30)
        bed = ObstacleInfo(id=1, type_id=1, center_x=10.0, center_y=15.0, width=8.0, height=5.0)
        with_bed = render_base_map(compressed, 40, 30, obstacles=[bed], origin_x=0, origin_y=0)
        assert plain is not None and with_bed is not None
        assert plain.tobytes() != with_bed.tobytes()
        assert render_base_map(compressed, 40, 30, obstacles=[]).tobytes() == plain.tobytes()

    def test_furniture_outside_grid_is_ignored_without_error(self) -> None:
        far = ObstacleInfo(id=1, type_id=14, center_x=500.0, center_y=500.0, width=8.0, height=5.0)
        assert render_base_map(_two_rooms(), 40, 30, obstacles=[far]) is not None


class TestRoomLabelPoints:
    def test_centroids_and_room_width(self) -> None:
        points = room_label_points(_two_rooms(), 40, 30, {1: "Salon", 2: "Kuchnia"})
        by_name = {p[0]: p for p in points}
        assert set(by_name) == {"Salon", "Kuchnia"}
        name, cx, cy, width_cells = by_name["Salon"]
        assert 4 <= cx < 20 and 4 <= cy < 26
        assert width_cells == 16
        assert by_name["Kuchnia"][1] > cx

    def test_unnamed_and_missing_rooms_skipped(self) -> None:
        points = room_label_points(_two_rooms(), 40, 30, {1: "", 7: "Nie ma"})
        assert points == []
        assert room_label_points(b"", 40, 30, {1: "Salon"}) == []


class TestRenderOverlay:
    def _base(self):
        base = render_base_map(_two_rooms(), 40, 30)
        assert base is not None
        return base

    @staticmethod
    def _png(data: bytes):
        from PIL import Image

        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        return Image.open(io.BytesIO(data)).convert("RGBA")

    def test_returns_png_of_base_size(self) -> None:
        base = self._base()
        img = self._png(render_overlay(base, 30))
        assert img.size == base.size

    def test_does_not_modify_base(self) -> None:
        base = self._base()
        before = base.tobytes()
        render_overlay(base, 30, robot_x=10.0, robot_y=10.0, robot_heading=45.0,
                       trail=[(5.0, 5.0), (10.0, 10.0)], room_labels=[("Salon", 10.0, 15.0, 16.0)],
                       dock_x=30.0, dock_y=10.0)
        assert base.tobytes() == before

    def test_trail_robot_dock_and_labels_change_pixels(self) -> None:
        base = self._base()
        empty = self._png(render_overlay(base, 30))
        full = self._png(render_overlay(
            base, 30, robot_x=10.0, robot_y=10.0, robot_heading=90.0,
            trail=[(6.0, 6.0), (6.0, 20.0), (float("nan"), float("nan")), (30.0, 6.0), (30.0, 20.0)],
            room_labels=[("Salon", 10.0, 15.0, 16.0), ("Garderoba sypialnia", 30.0, 15.0, 16.0)],
            dock_x=30.0, dock_y=10.0,
        ))
        assert empty.tobytes() != full.tobytes()
        # Robot: biały środek w komórce (10, 10).
        s = MAP_RENDER_SCALE
        ox, oy = base.info["crop_origin"]
        # Obok kreski kierunku (kierunek 90 = w górę), w obrębie białego kółka.
        robot_px = full.getpixel((int((10 + 0.5) * s + 1.2 * s) - ox, int((30 - 1 - 10 + 0.5) * s) - oy))
        assert robot_px[:3] == (255, 255, 255)

    def test_base_without_placement_info_uses_height_ratio(self) -> None:
        from PIL import Image

        base = Image.new("RGBA", (40 * 3, 30 * 3), (200, 200, 200, 255))
        png = render_overlay(base, 30, robot_x=10.0, robot_y=10.0, trail=[(1.0, 1.0), (5.0, 5.0)])
        assert self._png(png).size == base.size

    def test_unsupported_rotation_uses_unrotated_transform(self) -> None:
        assert _transform_point(3.0, 4.0, 40, 30, 45, 1.0) == (3.0, 4.0)
        assert _transform_point(3.0, 4.0, 40, 30, 90, 1.0) == (30 - 1 - 4.0, 3.0)

    def test_rotation_and_zoom_change_size(self) -> None:
        base = self._base()
        rotated = self._png(render_overlay(base, 30, rotation_degrees=90))
        assert rotated.size == (base.height, base.width)
        zoomed = self._png(render_overlay(base, 30, zoom=2.0))
        assert zoomed.width == base.width // 2


class TestRenderMapPng:
    def test_round_trip(self) -> None:
        from PIL import Image

        png = render_map_png(decompress_map(_two_rooms()), 40, 30, 10.0, 10.0, 0.0, 30.0, 10.0,
                             {1: "Salon", 2: "Kuchnia"})
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        img = Image.open(io.BytesIO(png))
        assert img.size[0] > 0

    def test_empty_returns_no_bytes(self) -> None:
        assert render_map_png(b"", 40, 30) == b""
        assert render_map_png(decompress_map(_two_rooms()), 0, 30) == b""
