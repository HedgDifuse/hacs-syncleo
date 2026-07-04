"""Climate platform for Syncleo / RusClimate air conditioners."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType

from .coordinator import PolarisDataUpdateCoordinator
from .const import (
    DOMAIN,
    CONDITIONER_MODE_OFF,
    CONDITIONER_MODE_AUTO,
    CONDITIONER_MODE_COOL,
    CONDITIONER_MODE_DRY,
    CONDITIONER_MODE_HEAT,
    CONDITIONER_MODE_FAN,
    CONDITIONER_FAN_MODES,
    CONDITIONER_MIN_TEMP,
    CONDITIONER_MAX_TEMP,
    CONDITIONER_TEMP_STEP,
)

_LOGGER = logging.getLogger(__name__)

# Map the conditioner Mode value (carried by the type-1 command) to a HA HVACMode.
MODE_TO_HVAC = {
    CONDITIONER_MODE_OFF: HVACMode.OFF,
    CONDITIONER_MODE_AUTO: HVACMode.AUTO,
    CONDITIONER_MODE_COOL: HVACMode.COOL,
    CONDITIONER_MODE_DRY: HVACMode.DRY,
    CONDITIONER_MODE_HEAT: HVACMode.HEAT,
    CONDITIONER_MODE_FAN: HVACMode.FAN_ONLY,
}
HVAC_TO_MODE = {v: k for k, v in MODE_TO_HVAC.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigType,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the conditioner climate entity from a config entry."""
    coordinator: PolarisDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    if coordinator.device_info is None:
        _LOGGER.error("Device info not available, cannot create climate entity")
        return

    # Only air conditioners get a climate entity.
    if coordinator.is_conditioner:
        async_add_entities([SyncleoConditionerClimate(coordinator, config_entry.entry_id)])


class SyncleoConditionerClimate(ClimateEntity):
    """Representation of a RusClimate / Hommyn air conditioner."""

    _attr_has_entity_name = True
    _attr_name = None
    # Enables the translations/icons for the custom "silent"/"turbo" fan modes.
    _attr_translation_key = "conditioner"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator: PolarisDataUpdateCoordinator, entry_id: str) -> None:
        """Initialize the air conditioner."""
        self.coordinator = coordinator
        self._entry_id = entry_id
        self._attr_unique_id = f"{coordinator._mac}_climate"
        self._attr_device_info = coordinator.device_info

        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.AUTO,
            HVACMode.COOL,
            HVACMode.HEAT,
            HVACMode.DRY,
            HVACMode.FAN_ONLY,
        ]
        # The vendor app presents one fan selector mixing real speeds with the
        # silent and turbo features (which are separate protocol commands). We
        # mirror that here; "silent"/"turbo" won't have a built-in frontend icon.
        self._attr_fan_modes = ["silent"] + list(CONDITIONER_FAN_MODES.keys()) + ["turbo"]
        self._attr_swing_modes = ["off", "vertical", "horizontal", "both"]
        self._attr_min_temp = CONDITIONER_MIN_TEMP
        self._attr_max_temp = CONDITIONER_MAX_TEMP
        self._attr_target_temperature_step = CONDITIONER_TEMP_STEP

        # Remember the last non-OFF mode so turning the unit back on restores it.
        self._last_active_mode = CONDITIONER_MODE_AUTO

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.data.get("connected", False)

    @property
    def current_temperature(self) -> float | None:
        """Return the current room temperature."""
        return self.coordinator.data.get("current_temperature")

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self.coordinator.data.get("target_temperature")

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation mode."""
        mode = self.coordinator.data.get("conditioner_mode", CONDITIONER_MODE_OFF)
        if mode != CONDITIONER_MODE_OFF:
            self._last_active_mode = mode
        return MODE_TO_HVAC.get(mode, HVACMode.OFF)

    @property
    def fan_mode(self) -> str | None:
        """Return current fan selection (turbo / silent take priority over speed)."""
        if self.coordinator.data.get("turbo"):
            return "turbo"
        if self.coordinator.data.get("silent"):
            return "silent"
        speed = self.coordinator.data.get("fan_speed")
        for name, value in CONDITIONER_FAN_MODES.items():
            if value == speed:
                return name
        return None

    @property
    def swing_mode(self) -> str | None:
        """Return current swing mode."""
        return self.coordinator.data.get("swing_mode", "off")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_set_temperature(int(temperature))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new operation mode."""
        mode = HVAC_TO_MODE.get(hvac_mode, CONDITIONER_MODE_OFF)
        if mode != CONDITIONER_MODE_OFF:
            self._last_active_mode = mode
        await self.coordinator.async_set_conditioner_mode(mode)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new fan selection.

        'turbo' and 'silent' are separate device features, so they are mutually
        exclusive with each other and with a normal speed.
        """
        if fan_mode == "turbo":
            if self.coordinator.data.get("silent"):
                await self.coordinator.async_set_silent(False)
            await self.coordinator.async_set_turbo(True)
        elif fan_mode == "silent":
            if self.coordinator.data.get("turbo"):
                await self.coordinator.async_set_turbo(False)
            await self.coordinator.async_set_silent(True)
        elif fan_mode in CONDITIONER_FAN_MODES:
            if self.coordinator.data.get("turbo"):
                await self.coordinator.async_set_turbo(False)
            if self.coordinator.data.get("silent"):
                await self.coordinator.async_set_silent(False)
            await self.coordinator.async_set_fan_speed(CONDITIONER_FAN_MODES[fan_mode])

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set new swing mode."""
        vertical = swing_mode in ("vertical", "both")
        horizontal = swing_mode in ("horizontal", "both")
        await self.coordinator.async_set_swing(vertical, horizontal)

    async def async_turn_on(self) -> None:
        """Turn the air conditioner on, restoring the last active mode."""
        await self.coordinator.async_set_conditioner_mode(self._last_active_mode)

    async def async_turn_off(self) -> None:
        """Turn the air conditioner off."""
        await self.coordinator.async_set_conditioner_mode(CONDITIONER_MODE_OFF)

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def should_poll(self) -> bool:
        """No need to poll, coordinator notifies of updates."""
        return False
