# Home Assistant

Home Assistant does three jobs here: it shows the launcher's state, it sends the notifications, and it owns the power
hardware. The launcher never switches the plug or presses the waker itself. It asks Home Assistant for one step at a
time over MQTT, and the package decides whether that step is safe.

There are two halves:

- The launcher creates its own entities over MQTT discovery. Nothing to install for those.
- The package [`homeassistant/ps5_autojb.yaml`](../homeassistant/ps5_autojb.yaml) adds the notifications, the
  recovery power steps and the shutdown request.

refree also announces its own device, "PS5 (refree)", with sensors and buttons of its own. You can use them, but
leave its macro buttons alone: the launcher does the launching.

## Install the package

1. You need the MQTT integration connected to the same broker as refree, and the companion app on your phone.
2. Turn on packages in `configuration.yaml` if you haven't:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

3. Copy `ps5_autojb.yaml` to `/config/packages/`.
4. Edit the lines marked `EDIT`:
   - `notify.mobile_app_your_phone` in `script.ps5_autojb_notify`: your phone's notify service. Developer tools →
     Actions lists them under `notify.mobile_app_...`.
   - The hardware entity ids, if yours differ (below). Renaming your entities to match is usually easier than editing
     the package.
   - The `ps5_autojb/` topics, only if you changed `mqtt_prefix` in `autojb.json`.
5. Restart Home Assistant. Later edits only need a reload of MQTT entities, automations and scripts.

Check: Settings → Automations shows six "PS5 auto-jailbreak" automations, all on, and Developer tools → States has
`sensor.ps5_autojb_recovery_request` (it reads `unknown` until the launcher first asks for something).

## From the launcher (MQTT discovery)

All on one device, "PS5 auto-jailbreak". They go `unavailable` when the launcher stops.

| Entity | What it is |
|---|---|
| `switch.ps5_autojb_auto` | Automatic jailbreak. Off: no automatic launches and no recovery, and anything running stops. Survives restarts. |
| `button.ps5_autojb_launch` | Run jailbreak now. Does nothing if it's already jailbroken. |
| `binary_sensor.ps5_autojb_ready` | Jailbreak ready: the probe port answers. |
| `sensor.ps5_autojb_power` | Console power: `awake`, `app`, `rest`, `offline` or `unknown`, from Remote Play discovery. |
| `sensor.ps5_autojb_result` | The last result, with the reason (below). |
| `button.ps5_autojb_discover_titles` | Walk the Media row and record how each tile reads. Under Configuration. |
| `sensor.ps5_autojb_titles` | How many tiles were found; the attributes list them with their readings. |
| `button.ps5_autojb_path_<name>` | One per run path in `paths.json`, named by its `button`. `button.ps5_autojb_path_linux` for the example. |

### `sensor.ps5_autojb_result`

States you'll see:

| State | Meaning |
|---|---|
| `checking PS5` | An automatic pass started after a boot. |
| `jailbreak confirmed` | Launched and the probe port opened. `launched` is true. |
| `already jailbroken` | Nothing to do. |
| `automatic reboot starting`, `automatic retry waiting` | The one verified restart after a failed launch, then its retry. |
| `automatic recovery: waking PS5`, `automatic recovery: power cycling` | Crash recovery steps. |
| `automatic recovery: press X on the PS5` | The warning screen is up and the TV couldn't press X. |
| `automatic launch cancelled` | Automatic jailbreak was turned off mid-pass. |
| `<path> done`, `<path>: <step>` | A run path's progress and finish. |
| `refused: ...` | Stopped before pressing anything, with why (a limit, a busy Remote Play, the tile not found). |
| `failed: ...` | Stopped after something went wrong, with the error. |

Attributes:

- `source`: `automatic`, `manual`, or `path:<name>`.
- `at`: when it was published (UTC). Each new result has a new `at`, even with the same state.
- `launched`: whether X was pressed on the tile this time.
- `preview`: on `jailbreak confirmed`, the URL of a fresh screenshot (through `public_url`). Empty otherwise.

## From the package

