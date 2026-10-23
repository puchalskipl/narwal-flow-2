"""Renderer mapy Narwal Flow 2 w stylu aplikacji Narwal.

Czysty Pillow, bez zależności od Home Assistant.

Dane mapy (sprawdzone na robocie):
  - pole 2.17 get_map: zlib, w środku protobuf z polem 1 = spakowane varinty,
  - każdy varint: room_id = wartość >> 8, typ piksela = wartość & 0xFF,
  - 0 = poza mapą, 0x20 = podłoga bez pokoju, 0x28 = przeszkoda bez pokoju,
  - bit 0x01 podłoga, bit 0x08 przeszkoda/ściana (pas 2–3 komórek wokół pokoi),
    bit 0x10 strefa pod meblami (łóżka itp.), bit 0x02 zacięcia/przeszkody w trakcie
    sprzątania. Rysowane są tylko podłoga, ściana stykająca się z podłogą i meble
    z listy mebli mapy (pole 2.32), reszta bitów jest pomijana.

Obraz bazowy (pokoje, ściany, meble) jest przycięty do zawartości, a przesunięcie
i skala siedzą w `Image.info` (`crop_origin`, `scale`), żeby nakładka (ślad, etykiety,
stacja, robot) trafiała w te same miejsca.
"""

from __future__ import annotations

import io
import logging
import math
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image, ImageDraw

_LOGGER = logging.getLogger(__name__)

MAP_RENDER_SCALE = 4
MAP_MARGIN_CELLS = 3

# Pastele jak w aplikacji Narwal, indeks = (room_id - 1) % len.
ROOM_COLORS: list[tuple[int, int, int]] = [
    (213, 203, 242),
    (190, 225, 247),
    (222, 206, 236),
    (198, 202, 242),
    (190, 236, 232),
    (205, 240, 198),
    (192, 206, 246),
    (196, 206, 246),
    (196, 210, 246),
    (190, 236, 232),
]
COLOR_UNKNOWN = (0, 0, 0, 0)
COLOR_UNASSIGNED_FLOOR = (222, 224, 230)
COLOR_WALL = (52, 60, 100)
COLOR_TEXT = (52, 60, 100)
COLOR_FURNITURE_FILL = (255, 255, 255, 170)
COLOR_FURNITURE_LINE = (52, 60, 100, 230)
COLOR_TRAIL = (90, 110, 170, 160)
COLOR_ROBOT_FILL = (255, 255, 255, 255)
COLOR_DOCK_LED = (80, 190, 120, 255)

ROOM_LABEL_FONT_CELLS = 5.2
FONT_PATH = Path(__file__).parent / "fonts" / "DejaVuSans-Bold.ttf"

# Typy mebli z aplikacji (ObstacleInfo.type_id).
FURNITURE_BEDS = frozenset({1, 2, 3})
FURNITURE_DOUBLE_BEDS = frozenset({2})
FURNITURE_SOFAS = frozenset({13, 14, 15, 16, 17, 18, 30})
FURNITURE_TOILETS = frozenset({28})
FURNITURE_ROUND = frozenset({5, 7, 77})

# Zachowane dla zgodności z testami i starszym kodem.
PIXEL_FLOOR = 0x01
PIXEL_OBSTACLE = 0x08
PIXEL_UNDER_FURNITURE = 0x10


def _load_font(image_font: object, size: int):
    """Ładuje dołączoną czcionkę (polskie znaki), w zapasie domyślną Pillow."""
    try:
        return image_font.truetype(str(FONT_PATH), size)
    except OSError:
        try:
            return image_font.load_default(size=size)
        except TypeError:
            return image_font.load_default()


def _normalize_rotation(rotation_degrees: int) -> int:
    """Obrót obsługiwany przez obraz i punkty: 0, 90, 180, 270."""
    rotation = int(rotation_degrees or 0) % 360
    return rotation if rotation in (0, 90, 180, 270) else 0


