"""Button platform: save/delete named pixel patterns from the UI."""
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry

from .const import DOMAIN
from .idealled import IDEALLEDInstance
from .select import PATTERNS_CHANGED_SIGNAL

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the pattern save/delete buttons."""
    instance = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        [
            IDEALLEDSavePatternButton(instance, config_entry.entry_id),
            IDEALLEDDeletePatternButton(instance, config_entry.entry_id),
        ]
    )


class _PatternButton(ButtonEntity):
    def __init__(self, instance: IDEALLEDInstance, entry_id: str, kind: str) -> None:
        self._instance = instance
        self._entry_id = entry_id
        self._attr_unique_id = f"{instance.mac}_pattern_{kind}"
        self._attr_name = f"{instance.name} {kind.replace('_', ' ').title()}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._instance.mac)},
            name=self._instance.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self._instance.mac)},
            model=self._instance._model,
            sw_version=self._instance.firmware_version,
        )

    def _name(self) -> str:
        name = (self._instance.pattern_name_input or "").strip()
        if not name:
            raise HomeAssistantError(
                "Set a name in the 'Pattern name' text entity first"
            )
        return name


class IDEALLEDSavePatternButton(_PatternButton):
    """Save the currently painted frame under the name from the text entity."""

    def __init__(self, instance: IDEALLEDInstance, entry_id: str) -> None:
        super().__init__(instance, entry_id, "save_pattern")
        self._attr_icon = "mdi:content-save"

    async def async_press(self) -> None:
        name = self._name()
        pixels = self._instance.pixels
        if not any(any(p[:3]) for p in pixels):
            raise HomeAssistantError(
                "Nothing painted yet: use set_pixels / set_pixel_range first"
            )
        rle = True
        if self._instance._pixel_frame_state is not None:
            rle = self._instance._pixel_frame_state["rle"]
        self._instance.save_pattern(name, rle, pixels)
        async_dispatcher_send(self.hass, PATTERNS_CHANGED_SIGNAL)
        self.async_write_ha_state()


class IDEALLEDDeletePatternButton(_PatternButton):
    """Delete the pattern named in the text entity."""

    def __init__(self, instance: IDEALLEDInstance, entry_id: str) -> None:
        super().__init__(instance, entry_id, "delete_pattern")
        self._attr_icon = "mdi:delete"

    async def async_press(self) -> None:
        name = self._name()
        try:
            self._instance.delete_pattern(name)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        async_dispatcher_send(self.hass, PATTERNS_CHANGED_SIGNAL)
        self.async_write_ha_state()
