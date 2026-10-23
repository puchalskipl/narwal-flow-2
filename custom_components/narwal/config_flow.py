"""Config flow integracji Narwal Flow 2."""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DEFAULT_PORT,
    DEFAULT_PRODUCT_KEY,
    DOMAIN,
    MODEL,
    PRODUCT_KEYS,
)
from .narwal_client import NarwalClient

_LOGGER = logging.getLogger(__name__)

# `NARWAL_7bb53c.local.` (nazwa DHCP/mDNS) i nazwa instancji zeroconf
# `_app_wss_server_7bb53c._narwal_sweeper._tcp.local.` kończą się ostatnimi
# sześcioma znakami device_id robota.
_DISCOVERY_NAME_RE = re.compile(r"(?:narwal|app_wss_server)[_-]([0-9a-f]{6})", re.I)


def _is_ipv6(host: str) -> bool:
    """Zwraca True, gdy host jest adresem IPv6."""
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def _preferred_host(discovery_info: ZeroconfServiceInfo) -> str:
    """Wybiera adres IPv4 z rekordu zeroconf, gdy jest ogłaszany."""
    for address in getattr(discovery_info, "ip_addresses", None) or ():
        if getattr(address, "version", None) == 4:
            return str(address)
    return str(discovery_info.host)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
    }
)

CONF_RETRY_AUTO_DETECT = "retry_auto_detect"

# device_id jest opcjonalny, żeby dało się wysłać samo pole „spróbuj ponownie”.
STEP_DEVICE_ID_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_DEVICE_ID, default=""): str,
        vol.Optional(CONF_RETRY_AUTO_DETECT, default=False): bool,
    }
)


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow robota Narwal Flow 2."""

    VERSION = 2
    MINOR_VERSION = 2

    _discovered_host: str | None = None

    def __init__(self) -> None:
        """Inicjalizacja."""
        self._pending_user_input: dict[str, Any] = {}

    @staticmethod
    def _user_schema(default_host: str | None) -> vol.Schema:
        """Schemat kroku użytkownika z podpowiedzianym hostem z wykrywania."""
        if not default_host:
            return STEP_USER_DATA_SCHEMA
        return vol.Schema(
            {
                vol.Required("host", default=default_host): str,
                vol.Optional("port", default=DEFAULT_PORT): int,
            }
        )

    @staticmethod
    def _device_id_suffix(hostname: str) -> str | None:
        """Sześć znaków hex z nazwy wykrytego robota (końcówka device_id)."""
        match = _DISCOVERY_NAME_RE.search(hostname)
        return match.group(1).lower() if match else None

    async def _async_discovered(self, host: str, fallback_uid: str) -> ConfigFlowResult:
        """Kieruje wykryty robot do kroku użytkownika albo przerywa, gdy już jest skonfigurowany.

        unique_id wpisu to device_id robota, którego mDNS ani DHCP nie widzą,
        więc dopasowanie idzie po końcówce device_id z nazwy wykrycia, a w zapasie po adresie.
        """
        suffix = self._device_id_suffix(fallback_uid)
        for entry in self._async_current_entries(include_ignore=False):
            same_device = bool(suffix) and str(
                entry.data.get(CONF_DEVICE_ID, "")
            ).lower().endswith(suffix)
            if not same_device and entry.data.get("host") != host:
                continue
            if same_device and entry.data.get("host") != host and not _is_ipv6(host):
                # Ten sam robot pod nowym adresem (DHCP). Nigdy nie przepinamy na IPv6:
                # wykrywanie wraca po każdym starcie HA z tym, co mDNS ma w pamięci.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "host": host}
                )
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(fallback_uid)
        if _is_ipv6(host):
            self._abort_if_unique_id_configured()
        else:
            self._abort_if_unique_id_configured(updates={"host": host})

        self._discovered_host = host
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_user()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Robot ogłasza `_app_wss_server_<6hex>._narwal_sweeper._tcp.local.` na porcie 9002."""
        return await self._async_discovered(
            _preferred_host(discovery_info), discovery_info.hostname.rstrip(".")
        )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Zapas, gdy mDNS nie przechodzi: nazwa hosta `narwal_*` z DHCP."""
        host = str(discovery_info.ip)
        return await self._async_discovered(host, discovery_info.hostname or host)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Krok użytkownika: adres i port robota, reszta wykrywana z robota."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            port = user_input.get("port", DEFAULT_PORT)
            self._pending_user_input = user_input
            client = NarwalClient(host=host, port=port)
            try:
                await client.connect()
            except Exception as ex:
                _LOGGER.warning("Połączenie z robotem nie powiodło się: %s: %s", type(ex).__name__, ex)
                errors["base"] = "cannot_connect"
            else:
                try:
                    await client.discover_device_id(timeout=15.0)
                    await client.drain_ws_buffer()
                    device_info = await client.get_device_info()
                except Exception as ex:
                    _LOGGER.info("Automatyczne wykrycie device_id nie powiodło się: %s", ex)
                    return await self.async_step_device_id()
                else:
                    return await self._async_create_entry(client, device_info.device_id)
            finally:
                await client.disconnect()

        default_host = (user_input or {}).get("host") or self._discovered_host
        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(default_host),
            errors=errors,
        )

    async def async_step_device_id(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ręczne podanie device_id, gdy wykrywanie zawiodło."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get(CONF_RETRY_AUTO_DETECT):
                return await self.async_step_user()

            device_id = user_input.get(CONF_DEVICE_ID, "").strip()
            if not device_id:
                return self.async_show_form(
                    step_id="device_id",
                    data_schema=STEP_DEVICE_ID_DATA_SCHEMA,
                    errors={"base": "device_id_required"},
                )

            setup = self._pending_user_input
            client = NarwalClient(
                host=setup["host"],
                port=setup.get("port", DEFAULT_PORT),
                device_id=device_id,
            )
            try:
                await client.connect()
                device_info = await client.get_device_info()
            except Exception as ex:
                _LOGGER.warning("Sprawdzenie device_id nie powiodło się: %s: %s", type(ex).__name__, ex)
                errors["base"] = "cannot_connect"
            else:
                return await self._async_create_entry(client, device_info.device_id)
            finally:
                await client.disconnect()

        return self.async_show_form(
            step_id="device_id",
            data_schema=STEP_DEVICE_ID_DATA_SCHEMA,
            errors=errors,
        )

    async def _async_create_entry(self, client: NarwalClient, device_id: str) -> ConfigFlowResult:
        """Tworzy wpis po potwierdzeniu tożsamości robota."""
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        product_key = (client.topic_prefix or "").lstrip("/") or DEFAULT_PRODUCT_KEY
        if product_key not in PRODUCT_KEYS:
            _LOGGER.warning(
                "Robot zgłasza klucz produktu %s, integracja obsługuje tylko Flow 2 (%s)",
                product_key,
                ", ".join(sorted(PRODUCT_KEYS)),
            )
            return self.async_abort(reason="unsupported_model")

        setup = self._pending_user_input
        return self.async_create_entry(
            title=f"Narwal {MODEL}",
            data={
                "host": setup["host"],
                "port": setup.get("port", DEFAULT_PORT),
                CONF_DEVICE_ID: device_id,
                CONF_PRODUCT_KEY: product_key,
                CONF_MODEL: f"Narwal {MODEL}",
            },
        )
