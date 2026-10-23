"""Tests for Narwal config flow -- covers HACS default listing requirements.

Mocks the homeassistant framework via ha_stubs so config_flow.py can be
imported and tested without a full HA installation.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.config_flow import NarwalConfigFlow  # noqa: E402
from custom_components.narwal.const import CONF_MODEL  # noqa: E402
from custom_components.narwal.narwal_client import NarwalConnectionError  # noqa: E402

AbortFlow = sys.modules["homeassistant.data_entry_flow"].AbortFlow
DhcpServiceInfo = sys.modules["homeassistant.helpers.service_info.dhcp"].DhcpServiceInfo
ZeroconfServiceInfo = sys.modules[
    "homeassistant.helpers.service_info.zeroconf"
].ZeroconfServiceInfo


class TestNarwalConfigFlow:
    """Krok użytkownika: adres i port, tożsamość i klucz produktu z robota."""

    def _make_flow(self) -> NarwalConfigFlow:
        flow = NarwalConfigFlow.__new__(NarwalConfigFlow)
        flow._pending_user_input = {}
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        flow.async_abort = MagicMock(side_effect=lambda reason: {"type": "abort", "reason": reason})
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        return flow

    @staticmethod
    def _client(topic_prefix: str = "/QxMSPG6VSO", device_id: str = "flow2_device") -> AsyncMock:
        mock_client = AsyncMock()
        mock_client.topic_prefix = topic_prefix
        mock_device_info = MagicMock()
        mock_device_info.device_id = device_id
        mock_client.get_device_info.return_value = mock_device_info
        return mock_client

    async def test_show_form_when_no_input(self) -> None:
        flow = self._make_flow()
        await flow.async_step_user(user_input=None)
        assert flow.async_show_form.call_args.kwargs["step_id"] == "user"

    async def test_successful_setup_creates_entry(self) -> None:
        flow = self._make_flow()
        mock_client = self._client()
        with patch("custom_components.narwal.config_flow.NarwalClient", return_value=mock_client):
            await flow.async_step_user(user_input={"host": "10.0.0.100", "port": 9002})
        mock_client.connect.assert_awaited_once()
        mock_client.discover_device_id.assert_awaited_once()
        mock_client.get_device_info.assert_awaited_once()
        flow.async_set_unique_id.assert_awaited_once_with("flow2_device")
        entry_kwargs = flow.async_create_entry.call_args.kwargs
        assert entry_kwargs["title"] == "Narwal Flow 2"
        assert entry_kwargs["data"]["host"] == "10.0.0.100"
        assert entry_kwargs["data"]["port"] == 9002
        assert entry_kwargs["data"]["device_id"] == "flow2_device"
        assert entry_kwargs["data"]["product_key"] == "QxMSPG6VSO"
        assert entry_kwargs["data"][CONF_MODEL] == "Narwal Flow 2"
        mock_client.disconnect.assert_awaited_once()

    async def test_alternate_flow2_key_is_accepted(self) -> None:
        flow = self._make_flow()
        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=self._client(topic_prefix="/mkbqaprvrb"),
        ):
            await flow.async_step_user(user_input={"host": "10.0.0.50", "port": 9002})
        assert flow.async_create_entry.call_args.kwargs["data"]["product_key"] == "mkbqaprvrb"

    async def test_other_model_is_rejected(self) -> None:
        """Robot innego modelu (inny klucz produktu) nie dostaje wpisu."""
        flow = self._make_flow()
        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=self._client(topic_prefix="/DrzDKQ0MU8"),
        ):
            result = await flow.async_step_user(user_input={"host": "10.0.0.50", "port": 9002})
        assert result["reason"] == "unsupported_model"
        flow.async_create_entry.assert_not_called()

    async def test_connection_error_shows_form_with_error(self) -> None:
        flow = self._make_flow()
        mock_client = AsyncMock()
        mock_client.connect.side_effect = NarwalConnectionError("timeout")
        with patch("custom_components.narwal.config_flow.NarwalClient", return_value=mock_client):
            await flow.async_step_user(user_input={"host": "10.0.0.200", "port": 9002})
        assert flow.async_show_form.call_args.kwargs["errors"] == {"base": "cannot_connect"}
        mock_client.disconnect.assert_awaited_once()

    async def test_duplicate_device_aborts(self) -> None:
        flow = self._make_flow()
        flow._abort_if_unique_id_configured.side_effect = AbortFlow("already_configured")
        with (
            patch("custom_components.narwal.config_flow.NarwalClient", return_value=self._client()),
            pytest.raises(AbortFlow, match="already_configured"),
        ):
            await flow.async_step_user(user_input={"host": "10.0.0.100", "port": 9002})

    async def test_failed_auto_discovery_opens_device_id_step(self) -> None:
        flow = self._make_flow()
        mock_client = AsyncMock()
        mock_client.discover_device_id.side_effect = TimeoutError
        with patch("custom_components.narwal.config_flow.NarwalClient", return_value=mock_client):
            await flow.async_step_user(user_input={"host": "10.0.0.80", "port": 9002})
        assert flow.async_show_form.call_args.kwargs["step_id"] == "device_id"
        mock_client.disconnect.assert_awaited_once()

    async def test_device_id_step_can_retry_auto_detection(self) -> None:
        flow = self._make_flow()
        flow._pending_user_input = {"host": "10.0.0.50", "port": 9002}
        with patch("custom_components.narwal.config_flow.NarwalClient") as client_class:
            await flow.async_step_device_id(user_input={"device_id": "", "retry_auto_detect": True})
        client_class.assert_not_called()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "user"

    async def test_blank_device_id_is_rejected_without_connecting(self) -> None:
        flow = self._make_flow()
        flow._pending_user_input = {"host": "10.0.0.50", "port": 9002}
        with patch("custom_components.narwal.config_flow.NarwalClient") as client_class:
            await flow.async_step_device_id(user_input={"device_id": " ", "retry_auto_detect": False})
        client_class.assert_not_called()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "device_id"
        assert call_kwargs["errors"] == {"base": "device_id_required"}

    async def test_manual_device_id_creates_entry(self) -> None:
        flow = self._make_flow()
        flow._pending_user_input = {"host": "10.0.0.70", "port": 9002}
        device_id = "0123456789abcdef0123456789abcdef"
        mock_client = self._client(device_id=device_id)
        with patch(
            "custom_components.narwal.config_flow.NarwalClient", return_value=mock_client
        ) as client_class:
            await flow.async_step_device_id(user_input={"device_id": f" {device_id} "})
        client_class.assert_called_once_with(host="10.0.0.70", port=9002, device_id=device_id)
        mock_client.discover_device_id.assert_not_awaited()
        assert flow.async_create_entry.call_args.kwargs["data"]["device_id"] == device_id


class TestDiscovery:
    """zeroconf / DHCP discovery routing into the user step."""

    def _make_flow(self, entries: list | None = None) -> NarwalConfigFlow:
        """Flow with the discovery-relevant base-class methods stubbed."""
        flow = NarwalConfigFlow.__new__(NarwalConfigFlow)
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        flow.async_abort = MagicMock(
            side_effect=lambda reason: {"type": "abort", "reason": reason}
        )
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow._async_current_entries = MagicMock(return_value=entries or [])
        flow.context = {}
        flow.hass = MagicMock()
        return flow

    @staticmethod
    def _entry(host: str, device_id: str = "") -> MagicMock:
        entry = MagicMock()
        entry.data = {"host": host, "device_id": device_id}
        return entry

    async def test_zeroconf_routes_to_user_step_with_host_prefilled(self) -> None:
        """A discovered robot lands on the user form with its address filled in."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(
                host="10.0.0.112",
                hostname="NARWAL_8d5298.local.",
                port=9002,
            )
        )

        assert flow._discovered_host == "10.0.0.112"
        assert flow.context["title_placeholders"] == {"host": "10.0.0.112"}
        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "user"

    async def test_zeroconf_uses_hostname_without_trailing_dot_as_unique_id(self) -> None:
        """The mDNS trailing dot must not end up in the unique_id."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.112", hostname="NARWAL_8d5298.local.")
        )

        flow.async_set_unique_id.assert_awaited_once_with("NARWAL_8d5298.local")

    async def test_dhcp_routes_to_user_step(self) -> None:
        """DHCP discovery is the fallback path when multicast is filtered."""
        flow = self._make_flow()

        await flow.async_step_dhcp(
            DhcpServiceInfo(
                ip="10.0.0.112",
                hostname="NARWAL_8d5298",
                macaddress="809d658d5298",
            )
        )

        assert flow._discovered_host == "10.0.0.112"
        flow.async_set_unique_id.assert_awaited_once_with("NARWAL_8d5298")

    async def test_dhcp_without_hostname_falls_back_to_ip(self) -> None:
        """A lease with no hostname still needs some unique_id."""
        flow = self._make_flow()

        await flow.async_step_dhcp(DhcpServiceInfo(ip="10.0.0.112", hostname=""))

        flow.async_set_unique_id.assert_awaited_once_with("10.0.0.112")

    async def test_known_host_aborts_without_showing_a_card(self) -> None:
        """A robot added by hand must not reappear as a Discovered card.

        Its entry's unique_id is the device_id, which discovery can't see, so
        the host is the only thing the two paths have in common.
        """
        flow = self._make_flow(entries=[self._entry("10.0.0.112")])

        result = await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.112", hostname="NARWAL_8d5298.local.")
        )

        assert result["reason"] == "already_configured"
        flow.async_show_form.assert_not_called()
        flow.async_set_unique_id.assert_not_awaited()

    async def test_other_host_still_offered(self) -> None:
        """A second robot is not suppressed by the first one's entry."""
        flow = self._make_flow(entries=[self._entry("10.0.0.112")])

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.113", hostname="NARWAL_aabbcc.local.")
        )

        flow.async_show_form.assert_called_once()
        assert flow._discovered_host == "10.0.0.113"

    async def test_rediscovery_at_a_new_address_updates_the_entry(self) -> None:
        """Hostname is the discovery id, so a DHCP renewal updates the host."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.150", hostname="NARWAL_8d5298.local.")
        )

        flow._abort_if_unique_id_configured.assert_called_once_with(
            updates={"host": "10.0.0.150"}
        )

    async def test_manually_added_robot_matched_by_device_id_suffix(self) -> None:
        """A hand-added entry is recognised by the device_id tail in the name.

        Verified on hardware: device_id 71c53f01…147bb53c advertises as
        NARWAL_7bb53c.local. — the only link between the two paths.
        """
        entry = self._entry(
            "10.0.0.112", device_id="71c53f01c14f49088338863e147bb53c"
        )
        flow = self._make_flow(entries=[entry])

        result = await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.112", hostname="NARWAL_7bb53c.local.")
        )

        assert result["reason"] == "already_configured"
        flow.async_show_form.assert_not_called()

    async def test_known_device_at_new_address_repoints_the_entry(self) -> None:
        """Same robot, new IP: update the entry rather than orphan it."""
        entry = self._entry(
            "10.0.0.112", device_id="71c53f01c14f49088338863e147bb53c"
        )
        flow = self._make_flow(entries=[entry])

        result = await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.199", hostname="NARWAL_7bb53c.local.")
        )

        assert result["reason"] == "already_configured"
        flow.hass.config_entries.async_update_entry.assert_called_once()
        updated = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert updated["host"] == "10.0.0.199"
        assert updated["device_id"] == "71c53f01c14f49088338863e147bb53c"

    async def test_a_different_robot_is_not_matched(self) -> None:
        """A non-matching suffix at a different address is a new device."""
        entry = self._entry(
            "10.0.0.112", device_id="71c53f01c14f49088338863e147bb53c"
        )
        flow = self._make_flow(entries=[entry])

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.113", hostname="NARWAL_aabbcc.local.")
        )

        flow.async_show_form.assert_called_once()
        flow.hass.config_entries.async_update_entry.assert_not_called()

    async def test_zeroconf_prefers_ipv4_when_both_families_advertised(self) -> None:
        """#101: mDNS may list the AAAA record first; the flow must pick the A record."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(
                host="fd00:1170:789a:20:998a:c982:df15:ab22",
                hostname="NARWAL_8d5298.local.",
                ip_addresses=[
                    IPv6Address("fd00:1170:789a:20:998a:c982:df15:ab22"),
                    IPv4Address("192.168.0.180"),
                ],
            )
        )

        assert flow._discovered_host == "192.168.0.180"
        assert flow.context["title_placeholders"] == {"host": "192.168.0.180"}

    async def test_ipv6_only_rediscovery_does_not_replace_stored_ipv4(self) -> None:
        """#101: a restart-time IPv6-only discovery must not repoint a working IPv4 entry."""
        entry = self._entry(
            "192.168.0.180", device_id="71c53f01c14f49088338863e147bb53c"
        )
        flow = self._make_flow(entries=[entry])

        result = await flow.async_step_zeroconf(
            ZeroconfServiceInfo(
                host="fd00:1170:789a:20:998a:c982:df15:ab22",
                hostname="NARWAL_7bb53c.local.",
            )
        )

        assert result["reason"] == "already_configured"
        flow.hass.config_entries.async_update_entry.assert_not_called()

    async def test_ipv6_only_rediscovery_by_unique_id_does_not_update_host(self) -> None:
        """The hostname-keyed path must not push an IPv6 host into the entry either."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(
                host="fd00:1170:789a:20:998a:c982:df15:ab22",
                hostname="NARWAL_8d5298.local.",
            )
        )

        flow._abort_if_unique_id_configured.assert_called_once_with()

    async def test_ipv6_only_robot_can_still_be_added(self) -> None:
        """An IPv6-only network is unusual but must not be a dead end."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(
                host="fd00:1170:789a:20:998a:c982:df15:ab22",
                hostname="NARWAL_8d5298.local.",
            )
        )

        assert flow._discovered_host == "fd00:1170:789a:20:998a:c982:df15:ab22"
        flow.async_show_form.assert_called_once()

    def test_device_id_suffix_parsing(self) -> None:
        """Both discovery name shapes yield the suffix; other names yield None."""
        parse = NarwalConfigFlow._device_id_suffix
        assert parse("NARWAL_7bb53c.local.") == "7bb53c"
        assert parse("narwal_7BB53C") == "7bb53c"
        assert parse("_app_wss_server_7bb53c._narwal_sweeper._tcp.local.") == "7bb53c"
        assert parse("10.0.0.112") is None
        assert parse("some-other-vacuum.local.") is None

    async def test_manual_flow_is_unaffected(self) -> None:
        """With no discovery, the user step shows the plain schema."""
        flow = self._make_flow()

        await flow.async_step_user(user_input=None)

        assert flow._discovered_host is None
        flow.async_show_form.assert_called_once()
