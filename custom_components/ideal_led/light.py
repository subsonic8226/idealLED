import asyncio
import logging
import voluptuous as vol
from typing import Any, Optional, Tuple

from .idealled import (
    IDEALLEDInstance,
    PIXEL_MODES,
    PATTERN_PREFIX,
    DEFAULT_PIXEL_MODE,
    DEFAULT_PIXEL_SPEED,
)
from .const import (
    DOMAIN,
    CONF_RESEND_INTERVAL,
    DEFAULT_RESEND_INTERVAL,
    RETRY_MIN_INTERVAL,
    COMMAND_TIMEOUT,
    RESET_AFTER_FAILURES,
    CONF_DEFAULT_TRANSITION,
    DEFAULT_TRANSITION,
    CONF_PIXEL_COUNT,
)
from .select import PATTERNS_CHANGED_SIGNAL

from homeassistant.const import CONF_MAC, STATE_ON, STATE_OFF
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.light import (
    PLATFORM_SCHEMA,
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ATTR_EFFECT,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.util.color import match_max_scale
from homeassistant.helpers import device_registry
from homeassistant.helpers import entity_platform
from homeassistant.helpers.dispatcher import async_dispatcher_connect

LOGGER = logging.getLogger(__name__)
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({vol.Required(CONF_MAC): cv.string})

# --- Pixel level service -----------------------------------------------------

SERVICE_SET_PIXELS = "set_pixels"
SET_PIXELS_SCHEMA = {
    vol.Required("pixels"): cv.ensure_list,
    vol.Optional("mode", default="solid"): vol.In(["solid", "fade", "flash"]),
    vol.Optional("speed", default=50): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=100)
    ),
    vol.Optional("pixel_count"): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=255)
    ),
    vol.Optional("rle", default=True): cv.boolean,
}

SERVICE_SAVE_PATTERN = "save_pattern"
SAVE_PATTERN_SCHEMA = {
    vol.Required("name"): cv.string,
    vol.Optional("pixels"): cv.ensure_list,
    vol.Optional("mode", default="solid"): vol.In(["solid", "fade", "flash"]),
    vol.Optional("speed", default=50): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=100)
    ),
    vol.Optional("pixel_count"): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=255)
    ),
    vol.Optional("rle", default=True): cv.boolean,
}

SERVICE_DELETE_PATTERN = "delete_pattern"
DELETE_PATTERN_SCHEMA = {
    vol.Required("name"): cv.string,
}

# Flat, UI-friendly variant of set_pixels for a single range: every field is
# a simple selector, so Developer Tools and the automation editor render a
# proper form.
SERVICE_SET_PIXEL_RANGE = "set_pixel_range"
SET_PIXEL_RANGE_SCHEMA = {
    vol.Required("from"): vol.All(vol.Coerce(int), vol.Range(min=0, max=254)),
    vol.Optional("to"): vol.All(vol.Coerce(int), vol.Range(min=0, max=254)),
    vol.Optional("color", default=[255, 255, 255]): vol.All(
        cv.ensure_list,
        [vol.All(vol.Coerce(int), vol.Range(min=0, max=255))],
        vol.Length(min=3, max=3),
    ),
    vol.Optional("mode", default="solid"): vol.In(["solid", "fade", "flash"]),
    vol.Optional("speed", default=50): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=100)
    ),
    vol.Optional("pixel_count"): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=255)
    ),
    vol.Optional("rle", default=True): cv.boolean,
}

async def async_setup_entry(hass, config_entry, async_add_devices):
    instance = hass.data[DOMAIN][config_entry.entry_id]
    # No initial connect here: with a weak BLE link it can stall HA startup for
    # minutes, and it learns nothing (the device reports no state). The entity
    # re-asserts its restored state in the background once added.
    resend_interval = config_entry.options.get(
        CONF_RESEND_INTERVAL, DEFAULT_RESEND_INTERVAL
    )
    default_transition = config_entry.options.get(
        CONF_DEFAULT_TRANSITION, DEFAULT_TRANSITION
    )
    pixel_count_option = config_entry.options.get(CONF_PIXEL_COUNT)
    async_add_devices(
        [
            IDEALLEDLight(
                instance,
                config_entry.data["name"],
                config_entry.entry_id,
                resend_interval,
                default_transition,
                pixel_count_option,
            )
        ]
    )

    # Register the pixel level services on this platform's entities.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_PIXELS,
        SET_PIXELS_SCHEMA,
        "async_set_pixels",
    )
    platform.async_register_entity_service(
        SERVICE_SAVE_PATTERN,
        SAVE_PATTERN_SCHEMA,
        "async_save_pattern",
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_PATTERN,
        DELETE_PATTERN_SCHEMA,
        "async_delete_pattern",
    )
    platform.async_register_entity_service(
        SERVICE_SET_PIXEL_RANGE,
        SET_PIXEL_RANGE_SCHEMA,
        "async_set_pixel_range",
    )


