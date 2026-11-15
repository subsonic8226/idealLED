> **Fork note (pvbaudin/idealLED):** this fork adds state enforcement. These
> controllers give no state feedback and can switch themselves on, or miss a
> command over a weak BLE link. The light remembers the last state HA asked for
> (restored across restarts) and re-sends it every `resend_interval` seconds
> (integration options, default 120, 0 disables), retrying with backoff on
> failure. On/off calls return immediately and the BLE work runs in the
> background. The `in_sync`, `last_confirmed` and `consecutive_failures`
> attributes show whether the device acknowledged the last send.
>
> It also adds software fades. The controller has no fade command, so on/off,
> brightness and colour changes are stepped from HA every 0.1 s. They honour
> `transition` in service calls, and the `default_transition` option (seconds,
> default 0 = instant) applies when a call gives none, e.g. from Alexa or a
> dashboard. Effects are not faded.
>
> It also adds per-LED control: the `ideal_led.set_pixels` entity service paints
> individual LEDs or ranges using the controller's bulk paint ("collections")
> protocol. The painted frame is part of the enforced state: it is restored
> after a restart and re-asserted on resends instead of a solid colour.
> Patterns saved with `ideal_led.save_pattern` appear in the effect list as
> `Pattern: <name>`, so scenes, automations and the effect dropdown can address
> them by name.

# iDeal LED control

