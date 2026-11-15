"""Text platform: name input for the save/delete pattern buttons."""
import logging

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry

from .const import DOMAIN
from .idealled import IDEALLEDInstance, MAX_PATTERN_NAME_LEN

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the pattern name text entity."""
    instance = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([IDEALLEDPatternName(instance, config_entry.entry_id)])


class IDEALLEDPatternName(TextEntity):
    """Name under which the save/delete pattern buttons act."""

    def __init__(self, instance: IDEALLEDInstance, entry_id: str) -> None:
        self._instance = instance
        self._entry_id = entry_id
        self._attr_name = f"{instance.name} Pattern name"
        self._attr_unique_id = f"{instance.mac}_pattern_name"
        self._attr_icon = "mdi:rename-box"
        self._attr_mode = TextMode.TEXT
        self._attr_native_max = MAX_PATTERN_NAME_LEN
        self._value = instance.pattern_name_input

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._instance.mac)},
            name=self._instance.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self._instance.mac)},
            model=self._instance._model,
            sw_version=self._instance.firmware_version,
        )

    @property
    def native_value(self) -> str:
        return self._value

    async def async_set_value(self, value: str) -> None:
        self._value = value
        self._instance.pattern_name_input = value
        self.async_write_ha_state()
