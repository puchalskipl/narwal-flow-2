"""Encja vacuum robota Narwal Flow 2."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.vacuum import (
    Segment,
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from . import NarwalConfigEntry
from .const import FAN_SPEED_KEYS, FAN_SPEED_LIST, FAN_SPEEDS
from .coordinator import (
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    can_locate_robot,
    can_pause_cleaning,
    can_prepare_clean_start,
    can_resume_cleaning,
    can_return_home,
    can_start_cleaning,
    can_stop_cleaning,
    clean_setting_applies_to_mode,
    is_live_clean_setting_available,
)
from .dock_tasks import ROBOT_RETURN_COMPATIBLE_DOCK_TASKS
from .entity import NarwalEntity
from .narwal_client import CommandResult, FanLevel, WorkingStatus
from .narwal_client.const import (
    ACTIVE_CLEANING_STATUSES,
    fan_level_for_live_command,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FanSettingRestoreData(ExtraStoredData):
    """Zapamiętane ssanie jako stała wartość enumu robota."""

    value: int
    version: int = 1

    def as_dict(self) -> dict[str, int]:
        """Postać do zapisu."""
        return {"version": self.version, "value": self.value}


WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.DOCKED: VacuumActivity.DOCKED,
    WorkingStatus.CHARGED: VacuumActivity.DOCKED,
    WorkingStatus.DOCKED_V2: VacuumActivity.DOCKED,
    WorkingStatus.STANDBY: VacuumActivity.IDLE,
    WorkingStatus.CLEANING_V2: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_ALT: VacuumActivity.CLEANING,
    WorkingStatus.CUSTOM_CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.REMAPPING: VacuumActivity.CLEANING,
    WorkingStatus.TASK_COMPLETED: VacuumActivity.RETURNING,
    WorkingStatus.ERROR: VacuumActivity.ERROR,
}


def _result_name(result_code: int | CommandResult) -> str:
    """Czytelna nazwa kodu odpowiedzi robota."""
    if result_code == 0:
        return "ACCEPTED"
    try:
        return CommandResult(result_code).name
    except ValueError:
        return f"UNKNOWN({result_code})"


def _raise_if_command_failed(response: Any, action: str) -> None:
    """Błąd usługi HA dla odrzuconego polecenia."""
    if response.accepted:
        return
    raise HomeAssistantError(
        f"Narwal {action} failed: {_result_name(response.result_code)}"
    )


def _task_status(state: Any) -> str:
    """Zwięzły stan zadania dla dashboardów i automatyzacji."""
    is_cleaning_state = (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.has_assumed_robot_clean
        or state.has_recent_active_working_status
        or state.has_paused_clean_task_context
    )
    if state.working_status == WorkingStatus.ERROR or getattr(state, "has_error", False):
        return "error"
    if state.is_paused and is_cleaning_state:
        return "paused"
    if state.working_status == WorkingStatus.REMAPPING:
        return "remapping"
    if state.is_returning or (
        state.working_status == WorkingStatus.TASK_COMPLETED
        and not state.has_current_dock_presence_signal
    ):
        return "returning"
    if state.is_cleaning or state.has_assumed_robot_clean:
        return "cleaning"
    if state.is_station_active:
        return "station_active"
    if state.is_docked:
        return "docked"
    if state.working_status == WorkingStatus.STANDBY:
        return "idle"
    return "unknown"


def _has_active_cleaning_metrics(state: Any) -> bool:
    """Czy dane o postępie sprzątania są aktualne."""
    return (
        state.is_cleaning
        or state.has_assumed_robot_clean
        or state.has_recent_active_working_status
        or state.has_paused_clean_task_context
    )


def _has_dock_stop_context(state: Any) -> bool:
    """Czy trzeba uwzględnić pracę stacji przed ogólnym stopem."""
    return (
        state.is_station_active
        or state.has_unmapped_active_dock_task
        or bool(state.active_dock_task_keys)
    )


def _can_stop_vacuum(state: Any) -> bool:
    """Czy można bezpiecznie udostępnić polecenie stop."""
    compatible_metric_clean = (
        state.has_recent_active_working_status
        and not state.has_unmapped_active_dock_task
        and set(state.active_dock_task_keys)
        <= ROBOT_RETURN_COMPATIBLE_DOCK_TASKS
    )
    if (
        _has_dock_stop_context(state)
        and not _has_live_robot_stop_context(state)
        and not compatible_metric_clean
    ):
        return False
    return can_stop_cleaning(state)


def _can_accept_return_home(state: Any) -> bool:
    """Czy powrót do stacji zadziała albo jest bezpiecznym pustym poleceniem."""
    return can_return_home(state) or (
        state.is_docked
        and not _has_dock_stop_context(state)
        and not _has_live_robot_stop_context(state)
    )


def _has_live_robot_stop_context(state: Any) -> bool:
    """Czy telemetria wskazuje trwającą pracę robota do zatrzymania."""
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.working_status == WorkingStatus.REMAPPING
        or state.has_assumed_robot_clean
        or state.has_paused_clean_task_context
        or state.is_returning
    )


def _status_summary(state: Any) -> str:
    """Jedna linia stanu do kafelka."""
    status = _task_status(state)
    if status in {"error", "paused", "remapping", "returning"}:
        return status.replace("_", " ").title()
    active_cleaning_metrics = _has_active_cleaning_metrics(state)
    parts: list[str] = []

    if active_cleaning_metrics and state.current_room_name:
        parts.append(state.current_room_name)
    if active_cleaning_metrics and state.task_progress_percent is not None:
        parts.append(f"{state.task_progress_percent}%")
    if parts:
        return " - ".join(parts)

    return status.replace("_", " ").title()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Dodaje encję vacuum."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalVacuum(coordinator)])


class NarwalVacuum(NarwalEntity, RestoreEntity, StateVacuumEntity):
    """Robot Narwal Flow 2."""

    _attr_translation_key = "vacuum"
    _attr_fan_speed_list = FAN_SPEED_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Inicjalizacja."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.data["device_id"]
        self._last_reported_segment_signature = None

    async def async_added_to_hass(self) -> None:
        """Przywraca zapamiętane ssanie do clean_settings."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is None:
            return
        data = extra.as_dict()
        raw_value = data.get("value")
        values = {int(value): value for value in FanLevel}
        if (
            data.get("version") == 1
            and isinstance(raw_value, int)
            and not isinstance(raw_value, bool)
            and raw_value in values
            and values[raw_value] in FAN_SPEED_KEYS
        ):
            self.coordinator.clean_settings.fan = values[raw_value]

    @property
    def supported_features(self) -> VacuumEntityFeature:
        """Funkcje dostępne w bieżącym stanie."""
        features = VacuumEntityFeature.STATE
        state = self.coordinator.data
        if state is None or not self.available:
            return features

        if can_resume_cleaning(state) or can_prepare_clean_start(state):
            features |= VacuumEntityFeature.START
        if _can_stop_vacuum(state):
            features |= VacuumEntityFeature.STOP
        if can_pause_cleaning(state):
            features |= VacuumEntityFeature.PAUSE
        if _can_accept_return_home(state):
            features |= VacuumEntityFeature.RETURN_HOME
        if can_locate_robot(state):
            features |= VacuumEntityFeature.LOCATE
        if self._fan_speed_available(state):
            features |= VacuumEntityFeature.FAN_SPEED
        if can_prepare_clean_start(state):
            features |= VacuumEntityFeature.CLEAN_AREA
        return features

    def _fan_speed_available(self, state: Any) -> bool:
        """Czy pokazać sterowanie ssaniem."""
        setup_available = can_edit_pending_clean_settings(state)
        live_available = super().available and is_live_clean_setting_available(state)
        setup_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_settings.work_mode,
        )
        live_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_setting_applicability_mode(live=True),
        )
        return (setup_available and setup_applies) or (
            live_available and live_applies
        )

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Ssanie do zapamiętania między restartami."""
        return FanSettingRestoreData(value=int(self.coordinator.clean_settings.fan))

    @property
    def activity(self) -> VacuumActivity:
        """Aktywność robota."""
        state = self.coordinator.data
        if state is None:
            return VacuumActivity.IDLE
        is_cleaning_state = (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_assumed_robot_clean
            or state.has_recent_active_working_status
            or state.has_paused_clean_task_context
        )
        if state.working_status == WorkingStatus.ERROR or getattr(state, "has_error", False):
            return VacuumActivity.ERROR
        # is_paused (pole 3.2) zostaje nieaktualne po zadokowaniu, liczy się tylko
        # podczas sprzątania. Pauza ma pierwszeństwo przed powrotem.
        if state.is_paused and is_cleaning_state:
            return VacuumActivity.PAUSED
        # Powrót przed sprzątaniem: working_status zostaje CLEANING w drodze do stacji.
        if state.is_returning:
            return VacuumActivity.RETURNING
        if state.is_cleaning or state.has_assumed_robot_clean:
            return VacuumActivity.CLEANING
        if state.is_docked:
            return VacuumActivity.DOCKED
        activity = WORKING_STATUS_TO_ACTIVITY.get(state.working_status)
        if activity == VacuumActivity.DOCKED:
            return VacuumActivity.IDLE
        if activity is not None:
            return activity
        if (
            state.working_status == WorkingStatus.UNKNOWN
            and state.has_explicit_off_dock_signal
        ):
            return VacuumActivity.CLEANING
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str | None:
        """Wybrane ssanie.

        Robot nie rozgłasza aktywnego poziomu, więc to wartość z clean_settings
        (użyta przy następnym starcie) albo ustawiona w trakcie przez set_fan_speed.
        """
        fan = self.coordinator.active_clean_setting("fan")
        if fan is None:
            fan = self.coordinator.clean_settings.fan
        return FAN_SPEED_KEYS.get(fan)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Kontekst zadania dla kart i automatyzacji."""
        state = self.coordinator.data
        if state is None:
            return None

        attributes: dict[str, Any] = {
            "task_status": _task_status(state),
            "status_summary": _status_summary(state),
        }
        active_cleaning_metrics = _has_active_cleaning_metrics(state)
        if active_cleaning_metrics and state.task_progress_percent is not None:
            attributes["progress"] = state.task_progress_percent
        if active_cleaning_metrics and state.current_room_name:
            attributes["current_room"] = state.current_room_name
        return attributes

    # Limit czasu poleceń akcji (start/stop/powrót): robot może ładować mapę
    # i planować trasę, zwłaszcza po wybudzeniu.
    _ACTION_TIMEOUT = 10.0

    async def _ensure_awake(self) -> None:
        """Wybudza robota, jeśli nie rozgłasza stanu."""
        client = self.coordinator.client
        if not client.robot_awake:
            _LOGGER.debug("Robot nie jest wybudzony, wysyłam serię wybudzania")
            await client.wake(timeout=10.0)

    async def _validate_clean_start(self) -> None:
        """Odświeża stan stacji i zatrzymuje bezpieczne blokady przed startem."""
        if not await self.coordinator.async_prepare_clean_start():
            raise HomeAssistantError("Narwal clean cannot be started right now")

    async def async_start(self) -> None:
        """Wznawia pauzę albo sprząta cały dom z ostatnimi ustawieniami."""
        room_ids: list[int] = []
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            if not await self.coordinator.async_refresh_action_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            state = self.coordinator.client.state
            if can_resume_cleaning(state):
                resp = await self.coordinator.client.resume(timeout=self._ACTION_TIMEOUT)
                _raise_if_command_failed(resp, "resume")
                return

            room_ids = await self._all_room_ids()
            if not room_ids:
                raise HomeAssistantError("Narwal room map is not available")
            resp = await self._start_rooms(room_ids)
        _LOGGER.info(
            "Start całego domu: code=%s, success=%s, rooms=%s",
            resp.result_code, resp.success, room_ids,
        )
        if not resp.accepted:
            raise HomeAssistantError(
                f"Narwal start command failed: {_result_name(resp.result_code)}"
            )
        self.async_write_ha_state()

    async def _start_rooms(self, room_ids: list[int]) -> Any:
        """Wysyła start pokoi z ostatnimi ustawieniami (pod blokadą stacji)."""
        settings = self.coordinator.clean_settings
        await self._validate_clean_start()
        if not can_start_cleaning(self.coordinator.client.state):
            raise HomeAssistantError("Narwal clean cannot be started right now")
        resp = await self.coordinator.client.start_rooms(
            room_ids,
            work_mode=settings.work_mode,
            fan=settings.fan,
            water=settings.water,
            mop_strength=settings.mop_strength,
            passes=settings.passes,
            route=settings.route,
            mop_passes=settings.mop_passes,
        )
        if resp.accepted:
            self.coordinator.record_accepted_clean_start(settings)
            self.coordinator.client.state.assume_robot_clean()
            await self.coordinator.async_clear_map_display_cache()
            self.coordinator.async_set_updated_data(self.coordinator.client.state)
        return resp

    async def _all_room_ids(self) -> list[int]:
        """Wszystkie pokoje aktywnej mapy po jej odświeżeniu."""
        try:
            await self.coordinator.client.get_map()
        except Exception as err:
            raise HomeAssistantError("Narwal map could not be refreshed") from err
        state = self.coordinator.client.state
        if state and state.map_data:
            return [r.room_id for r in state.map_data.rooms if r.room_id > 0]
        return []

    async def async_stop(self, **kwargs) -> None:
        """Zatrzymuje zadanie robota bez wpływu na pracę stacji."""
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            refreshed = await self.coordinator.async_refresh_action_status()
            if not refreshed:
                raise HomeAssistantError("Narwal status could not be refreshed")
            state = self.coordinator.client.state
            if not _can_stop_vacuum(state):
                raise HomeAssistantError("Narwal has no active robot task to stop")
            resp = await self.coordinator.client.stop()
        _LOGGER.info("Stop: code=%s, success=%s", resp.result_code, resp.success)
        _raise_if_command_failed(resp, "stop")

    async def async_pause(self) -> None:
        """Pauza."""
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            if not await self.coordinator.async_refresh_action_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            state = self.coordinator.client.state
            if not can_pause_cleaning(state):
                raise HomeAssistantError("Narwal clean cannot be paused right now")
            resp = await self.coordinator.client.pause()
        _LOGGER.info("Pauza: code=%s, success=%s", resp.result_code, resp.success)
        _raise_if_command_failed(resp, "pause")

    async def async_return_to_base(self, **kwargs) -> None:
        """Powrót do stacji."""
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            if not await self.coordinator.async_refresh_action_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            state = self.coordinator.client.state
            if state.is_docked and _can_accept_return_home(state):
                return
            if not can_return_home(state):
                raise HomeAssistantError("Narwal cannot return to the dock right now")
            resp = await self.coordinator.client.return_to_base(timeout=self._ACTION_TIMEOUT)
        _LOGGER.info(
            "Powrót do stacji: code=%s, success=%s",
            resp.result_code, resp.success,
        )
        _raise_if_command_failed(resp, "return to dock")
        self.async_write_ha_state()

    async def async_locate(self, **kwargs) -> None:
        """Robot mówi, gdzie jest."""
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            if not await self.coordinator.async_refresh_action_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            state = self.coordinator.client.state
            if not can_locate_robot(state):
                raise HomeAssistantError("Narwal locate cannot be used right now")
            resp = await self.coordinator.client.locate()
        _raise_if_command_failed(resp, "locate")

    async def async_set_fan_speed(self, fan_speed: str, **kwargs) -> None:
        """Ustawia ssanie na następny start, a w trakcie sprzątania także na żywo."""
        level = FAN_SPEEDS.get(fan_speed)
        if level is None:
            raise HomeAssistantError(f"Unsupported Narwal fan speed: {fan_speed}")
        state = self.coordinator.data
        setup_available = can_edit_pending_clean_settings(state)
        live_available = super().available and is_live_clean_setting_available(state)
        setup_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_settings.work_mode,
        )
        live_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_setting_applicability_mode(live=True),
        )
        if not (
            (setup_available and setup_applies)
            or (live_available and live_applies)
        ):
            if not setup_applies and not live_applies:
                raise HomeAssistantError(
                    "Narwal fan speed is not available in mop-only mode"
                )
            raise HomeAssistantError("Narwal fan speed cannot be changed right now")
        if live_available and not live_applies:
            raise HomeAssistantError(
                "Narwal fan speed is not available in mop-only mode"
            )
        if live_available:
            live_level = fan_level_for_live_command(level)
            resp = await self.coordinator.client.set_fan_speed(live_level)
            _raise_if_command_failed(resp, "set fan speed")
            self.coordinator.set_active_clean_setting("fan", live_level)
        self.coordinator.clean_settings.fan = level
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()

    # --- Segmenty (sprzątanie pokoi przez obszary HA, vacuum.clean_area) ---

    async def async_get_segments(self) -> list:
        """Pokoje aktywnej mapy jako segmenty HA.

        Nazwy jak w aplikacji Narwal (RoomInfo.display_name). Gdy mapa nie jest
        jeszcze wczytana (robot śpi po starcie HA), zwraca segmenty zapamiętane
        przez HA, żeby clean_area działało bez wybudzania robota.
        """
        state = self.coordinator.data
        if state is None or state.map_data is None:
            last = getattr(self, "last_seen_segments", None)
            return list(last) if last else []
        return [
            Segment(
                id=str(room.room_id),
                name=room.display_name,
                group="Rooms" if room.category == 1 else "Utility" if room.category == 2 else None,
            )
            for room in state.map_data.rooms
            if room.room_id > 0
        ]

    async def async_clean_segments(
        self, segment_ids: list[str], **kwargs: Any
    ) -> None:
        """Sprząta wskazane pokoje (segmenty HA) z ostatnimi ustawieniami."""
        await self._ensure_awake()
        try:
            room_ids = list(dict.fromkeys(int(sid) for sid in segment_ids))
        except (TypeError, ValueError) as err:
            raise HomeAssistantError("Narwal segment IDs must be numeric") from err
        if not room_ids:
            raise HomeAssistantError("Narwal segment IDs must not be empty")
        if any(room_id <= 0 for room_id in room_ids):
            raise HomeAssistantError("Narwal segment IDs must be positive")

        async with self.coordinator.dock_action_lock:
            try:
                await self.coordinator.client.get_map()
            except Exception as err:
                raise HomeAssistantError("Narwal map could not be refreshed") from err
            state = self.coordinator.client.state
            known_ids = {
                room.room_id
                for room in getattr(getattr(state, "map_data", None), "rooms", ())
                if room.room_id > 0
            }
            if not known_ids:
                raise HomeAssistantError("Narwal map is not available")
            unknown_ids = [room_id for room_id in room_ids if room_id not in known_ids]
            if unknown_ids:
                raise HomeAssistantError(
                    "Unknown Narwal room ID: "
                    f"{', '.join(str(room_id) for room_id in unknown_ids)}"
                )
            settings = self.coordinator.clean_settings
            _LOGGER.info(
                "Start pokoi: rooms=%s mode=%s fan=%s water=%s mop_strength=%s passes=%s route=%s",
                room_ids, settings.work_mode.name, settings.fan.name,
                settings.water.name, settings.mop_strength.name, settings.passes,
                settings.route.name,
            )
            resp = await self._start_rooms(room_ids)
        result_name = _result_name(resp.result_code)
        _LOGGER.info(
            "Odpowiedź na start pokoi: %s (code=%s), rooms=%s",
            result_name, resp.result_code, room_ids,
        )
        if not resp.accepted:
            _LOGGER.warning(
                "Start pokoi odrzucony: %s (code=%s), rooms=%s. CONFLICT = robot zajęty, "
                "NOT_APPLICABLE = nie może teraz sprzątać, NOT_READY = poza stacją.",
                result_name, resp.result_code, room_ids,
            )
            raise HomeAssistantError(
                f"Narwal room clean failed: {result_name}"
            )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Aktualizacja z koordynatora."""
        self._check_segment_changes()
        super()._handle_coordinator_update()

    def _check_segment_changes(self) -> None:
        """Zgłasza naprawę HA, gdy pokoje mapy zmieniły się względem mapowania obszarów."""
        last = getattr(self, "last_seen_segments", None)
        if last is None:
            return
        state = self.coordinator.data
        if state is None or state.map_data is None:
            return
        current_set = {
            (str(r.room_id), r.display_name)
            for r in state.map_data.rooms
            if r.room_id > 0
        }
        last_set = {(s.id, s.name) for s in last}
        if current_set == last_set:
            self._last_reported_segment_signature = None
            return
        signature = (frozenset(last_set), frozenset(current_set))
        if signature == self._last_reported_segment_signature:
            return
        self._last_reported_segment_signature = signature
        _LOGGER.info(
            "Zmiana pokoi mapy: %d -> %d",
            len(last_set), len(current_set),
        )
        self.async_create_segments_issue()
