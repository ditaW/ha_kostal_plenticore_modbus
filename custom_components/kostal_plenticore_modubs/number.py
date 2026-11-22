import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_IP_ADDRESS,
    MANUFACTURER,
    MODEL,
    NAME,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the number inputs from a config entry."""

    ip_address = entry.data[CONF_IP_ADDRESS]

    inverter_coordinator = entry.runtime_data.inverter_coordinator
    async_add_entities([
        MinimumSocNumber(inverter_coordinator, ip_address),
        MaximumSocNumber(inverter_coordinator, ip_address),
        # BatteryChargePowerNumberAC(inverter_coordinator, ip_address),
        BatteryChargePowerNumberDC(inverter_coordinator, ip_address),       # 1036, %
        BatteryChargePowerNumberDCAbs(inverter_coordinator, ip_address),    # 1034, W
        BatteryMaxChargePowerLimitNumber(inverter_coordinator, ip_address),     # 1038, W
        BatteryMaxDischargePowerLimitNumber(inverter_coordinator, ip_address),  # 1040, W
    ])


class KostalModbusNumber(CoordinatorEntity, NumberEntity):
    """number input."""

    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, ip_address, property_name, modbus_address,
                 is_scaled, name, icon, unit, min_value, step, is_setpoint=False,
                 max_value=100, mode=NumberMode.SLIDER):
        super().__init__(coordinator, context=0)

        self._ip_address = ip_address  # Initialize the IP address
        self._state = None
        self._name = name
        self._unique_id = f"{property_name}_number_{ip_address.replace('.', '_')}"
        self._device_id = f"{NAME}_{ip_address.replace('.', '_')}"
        self._property_name = property_name
        self._modbus_address = modbus_address
        self._is_scaled = is_scaled
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_mode = mode
        self._is_setpoint = is_setpoint

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def device_info(self):
        """Get information about this device."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": NAME,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

    @property
    def scale_factor(self) -> float:
        return 10 ** self.coordinator.read_float32(1025) if self._is_scaled else 1.0

    @property
    def native_value(self):
        # Setpoints: show the value the user chose (even if it is currently not
        # sent), otherwise the value read back from the inverter.
        if self._is_setpoint:
            stored = self.coordinator.keepalive_setpoints.get(self._modbus_address)
            if stored is not None:
                return stored
        return self.coordinator.read_float32(self._modbus_address) * self.scale_factor

    @property
    def extra_state_attributes(self):
        if not self._is_setpoint:
            return None
        return {
            "send_enabled": self.coordinator.is_send_enabled(self._modbus_address),
            "register": self._modbus_address,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    async def async_update(self):
        """Synchronize state"""
        _LOGGER.info("async update")
        await self.coordinator.async_request_refresh()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        if self._is_setpoint:
            self.coordinator.keepalive_setpoints[self._modbus_address] = value
            if not self.coordinator.is_send_enabled(self._modbus_address):
                _LOGGER.info(
                    "%s = %s stored, not sent (send switch for register %s is off)",
                    self._name, value, self._modbus_address,
                )
                self.async_write_ha_state()
                return
        await self.coordinator.async_set_float_value(self._modbus_address, value)
        await self.coordinator.async_request_refresh()


class MinimumSocNumber(KostalModbusNumber):
    """Minimum Battery SoC Number Input."""

    def __init__(self, coordinator, ip_address):
        super().__init__(coordinator, ip_address, "min_soc", 1042, False, "Mininum SoC", "mdi:battery-10", "%", 5, 5)


class MaximumSocNumber(KostalModbusNumber):
    """Maximum Battery SoC Number Input."""

    def __init__(self, coordinator, ip_address):
        super().__init__(coordinator, ip_address, "max_soc", 1044, False, "Maximum SoC", "mdi:battery-90", "%", 5, 5)


class BatteryChargePowerNumberAC(KostalModbusNumber):
    """Battery Charge Power Number Input AC Percent."""

    def __init__(self, coordinator, ip_address):
        super().__init__(
            coordinator,
            ip_address,
            "charge_power_ac",
            1030,
            True,
            "Battery charge power AC percent",
            "mdi:battery-charging-50",
            "%",
            -100,
            10,
            is_setpoint=True
        )


class BatteryChargePowerNumberDC(KostalModbusNumber):
    """Battery Charge Power Number Input DC Percent (register 1036)."""

    def __init__(self, coordinator, ip_address):
        super().__init__(
            coordinator,
            ip_address,
            "charge_power_dc",
            1036,
            True,
            "Battery charge power DC percent",
            "mdi:battery-charging-50",
            "%",
            -100,
            10,
            is_setpoint=True
        )


class BatteryChargePowerNumberDCAbs(KostalModbusNumber):
    """Battery charge/discharge power DC in watt (register 1034).

    Negative = charge, positive = discharge.
    """

    def __init__(self, coordinator, ip_address):
        super().__init__(
            coordinator,
            ip_address,
            "charge_power_dc_abs",
            1034,
            False,
            "Battery charge power DC",
            "mdi:battery-charging-50",
            "W",
            -10000,
            10,
            is_setpoint=True,
            max_value=10000,
            mode=NumberMode.BOX,
        )


class BatteryMaxChargePowerLimitNumber(KostalModbusNumber):
    """Battery max. charge power limit in watt (register 1038)."""

    def __init__(self, coordinator, ip_address):
        super().__init__(
            coordinator,
            ip_address,
            "max_charge_power_limit",
            1038,
            False,
            "Battery max charge power limit",
            "mdi:battery-charging-90",
            "W",
            0,
            10,
            is_setpoint=True,
            max_value=10000,
            mode=NumberMode.BOX,
        )


class BatteryMaxDischargePowerLimitNumber(KostalModbusNumber):
    """Battery max. discharge power limit in watt (register 1040)."""

    def __init__(self, coordinator, ip_address):
        super().__init__(
            coordinator,
            ip_address,
            "max_discharge_power_limit",
            1040,
            False,
            "Battery max discharge power limit",
            "mdi:battery-charging-10",
            "W",
            0,
            10,
            is_setpoint=True,
            max_value=10000,
            mode=NumberMode.BOX,
        )
