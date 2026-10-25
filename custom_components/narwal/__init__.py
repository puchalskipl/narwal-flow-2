"""Narwal Flow Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import TypeAlias

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service

from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DEFAULT_PRODUCT_KEY,
    DOMAIN,
    FAN_SPEEDS,
    MANUFACTURER,
    MODEL,
    MOP_STRENGTH_MAP,
    PLATFORMS,
    ROUTE_MAP,
    SERVICE_CLEAN_ROOMS,
    SERVICE_GET_CURRENT_TASK,
    SERVICE_RESET_CONSUMABLE,
    SERVICE_SEND_RAW,
    WATER_MAP,
    WORK_MODE_MAP,
)
from .coordinator import NarwalCoordinator, can_prepare_clean_start
from .narwal_client import (
    CommandResult,
    NarwalConnectionError,
    RoomCleanSettings,
)

_LOGGER = logging.getLogger(__name__)

# Configured from config entries only; async_setup just registers services.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

NarwalConfigEntry: TypeAlias = ConfigEntry[NarwalCoordinator]

_CONFIG_ENTRY_MINOR_VERSION = 2
_LEGACY_REPLACED_SENSOR_SUFFIXES = (
    "base_station_cleaning_filter_used_hours",
    "current_room",
    "map_metadata",
    "status",
    "task_progress",
    "task_status",
)

FIELD_ROOMS = "rooms"
FIELD_MODE = "mode"
FIELD_SUCTION = "suction"
FIELD_WATER = "water"
FIELD_MOP_STRENGTH = "mop_strength"
FIELD_PASSES = "passes"
FIELD_MOP_PASSES = "mop_passes"
FIELD_ROUTE = "route"
FIELD_FREO = "freo"

WORK_MODE_OPTIONS = WORK_MODE_MAP
SUCTION_OPTIONS = tuple(FAN_SPEEDS)
WATER_OPTIONS = WATER_MAP
MOP_STRENGTH_OPTIONS = MOP_STRENGTH_MAP
ROUTE_OPTIONS = ROUTE_MAP

CLEAN_ROOMS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids,
        vol.Optional(ATTR_DEVICE_ID): cv.ensure_list,
        vol.Optional(ATTR_AREA_ID): cv.ensure_list,
        vol.Required(FIELD_ROOMS): cv.ensure_list,
        vol.Optional(FIELD_MODE, default="vacuum_and_mop"): vol.In(WORK_MODE_OPTIONS),
        vol.Optional(FIELD_SUCTION, default="standard"): vol.In(SUCTION_OPTIONS),
        vol.Optional(FIELD_WATER, default="normal"): vol.In(WATER_OPTIONS),
        vol.Optional(FIELD_MOP_STRENGTH, default="normal"): vol.In(
            MOP_STRENGTH_OPTIONS
        ),
        vol.Optional(FIELD_PASSES, default=1): lambda value: _validate_pass_count(
            value
        ),
        vol.Optional(FIELD_MOP_PASSES): lambda value: _validate_pass_count(value),
        vol.Optional(FIELD_ROUTE): vol.In(ROUTE_OPTIONS),
        vol.Optional(FIELD_FREO, default=False): cv.boolean,
    }
)

GET_CURRENT_TASK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids,
        vol.Optional(ATTR_DEVICE_ID): cv.ensure_list,
        vol.Optional(ATTR_AREA_ID): cv.ensure_list,
    }
)

RESET_KINDS = {"konserwacja": 1, "wymiana": 2}

RESET_CONSUMABLE_SCHEMA = GET_CURRENT_TASK_SCHEMA.extend(
    {
        vol.Required("rodzaj"): vol.In(RESET_KINDS),
        vol.Required("pozycja"): vol.Coerce(int),
    }
)

SEND_RAW_SCHEMA = GET_CURRENT_TASK_SCHEMA.extend(
    {
        vol.Required("topic"): str,
        vol.Optional("payload_hex", default=""): str,
        vol.Optional("timeout", default=15.0): vol.Coerce(float),
    }
)


def _jsonable(value: object) -> object:
    """Convert a decoded protobuf dict to JSON-safe values (bytes as hex)."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value


def _domain_data(hass: HomeAssistant) -> dict[str, NarwalCoordinator]:
    """Return Narwal domain runtime data."""
    return hass.data.setdefault(DOMAIN, {})