![image](https://github.com/8none1/idealLED/assets/6552931/c5fcd8fc-440a-48dd-abe4-d6fdd2e4a422)

## Supported Models

Devices are coming on to the market which report as IDL or ISP lights but which use a different protocol.  Support for these lights can be added by a sample of the lights will be needed OR the gathering of `btsnoop` debug logs from an Android device.  If you are willing to help gather the data and test the new code, please log an issue.

## Supported Features

- On / Off
- Set RGB colour of entire strip
- Set DIY LED patterns (not supported in Home Assistant)
- Set brightness
- Set effect & effect speed
- Bulk paint entire strip using collections feature
- Individual LED control via the `ideal_led.set_pixels` Home Assistant service
- Automatic discovery of supported devices in Home Assistant

## Not supported

- Discovery of current light state
- On-chip timer functionality

## Home Assistant

There is a Home Assistant custom component to let you control these lights.

### Installation

Add this repo to HACS as a custom repo. Click through:

- HACS -> Integrations -> Top right menu -> Custom Repositories
- Paste the Github URL to this repo in to the Repository box
- Choose category Integration
- Click Add
- The repo should show up in HACS as a new integration.  Click on it and choose `DOWNLOAD`.
- Restart Home Assistant
- iDeal LED devices should start to appear in your Integrations page

### Per-LED control (`set_pixels`)

The integration registers an entity service, `ideal_led.set_pixels`, which paints
individual LEDs or LED ranges using the controller's bulk paint ("collections")
protocol. Colours are reduced to 5 bits per channel internally, to protect the
controller's voltage regulator. The call returns immediately; the BLE transfer
runs in the background like other commands, and the painted frame is remembered,
restored across restarts and re-asserted by the state enforcer.

```yaml
service: ideal_led.set_pixels
target:
  entity_id: light.my_ideal_led
data:
  pixel_count: 100 # optional, only needed if your strip has a different LED count
  pixels:
    - { from: 0,  to: 24, color: [255, 0, 0] }
    - { from: 25, to: 49, color: [0, 255, 0], mode: flash, speed: 80 }
    - { from: 50, to: 99, color: [0, 0, 255] }
```

- `pixels`: list of ranges. `from` is the first LED (0-based), `to` the last
  (inclusive, defaults to `from`), `color` is `[r, g, b]` (0-255). A single LED
  can be given as `{ led: 42, color: [255, 0, 0] }`. Each entry may also carry
  its own `mode` and `speed` — mode and speed are per LED, not per call.
  LEDs that are not named keep their previously painted colour, mode and
  speed; the whole frame is re-sent on every call.
- `mode`: `solid` (default), `fade` or `flash` — default for entries that
  don't set their own.
- `speed`: 1-100, used by `fade` and `flash` — default for entries that don't
  set their own.
- `rle`: compress runs of identical colours into a single collection (default,
  much faster). Set to `false` if your strip shows unexpected colours.
- The current frame is exposed as the `pixels` attribute (rrggbb per LED, plus
  `pixel_params` with mmss mode/speed per LED, and `pixel_count`, `pixel_rle`,
  `pixel_active`) so it is restored across restarts. A paint applied via `set_pixels` is anonymous: while it is
  showing it is restored and re-asserted like any state, but it is not
  addressable through the effect dropdown or scenes. Save it as a pattern
  (below) to give it a name.

### Named patterns: scenes and the UI dropdown

Pixel patterns become addressable through **named patterns**: each pattern
saved with `ideal_led.save_pattern` appears in the effect list as
**`Pattern: <name>`**, alongside the controller's real effects. Scenes and
automations capture the effect as a plain, readable string, which is the
recommended way to capture and restore pixel patterns.

```yaml
service: ideal_led.save_pattern
target:
  entity_id: light.my_ideal_led
data:
  name: Advent
  pixels:
    - { from: 0, to: 24, color: [120, 10, 0] }
  mode: flash
  speed: 40
```

- Without `pixels`, the currently painted framebuffer is saved.
- Each `pixels` entry may override `mode`/`speed` for its own range; the
  call-level `mode`/`speed` are the defaults for the remaining entries.
- Applying works like any effect: `light.turn_on` with `effect: "Pattern:
  Advent"`, via the effect dropdown, or by capturing it in a scene.
- The pattern registry persists across restarts (entity attribute) and is
  per entity, so each strip has its own names.
- While a named pattern is showing, the state effect stays the short name
  (scenes capture `Pattern: Advent`, not technical data), and the state
  enforcer re-asserts it on resends and after restarts.
- Speed and brightness sliders act on a showing named pattern; speed changes
  are written back into the saved pattern.
- Re-applying the same scene is safe: a pattern that is already showing is
  not repainted unless the brightness changed (repainting restarts
  fade/flash animations; solid patterns don't visibly change).
- Solid colours and controller effects deactivate the showing pattern;
  re-apply the `Pattern: <name>` effect to bring it back.

### Configuring patterns from the HA UI

Besides the services, each device gets UI entities:

- **`<name> Pattern`** (select): dropdown of all saved patterns; choosing one
  applies it like the effect dropdown.
- **`<name> Pattern name`** (text): the name the buttons below act on.
- **`<name> Save Pattern`** / **`<name> Delete Pattern`** (buttons): save the
  currently painted frame under that name, or delete the pattern.

For painting single ranges without YAML, use the flat service
`ideal_led.set_pixel_range` (`from`, `to`, `color`, `mode`, `speed`, ...) —
its fields have selectors, so it renders as a form in Developer Tools and the
automation editor. The strip's LED count can be set once per device in the
integration options (`pixel_count`, default 100) instead of passing it in
every service call.
- A pattern's LED count is applied via a `LAMPN` packet when it differs from
  the strip's current count.
- Limits: max 20 patterns per strip, names up to 64 characters, 1-255 LEDs per
  pattern. `delete_pattern` removes one; scenes still referencing it will
  then log an error when applied.

Notes:

- The RLE compression (several LEDs in one collection) follows the protocol
  notes but has not been verified on real hardware. `rle: false` reproduces the
  exact, verified byte layout of the original demo script.
- Bulk paint has only been verified on TYPE2 controllers.

## Other tools

There are a few other tools in this repo that might help you improve support or test your own lights.

- The btsnoop directory has a few HCI log dumps if you want to look at the raw values
- `aes_decrypt.py` was used to test the encryption implementation
- `att_protocol.md` has notes on the protocol and the Android companion app

## An important note on upgrades

As bugs are fixed and new features are added it might be necessary to delete your existing devices and re-add them.  If you experience strange behaviour after an upgrade please try to delete the device and re-add it.  If this doesn't help, open an issue describing the problem.

## Warning

I have bricked one set of lights by sending invalid data to them while building this integration.  Use this repo at your own risk.
You should be careful when sending bytes to these lights which are outside of the already discovered values.

You can read more on this here: https://www.whizzy.org/2023-12-14-bricked-xmas/

As far as I can tell this integration does not use any unsafe values by default.  If you know otherwise, please open an issue, ideally with an accompanying PR.

## Enabling Debug Logging

If you're experiencing issues with the integration or want to help troubleshoot problems, you can enable debug logging for this integration in Home Assistant.

### Method 1: Using configuration.yaml (Persistent)

Add the following to your `configuration.yaml` file:

```yaml
logger:
  default: info
  logs:
    custom_components.ideal_led: debug
```

After adding this configuration, restart Home Assistant for the changes to take effect.

### Method 2: Using the Home Assistant UI (Temporary)

For temporary debugging without restarting Home Assistant:

1. Go to **Settings** → **Devices & Services**
2. Find the **iDeal LED** integration
3. Click the three-dot menu (⋮) on the integration card
4. Select **Enable debug logging**

This will immediately start capturing debug logs. When you're done debugging:

1. Return to the integration
2. Click the three-dot menu (⋮) again
3. Select **Disable debug logging**

A download will be offered containing the captured debug logs.

### Method 3: Using Developer Tools (Temporary)

1. Go to **Developer Tools** → **Services**
2. Search for and select `logger.set_level`
3. Enter the following in the YAML editor:

```yaml
service: logger.set_level
data:
  custom_components.ideal_led: debug
```

4. Click **Call Service**

This enables debug logging immediately without a restart. The setting will revert to default after Home Assistant restarts.

### Viewing the Logs

You can view the logs in several ways:

#### Via Home Assistant UI

1. Go to **Settings** → **System** → **Logs**
2. Click **Load Full Logs** to see all entries
3. Use the search/filter to look for `ideal_led` entries

#### Via Command Line (Docker)

```bash
docker logs -f homeassistant 2>&1 | grep ideal_led
```

#### Via Command Line (Home Assistant OS / Supervised)

```bash
ha core logs --follow | grep ideal_led
```

#### Via Command Line (Core Installation)

Check your Home Assistant log file location (typically `home-assistant.log` in your config directory):

```bash
tail -f /path/to/config/home-assistant.log | grep ideal_led
```

### What to Include in Bug Reports

When opening an issue, please include:

1. **Debug logs** - Enable debug logging, reproduce the issue, then capture the relevant log entries
2. **Device information** - The model/type of your LED device (visible in the device info)
3. **Home Assistant version** - Found in Settings → About
4. **Integration version** - Found in HACS or the manifest.json
5. **Steps to reproduce** - What actions led to the problem

### Bluetooth Debugging

For Bluetooth-specific issues, you may also want to enable debug logging for the Bluetooth integration:

```yaml
logger:
  default: info
  logs:
    custom_components.ideal_led: debug
    homeassistant.components.bluetooth: debug
    bleak: debug
```

> **Note:** Enabling `bleak` debug logging can produce a large volume of logs. Only enable it when specifically troubleshooting Bluetooth connectivity issues.

## Other projects that might be of interest

- [iDotMatrix](https://github.com/8none1/idotmatrix)
- [Zengge LEDnet WF](https://github.com/8none1/zengge_lednetwf)
- [iDealLED](https://github.com/8none1/idealLED)
- [BJ_LED](https://github.com/8none1/bj_led)
- [ELK BLEDOB](https://github.com/8none1/elk-bledob)
- [HiLighting LED](https://github.com/8none1/hilighting_homeassistant)
- [BLELED LED Lamp](https://github.com/8none1/ledble-ledlamp)

