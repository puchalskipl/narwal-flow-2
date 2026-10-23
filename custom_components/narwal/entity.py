"""Bazowe klasy encji integracji Narwal Flow 2."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import NarwalCoordinator


def narwal_robot_device_info(coordinator: NarwalCoordinator) -> DeviceInfo:
    """Urządzenie robota."""
    device_id = coordinator.config_entry.data["device_id"]
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        sw_version=coordinator.client.state.firmware_version or None,
        name=coordinator.config_entry.title,
    )


def narwal_dock_device_info(coordinator: NarwalCoordinator) -> DeviceInfo:
    """Urządzenie stacji (dok) podpięte pod robota."""
    device_id = coordinator.config_entry.data["device_id"]
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{device_id}_dock")},
        manufacturer=MANUFACTURER,
        model=f"{MODEL} Dock",
        sw_version=coordinator.client.state.firmware_version or None,
        name=f"{coordinator.config_entry.title} Dock",
    )
    if coordinator.robot_device_id:
        info["via_device_id"] = coordinator.robot_device_id
    return info


class NarwalEntity(CoordinatorEntity[NarwalCoordinator]):
    """Encja robota."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Inicjalizacja."""
        super().__init__(coordinator)
        self._attr_device_info = narwal_robot_device_info(coordinator)

    @property
    def available(self) -> bool:
        """Dostępna, gdy koordynator ma aktualne dane."""
        return self.coordinator.last_update_success


class NarwalDockEntity(NarwalEntity):
    """Encja stacji."""

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Inicjalizacja."""
        super().__init__(coordinator)
        self._attr_device_info = narwal_dock_device_info(coordinator)