def _validate_pass_count(value: object) -> int:
    """Return a pass count, rejecting fractional coercion."""
    if isinstance(value, bool):
        raise vol.Invalid("Narwal pass count must be an integer")
    if isinstance(value, int):
        passes = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise vol.Invalid("Narwal pass count must be an integer")
        passes = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            raise vol.Invalid("Narwal pass count must be an integer")
        passes = int(text)
    else:
        raise vol.Invalid("Narwal pass count must be an integer")
    if passes < 1 or passes > 3:
        raise vol.Invalid("Narwal pass count must be between 1 and 3")
    return passes


def _room_tokens(raw_rooms: list) -> list[object]:
    """Return room request tokens split the same way for all/all+room checks."""
    tokens: list[object] = []
    for item in raw_rooms:
        if isinstance(item, str):
            cleaned = item.strip().strip("[]")
            tokens.extend(cleaned.replace(",", " ").split())
        else:
            tokens.append(item)
    return tokens


def _normalise_room_ids(raw_rooms: list) -> list[int]:
    """Return room IDs from HA service data."""
    room_ids: list[int] = []
    seen: set[int] = set()
    for value in _room_tokens(raw_rooms):
        if isinstance(value, str) and value.lower() == "all":
            continue
        if isinstance(value, bool):
            raise HomeAssistantError(f"Invalid Narwal room ID: {value}")
        try:
            if isinstance(value, float):
                if not value.is_integer():
                    raise ValueError
                room_id = int(value)
            elif isinstance(value, int):
                room_id = value
            elif isinstance(value, str) and value.isdecimal():
                room_id = int(value)
            else:
                raise ValueError
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(f"Invalid Narwal room ID: {value}") from err
        if room_id <= 0:
            raise HomeAssistantError("Narwal room IDs must be positive")
        if room_id not in seen:
            seen.add(room_id)
            room_ids.append(room_id)
    return room_ids


def _rooms_requested_all(raw_rooms: list) -> bool:
    """Return True if a service call requests all current map rooms."""
    return any(
        isinstance(item, str) and item.lower() == "all"
        for item in _room_tokens(raw_rooms)
    )


async def _async_room_ids_for_coordinator(
    coordinator: NarwalCoordinator,
    raw_rooms: list,
) -> list[int]:
    """Resolve configured service rooms for a single Narwal coordinator."""
    requested_all = _rooms_requested_all(raw_rooms)
    tokens = _room_tokens(raw_rooms)
    if requested_all and len(tokens) > 1:
        raise HomeAssistantError('Use either "all" or explicit room IDs, not both')
    state = coordinator.client.state
    room_ids = [] if requested_all else _normalise_room_ids(raw_rooms)
    # Explicit IDs already on the cached map leave nothing to resolve, so a
    # map refresh that times out must not abort the clean (#108).
    if room_ids and state.map_data is not None:
        cached_ids = {room.room_id for room in state.map_data.rooms if room.room_id > 0}
        if all(room_id in cached_ids for room_id in room_ids):
            return room_ids
    try:
        await coordinator.client.get_map()
    except Exception as err:
        raise HomeAssistantError("Narwal map could not be refreshed") from err
    if state.map_data is None:
        raise HomeAssistantError("Narwal map is not available")
    known_room_ids = {room.room_id for room in state.map_data.rooms if room.room_id > 0}
    if requested_all:
        return sorted(known_room_ids)

    unknown_ids = [room_id for room_id in room_ids if room_id not in known_room_ids]
    if unknown_ids:
        raise HomeAssistantError(
            f"Unknown Narwal room ID: {', '.join(str(room_id) for room_id in unknown_ids)}"
        )
    return room_ids


async def _async_validate_target_permissions(
    hass: HomeAssistant,
    call: ServiceCall,
    entity_ids: list[str],
) -> None:
    """Validate control permission for service target entities."""
    user_id = call.context.user_id
    if not user_id:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    if user.is_admin:
        return
    for entity_id in entity_ids:
        if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
            raise Unauthorized(
                context=call.context,
                entity_id=entity_id,
                permission=POLICY_CONTROL,
            )


