from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_IP_ADDRESS, MANUFACTURER, MODEL, NAME


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data.inverter_coordinator
    ip_address = entry.data[CONF_IP_ADDRESS]

    async_add_entities([
        ExternalControlSwitch(coordinator, ip_address),
        RegisterSendSwitch(
            coordinator, ip_address, 1034, "dc_power_abs",
            "Send DC power setpoint W (1034)", "mdi:flash",
        ),
        RegisterSendSwitch(
            coordinator, ip_address, 1036, "dc_power_rel",
            "Send DC power setpoint % (1036)", "mdi:percent",
        ),
        RegisterSendSwitch(
            coordinator, ip_address, 1038, "max_charge_limit",
            "Send max charge power limit (1038)", "mdi:battery-arrow-up",
        ),
        RegisterSendSwitch(
            coordinator, ip_address, 1040, "max_discharge_limit",
            "Send max discharge power limit (1040)", "mdi:battery-arrow-down",
        ),
    ])


def _device_info(ip: str) -> dict:
    return {
        "identifiers": {(DOMAIN, f"{NAME}_{ip}")},
        "name": NAME,
        "manufacturer": MANUFACTURER,
        "model": MODEL,
    }


class ExternalControlSwitch(CoordinatorEntity, SwitchEntity):
    """Master switch: re-send the selected setpoints cyclically."""

    _attr_icon = "mdi:battery-sync"
    _attr_name = "External battery control keep-alive"

    def __init__(self, coordinator, ip_address):
        super().__init__(coordinator)
        ip = ip_address.replace(".", "_")
        self._attr_unique_id = f"keepalive_switch_{ip}"
        self._attr_device_info = _device_info(ip)

    @property
    def is_on(self) -> bool:
        return self.coordinator.keepalive_active

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_start_keepalive()
        self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs):
        self.coordinator.async_stop_keepalive()
        # stop clears the stored setpoints, so refresh the number entities too
        self.coordinator.async_update_listeners()


class RegisterSendSwitch(CoordinatorEntity, SwitchEntity, RestoreEntity):
    """Selects whether a register is part of the values sent to the inverter.

    1034 and 1036 exclude each other (handled in the coordinator).
    The state is restored after a restart.
    """

    def __init__(self, coordinator, ip_address, address, key, name, icon):
        super().__init__(coordinator)
        ip = ip_address.replace(".", "_")
        self._address = address
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"send_{key}_switch_{ip}"
        self._attr_device_info = _device_info(ip)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in (STATE_ON, STATE_OFF):
            self.coordinator.restore_send_enabled(self._address, last.state == STATE_ON)
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_send_enabled(self._address)

    @property
    def extra_state_attributes(self):
        return {"register": self._address}

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_send_enabled(self._address, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_send_enabled(self._address, False)
