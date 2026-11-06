# Setup

The order that worked here. Each step ends with a check; don't move on until it passes. Steps 1 to 4 get you
automatic launching with notifications. Steps 5 and 6 add crash recovery, step 7 the TV's X press, and step 8 run paths.

What you need before starting:

- A PS5 that already jailbreaks from an app on its home screen (this was built with Y2JB). Nothing here installs or
  changes the jailbreak.
- A Linux machine on the same network that stays on: a mini PC, a VM or an LXC container. It runs refree and the
  launcher.
- Home Assistant with an MQTT broker (the Mosquitto app is fine) and the companion app on an Android phone.

## 1. PS5 settings

1. Settings → System → Remote Play → Enable Remote Play: on.
2. Settings → System → Power Saving → Features Available in Rest Mode → Stay Connected to the Internet: on.
3. Settings → Users and Accounts → Login Settings → Log In to PS5 Automatically: on for the profile that owns the
   jailbreak app. Without it, a restarted console waits at the user picker and Remote Play can't get past it.
4. Put the jailbreak app's tile on the home screen, so it shows in the Media tab.

Check: restart the PS5 with no controller nearby. It reaches the home screen by itself.

## 2. refree

[refree](https://gitlab.com/refree/refree) does the Remote Play part. It has release tarballs for Linux x86_64 and
arm64; this was built and run on v0.1.0 for x86_64, and refree notes its arm64 build isn't run in CI. Run it as its
own user. Here that's `refree` with home `/var/lib/refree`, so its config is
`/var/lib/refree/.config/refree/config.json`. refree is built and tested on consoles kept off PSN; read the limits in
its README first.

The launcher reads the same `config.json`: `host`, `probe_port` and `mqtt`. A minimal one:

```json
{
  "host": "192.0.2.10",
  "probe_port": 9021,
  "web_listen": "0.0.0.0:8091",
  "mqtt": {"host": "192.0.2.2", "port": 1883, "username": "refree", "password": "..."}
}
```

- `host`: the PS5's address. Give it a fixed one in your router.
- `probe_port`: a port that answers only once the jailbreak has loaded, and that stock firmware never opens. The
  launcher treats an open port as "jailbroken". refree's default is 9021, the ELF loader port. If your loader opens
  9021 before your payloads have run, use a port one of your payloads opens instead; this setup uses 3232, the klog
  server its autoload starts. Set it in `config.json`, not refree's Config page: that page writes
  `web-overrides.json`, which the launcher doesn't read.
- `web_listen`: refree's web UI and API are off without it, and the launcher can't work without the API. It has no
  login, so keep it on your LAN and never forward the port.
- `mqtt`: a login of its own on your broker. The launcher uses the same one.

Pair it on refree's Pairing page (`http://<machine>:8091/pair`) with the account ID and 8-digit PIN. On a console kept
off PSN, the LinkDev homebrew shows both; on a signed-in one the PIN is under Settings → System → Remote Play → Link
Device. If you already paired chiaki-ng, `refree import-chiaki` imports its keys instead.

Run it as a service with `refree run --no-auto-macro`. That serves the web UI and runs no boot macro of its own: the
launcher handles boots, and both pressing buttons at once would go wrong.

Check: `curl http://127.0.0.1:8091/api/config` answers. Open a session on the Macro maker page
(`http://<machine>:8091/maker`) and you see the PS5's screen. Close the session afterwards: Remote Play is one session
at a time, and the launcher can't connect while yours is open. Also check the probe: before a jailbreak,
`nc -z <ps5> <probe_port>` fails; after one, it succeeds.

## 3. The launcher

```sh
sudo apt install python3-pil python3-paho-mqtt tesseract-ocr-eng
sudo mkdir -p /opt/ps5-autojb
sudo cp launcher/ps5_autojb.py /opt/ps5-autojb/
sudo -u refree cp launcher/autojb.example.json /var/lib/refree/.config/refree/autojb.json
```

Edit `autojb.json`. Unknown keys are refused, so a typo stops it from starting.

| Key | What to put |
|---|---|
| `target_name` | The jailbreak app's title exactly as the PS5 shows it under the Media row. |
| `mqtt_prefix` | Leave as `ps5_autojb` unless you run two. The Home Assistant package uses the same prefix. |
| `max_tiles` | How far along the Media row to look. |
| `remote_play_url` | refree's API as the launcher reaches it, usually `http://127.0.0.1:8091`. |
| `public_url` | refree's API as Home Assistant reaches it. The ready notification's picture is fetched from here. |
| `tv_host` | Empty for now. Step 7. |
| `off_watts`, `hung_watts` | Leave at 2 and 30 unless your plug reads differently. See step 5. |

Try one pass by hand while the PS5 is on the home screen and not jailbroken:

```sh
sudo -u refree python3 /opt/ps5-autojb/ps5_autojb.py --config /var/lib/refree/.config/refree/config.json
```

It prints a JSON result: `jailbreak confirmed` if it worked. `refused: ...` and `failed: ...` say why it stopped. The
usual first failure is a `target_name` that doesn't match what OCR reads; step 8's Discover titles shows the readings.

Then install the service:

```sh
sudo cp systemd/ps5-autojb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ps5-autojb
```

Check: `journalctl -u ps5-autojb -f` shows it connected to MQTT. In Home Assistant, Settings → Devices → MQTT has a
device "PS5 auto-jailbreak" with Run jailbreak, Automatic jailbreak, Jailbreak ready, Console power, Last jailbreak
result, Discover titles and Titles.

## 4. The Home Assistant package

The details, every entity and a dashboard card are in [home-assistant.md](home-assistant.md).

1. Turn on packages in `configuration.yaml` if you haven't (`homeassistant: packages: !include_dir_named packages`).
2. Copy `homeassistant/ps5_autojb.yaml` to `/config/packages/`.
3. Edit the `EDIT` lines at the top: your phone's notify service, and your plug and waker entity ids if you'll add them.
4. Restart Home Assistant, or reload MQTT entities, automations and scripts.

Check: with the PS5 not jailbroken, press Run jailbreak. Your phone shows "PS5 jailbreak ready" with a picture of
the PS5's screen. Then restart the PS5 from its own menu and leave it. About 30 seconds after it reaches the home
screen you get "PS5 jailbreak starting", then "PS5 jailbreak ready", with no one touching anything.

At this point the basic setup is done. If your console never crashes, you can stop here.

## 5. Smart plug (crash recovery)

A plug with power monitoring, on the PS5's mains lead. The launcher uses the power reading to tell a console that's
off from one in rest mode or hung. This was built with a Tapo P110.

Name its entities `switch.ps5_power_plug` and `sensor.ps5_power_plug_current_power`, or change those ids in the package.

Note what it reads in each state. Here: about 0.5 W off, 4 W in rest mode, 60 to 200 W running. `off_watts` must sit
between off and rest, and `hung_watts` above rest. The plug never cuts power between those two, so rest mode is safe.
The package has its own guard too: it refuses a power cut at 3 W or more unless the launcher reports the console
hung. If your plug reads 3 W or more with the console off, raise that `3` in `ps5_autojb_recovery_step`.

Check: the power sensor follows the console as you put it into rest mode and wake it again.

## 6. Bluetooth waker (crash recovery)

An original ESP32 running refree's `bt-waker`. It powers the console on from fully off the way the controller does.
Build it with [esphome/README.md](../esphome/README.md).

Check: turn the PS5 fully off from its Power menu, press `button.ps5_bt_waker_power_on`, and the console starts.

With steps 5 and 6 done, a crash on launch is recovered: wake, and if that's ignored, a 15-second power cut and wake
again. The limits are in [how-it-works.md](how-it-works.md#limits-durable-in-the-launchers-state-folder).

## 7. TV X press (optional)

Only for a rooted LG webOS TV. Without it, after a power cut you get a "PS5 needs X" notification and press X yourself.
Set it up with [tv/README.md](../tv/README.md), then set `tv_host` in `autojb.json` and restart the service.

Check: from the launcher's user, the forced-command SSH `probe` prints `ps5 4`, and any other command is refused.

## 8. Run paths (optional)

1. Press Discover titles. The launcher walks the Media row with the arrow keys only and stores how OCR reads each
   tile. The Titles sensor's attributes list them.
2. Copy `launcher/paths.example.json` to `/var/lib/refree/.config/refree/paths.json` and edit it. A tile's name is
   what the PS5 shows; Discover titles adds the OCR's misreadings for you. Steps are in the
   [README](../README.md#run-paths).
3. Restart the service. Each path appears as a button on the PS5 auto-jailbreak device.

Check: the journal shows no `paths.json not loaded` line, and pressing the path's button sends a notification for
each step.

## If something goes wrong

- Turning off Automatic jailbreak stops anything in progress and keeps the launcher's hands off the console.
- The journal (`journalctl -u ps5-autojb`) logs every press and every reading it acted on.
- The last result, with its reason, is in `sensor.ps5_autojb_result` and in `launcher-last.json` next to refree's config.
- The limit files (`launcher-attempts.json`, `launcher-reboots.json`, `launcher-hard.json`, `launcher-hung.json`) sit in
  the same folder. To reset a limit, stop the service, delete its file, and start the service again.
