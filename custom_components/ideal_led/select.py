"""Select platform: pick a saved pixel pattern to show."""
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry

from .const import DOMAIN
from .idealled import IDEALLEDInstance, PATTERN_PREFIX

LOGGER = logging.getLogger(__name__)

NO_PATTERN_OPTION = "—"
PATTERNS_CHANGED_SIGNAL = f"{DOMAIN}_patterns_changed"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the pattern select entity."""
    instance = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([IDEALLEDPatternSelect(instance, config_entry.entry_id)])


class IDEALLEDPatternSelect(SelectEntity):
    """Dropdown of the saved named patterns; selecting one applies it."""

    def __init__(self, instance: IDEALLEDInstance, entry_id: str) -> None:
        self._instance = instance
        self._entry_id = entry_id
        self._attr_name = f"{instance.name} Pattern"
        self._attr_unique_id = f"{instance.mac}_pattern"
        self._attr_icon = "mdi:palette"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._instance.mac)},
            name=self._instance.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self._instance.mac)},
            model=self._instance._model,
            sw_version=self._instance.firmware_version,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, PATTERNS_CHANGED_SIGNAL, self.async_write_ha_state
            )
        )

    @property
    def options(self) -> list[str]:
        return [NO_PATTERN_OPTION] + sorted(self._instance._patterns)

    @property
    def current_option(self) -> str:
        effect = self._instance.pattern_effect_name()
        if effect is not None:
            return effect[len(PATTERN_PREFIX):]
        return NO_PATTERN_OPTION

    async def async_select_option(self, option: str) -> None:
        """Apply the chosen pattern like the effect dropdown would."""
        if option == NO_PATTERN_OPTION:
            return
        await self._instance.set_effect(
            PATTERN_PREFIX + option, self._instance._brightness or 255
        )
        async_dispatcher_send(self.hass, PATTERNS_CHANGED_SIGNAL)
        self.async_write_ha_state()