| Entity | What it is |
|---|---|
| `script.ps5_autojb_notify` | Every phone message goes through it: `title`, `message`, `level` (`status`, `ask` or `done`), `image`, `clear`. |
| `script.ps5_autojb_tv_notice` | Called with each message for your TV. Empty until you wire it ([notifications.md](notifications.md#on-the-tv)). |
| `script.ps5_autojb_full_shutdown` | Turn the PS5 fully off through its Power menu. Never rest mode. |
| `sensor.ps5_autojb_recovery_request` | The launcher's latest power-step request. Diagnostic. |
| `sensor.ps5_autojb_shutdown_result` | The last shutdown's outcome. |

Automations:

| Automation | Does |
|---|---|
| Run jailbreak from the phone | The notification's Run jailbreak button presses `button.ps5_autojb_launch`. |
| stopped | "PS5 jailbreak needs attention" when an automatic pass stops. |
| ready | "PS5 jailbreak ready" with the screenshot, once per confirmed launch. |
| progress | The status messages during an automatic pass and recovery. |
| run path progress | One message per path step, then done or stopped. |
| recovery power step | Answers the launcher's `status`, `power_cycle` and `bt_wake` requests. |

## Power hardware

Optional, for crash recovery ([setup.md](setup.md#5-smart-plug-crash-recovery)). The package expects these ids:

| Entity | From |
|---|---|
| `switch.ps5_power_plug` | A smart plug on the PS5's mains lead |
| `sensor.ps5_power_plug_current_power` | The same plug's power reading, in W |
| `button.ps5_bt_waker_power_on` | The ESP32 waker ([esphome/README.md](../esphome/README.md)) |
| `sensor.ps5_bt_waker_status` | The waker's last result (`Success`, `Page Timeout`, ...) |

The recovery power step refuses, and tells the launcher why, when:

- the request is older than 30 seconds, or isn't one of the three steps;
- the plug is off (someone switched it off) or its reading is unavailable;
- the console is in rest mode;
- for a power cut, the plug reads 3 W or more and the launcher didn't report the console hung. A Bluetooth wake is
  only refused when the waker is unavailable.

## A dashboard card

Built-in cards only. Change or drop the Boot Linux tile to match your paths.

```yaml
type: vertical-stack
cards:
  - type: tile
    entity: binary_sensor.ps5_autojb_ready
    name: Jailbreak
  - type: grid
    columns: 2
    square: false
    cards:
      - type: tile
        entity: sensor.ps5_autojb_power
        name: Console
      - type: tile
        entity: switch.ps5_autojb_auto
        name: Automatic
      - type: tile
        entity: button.ps5_autojb_launch
        name: Run jailbreak
        tap_action:
          action: perform-action
          perform_action: button.press
          target: {entity_id: button.ps5_autojb_launch}
      - type: tile
        entity: button.ps5_autojb_path_linux
        name: Boot Linux
        tap_action:
          action: perform-action
          perform_action: button.press
          target: {entity_id: button.ps5_autojb_path_linux}
          confirmation:
            text: Restart the PS5 and boot Linux?
  - type: tile
    entity: script.ps5_autojb_full_shutdown
    name: Turn off PS5
    icon: mdi:power
    tap_action:
      action: perform-action
      perform_action: script.ps5_autojb_full_shutdown
      confirmation:
        text: Turn the PS5 fully off?
  - type: entities
    entities:
      - entity: sensor.ps5_autojb_result
        name: Last result
        secondary_info: last-changed
```

## Checking it end to end

1. With the PS5 on the home screen and not jailbroken, press Run jailbreak. `sensor.ps5_autojb_result` goes to
   `jailbreak confirmed`, `binary_sensor.ps5_autojb_ready` turns on, and your phone gets the ready message.
2. Press it again: `already jailbroken`, and nothing happens on the console.
3. Turn Automatic jailbreak off, restart the PS5 from its menu, and nothing happens. Turn it back on and restart
   again: `checking PS5`, then `jailbreak confirmed`.
4. With the hardware: Developer tools → Actions, `mqtt.publish` to `ps5_autojb/recover/request` with
   `{"id": "<32 hex characters>", "at": <now as a Unix timestamp>, "action": "status"}`. Watch
   `ps5_autojb/recover/result` (MQTT integration → Configure → Listen to a topic): it answers with the plug state,
   the watts and the waker's status, and switches nothing.