def _transform_point(
    x: float,
    y: float,
    width: int,
    height: int,
    rotation_degrees: int,
    zoom: float,
) -> tuple[float, float] | None:
    """Przenosi punkt obrazu bez obrotu do współrzędnych końcowego widoku."""
    rotation = _normalize_rotation(rotation_degrees)
    if rotation == 0:
        rx, ry = x, y
        rotated_width, rotated_height = width, height
    elif rotation == 90:
        rx, ry = height - 1 - y, x
        rotated_width, rotated_height = height, width
    elif rotation == 180:
        rx, ry = width - 1 - x, height - 1 - y
        rotated_width, rotated_height = width, height
    else:
        rx, ry = y, width - 1 - x
        rotated_width, rotated_height = height, width
    zoom = max(1.0, min(2.0, float(zoom or 1.0)))
    crop_width = max(1, int(rotated_width / zoom))
    crop_height = max(1, int(rotated_height / zoom))
    left = max(0, (rotated_width - crop_width) // 2)
    top = max(0, (rotated_height - crop_height) // 2)
    return rx - left, ry - top


def _rotated_heading(heading: float | None, rotation_degrees: int) -> float | None:
    """Obraca kierunek robota zgodnie z obrotem mapy."""
    if heading is None:
        return None
    return (heading - _normalize_rotation(rotation_degrees)) % 360


def decompress_map(compressed: bytes) -> bytes:
    """Rozpakowuje siatkę mapy (zlib, w zapasie gzip albo surowy deflate)."""
    if not compressed:
        return b""
    for wbits in (47, 15, -15):
        try:
            return zlib.decompress(compressed, wbits)
        except zlib.error:
            continue
    _LOGGER.warning("Nie udało się rozpakować mapy (%d B), używam surowych danych", len(compressed))
    return compressed


def _decode_packed_varints(data: bytes) -> list[int]:
    """Dekoduje spakowane varinty protobuf z rozpakowanej mapy.

    Dane zaczynają się nagłówkiem pola 1 (0x0a) i długością varint, potem idą
    wartości pikseli.
    """
    if len(data) < 4:
        return []
    pos = 0
    if data[0] == 0x0A:
        pos = 1
        while pos < len(data) and data[pos] & 0x80:
            pos += 1
        pos += 1
    pixels: list[int] = []
    while pos < len(data):
        val = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        pixels.append(val)
    return pixels


def _pixels(decompressed: bytes, width: int, height: int) -> list[int]:
    """Piksele dopełnione albo przycięte do width × height."""
    pixels = _decode_packed_varints(decompressed)
    expected = width * height
    if len(pixels) < expected:
        if pixels:
            _LOGGER.warning(
                "Mapa ma %d pikseli, oczekiwano %d (%dx%d), dopełniam",
                len(pixels), expected, width, height,
            )
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]
    return pixels


def lookup_room_at_grid(
    compressed: bytes,
    width: int,
    height: int,
    grid_x: float,
    grid_y: float,
) -> tuple[int, str]:
    """Zwraca (room_id, opis) dla komórki siatki."""
    px = int(grid_x)
    py = int(grid_y)
    if px < 0 or px >= width or py < 0 or py >= height:
        return (-1, f"(out_of_bounds: {px},{py} vs {width}x{height})")
    decompressed = decompress_map(compressed)
    if not decompressed:
        return (-1, "(no_data)")
    pixels = _decode_packed_varints(decompressed)
    idx = py * width + px
    if idx >= len(pixels):
        return (-1, f"(idx_overflow: {idx} >= {len(pixels)})")
    val = pixels[idx]
    if val == 0:
        return (0, "(empty)")
    if val in (0x20, 0x28):
        return (0, "(unassigned)")
    room_id = val >> 8
    ptype = val & 0xFF
    wall = " wall" if ptype & PIXEL_OBSTACLE else ""
    return (room_id, f"room_{room_id}{wall}")


def _is_room_floor(val: int, room_id: int) -> bool:
    return val != 0 and (val >> 8) == room_id and not (val & PIXEL_OBSTACLE)


def _is_any_floor(val: int) -> bool:
    return val != 0 and not (val & PIXEL_OBSTACLE)


def room_label_points(
    compressed: bytes,
    width: int,
    height: int,
    room_names: dict[int, str] | None,
) -> list[tuple[str, float, float, float]]:
    """Środki etykiet pokoi: (nazwa, x, y w komórkach, szerokość pokoju w komórkach)."""
    if not compressed or width <= 0 or height <= 0 or not room_names:
        return []
    decompressed = decompress_map(compressed)
    if not decompressed:
        return []
    pixels = _pixels(decompressed, width, height)
    sums: dict[int, list[float]] = {}
    for i, val in enumerate(pixels):
        if val == 0 or val & PIXEL_OBSTACLE:
            continue
        room_id = val >> 8
        if room_id not in room_names:
            continue
        x = i % width
        y = i // width
        acc = sums.setdefault(room_id, [0.0, 0.0, 0.0, float(width), 0.0])
        acc[0] += x
        acc[1] += y
        acc[2] += 1
        acc[3] = min(acc[3], x)
        acc[4] = max(acc[4], x)
    points: list[tuple[str, float, float, float]] = []
    for room_id, name in room_names.items():
        if not name or room_id not in sums:
            continue
        sx, sy, n, xmin, xmax = sums[room_id]
        points.append((name, sx / n, sy / n, xmax - xmin + 1))
    return points


def _mask(pixels: list[int], width: int, height: int, pred) -> Image.Image:
    """Maska L (0/255) w układzie obrazu (oś Y odwrócona względem siatki)."""
    from PIL import Image

    mask = Image.new("L", (width, height), 0)
    mask.frombytes(bytes(255 if pred(v) else 0 for v in pixels))
    return mask.transpose(Image.FLIP_TOP_BOTTOM)


def _upscale_mask(mask: Image.Image, scale: int, threshold: int = 128) -> Image.Image:
    """Powiększa maskę z wygładzeniem krawędzi (interpolacja + próg)."""
    from PIL import Image

    big = mask.resize((mask.width * scale, mask.height * scale), Image.BILINEAR)
    return big.point(lambda p: 255 if p >= threshold else 0)


def _grid_to_image(gx: float, gy: float, height: int, scale: float) -> tuple[float, float]:
    """Komórka siatki (środek) → piksel obrazu bez przycięcia."""
    return ((gx + 0.5) * scale, (height - 1 - gy + 0.5) * scale)


def _draw_furniture(
    draw: ImageDraw.ImageDraw,
    obstacles: list,
    height: int,
    scale: float,
    origin_x: int,
    origin_y: int,
) -> None:
    """Rysuje meble z listy mebli mapy jako obrysy w stylu aplikacji."""
    line_width = max(1, int(round(0.45 * scale)))

    for obs in obstacles:
        cx, cy = obs.to_grid_coords(origin_x, origin_y)
        w = float(obs.width)
        h = float(obs.height)
        if w <= 0 or h <= 0:
            continue
        angle = math.radians(float(obs.angle or 0.0))

        def local(lx: float, ly: float, cx=cx, cy=cy, angle=angle) -> tuple[float, float]:
            # Obrót zgodnie z ruchem wskazówek w układzie świata (oś Y w górę).
            wx = cx + lx * math.cos(angle) + ly * math.sin(angle)
            wy = cy - lx * math.sin(angle) + ly * math.cos(angle)
            return _grid_to_image(wx, wy, height, scale)

        def rect(x0: float, y0: float, x1: float, y1: float, **kwargs) -> None:
            draw.polygon([local(x0, y0), local(x1, y0), local(x1, y1), local(x0, y1)], **kwargs)

        type_id = int(obs.type_id)
        if type_id in FURNITURE_BEDS:
            rect(-w / 2, -h / 2, w / 2, h / 2, fill=COLOR_FURNITURE_FILL,
                 outline=COLOR_FURNITURE_LINE, width=line_width)
            pillow_w = w * 0.22
            if type_id in FURNITURE_DOUBLE_BEDS:
                gap = h * 0.04
                for side in (-1, 1):
                    y0 = gap if side > 0 else -(h / 2 - 1.5)
                    y1 = (h / 2 - 1.5) if side > 0 else -gap
                    rect(-w / 2 + 1.5, y0, -w / 2 + 1.5 + pillow_w, y1,
                         outline=COLOR_FURNITURE_LINE, width=line_width)
            else:
                rect(-w / 2 + 1.5, -h / 2 + 2, -w / 2 + 1.5 + pillow_w, h / 2 - 2,
                     outline=COLOR_FURNITURE_LINE, width=line_width)
        elif type_id in FURNITURE_SOFAS:
            rect(-w / 2, -h / 2, w / 2, h / 2, fill=COLOR_FURNITURE_FILL,
                 outline=COLOR_FURNITURE_LINE, width=line_width)
            back = w * 0.25
            arm = h * 0.15
            draw.line([local(-w / 2 + back, -h / 2), local(-w / 2 + back, h / 2)],
                      fill=COLOR_FURNITURE_LINE, width=line_width)
            draw.line([local(-w / 2 + back, -h / 2 + arm), local(w / 2, -h / 2 + arm)],
                      fill=COLOR_FURNITURE_LINE, width=line_width)
            draw.line([local(-w / 2 + back, h / 2 - arm), local(w / 2, h / 2 - arm)],
                      fill=COLOR_FURNITURE_LINE, width=line_width)
        elif type_id in FURNITURE_TOILETS:
            rect(-w / 2, -h / 2 * 0.8, -w / 2 + w * 0.35, h / 2 * 0.8,
                 fill=COLOR_FURNITURE_FILL, outline=COLOR_FURNITURE_LINE, width=line_width)
            bowl_cx = -w / 2 + w * 0.35 + w * 0.325
            points = [
                local(bowl_cx + w * 0.325 * math.cos(t), h / 2 * math.sin(t))
                for t in (i / 24 * 2 * math.pi for i in range(24))
            ]
            draw.polygon(points, fill=COLOR_FURNITURE_FILL,
                         outline=COLOR_FURNITURE_LINE, width=line_width)
        elif type_id in FURNITURE_ROUND:
            points = [
                local(w / 2 * math.cos(t), h / 2 * math.sin(t))
                for t in (i / 32 * 2 * math.pi for i in range(32))
            ]
            draw.polygon(points, fill=COLOR_FURNITURE_FILL,
                         outline=COLOR_FURNITURE_LINE, width=line_width)
        else:
            rect(-w / 2, -h / 2, w / 2, h / 2, fill=COLOR_FURNITURE_FILL,
                 outline=COLOR_FURNITURE_LINE, width=line_width)


def _render_base(
    pixels: list[int],
    width: int,
    height: int,
    room_ids: set[int],
    obstacles: list | None,
    origin_x: int,
    origin_y: int,
    scale: int,
) -> Image.Image:
    """Obraz bazowy: pokoje, podłoga bez pokoju, meble, ściany. Przycięty."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    img = Image.new("RGBA", (width * scale, height * scale), COLOR_UNKNOWN)
    floor_all = _mask(pixels, width, height, _is_any_floor)

    for room_id in sorted(room_ids):
        mask = _upscale_mask(_mask(pixels, width, height, lambda v, r=room_id: _is_room_floor(v, r)), scale)
        color = ROOM_COLORS[(room_id - 1) % len(ROOM_COLORS)]
        img.paste(Image.new("RGBA", img.size, (*color, 255)), (0, 0), mask)

    unassigned = _upscale_mask(_mask(pixels, width, height, lambda v: v == 0x20), scale)
    img.paste(Image.new("RGBA", img.size, (*COLOR_UNASSIGNED_FLOOR, 255)), (0, 0), unassigned)

    draw = ImageDraw.Draw(img, "RGBA")
    if obstacles:
        _draw_furniture(draw, obstacles, height, scale, origin_x, origin_y)

    # Ściana: komórki przeszkód stykające się z podłogą, jedna komórka grubości.
    obstacles_mask = _mask(pixels, width, height, lambda v: bool(v & PIXEL_OBSTACLE))
    walls = ImageChops.multiply(obstacles_mask, floor_all.filter(ImageFilter.MaxFilter(3)))
    walls = _upscale_mask(walls, scale, threshold=110)
    img.paste(Image.new("RGBA", img.size, (*COLOR_WALL, 255)), (0, 0), walls)

    bbox = img.getbbox()
    margin = MAP_MARGIN_CELLS * scale
    if bbox:
        left = max(0, bbox[0] - margin)
        top = max(0, bbox[1] - margin)
        right = min(img.width, bbox[2] + margin)
        bottom = min(img.height, bbox[3] + margin)
        img = img.crop((left, top, right, bottom))
        img.info["crop_origin"] = (left, top)
    else:
        img.info["crop_origin"] = (0, 0)
    img.info["scale"] = scale
    img.info["grid_height"] = height
    return img


def _room_ids(pixels: list[int]) -> set[int]:
    """Numery pokoi obecne w siatce."""
    return {v >> 8 for v in pixels if v and (v >> 8) > 0 and v not in (0x20, 0x28)}


def render_base_map(
    compressed: bytes,
    width: int,
    height: int,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
    obstacles: list | None = None,
    origin_x: int = 0,
    origin_y: int = 0,
    show_obstacle_labels: bool = False,
    show_room_labels: bool = True,
    show_dock: bool = False,
) -> Image.Image | None:
    """Statyczny plan (pokoje, ściany, meble) jako obraz PIL do cache'owania.

    Etykiety, stacja i robot są rysowane w nakładce (`render_overlay`), żeby
    zawsze leżały nad śladem przejazdu. Parametry `show_*` i `dock_*` zostają
    dla zgodności wywołań.
    """
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        _LOGGER.error("Do rysowania mapy potrzebny jest Pillow")
        return None
    decompressed = decompress_map(compressed)
    if not decompressed or width <= 0 or height <= 0:
        return None
    pixels = _pixels(decompressed, width, height)
    return _render_base(
        pixels, width, height, _room_ids(pixels), obstacles, origin_x, origin_y, MAP_RENDER_SCALE,
    )


def _draw_dock(draw: ImageDraw.ImageDraw, x: float, y: float, scale: float) -> None:
    """Stacja: zaokrąglony prostokąt z zieloną diodą."""
    r = 2.2 * scale
    draw.rounded_rectangle(
        [x - r, y - r * 0.7, x + r, y + r * 0.7],
        radius=max(1, int(scale)),
        fill=COLOR_ROBOT_FILL,
        outline=(*COLOR_WALL, 255),
        width=max(1, int(scale // 3)),
    )
    led = r * 0.35
    draw.ellipse([x - led, y - led, x + led, y + led], fill=COLOR_DOCK_LED)


def _draw_robot(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    heading: float | None,
    scale: float,
) -> None:
    """Robot: białe kółko z kreską kierunku."""
    r = 2.0 * scale
    draw.ellipse(
        [x - r, y - r, x + r, y + r],
        fill=COLOR_ROBOT_FILL,
        outline=(*COLOR_WALL, 255),
        width=max(1, int(scale // 3)),
    )
    if heading is None:
        return
    rad = math.radians(heading)
    draw.line(
        [(x, y), (x + math.cos(rad) * r * 0.9, y - math.sin(rad) * r * 0.9)],
        fill=(*COLOR_WALL, 255),
        width=max(1, int(scale // 2)),
    )


def _draw_room_label(
    draw: ImageDraw.ImageDraw,
    font,
    x: float,
    y: float,
    text: str,
    max_width_px: float | None,
    scale: float,
) -> None:
    """Etykieta pokoju: granatowy tekst bez obrysu, łamany na dwie linie."""
    lines = [text]
    if max_width_px and " " in text and font.getlength(text) > max_width_px:
        words = text.split(" ")
        k = max(1, len(words) // 2)
        lines = [" ".join(words[:k]), " ".join(words[k:])]
    line_height = font.getbbox("Ag")[3] + scale
    y0 = y - line_height * len(lines) / 2
    for j, line in enumerate(lines):
        bbox = font.getbbox(line)
        tw = bbox[2] - bbox[0]
        draw.text(
            (x - tw / 2 - bbox[0], y0 + j * line_height),
            line,
            font=font,
            fill=(*COLOR_TEXT, 255),
        )


def render_overlay(
    base_img: Image.Image,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    trail: list[tuple[float, float]] | None = None,
    rotation_degrees: int = 0,
    zoom: float = 1.0,
    room_labels: list[tuple] | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
) -> bytes:
    """Rysuje ślad, etykiety, stację i robota na kopii obrazu bazowego.

    Współrzędne wejściowe są w komórkach siatki (jak get_map). Zwraca PNG.
    """
    from PIL import ImageDraw, ImageFont

    img = base_img.copy()
    scale = float(base_img.info.get("scale") or (img.height / height if height > 0 else 1.0))
    crop_x, crop_y = base_img.info.get("crop_origin", (0, 0))
    grid_height = int(base_img.info.get("grid_height") or height)
    original_width = img.width
    original_height = img.height
    map_width = (original_width + crop_x) / scale if scale > 0 else original_width
    max_grid_segment = max(24.0, min(map_width, grid_height) * 0.18)

    img = _apply_view_transform(img, rotation_degrees, zoom)
    draw = ImageDraw.Draw(img, "RGBA")

    def is_valid_grid_point(gx: float, gy: float) -> bool:
        return math.isfinite(gx) and math.isfinite(gy) and -1 <= gx <= map_width + 1 and -1 <= gy <= grid_height + 1

    def final_point(gx: float, gy: float) -> tuple[float, float] | None:
        if not is_valid_grid_point(gx, gy):
            return None
        x, y = _grid_to_image(gx, gy, grid_height, scale)
        return _transform_point(
            x - crop_x, y - crop_y, original_width, original_height, rotation_degrees, zoom
        )

    if trail and len(trail) >= 2:
        trail_width = max(1, int(round(scale / 2)))
        previous_grid: tuple[float, float] | None = None
        previous_point: tuple[float, float] | None = None
        for gx, gy in trail:
            point = final_point(gx, gy)
            if point is None:
                previous_grid = None
                previous_point = None
                continue
            if previous_grid is not None and previous_point is not None:
                distance = math.hypot(gx - previous_grid[0], gy - previous_grid[1])
                if distance <= max_grid_segment:
                    draw.line([previous_point, point], fill=COLOR_TRAIL, width=trail_width)
            previous_grid = (gx, gy)
            previous_point = point

    if room_labels:
        font = _load_font(ImageFont, int(ROOM_LABEL_FONT_CELLS * scale))
        for label in room_labels:
            name, gx, gy = label[0], label[1], label[2]
            room_width_cells = label[3] if len(label) > 3 else None
            point = final_point(gx, gy)
            if point is None:
                continue
            x, y = point
            if -80 <= x <= img.width + 80 and -80 <= y <= img.height + 80:
                max_width_px = room_width_cells * scale * 0.85 if room_width_cells else None
                _draw_room_label(draw, font, x, y, name, max_width_px, scale)

    if dock_x is not None and dock_y is not None:
        point = final_point(dock_x, dock_y)
        if point is not None:
            _draw_dock(draw, point[0], point[1], scale)

    if robot_x is not None and robot_y is not None:
        point = final_point(robot_x, robot_y)
        if point is not None:
            _draw_robot(draw, point[0], point[1], _rotated_heading(robot_heading, rotation_degrees), scale)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _apply_view_transform(
    img: Image.Image,
    rotation_degrees: int = 0,
    zoom: float = 1.0,
) -> Image.Image:
    """Obrót mapy i powiększenie środka."""
    rotation = _normalize_rotation(rotation_degrees)
    if rotation:
        img = img.rotate(-rotation, expand=True)
    zoom = max(1.0, min(2.0, float(zoom or 1.0)))
    if zoom <= 1.0:
        return img
    crop_width = max(1, int(img.width / zoom))
    crop_height = max(1, int(img.height / zoom))
    left = max(0, (img.width - crop_width) // 2)
    top = max(0, (img.height - crop_height) // 2)
    return img.crop((left, top, left + crop_width, top + crop_height))


def render_map_png(
    decompressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
) -> bytes:
    """Mapa w jednym kroku (obraz bazowy plus nakładka), jako PNG."""
    if not decompressed or width <= 0 or height <= 0:
        return b""
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        _LOGGER.error("Do rysowania mapy potrzebny jest Pillow")
        return b""
    pixels = _pixels(decompressed, width, height)
    base = _render_base(pixels, width, height, _room_ids(pixels), None, 0, 0, MAP_RENDER_SCALE)
    labels: list[tuple] = []
    if room_names:
        sums: dict[int, list[float]] = {}
        for i, val in enumerate(pixels):
            if val == 0 or val & PIXEL_OBSTACLE:
                continue
            room_id = val >> 8
            if room_id not in room_names:
                continue
            acc = sums.setdefault(room_id, [0.0, 0.0, 0.0])
            acc[0] += i % width
            acc[1] += i // width
            acc[2] += 1
        labels = [
            (room_names[rid], acc[0] / acc[2], acc[1] / acc[2])
            for rid, acc in sums.items()
            if room_names.get(rid)
        ]
    return render_overlay(
        base,
        height,
        robot_x,
        robot_y,
        robot_heading,
        None,
        0,
        1.0,
        labels,
        dock_x,
        dock_y,
    )


def render_map_from_compressed(
    compressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
) -> bytes:
    """Rozpakowanie i render w jednym kroku."""
    decompressed = decompress_map(compressed)
    return render_map_png(
        decompressed, width, height, robot_x, robot_y, robot_heading,
        dock_x, dock_y, room_names,
    )