async def _async_validate_clean_rooms_targets(
    hass: HomeAssistant,
    call: ServiceCall,
    entity_ids: list[str],
) -> list[str]:
    """Validate clean-room target domains and user permissions."""
    registry = er.async_get(hass)
    vacuum_entity_ids: list[str] = []
    for entity_id in entity_ids:
        registry_entry = registry.async_get(entity_id)
        if (
            entity_id.startswith(f"{VACUUM_DOMAIN}.")
            and registry_entry is not None
            and registry_entry.platform == DOMAIN
            and hass.states.get(entity_id) is not None
        ):
            vacuum_entity_ids.append(entity_id)

    direct_entity_ids = call.data.get(ATTR_ENTITY_ID, [])
    if isinstance(direct_entity_ids, str):
        direct_entity_ids = [direct_entity_ids]
    direct_entity_ids = [
        entity_id
        for entity_id in direct_entity_ids
        if entity_id != "all" and not entity_id.startswith("group.")
    ]
    if any(entity_id not in vacuum_entity_ids for entity_id in direct_entity_ids):
        raise HomeAssistantError("Target must be a Narwal vacuum entity")

    await _async_validate_target_permissions(hass, call, vacuum_entity_ids)
    return vacuum_entity_ids


async def _async_get_service_coordinators(
    hass: HomeAssistant,
    entity_ids: list[str] | None,
) -> list[NarwalCoordinator]:
    """Resolve service entity IDs to Narwal coordinators."""
    data = _domain_data(hass)
    if not entity_ids:
        raise HomeAssistantError("Target a Narwal vacuum")

    registry = er.async_get(hass)
    coordinators: list[NarwalCoordinator] = []
    for entity_id in entity_ids:
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None or registry_entry.config_entry_id is None:
            raise HomeAssistantError(
                f"Narwal target is not loaded: {entity_id}"
            )
        coordinator = data.get(registry_entry.config_entry_id)
        if not isinstance(coordinator, NarwalCoordinator):
            raise HomeAssistantError(
                f"Narwal target is not loaded: {entity_id}"
            )
        if coordinator not in coordinators:
            coordinators.append(coordinator)
    if not coordinators:
        raise HomeAssistantError("Target does not contain a Narwal entity")
    return coordinators


