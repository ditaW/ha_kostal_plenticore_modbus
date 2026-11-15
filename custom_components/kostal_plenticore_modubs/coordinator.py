"""Coordinator for the Kostal Plenticore Modbus integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, NAME, MANUFACTURER, MODEL

_LOGGER = logging.getLogger(__name__)

MODBUS_PORT = 1502
MODBUS_UNIT_ID = 71

# Highest register index we store (40354 + 2 registers)
REGISTER_COUNT = 40356

# Index 18 = "Unknown" in the inverter state enum of the sensor platform
INVERTER_STATE_UNKNOWN = 18

POLL_INTERVAL = timedelta(seconds=15)
KEEPALIVE_INTERVAL = timedelta(seconds=30)

# Slow blocks are read only every SLOW_EVERY-th poll (4 x 15 s = ~60 s).
SLOW_EVERY = 5

# (start address, register count)
# Fast blocks: read on every poll.
FAST_BLOCKS: list[tuple[int, int]] = [
    (98, 124),  # general values
    (220, 68),  # powermeter (220..257) + DC1..DC3 (258..287)
    (512, 18),  # battery
]

# Slow blocks: yield, scale factor, battery limits/setpoints, energy counters.
# The gaps between 1025/1030 and 40346/40354 are deliberately not merged;
# merge them only after testing that the gap registers are readable.
SLOW_BLOCKS: list[tuple[int, int]] = [
    (320, 8),  # yield
    (1025, 1),  # power scale factor
    (1030, 53),  # battery setpoints, min/max SoC, limits, energy counters
    (40346, 2),  # total real energy exported
    (40354, 2),  # total real energy imported
]


class InverterCoordinator(DataUpdateCoordinator):
    """Inverter coordinator.

    Polls the inverter via Modbus TCP and optionally re-sends external
    battery setpoints cyclically (keep-alive) so that the inverter does not
    fall back to its internal battery control after the configured timeout.
    """

    def __init__(self, hass, entry, ip_address):
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )

        self._hass = hass
        self._entry = entry
        self._ip_address = ip_address

        # Only one Modbus connection at a time (poll, manual write, keep-alive)
        self._lock = asyncio.Lock()

        # Persistent register image; slow blocks are only refreshed occasionally
        self._registers: list[int] = [0] * REGISTER_COUNT
        self._inverter_state: int = INVERTER_STATE_UNKNOWN
        self._poll_count = 0
        self._force_slow = False

        # Send every x seconds the setpoints again to prevent the inverter from
        # falling back to its internal battery control.
        self._keepalive_unsub = None
        self.keepalive_interval = KEEPALIVE_INTERVAL
        self.keepalive_setpoints: dict[int, float] = {}  # register -> value

        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN].setdefault(
            entry.entry_id,
            {
                "name": entry.title,
                "ip": ip_address,
                "model": MODEL,
                "status": "OFFLINE",
            },
        )

    @property
    def device_info(self):
        """Return information to link this entity with the correct device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": NAME,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

    # ------------------------------------------------------------------
    # Keep-alive
    # ------------------------------------------------------------------

    @property
    def keepalive_active(self) -> bool:
        return self._keepalive_unsub is not None

    async def async_start_keepalive(self) -> None:
        if self._keepalive_unsub is not None:
            return
        await self._async_send_setpoints()
        self._keepalive_unsub = async_track_time_interval(
            self.hass, self._async_keepalive_tick, self.keepalive_interval
        )

    @callback
    def async_stop_keepalive(self) -> None:
        if self._keepalive_unsub is not None:
            self.keepalive_setpoints.clear()
            self._keepalive_unsub()
            self._keepalive_unsub = None

    async def _async_keepalive_tick(self, _now) -> None:
        await self._async_send_setpoints()

    async def _async_send_setpoints(self) -> None:
        # copy: the dict may be changed by the UI while we are awaiting
        for address, value in list(self.keepalive_setpoints.items()):
            await self.async_set_float_value(address, value, source="keepalive")

    async def async_shutdown(self) -> None:
        self.async_stop_keepalive()
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self):
        """Fetch data from the inverter.

        Fast blocks are read on every poll, slow blocks only every
        SLOW_EVERY-th poll or right after a manual write.
        """
        slow = self._force_slow or self._poll_count % SLOW_EVERY == 0
        self._poll_count += 1
        self._force_slow = False
        blocks = FAST_BLOCKS + SLOW_BLOCKS if slow else FAST_BLOCKS

        ok_blocks = 0

        async with self._lock:
            client = AsyncModbusTcpClient(self._ip_address, port=MODBUS_PORT)
            started = time.monotonic()

            try:
                if not await client.connect():
                    raise UpdateFailed("Connection to inverter failed")
                _LOGGER.debug("connect took %.2fs", time.monotonic() - started)

                for address, count in blocks:
                    t_block = time.monotonic()
                    result = await client.read_holding_registers(
                        address, count=count, device_id=MODBUS_UNIT_ID
                    )
                    if result.isError() or len(result.registers) != count:
                        _LOGGER.warning(
                            "Error reading registers: addr=%s count=%s", address, count
                        )
                        continue

                    self._registers[address: address + count] = result.registers
                    ok_blocks += 1
                    _LOGGER.debug(
                        "read %s+%s took %.2fs",
                        address,
                        count,
                        time.monotonic() - t_block,
                    )

                # Inverter state
                result = await client.read_holding_registers(
                    56, count=2, device_id=MODBUS_UNIT_ID
                )
                if result.isError() or len(result.registers) != 2:
                    _LOGGER.warning("Error reading inverter state")
                else:
                    state = client.convert_from_registers(
                        registers=list(reversed(result.registers)),
                        data_type=client.DATATYPE.UINT32,
                    )
                    self._inverter_state = min(int(state), INVERTER_STATE_UNKNOWN)
                    ok_blocks += 1

            except ModbusException as err:
                raise UpdateFailed(f"Modbus error: {err}") from err

            finally:
                client.close()

            _LOGGER.debug(
                "poll finished (%s blocks%s) in %.2fs",
                len(blocks) + 1,
                ", incl. slow" if slow else "",
                time.monotonic() - started,
            )

        if ok_blocks == 0:
            raise UpdateFailed("No register block could be read")

        return {
            "inverter_state": self._inverter_state,
            "registers": self._registers,
        }

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def async_set_float_value(
            self, address: int, value: float, source: str = "manual"
    ) -> bool:
        """Write a Float32 value to the inverter."""

        async with self._lock:
            client = AsyncModbusTcpClient(self._ip_address, port=MODBUS_PORT)

            try:
                if not await client.connect():
                    _LOGGER.error("Connection failed (write register %s)", address)
                    return False

                registers = client.convert_to_registers(
                    value=value, data_type=client.DATATYPE.FLOAT32
                )
                result = await client.write_registers(
                    address,
                    values=list(reversed(registers)),
                    device_id=MODBUS_UNIT_ID,
                )
                if result.isError():
                    _LOGGER.error(
                        "Error writing register %s = %s (%s)", address, value, source
                    )
                    return False

                level = logging.DEBUG if source == "keepalive" else logging.INFO
                _LOGGER.log(
                    level, "Kostal write (%s): register %s = %s", source, address, value
                )

                # Make the next poll read the slow blocks (setpoints, SoC limits),
                # so the UI shows the new value. Not for keep-alive writes, they
                # would turn almost every poll into a slow one.
                if source == "manual":
                    self._force_slow = True

                return True

            except ModbusException as err:
                _LOGGER.error("Modbus error: %s", err, exc_info=True)
                return False

            finally:
                client.close()

    # ------------------------------------------------------------------
    # Register accessors
    # ------------------------------------------------------------------

    def read_float32(self, address: int) -> float:
        """Read a Float32 value from the register image."""
        return AsyncModbusTcpClient.convert_from_registers(
            registers=list(reversed(self.data["registers"][address: address + 2])),
            data_type=AsyncModbusTcpClient.DATATYPE.FLOAT32,
        )

    def read_int16(self, address: int) -> int:
        """Read an Int16 value from the register image."""
        return AsyncModbusTcpClient.convert_from_registers(
            registers=list(self.data["registers"][address: address + 1]),
            data_type=AsyncModbusTcpClient.DATATYPE.INT16,
        )

    def read_uint16(self, address: int) -> int:
        """Read a UInt16 value from the register image."""
        return AsyncModbusTcpClient.convert_from_registers(
            registers=list(self.data["registers"][address: address + 1]),
            data_type=AsyncModbusTcpClient.DATATYPE.UINT16,
        )

    def read_int32(self, address: int) -> int:
        """Read an Int32 value from the register image."""
        return AsyncModbusTcpClient.convert_from_registers(
            registers=list(reversed(self.data["registers"][address: address + 2])),
            data_type=AsyncModbusTcpClient.DATATYPE.INT32,
        )

    def read_uint32(self, address: int) -> int:
        """Read a UInt32 value from the register image."""
        return AsyncModbusTcpClient.convert_from_registers(
            registers=list(reversed(self.data["registers"][address: address + 2])),
            data_type=AsyncModbusTcpClient.DATATYPE.UINT32,
        )
