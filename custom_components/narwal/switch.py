"""Przełączniki zadań stacji Narwal (opróżnianie, mycie, suszenie)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .dock_tasks import (
    DOCK_TASKS,
    can_start_dock_task,
    can_stop_dock_task,
    is_robot_work_context,
)
from .entity import NarwalDockEntity
from .narwal_client import CommandResponse, CommandResult


@dataclass(frozen=True, kw_only=True)
class NarwalDockTaskSwitchEntityDescription(SwitchEntityDescription):
    """Opis przełącznika zadania stacji."""

    action: str
    icon: str


DOCK_TASK_SWITCHES: tuple[NarwalDockTaskSwitchEntityDescription, ...] = tuple(
    NarwalDockTaskSwitchEntityDescription(
        key=task.key,
        translation_key=task.translation_key,
        action=task.action,
        icon=task.icon,
    )
    for task in DOCK_TASKS
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Dodaje przełączniki zadań stacji."""
    coordinator = entry.runtime_data
    async_add_entities(
        NarwalDockTaskSwitch(coordinator, description)
        for description in DOCK_TASK_SWITCHES
    )


def _format_duration(seconds: int) -> str:
    """Krótki czas trwania do atrybutu."""
    minutes = _duration_minutes(seconds)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _duration_minutes(seconds: int) -> int:
    """Pozostały czas zaokrąglony w górę do pełnych minut."""
    if seconds <= 0:
        return 0
    return (seconds + 59) // 60


def _accepted_response(response: CommandResponse) -> bool:
    """Czy robot przyjął polecenie."""
    return response.accepted


class NarwalDockTaskSwitch(NarwalDockEntity, SwitchEntity):
    """Start/stop jednego zadania stacji."""

    entity_description: NarwalDockTaskSwitchEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalDockTaskSwitchEntityDescription,
    ) -> None:
        """Inicjalizacja."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_icon = description.icon

    @property
    def is_on(self) -> bool | None:
        """Czy zadanie trwa."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.key in state.active_dock_task_keys

    @property
    def available(self) -> bool:
        """Dostępny, gdy zadanie da się uruchomić albo zatrzymać."""
        if not self.coordinator.client.connected:
            return False
        state = self.coordinator.data
        if state is None:
            return False
        if not self.coordinator.has_fresh_state:
            # Zostaje osiągalny, żeby móc wybudzić robota i odświeżyć stan stacji.
            return True
        if self.is_on:
            return can_stop_dock_task(state, self.entity_description.key)
        return can_start_dock_task(state, self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, str | int] | None:
        """Postęp zadania z telemetrii stacji."""
        state = self.coordinator.data
        if state is None or not self.is_on:
            return None
        timer = state.dock_task_timer(self.entity_description.key)
        if timer is None:
            return None
        return {
            "time_left": _format_duration(timer.remaining),
            "progress": timer.progress_percent,
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Uruchamia zadanie stacji."""
        async with self.coordinator.dock_action_lock:
            client = self.coordinator.client
            stale_state = not self.coordinator.has_fresh_state
            force_wake = stale_state and not is_robot_work_context(client.state)
            if not client.robot_awake or force_wake:
                await client.wake(timeout=10.0, force=force_wake)
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal dock status could not be refreshed")
            if self.is_on:
                return
            if not can_start_dock_task(client.state, self.entity_description.key):
                raise HomeAssistantError("Narwal dock task cannot be started right now")

            command: Callable[[], Awaitable[CommandResponse]] = getattr(
                client,
                self.entity_description.action,
            )
            response = await command()
            self._raise_if_command_failed(response, "start")
            self.coordinator.async_set_updated_data(client.state)
            await self.coordinator.async_refresh_dock_status()

    async def async_turn_off(self, **kwargs) -> None:
        """Zatrzymuje zadanie stacji."""
        async with self.coordinator.dock_action_lock:
            client = self.coordinator.client
            stale_state = not self.coordinator.has_fresh_state
            force_wake = stale_state and not is_robot_work_context(client.state)
            if not client.robot_awake or force_wake:
                await client.wake(timeout=10.0, force=force_wake)
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal dock status could not be refreshed")
            if self.entity_description.key not in client.state.active_dock_task_keys:
                return
            if not can_stop_dock_task(client.state, self.entity_description.key):
                raise HomeAssistantError("Narwal dock task cannot be stopped right now")

            response = await client.stop_dock_task(self.entity_description.key)
            self._raise_if_command_failed(response, "stop")
            self.coordinator.async_set_updated_data(client.state)
            await self.coordinator.async_refresh_dock_status()

    def _raise_if_command_failed(self, response: CommandResponse, action: str) -> None:
        """Błąd usługi HA dla odrzuconego polecenia stacji."""
        if _accepted_response(response):
            return
        try:
            result_name = CommandResult(response.result_code).name
        except (TypeError, ValueError):
            result_name = f"UNKNOWN({response.result_code})"
        raise HomeAssistantError(
            f"Narwal {action} {self.entity_description.key} failed: {result_name}"
        )
