# How it works

## One automatic pass

1. **Watch.** Every second the launcher asks the console (UDP discovery, port 9302) whether it's awake, in rest mode or
   silent, and every 5 s whether the jailbreak's port answers. After a boot (three missed replies, or rest mode, then
   awake), it waits 30 s on Home.
2. **Claim the boot** in `launcher-auto.json` *before* any input, so a restarted helper never repeats the same pass.
3. **Launch** (`launch()`): open a Remote Play session, go Home, open the Media tab, walk the row. For each tile, OCR
   the title; act only when it equals `target_name` (ignoring case, spaces and dashes) in two frames 0.8 s apart.
   Press X once and **close the session straight away**: refree crashed when a session was open while the app started.
4. **Confirm:** poll the jailbreak's port for up to 3 minutes. Three missed discovery replies in a row end the wait
   early: the console crashed.
5. **Report:** `jailbreak confirmed` with a fresh screenshot, or the reason it stopped.

## When it fails

```
launch failed after X ─▶ console still answers? ─yes─▶ verified restart ─▶ retry once
                          │no
                          ▼
            silent 20 s, read the plug (wait up to a minute for its reading to catch up)
              ├─ rest mode or 2–30 W ─▶ Bluetooth wake only (never cut)
              ├─ under 2 W (off)     ─▶ Bluetooth wake ─ignored─▶ plug off 15 s, on ─▶ wake again
              └─ over 30 W, silent 3+ min (hung) ─▶ plug cycle (once a day) ─▶ wake
                          ▼
            awake ─▶ Remote Play refused? ─▶ TV presses X (≤ 2) ─▶ else ask the owner ─▶ retry
```

The launcher never touches the plug or the waker itself. It asks Home Assistant over MQTT for one fixed step at a time
(`status`, `power_cycle`, `bt_wake`), with a fresh id and timestamp. Home Assistant refuses a plug someone switched off,
an unreadable plug, a console in rest mode, and a lit console unless the launcher saw it hung.

## Limits (durable, in the launcher's state folder)

| File | Limit |
|---|---|
| `launcher-attempts.json` | 3 launched failures in 30 minutes. A refusal before the app opens gives its attempt back. |
| `launcher-reboots.json` | 1 verified restart in 30 minutes |
| `launcher-hard.json` | 1 power recovery in 2 hours |
| `launcher-hung.json` | 1 power cut of a hung console per day |

## Lessons from live testing

- **macOS re-pairs a USB DualSense to the Mac**, even with Bluetooth off. Read the addresses on Linux.
- **After a crash the PS5 ignores Bluetooth pages** until its mains power is cycled. From a clean shutdown it wakes at once.
- **Rest mode draws about 4 W.** A first version used 3 W as "hung" and would have power-cut a console in rest mode;
  it was cancelled in time. Now rest mode only gets a Bluetooth wake.
- **refree segfaults if a Remote Play session is open while the app starts.** A "press X again if it didn't open"
  idea did exactly that, twice. It was removed: one press, session closed.
- **The plug's power reading lags about 30 s** behind a crash, so a just-crashed console still reads as running.
- **With HDMI Device Link on, the TV remote reaches the PS5** while its input is showing. Keep One-Touch Play off so
  wakes don't turn the TV on.
- A refusal before anything was launched used to count as a failed attempt, and blocked a retry after a successful
  recovery. It no longer counts.
- **The PS5 doesn't always outline the selected Power option.** With Restart PS5 remembered from an earlier restart it
  showed only that row's description line. The reader now also accepts "three labels, one with a description under
  it", reads that label in three crops that must agree, and still needs two matching reads before X.