def _async_register_services(hass: HomeAssistant) -> None:
    """Register Narwal domain services."""
    if hass.services.has_service(DOMAIN, SERVICE_CLEAN_ROOMS):
        return

    async def _async_extract_entity_ids(call: ServiceCall) -> list[str]:
        """Extract target entity IDs across supported HA helper signatures."""
        try:
            entity_ids = await service.async_extract_entity_ids(call)
        except TypeError:
            entity_ids = await service.async_extract_entity_ids(hass, call)
        return list(entity_ids)

    async def async_clean_rooms(call: ServiceCall) -> None:
        entity_ids = await _async_extract_entity_ids(call)
        if not entity_ids and any(
            key in call.data for key in (ATTR_ENTITY_ID, ATTR_DEVICE_ID, ATTR_AREA_ID)
        ):
            raise HomeAssistantError("Target does not contain a Narwal entity")
        entity_ids = await _async_validate_clean_rooms_targets(hass, call, entity_ids)
        coordinators = await _async_get_service_coordinators(hass, entity_ids)
        if len(coordinators) != 1:
            raise HomeAssistantError("Target exactly one Narwal vacuum")
        coordinator = coordinators[0]

        work_mode = WORK_MODE_OPTIONS[call.data[FIELD_MODE]]
        water = WATER_OPTIONS[call.data[FIELD_WATER]]
        mop_strength = MOP_STRENGTH_OPTIONS[call.data[FIELD_MOP_STRENGTH]]
        passes = call.data[FIELD_PASSES]
        route_label = call.data.get(FIELD_ROUTE)

        if not coordinator.client.robot_awake:
            await coordinator.client.wake(timeout=10.0)

        room_ids = await _async_room_ids_for_coordinator(
            coordinator,
            call.data[FIELD_ROOMS],
        )
        if not room_ids:
            raise HomeAssistantError("At least one room must be selected")

        fan = FAN_SPEEDS[call.data[FIELD_SUCTION]]
        route = ROUTE_OPTIONS.get(route_label, coordinator.clean_settings.route)
        requested_settings = RoomCleanSettings(
            work_mode=work_mode,
            fan=fan,
            water=water,
            mop_strength=mop_strength,
            passes=passes,
            route=route,
            mop_passes=call.data.get(FIELD_MOP_PASSES),
            freo=call.data.get(FIELD_FREO, False),
        )
        async with coordinator.dock_action_lock:
            if not await coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            if not can_prepare_clean_start(coordinator.client.state):
                raise HomeAssistantError("Narwal room clean cannot be started right now")
            if not await coordinator.async_prepare_clean_start():
                raise HomeAssistantError("Narwal room clean cannot be started right now")

            client = coordinator.client
            resp = await client.start_rooms(
                room_ids,
                work_mode=requested_settings.work_mode,
                fan=requested_settings.fan,
                water=requested_settings.water,
                mop_strength=requested_settings.mop_strength,
                passes=requested_settings.passes,
                route=requested_settings.route,
                mop_passes=requested_settings.mop_passes,
                freo=requested_settings.freo,
            )
            if resp.result_code == 0:
                result_name = "ACCEPTED"
            else:
                try:
                    result_name = CommandResult(resp.result_code).name
                except ValueError:
                    result_name = f"UNKNOWN({resp.result_code})"
            _LOGGER.info(
                "Clean rooms response: %s (code=%s), rooms=%s",
                result_name,
                resp.result_code,
                room_ids,
            )
            if not resp.accepted:
                raise HomeAssistantError(
                    f"Narwal room clean failed: {result_name} ({resp.result_code})"
                )
            coordinator.clean_settings.work_mode = requested_settings.work_mode
            coordinator.clean_settings.fan = requested_settings.fan
            coordinator.clean_settings.water = requested_settings.water
            coordinator.clean_settings.mop_strength = requested_settings.mop_strength
            coordinator.clean_settings.passes = requested_settings.passes
            coordinator.clean_settings.route = requested_settings.route
            coordinator.record_accepted_clean_start(requested_settings)
            client.state.assume_robot_clean()
            await coordinator.async_clear_map_display_cache()
            coordinator.async_set_updated_data(client.state)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAN_ROOMS,
        async_clean_rooms,
        schema=CLEAN_ROOMS_SCHEMA,
    )

    async def async_get_current_task(call: ServiceCall) -> ServiceResponse:
        """Return the robot's current CleanTask (clean/current_clean_task/get)."""
        entity_ids = await _async_extract_entity_ids(call)
        entity_ids = await _async_validate_clean_rooms_targets(hass, call, entity_ids)
        coordinators = await _async_get_service_coordinators(hass, entity_ids)
        if len(coordinators) != 1:
            raise HomeAssistantError("Target exactly one Narwal vacuum")
        client = coordinators[0].client
        if not client.robot_awake:
            await client.wake(timeout=10.0)
        resp = await client.get_current_task()
        return {
            "result_code": resp.result_code,
            "data": _jsonable(resp.data),
            "raw_hex": resp.raw_payload.hex(),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CURRENT_TASK,
        async_get_current_task,
        schema=GET_CURRENT_TASK_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    async def _async_one_coordinator(call: ServiceCall):
        entity_ids = await _async_extract_entity_ids(call)
        entity_ids = await _async_validate_clean_rooms_targets(hass, call, entity_ids)
        coordinators = await _async_get_service_coordinators(hass, entity_ids)
        if len(coordinators) != 1:
            raise HomeAssistantError("Target exactly one Narwal vacuum")
        coordinator = coordinators[0]
        if not coordinator.client.robot_awake:
            await coordinator.client.wake(timeout=10.0)
        return coordinator

    async def async_reset_consumable(call: ServiceCall) -> ServiceResponse:
        """Kasuje przypomnienie o pozycji konserwacji albo wymiany (po wykonaniu)."""
        coordinator = await _async_one_coordinator(call)
        resp = await coordinator.client.reset_consumable(
            RESET_KINDS[call.data["rodzaj"]], call.data["pozycja"]
        )
        coordinator.async_set_updated_data(coordinator.client.state)
        state = coordinator.client.state
        return {
            "result_code": resp.result_code,
            "raw_hex": resp.raw_payload.hex(),
            "konserwacja": list(state.maintain_items),
            "wymiana": list(state.replace_items),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_CONSUMABLE,
        async_reset_consumable,
        schema=RESET_CONSUMABLE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def async_send_raw(call: ServiceCall) -> ServiceResponse:
        """Diagnostyka: dowolne polecenie (krótki temat + ładunek hex), zwraca odpowiedź."""
        coordinator = await _async_one_coordinator(call)
        resp = await coordinator.client.send_command(
            call.data["topic"],
            bytes.fromhex(call.data["payload_hex"]),
            timeout=call.data["timeout"],
        )
        return {
            "result_code": resp.result_code,
            "data": _jsonable(resp.data),
            "raw_hex": resp.raw_payload.hex(),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_RAW,
        async_send_raw,
        schema=SEND_RAW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Narwal services."""
    _async_register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries and remove replaced status sensors."""
    update_kwargs: dict[str, object] = {}

    if config_entry.version < 2:
        _LOGGER.info(
            "Migrating Narwal config entry from version %d to 2",
            config_entry.version,
        )
        new_data = {**config_entry.data}
        if CONF_PRODUCT_KEY not in new_data:
            new_data[CONF_PRODUCT_KEY] = DEFAULT_PRODUCT_KEY
        if CONF_MODEL not in new_data:
            new_data[CONF_MODEL] = f"Narwal {MODEL}"
        update_kwargs["data"] = new_data
        update_kwargs["version"] = 2
        _LOGGER.info("Migration complete: product_key=%s", new_data[CONF_PRODUCT_KEY])

    if getattr(config_entry, "minor_version", 1) < _CONFIG_ENTRY_MINOR_VERSION:
        _async_remove_legacy_replaced_sensors(hass, config_entry)
        update_kwargs["minor_version"] = _CONFIG_ENTRY_MINOR_VERSION

    if update_kwargs:
        hass.config_entries.async_update_entry(config_entry, **update_kwargs)
    return True


def _async_remove_legacy_replaced_sensors(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> None:
    """Remove old sensors now represented by native entities or attributes."""
    device_id = config_entry.data.get(CONF_DEVICE_ID)
    if not device_id:
        return

    registry = er.async_get(hass)
    unique_ids = {
        f"{device_id}_{suffix}" for suffix in _LEGACY_REPLACED_SENSOR_SUFFIXES
    }
    entity_ids: set[str] = set()

    for unique_id in unique_ids:
        entity_id = registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            unique_id,
        )
        if entity_id is not None:
            entity_ids.add(entity_id)

    for entry in er.async_entries_for_config_entry(
        registry,
        config_entry.entry_id,
    ):
        if (
            entry.domain == "sensor"
            and entry.platform == DOMAIN
            and entry.unique_id in unique_ids
        ):
            entity_ids.add(entry.entity_id)

    for entity_id in sorted(entity_ids):
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None or registry_entry.platform != DOMAIN:
            continue
        _LOGGER.info("Removing legacy Narwal sensor %s", entity_id)
        registry.async_remove(entity_id)


def _async_remove_dropped_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> None:
    """Usuwa z rejestru encje, których fork już nie tworzy.

    Integracja autora dawała selecty i number z ustawieniami, przełączniki
    wyboru pokoi i opcji mapy oraz kamery (mapa MJPEG i obraz diagnostyczny).
    Po zmianie na fork zostałyby w rejestrze jako „niedostępne”.
    """
    registry = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        if entry.platform != DOMAIN:
            continue
        unique_id = str(entry.unique_id)
        dropped = (
            entry.domain in ("select", "number")
            or (entry.domain == "switch" and (unique_id.endswith("_selected") or "_map_show_" in unique_id))
            or entry.domain == "camera"  # mapa jest teraz encją image, obraz diagnostyczny zniknął
        )
        if dropped:
            _LOGGER.info("Usuwam encję bez odpowiednika w forku: %s", entry.entity_id)
            registry.async_remove(entry.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Set up Narwal from a config entry."""
    _async_remove_legacy_replaced_sensors(hass, entry)
    _async_remove_dropped_entities(hass, entry)
    coordinator = NarwalCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except NarwalConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Narwal vacuum at {entry.data['host']}: {err}"
        ) from err
    except Exception as err:  # noqa: BLE001 - any startup failure must retry
        # An unexpected exception here leaves the entry in HA's setup-error
        # state, which is never retried (#101). Ask for a retry instead.
        _LOGGER.warning(
            "Narwal setup at %s failed unexpectedly, will retry: %s",
            entry.data.get("host"),
            err,
        )
        raise ConfigEntryNotReady(
            f"Narwal setup at {entry.data.get('host')} failed: {err}"
        ) from err

    entry.runtime_data = coordinator
    _domain_data(hass)[entry.entry_id] = coordinator

    # Urządzenie robota powstaje tu, przed platformami, żeby encje stacji mogły
    # wskazać je przez `via_device_id` (parametr `via_device` znika w HA 2027.8).
    device_registry = dr.async_get(hass)
    robot_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=entry.title,
        sw_version=coordinator.client.state.firmware_version or None,
    )
    coordinator.robot_device_id = robot_device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await entry.runtime_data.async_shutdown()
        _domain_data(hass).pop(entry.entry_id, None)

    return unload_ok
