"""Encja image z mapą Narwala, odświeżana przy każdej zmianie danych robota."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import NarwalConfigEntry
from .coordinator import (
    NATIVE_TRAJECTORY_MAX_POINTS,
    NATIVE_TRAJECTORY_RECENT_TAIL_POINTS,
    NarwalCoordinator,
)
from .entity import NarwalEntity
from .narwal_client.models import overlay_to_grid

_LOGGER = logging.getLogger(__name__)

# Minimum seconds between re-renders (display_map arrives every ~1.5s
# but PIL rendering is CPU-bound — no need to render every broadcast).
_MIN_RENDER_INTERVAL = 2

# Native trajectory points can repeat across display_map frames because field 2 is
# the accumulated Narwal trajectory. Deduplicate tiny adjacent moves only.
_NATIVE_TRAIL_MIN_GRID_DELTA = 0.25
_NATIVE_TRAIL_MAX_POINTS = NATIVE_TRAJECTORY_MAX_POINTS
_NATIVE_TRAIL_RECENT_TAIL_POINTS = NATIVE_TRAJECTORY_RECENT_TAIL_POINTS

# Opcje rysowania mapy: etykiety pokoi tak, meble z listy mebli mapy na razie nie
# (decyzja użytkownika 2026-10-23), bez podpisów mebli, bez obrotu i powiększenia.
_MAP_OPTIONS_KEY: tuple[bool, bool, bool] = (True, False, False)
_VIEW_OPTIONS_KEY: tuple[int, float] = (0, 1.0)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Dodaje encję obrazu mapy."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalMapImage(coordinator)])


class NarwalMapImage(NarwalEntity, ImageEntity):
    """Obraz mapy (PNG) z pozycją robota, stacją i śladem przejazdu."""

    _attr_translation_key = "map"
    _attr_content_type = "image/png"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Inicjalizacja."""
        NarwalEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_map"
        self._cached_image: bytes | None = None
        self._cache_key: tuple = ()
        self._last_render_time: float = 0.0
        self._render_count: int = 0
        self._pending_render: tuple[object, tuple] | None = None
        self._render_task: asyncio.Task[None] | None = None
        # Cached base map (PIL Image) — only re-rendered when static map changes
        self._base_map_image = None  # PIL Image or None
        self._base_map_ts: int = 0  # created_at of the static map used for base
        self._room_label_points: list[tuple[str, float, float]] = []

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Licznik renderów do diagnostyki."""
        return {"render_count": self._render_count}

    async def async_image(self) -> bytes | None:
        """Ostatnio wyrenderowana mapa jako PNG."""
        return self._cached_image

    async def async_will_remove_from_hass(self) -> None:
        """Przerywa kolejkę renderowania przed usunięciem encji."""
        self._pending_render = None
        render_task = self._render_task
        if render_task is not None and not render_task.done():
            render_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await render_task
        self._render_task = None
        await super().async_will_remove_from_hass()

    @staticmethod
    def _native_trajectory_grid_points(
        points: list[tuple[float, float]],
        static_map,
    ) -> list[tuple[float, float]]:
        """Return in-bounds grid points from Narwal's native trajectory."""
        converted: list[tuple[float, float]] = []
        pending_break = False

        for raw_x, raw_y in NarwalMapImage._native_trajectory_render_points(points):
            if not math.isfinite(raw_x) or not math.isfinite(raw_y):
                pending_break = bool(converted)
                continue
            grid_x = overlay_to_grid(raw_x, static_map.origin_x)
            grid_y = overlay_to_grid(raw_y, static_map.origin_y)
            if grid_x is None or grid_y is None:
                continue
            if (
                grid_x < 0
                or grid_y < 0
                or grid_x >= static_map.width
                or grid_y >= static_map.height
            ):
                _LOGGER.debug(
                    "Skipping native Narwal trail point outside map bounds: "
                    "%.1f, %.1f (%dx%d)",
                    grid_x,
                    grid_y,
                    static_map.width,
                    static_map.height,
                )
                pending_break = bool(converted)
                continue
            point = (grid_x, grid_y)
            if pending_break:
                converted.append((float("nan"), float("nan")))
                pending_break = False
            if converted and math.hypot(
                point[0] - converted[-1][0],
                point[1] - converted[-1][1],
            ) < _NATIVE_TRAIL_MIN_GRID_DELTA:
                continue
            converted.append(point)
        return converted

    @staticmethod
    def _native_trajectory_render_points(
        points: list[tuple[float, float]],
    ):
        """Yield render-capped trajectory points while preserving the latest tail."""
        if len(points) <= _NATIVE_TRAIL_MAX_POINTS:
            yield from points
            return

        tail_size = min(_NATIVE_TRAIL_RECENT_TAIL_POINTS, _NATIVE_TRAIL_MAX_POINTS)
        tail_start = len(points) - tail_size
        prefix_slots = _NATIVE_TRAIL_MAX_POINTS - tail_size
        if prefix_slots <= 0:
            yield from points[-_NATIVE_TRAIL_MAX_POINTS:]
            return

        span = max(tail_start - 1, 0)
        slots = max(prefix_slots - 1, 1)
        selected_indices: set[int] = set()
        for slot in range(prefix_slots):
            selected_indices.add(round(slot * span / slots))
        selected_indices.update(range(tail_start, len(points)))
        selected_indices.update(
            index
            for index, point in enumerate(points)
            if not math.isfinite(point[0]) or not math.isfinite(point[1])
        )
        for index in sorted(selected_indices):
            yield points[index]

    @staticmethod
    def _trajectory_cache_key(
        display,
    ) -> tuple[int, int, int] | tuple[()]:
        """Return a compact cache key for native trajectory changes."""
        if display is None:
            return ()
        return display.trajectory_signature

    @classmethod
    def _render_overlay_image(
        cls,
        base_map_image,
        static_map,
        display,
        map_rotation: int,
        map_zoom: float,
        room_label_points: list[tuple[str, float, float]],
    ) -> bytes | None:
        """Render dynamic map overlays in an executor thread."""
        from .narwal_client.map_renderer import render_overlay

        native_trail = None
        if display and display.has_trajectory:
            native_trail = cls._native_trajectory_grid_points(
                display.trajectory_render_points(),
                static_map,
            )

        robot_x = None
        robot_y = None
        robot_heading = None
        if display:
            grid_pos = display.to_grid_coords(
                static_map.resolution, static_map.origin_x, static_map.origin_y,
            )
            if grid_pos is not None:
                robot_x, robot_y = grid_pos
                robot_heading = display.robot_heading

        return render_overlay(
            base_map_image,
            static_map.height,
            robot_x,
            robot_y,
            robot_heading,
            native_trail,
            map_rotation,
            map_zoom,
            room_label_points,
            static_map.dock_x,
            static_map.dock_y,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Re-render the map when new data arrives from the coordinator."""
        state = self.coordinator.client.state
        display = state.map_display_data

        static_map = state.map_data
        if not static_map or not static_map.compressed_map:
            self.async_write_ha_state()
            return
        if static_map.width <= 0 or static_map.height <= 0:
            self.async_write_ha_state()
            return

        static_ts = static_map.created_at or 0
        map_options_key = _MAP_OPTIONS_KEY
        view_options_key = _VIEW_OPTIONS_KEY
        if display:
            new_key = (
                static_ts,
                map_options_key,
                view_options_key,
                display.robot_x,
                display.robot_y,
                display.robot_heading,
                self._trajectory_cache_key(display),
            )
        else:
            new_key = (static_ts, map_options_key, view_options_key)

        if new_key == self._cache_key and self._cached_image:
            if self._render_task is not None and not self._render_task.done():
                self._pending_render = (display, new_key)
            self.async_write_ha_state()
            return

        self._schedule_render(display, new_key)
        self.async_write_ha_state()

    def _render_options_changed(self, new_key: tuple) -> bool:
        """Return True when map display options changed and should bypass throttle."""
        return (
            bool(self._cache_key)
            and len(self._cache_key) > 2
            and len(new_key) > 2
            and (
                self._cache_key[1] != new_key[1]
                or self._cache_key[2] != new_key[2]
            )
        )

    def _schedule_render(self, display, new_key: tuple) -> None:
        """Queue the latest render request, coalescing while a render is active."""
        self._pending_render = (display, new_key)
        if self._render_task is not None and not self._render_task.done():
            return
        self._render_task = self.hass.async_create_task(self._async_render_pending())

    async def _async_render_pending(self) -> None:
        """Render queued map updates sequentially, preserving the newest request."""
        try:
            while self._pending_render is not None:
                display, new_key = self._pending_render
                self._pending_render = None
                since_render = (
                    time.monotonic() - self._last_render_time
                    if self._last_render_time
                    else 999
                )
                if (
                    self._cached_image
                    and since_render < _MIN_RENDER_INTERVAL
                    and not self._render_options_changed(new_key)
                ):
                    await asyncio.sleep(_MIN_RENDER_INTERVAL - since_render)
                    if self._pending_render is not None:
                        display, new_key = self._pending_render
                        self._pending_render = None
                if new_key == self._cache_key and self._cached_image:
                    continue
                await self._async_render(display, new_key)
        finally:
            self._render_task = None

    async def _async_render(self, display, new_key) -> None:
        """Render the map image in an executor thread."""
        state = self.coordinator.client.state
        static_map = state.map_data
        if not static_map:
            self.async_write_ha_state()
            return

        from .narwal_client.map_renderer import (
            render_base_map,
            room_label_points,
        )

        # Rebuild base map only when static map data changes
        static_ts = static_map.created_at or 0
        map_options_key = _MAP_OPTIONS_KEY
        if self._base_map_image is None or static_ts != self._base_map_ts:
            room_names: dict[int, str] | None = None
            if map_options_key[0] and static_map.rooms:
                room_names = {
                    r.room_id: r.display_name for r in static_map.rooms
                }
            obstacles = static_map.obstacles if map_options_key[1] else None
            base_img = await self.hass.async_add_executor_job(
                render_base_map,
                static_map.compressed_map,
                static_map.width,
                static_map.height,
                None,
                None,
                room_names,
                obstacles,
                static_map.origin_x,
                static_map.origin_y,
                map_options_key[2],
                False,
                False,
            )
            if base_img:
                self._base_map_image = base_img
                self._base_map_ts = static_ts
                self._room_label_points = await self.hass.async_add_executor_job(
                    room_label_points,
                    static_map.compressed_map,
                    static_map.width,
                    static_map.height,
                    room_names,
                )
                _LOGGER.info(
                    "Base map rendered (ts=%d, %dx%d)",
                    static_ts,
                    static_map.width,
                    static_map.height,
                )
            else:
                self.async_write_ha_state()
                return

        try:
            png_bytes = await self.hass.async_add_executor_job(
                self._render_overlay_image,
                self._base_map_image,
                static_map,
                display,
                _VIEW_OPTIONS_KEY[0],
                _VIEW_OPTIONS_KEY[1],
                self._room_label_points,
            )

            if png_bytes:
                self._cached_image = png_bytes
                self._cache_key = new_key
                self._last_render_time = time.monotonic()
                self._render_count += 1
                self._attr_image_last_updated = dt_util.utcnow()

        except Exception:
            _LOGGER.exception("Failed to render map overlay")

        self.async_write_ha_state()
