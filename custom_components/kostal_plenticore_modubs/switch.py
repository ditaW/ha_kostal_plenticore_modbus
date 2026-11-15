from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, CONF_IP_ADDRESS, MANUFACTURER, MODEL, NAME


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data.inverter_coordinator
    async_add_entities([ExternalControlSwitch(coordinator, entry.data[CONF_IP_ADDRESS])])


class ExternalControlSwitch(CoordinatorEntity, SwitchEntity):
    _attr_icon = "mdi:battery-sync"
    _attr_name = "External battery control keep-alive"

    def __init__(self, coordinator, ip_address):
        super().__init__(coordinator)
        ip = ip_address.replace(".", "_")
        self._attr_unique_id = f"keepalive_switch_{ip}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{NAME}_{ip}")},
            "name": NAME, "manufacturer": MANUFACTURER, "model": MODEL,
        }

    @property
    def is_on(self) -> bool:
        return self.coordinator.keepalive_active

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_start_keepalive()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self.coordinator.async_stop_keepalive()
        self.async_write_ha_state()
