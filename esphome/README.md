# The Bluetooth waker: power the PS5 on from fully off

A PS5 that crashed off, or was fully shut down, can't be woken over the network. An ESP32 can wake it the way the
controller does: it takes the paired DualSense's Bluetooth address and pages the console, and the console powers on as
if the PS button was pressed. No pairing keys and no wiring inside the console.

This uses **refree's `bt-waker`** firmware. It isn't copied here: refree is AGPL-3.0, and its `bt-waker` folder carries
MIT only for the parts from [wakepsx-on-bt](https://github.com/FreeTHX/wakepsx-on-bt). Fetch it at the commit
this was proven with:

```sh
mkdir ps5-waker && cd ps5-waker
for f in ps5-bt-waker.yaml wake_psx_vhci.h; do
  curl -fsSLO "https://gitlab.com/refree/refree/-/raw/c39f9bd1104786001e4e3a4a24751b4754d09106/bt-waker/$f"
done
shasum -a 256 wake_psx_vhci.h   # dcf31ce0b85965fc735838a98101943b3537618a142832b89324ac25867b8f82
```

## What you need

- An **original ESP32** (ESP-WROOM-32 dev board). ESP32-S2, S3, C3 and C6 have no Bluetooth Classic and **won't
  work**. The waker takes over the radio, so the board can't also be a Bluetooth proxy.
- A USB power supply near the PS5. Not the console's own USB ports, and not on the smart plug.
- A DualSense paired to the PS5, and a Linux computer for one read.

## Steps

1. **Read the two addresses** with [`tools/read_dualsense.py`](../tools/read_dualsense.py). Pair the controller to the
   PS5 first (USB cable to the console, press PS), then plug it into a Linux machine with no Bluetooth service running.
   **Not a Mac:** macOS re-pairs a USB DualSense to itself, even with Bluetooth off, and reports the Mac instead of the
   PS5. Afterwards, plug the controller back into the PS5 and press PS.
2. **Add the secrets** to your ESPHome `secrets.yaml`: the two addresses from step 1, plus `ps5_waker_wifi_ssid`,
   `ps5_waker_wifi_password`, `ps5_waker_ota_password`, `ps5_waker_web_server_username` and
   `ps5_waker_web_server_password`, and `ps5_waker_api_encryption_key` (`openssl rand -base64 32`). To reuse your
   usual Wi-Fi secrets instead, change the two `wifi:` lines in `ps5-bt-waker.yaml`.
3. **Build and flash.** The first flash is over USB (ESPHome Device Builder's "Manual download → Factory format", then
   `esptool write-flash 0x0 firmware.factory.bin`); after that, over the air.
4. **Add it to Home Assistant** (ESPHome integration, with the encryption key). Rename the two entities to
   `button.ps5_bt_waker_power_on` and `sensor.ps5_bt_waker_status`, or edit the package to match.
5. **Test:** fully shut the PS5 down, press the button, and the status should read `Success` and the console start.

## What we learned live

- From a clean full shutdown the console answers straight away: awake in about 30 s.
- **After a crash** it ignored Bluetooth pages (`Page Timeout`) until its mains power was cycled. That's why the
  launcher pairs the waker with a smart plug: cut 15 s, restore, wake again.
- Rest mode also wakes over Bluetooth. Never cut power in rest mode: Sony warns it can corrupt data.