class IDEALLEDLight(LightEntity, RestoreEntity):
    def __init__(
        self,
        idealledinstance: IDEALLEDInstance,
        name: str,
        entry_id: str,
        resend_interval: int = DEFAULT_RESEND_INTERVAL,
        default_transition: float = DEFAULT_TRANSITION,
        pixel_count_option: int | None = None,
    ) -> None:
        self._instance = idealledinstance
        self._entry_id = entry_id
        self._pixel_count_option = (
            int(pixel_count_option) if pixel_count_option else None
        )
        self._attr_supported_color_modes = {ColorMode.HS}
        self._attr_supported_features = LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION
        #self._attr_brightness_step_pct = 10
        self._attr_name = name
        self._attr_unique_id = self._instance.mac
        self._instance.local_callback = self.light_local_callback
        # The device gives no state feedback, so we remember what HA last asked
        # for and keep re-sending it. None = no command seen yet, don't enforce.
        self._desired_on: bool | None = None
        self._in_sync = False
        self._last_confirmed = None
        self._consecutive_failures = 0
        self._resend_interval = resend_interval
        self._default_transition = default_transition
        self._command_task: asyncio.Task | None = None
        self._enforce_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._pending_transition: float = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, PATTERNS_CHANGED_SIGNAL, self.async_write_ha_state
            )
        )
        last = await self.async_get_last_state()
        if last is not None and last.state in (STATE_ON, STATE_OFF):
            self._desired_on = last.state == STATE_ON
            if (bri := last.attributes.get(ATTR_BRIGHTNESS)) is not None:
                self._instance._brightness = bri
            if (hs := last.attributes.get(ATTR_HS_COLOR)) is not None:
                self._instance._hs_color = tuple(hs)
            if (effect := last.attributes.get(ATTR_EFFECT)) is not None:
                # Restore the effect too: without it the first enforcement
                # after a restart would overwrite the running effect with
                # a solid colour (the instance thinks _effect is None).
                self._instance._effect = effect
            LOGGER.debug(
                "%s: restored desired state %s", self.name, last.state
            )
            # Restore the pattern registry and, if a painted pattern was
            # showing, the active frame, so the enforcer re-asserts it
            # instead of a solid colour after a restart.
            if painted := last.attributes.get("pixels"):
                try:
                    # The attribute is comma separated; parse it as one
                    # continuous hex string.
                    painted = painted.replace(",", "")
                    if len(painted) % 6:
                        raise ValueError("pixels attribute must be rrggbb per LED")
                    rgbs = [
                        tuple(int(painted[i + j : i + j + 2], 16) for j in (0, 2, 4))
                        for i in range(0, len(painted), 6)
                    ]
                    params_attr = last.attributes.get("pixel_params")
                    if params_attr:
                        # Per-LED mode/speed (mmss hex per LED).
                        params_attr = params_attr.replace(",", "")
                        if len(params_attr) != len(rgbs) * 4:
                            raise ValueError("pixel_params length mismatch")
                        pixels = []
                        for idx, (r, g, b) in enumerate(rgbs):
                            m = int(params_attr[idx * 4 : idx * 4 + 2], 16)
                            s = int(params_attr[idx * 4 + 2 : idx * 4 + 4], 16)
                            if m not in PIXEL_MODES.values():
                                raise ValueError(f"Unknown pixel mode id: {m}")
                            pixels.append((r, g, b, m, max(1, min(100, s))))
                    else:
                        # Legacy state: one mode/speed for the whole frame.
                        # `is not None` matters: mode 0 (flash) is falsy.
                        mode = last.attributes.get("pixel_mode")
                        speed = last.attributes.get("pixel_speed")
                        m = (
                            int(mode)
                            if mode is not None
                            else DEFAULT_PIXEL_MODE
                        )
                        s = (
                            int(speed)
                            if speed is not None
                            else DEFAULT_PIXEL_SPEED
                        )
                        pixels = [(r, g, b, m, max(1, min(100, s))) for r, g, b in rgbs]
                    if pixels:
                        self._instance._pixels = pixels
                        self._instance._pixel_count = len(pixels)
                        active = last.attributes.get("pixel_active")
                        if active is None:
                            # Pre-0.0.11 states only exposed `pixels` while
                            # the pattern was showing.
                            active = True
                        if active:
                            self._instance._pixel_frame_state = {
                                "rle": bool(last.attributes.get("pixel_rle", True))
                            }
                except ValueError:
                    LOGGER.debug("%s: could not restore painted frame", self.name)
            if (registry := last.attributes.get("patterns")):
                # Named pattern registry: restored so pattern effects keep
                # resolving after a restart.
                try:
                    self._instance.load_pattern_registry(registry)
                except Exception:  # noqa: BLE001 - malformed attr, keep going
                    LOGGER.debug(
                        "%s: could not restore pattern registry", self.name
                    )
            restored_effect = self._instance._effect
            if (
                isinstance(restored_effect, str)
                and restored_effect.startswith(PATTERN_PREFIX)
                and self._instance._pixel_frame_state is None
                and not self._instance._patterns
            ):
                # Effect promises a pattern but no data survived to back it.
                self._instance._effect = None
        # The pixel_count option (per device) wins over the restored frame
        # length; tell the controller once via LAMPN in the background.
        if (
            self._pixel_count_option is not None
            and self._pixel_count_option != self._instance._pixel_count
        ):
            self._run_command(
                lambda: self._instance.set_pixel_count(self._pixel_count_option)
            )
        if self._resend_interval:
            self._enforce_task = self.hass.async_create_background_task(
                self._enforce_loop(), f"ideal_led enforce {self._instance.mac}"
            )
            # Assert the restored state straight away rather than waiting.
            self._wake.set()

    async def async_will_remove_from_hass(self) -> None:
        for task in (self._enforce_task, self._command_task):
            if task and not task.done():
                task.cancel()
        await super().async_will_remove_from_hass()

    def _transition(self, kwargs: dict) -> float:
        transition = kwargs.get(ATTR_TRANSITION)
        return self._default_transition if transition is None else transition

    def _run_command(self, make_coro, transition: float = 0) -> asyncio.Task:
        """Run a device command in the background, superseding any in-flight one.

        Service calls return immediately so a slow/unreachable strip can't block
        the calling automation (which would drop later triggers in mode: single).
        """
        if self._command_task and not self._command_task.done():
            self._command_task.cancel()
        self._pending_transition = transition
        # Background task so HA cancels it at shutdown instead of waiting on BLE.
        self._command_task = self.hass.async_create_background_task(
            self._guarded(make_coro), f"ideal_led command {self._instance.mac}"
        )
        return self._command_task

    async def _guarded(self, make_coro) -> None:
        try:
            # A fade is part of the command, so it gets its own time on top.
            async with asyncio.timeout(COMMAND_TIMEOUT + self._pending_transition):
                await make_coro()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - BLE stack raises all sorts
            self._in_sync = False
            self._consecutive_failures += 1
            LOGGER.warning(
                "%s: failed to send %s (attempt %s), will retry: %s: %s",
                self.name,
                "on" if self._desired_on else "off",
                self._consecutive_failures,
                type(err).__name__,
                err,
            )
            if self._consecutive_failures % RESET_AFTER_FAILURES == 0:
                # Repeated failures can mean a wedged connection rather than range:
                # start the next attempt from a fresh client.
                try:
                    await self._instance.reset_connection()
                except Exception as reset_err:  # noqa: BLE001
                    LOGGER.debug("%s: reset failed: %r", self.name, reset_err)
        else:
            self._in_sync = True
            self._consecutive_failures = 0
            self._last_confirmed = dt_util.utcnow()
        # Restart the enforcer's clock: full interval if in sync, backoff if not.
        self._wake.set()
        self.async_write_ha_state()

    def _next_delay(self) -> float:
        if self._in_sync:
            return self._resend_interval
        backoff = RETRY_MIN_INTERVAL * 2 ** max(0, self._consecutive_failures - 1)
        return min(backoff, self._resend_interval)

    async def _enforce_loop(self) -> None:
        while True:
            try:
                async with asyncio.timeout(self._next_delay()):
                    await self._wake.wait()
            except TimeoutError:
                pass
            woken = self._wake.is_set()
            self._wake.clear()
            if self._desired_on is None:
                continue
            if self._command_task is not None:
                # Woken because a command just finished: restart the clock.
                # Or a command is still in flight: leave it be.
                if woken or not self._command_task.done():
                    continue
            # else: timer expired, or first wake after restore (no command yet).
            LOGGER.debug(
                "%s: re-sending desired state %s",
                self.name,
                "on" if self._desired_on else "off",
            )
            task = self._run_command(lambda on=self._desired_on: self._send_power(on))
            await asyncio.wait([task])

    async def _send_power(self, on: bool) -> None:
        if on:
            await self._instance.turn_on()
            # Re-assert the desired output only when it may be wrong (startup,
            # or the last send failed part-way). Re-writing it on every in-sync
            # resend makes the strip visibly blink or restart its effect.
            if self._instance._pixel_frame_state is not None:
                # The desired state is a painted pattern, not a solid
                # colour. Keep the pattern name as the state effect if a
                # named pattern is showing. Like effects, re-assert only
                # when out of sync: re-pushing on every in-sync resend
                # restarts flash/fade animations.
                if not self._in_sync:
                    await self._instance.push_frame(
                        **self._instance._pixel_frame_state,
                        effect_name=self._instance.pattern_effect_name(),
                    )
            elif self._instance._effect is not None:
                # Effects animate on the controller itself, so an in-sync
                # resend only powers on and leaves the animation running.
                # Out of sync (restart, failed send) re-triggers the effect.
                if not self._in_sync:
                    await self._instance.set_effect(
                        self._instance._effect, self._instance._brightness or 255
                    )
            elif (
                not self._in_sync or self._instance._output_rgb is None
            ):
                await self._instance.set_rgb_color(self._instance._hs_color)
        else:
            await self._instance.turn_off()

    async def _apply_turn_on(self, kwargs: dict, transition: float) -> None:
        bri = kwargs.get(ATTR_BRIGHTNESS, self._instance._brightness)
        if ATTR_EFFECT in kwargs:
            requested = kwargs[ATTR_EFFECT]
            if requested.startswith(PATTERN_PREFIX):
                # Named pattern: resolves via the pattern registry, so a
                # scene or automation restores it under a short, meaningful
                # name. Unknown names fail the command with a warning.
                if (
                    requested == self.effect
                    and self._instance._pixel_frame_state is not None
                ):
                    # Same pattern already showing (scene re-apply): only
                    # power on and adjust brightness. Brightness is baked
                    # into the pixel data, so a change repaints the frame,
                    # which restarts fade/flash animations; solid patterns
                    # don't visibly change.
                    await self._instance.turn_on()
                    if ATTR_BRIGHTNESS in kwargs:
                        await self._instance.set_brightness(kwargs[ATTR_BRIGHTNESS])
                    return
                await self._instance.turn_on()
                await self._instance.set_effect(requested, bri)
                return
            elif requested != self.effect:
                await self._instance.turn_on()
                await self._instance.set_effect(requested, bri)
                return
            else:
                # Same effect already showing (scene re-apply): re-sending
                # would restart its animation. Power on, adjust brightness,
                # and leave the animation alone instead of clobbering it
                # with a solid colour.
                await self._instance.turn_on()
                if ATTR_BRIGHTNESS in kwargs:
                    await self._instance.set_brightness(kwargs[ATTR_BRIGHTNESS])
                return

        # Always send the power command: the device may have been switched off
        # behind our back, so our cached is_on can't be trusted to skip it.
        fade_in = bool(transition) and not self._instance.is_on
        await self._instance.turn_on(from_black=fade_in)

        if self._instance._effect is not None and ATTR_HS_COLOR not in kwargs:
            if ATTR_BRIGHTNESS in kwargs:
                # Effects can't fade; re-send the effect at the new brightness.
                await self._instance.set_brightness(kwargs[ATTR_BRIGHTNESS])
            return

        # Always send the colour: after a fade-out the strip holds black.
        hs = kwargs.get(ATTR_HS_COLOR, self._instance._hs_color)
        LOGGER.debug(f"Setting color: {hs}. Brightness: {bri}. Transition: {transition}")
        await self._instance.set_rgb_color(hs, bri, transition)

    @property
    def extra_state_attributes(self):
        frame = self._instance._pixel_frame_state
        # pixel_active distinguishes "pattern showing" from "no pattern";
        # the enforcer only re-asserts the active frame. `pixels` (rrggbb per
        # LED) and `pixel_params` (mmss per LED) are only exposed while a
        # pattern is active: they are what restore and scene debugging need,
        # and inactive anonymous paints are not addressable.
        return {
            "in_sync": self._in_sync,
            "last_confirmed": self._last_confirmed,
            "consecutive_failures": self._consecutive_failures,
            "pixel_count": self._instance.pixel_count,
            "pixel_active": frame is not None,
            "pixel_rle": frame["rle"] if frame else None,
            "pixels": (
                ",".join(
                    f"{r:02x}{g:02x}{b:02x}"
                    for r, g, b, _m, _s in self._instance.pixels
                )
                if frame
                else None
            ),
            "pixel_params": (
                ",".join(
                    f"{m:02x}{s:02x}" for _r, _g, _b, m, s in self._instance.pixels
                )
                if frame
                else None
            ),
            "patterns": (
                self._instance.pattern_registry_json()
                if self._instance._patterns
                else None
            ),
        }

    @property
    def available(self):
        # Always report as available so users can attempt to control the light.
        # The actual connection happens on command, which will succeed once
        # Bluetooth is ready. This avoids the chicken-and-egg problem where
        # an "unavailable" entity can't be controlled to trigger a connection.
        return True

    @property
    def brightness(self):
        return self._instance.brightness

    @property
    def brightness_pct(self):
        return (self._instance.brightness / 255) * 100
    
    @property
    def brightness_step_pct(self):
        return self._attr_brightness_step_pct
    
    @property
    def is_on(self) -> Optional[bool]:
        # Report what we're enforcing; in_sync says whether the device has acked it.
        if self._desired_on is not None:
            return self._desired_on
        return self._instance.is_on

    @property
    def effect_list(self):
        return self._instance.effect_list

    @property
    def effect(self):
        return self._instance._effect

    @property
    def supported_features(self) -> int:
        """Flag supported features."""
        return self._attr_supported_features

    @property
    def supported_color_modes(self) -> int:
        """Flag supported color modes."""
        return self._attr_supported_color_modes

    # @property
    # def rgb_color(self):
    #     """Return the hs color value."""
    #     return self._instance.rgb_color
    
    @property
    def hs_color(self):
        """Return the hs color value."""
        return self._instance.hs_color
    
    @property
    def color_mode(self):
        """Return the color mode of the light."""
        return self._instance._color_mode

    @property
    def firmware_version(self):
        return self._instance.firmware_version
    
    @property
    def device_info(self):
        """Return device info."""
        return DeviceInfo(
            identifiers={
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._instance.mac)
            },
            name=self.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self._instance.mac)},
            model=self._instance._model,
            sw_version=self.firmware_version
        )

    @property
    def should_poll(self):
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        LOGGER.info("Turn on called.  kwargs: "+str(kwargs))
        self._desired_on = True
        self._in_sync = False
        transition = self._transition(kwargs)
        self._run_command(lambda: self._apply_turn_on(kwargs, transition), transition)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._desired_on = False
        self._in_sync = False
        transition = self._transition(kwargs)
        self._run_command(lambda: self._instance.turn_off(transition), transition)
        self.async_write_ha_state()

    def _pixel_assignments(
        self, pixels: list, count: int, default_mode: int, default_speed: int
    ) -> list:
        """Parse and validate the `pixels` field of a service call.

        Each entry may carry its own mode/speed for its range; entries
        without them use the call-level defaults. Returns
        (start, end, (r, g, b), mode, speed) tuples.
        """
        assignments = []
        for item in pixels:
            if not isinstance(item, dict):
                raise ValueError(f"Each pixels entry must be a mapping, got: {item}")
            if "from" in item:
                start = int(item["from"])
            elif "led" in item:
                start = int(item["led"])
            else:
                raise ValueError(
                    f"Each pixels entry needs 'from' (or 'led'), got: {item}"
                )
            end = int(item.get("to", start))
            color = item.get("color", [255, 255, 255])
            if len(color) != 3:
                raise ValueError(f"color must be [r, g, b], got: {color}")
            mode = item.get("mode")
            if mode is None:
                mode_id = default_mode
            else:
                if mode not in PIXEL_MODES:
                    raise ValueError(
                        f"mode must be one of {list(PIXEL_MODES)}, got '{mode}'"
                    )
                mode_id = PIXEL_MODES[mode]
            speed = max(1, min(100, int(item.get("speed", default_speed))))
            assignments.append(
                (start, end, (int(color[0]), int(color[1]), int(color[2])), mode_id, speed)
            )
        for start, end, _rgb, _m, _s in assignments:
            if start > end or start < 0 or end >= count:
                raise ValueError(
                    f"Pixel range {start}-{end} is outside the strip (0-{count - 1}). "
                    f"If your strip has more LEDs, pass 'pixel_count'."
                )
        return assignments

    async def async_set_pixels(
        self,
        pixels: list,
        mode: str = "solid",
        speed: int = 50,
        pixel_count=None,
        rle: bool = True,
    ) -> None:
        """Handle the ideal_led.set_pixels service call."""
        # Validate in the foreground so bad input fails the service call.
        if mode not in PIXEL_MODES:
            raise ValueError(f"mode must be one of {list(PIXEL_MODES)}, got '{mode}'")
        count = int(pixel_count) if pixel_count is not None else self._instance.pixel_count
        assignments = self._pixel_assignments(pixels, count, PIXEL_MODES[mode], speed)
        # Painting implies power on; send in the background like other
        # commands so a slow strip can't block the calling automation.
        self._desired_on = True
        self._in_sync = False
        self._run_command(
            lambda: self._instance.async_set_pixels(
                assignments, mode=mode, speed=speed, pixel_count=pixel_count, rle=rle
            )
        )
        self.async_write_ha_state()

    async def async_save_pattern(
        self,
        name: str,
        pixels: list | None = None,
        mode: str = "solid",
        speed: int = 50,
        pixel_count=None,
        rle: bool = True,
    ) -> None:
        """Handle ideal_led.save_pattern: store a named pattern.

        Without `pixels`, the current framebuffer is saved; otherwise the
        given ranges are written onto a black background. Saving is local,
        nothing is sent to the strip.
        """
        if mode not in PIXEL_MODES:
            raise ValueError(f"mode must be one of {list(PIXEL_MODES)}, got '{mode}'")
        default_mode = PIXEL_MODES[mode]
        if pixels is None:
            pix_list = self._instance.pixels
            if not any(any(p[:3]) for p in pix_list):
                raise ValueError(
                    "The current framebuffer is empty; paint first or pass pixels"
                )
        else:
            count = (
                int(pixel_count)
                if pixel_count is not None
                else self._instance.pixel_count
            )
            assignments = self._pixel_assignments(pixels, count, default_mode, speed)
            pix_list = [(0, 0, 0, default_mode, speed)] * count
            for start, end, rgb, m, s in assignments:
                pix_list[start : end + 1] = [(rgb[0], rgb[1], rgb[2], m, s)] * (
                    end - start + 1
                )
        self._instance.save_pattern(name, rle, pix_list)
        self.async_write_ha_state()

    async def async_delete_pattern(self, name: str) -> None:
        """Handle ideal_led.delete_pattern."""
        self._instance.delete_pattern(name)
        self.async_write_ha_state()

    async def async_set_pixel_range(self, **kwargs) -> None:
        """Handle ideal_led.set_pixel_range: paint a single LED range.

        Flat fields so the service renders as a form in the HA UI. `from` is
        a reserved word, hence the **kwargs access.
        """
        mode = kwargs.get("mode", "solid")
        if mode not in PIXEL_MODES:
            raise ValueError(f"mode must be one of {list(PIXEL_MODES)}, got '{mode}'")
        speed = kwargs.get("speed", 50)
        pixel_count = kwargs.get("pixel_count")
        rle = kwargs.get("rle", True)
        count = (
            int(pixel_count) if pixel_count is not None else self._instance.pixel_count
        )
        entry = {
            "from": kwargs["from"],
            "to": kwargs.get("to", kwargs["from"]),
            "color": kwargs.get("color", [255, 255, 255]),
            "mode": mode,
            "speed": speed,
        }
        assignments = self._pixel_assignments(
            [entry], count, PIXEL_MODES[mode], speed
        )
        self._desired_on = True
        self._in_sync = False
        self._run_command(
            lambda: self._instance.async_set_pixels(
                assignments, mode=mode, speed=speed, pixel_count=pixel_count, rle=rle
            )
        )
        self.async_write_ha_state()

    async def async_update(self) -> None:
        LOGGER.debug("async update called")
        await self._instance.update()
        self.async_write_ha_state()
    
    def light_local_callback(self):
        self.async_write_ha_state()

    async def update_ha_state(self) -> None:
        await self._instance.update()
        self.async_write_ha_state()
