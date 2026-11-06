# Notes for refree's maintainers (gitlab.com/refree/refree)

Findings from running ps5-autojb on top of refree. Not filed upstream; anyone is welcome to.

Tested with refree v0.1.0 (upstream 1e8ee116), a PS5 running Y2JB, refree's bt-waker on an ESP-WROOM-32, Ubuntu 24.04.

---

## 1. Bug: segfault when a Remote Play session is open, or opened, while an app is starting

**What happens:** `refree run --no-auto-macro` serving the macro maker API, driven by a helper over HTTP.
- **First crash:** the helper pressed Cross on a Media tile (Y2JB) through `/api/maker/press` and kept the session open
  while the app started. refree died with SIGSEGV about 3 s later.
- **Second crash:** the helper had closed the session straight after Cross, then opened a new one 10 s later, while the
  app was still starting. refree died with SIGSEGV during the session request.

systemd restarted refree after `RestartSec`, and the app itself launched fine both times.

```
11:34:01 macro maker: pressed cross (150 ms)
11:34:04 [chiaki I] CTRL RECEIVED ... 00 00 00 02 00 16 00 00 cd fa
11:34:04 refree.service: Main process exited, code=killed, status=11/SEGV

11:38:49 macro maker: pressed cross (150 ms)
11:38:50 macro maker: session closed: stopped from the page
11:39:00 macro maker: opening a Remote Play session to <console>
11:39:00 [chiaki I] Sending session request
11:39:00 refree.service: Main process exited, code=killed, status=11/SEGV
```

**Expected:** the session fails or closes cleanly while the console is busy starting an app.
**Workaround:** close the session right after the launching press, and open none until the probe port answers.

---

## 2. Feature: select a tile by its on-screen title, not its position

The README already warns that menu-position macros depend on menu customisation. On our console the Media row also
**reorders by recent use**: "Y2JB - Linux" was tile 9 in one pass and tile 1 twenty minutes later.

What worked for us, through the existing `screenshot` API:
- Open Media, then for each tile, OCR the title line under the row (640×360 frame, crop (45, 238, 330, 266), ×6, Tesseract
  `--psm 7`).
- Press Cross only when two frames 0.8 s apart read the target exactly.
- **Learning readings:** the system font's J reads as I or `)` ("Y2JB - Linux" came back as "Y2IB - Linux" and
  "Y2)B - Linux", at 0–40% confidence). A one-time walk of the row records each tile's readings, and those count as
  that tile's name.

A `select_title` macro step (title plus known readings, refuse if not found) would make the built-in macros robust.

---

## 3. Feature: steps that wait on the console, for multi-stage launches (worked example: Y2JB Linux)

Y2JB Linux needs a sequence a button macro can't express:
1. A **fresh boot**, because it doesn't run on an already-jailbroken console. (Rest mode keeps the jailbreak, as the README
   says, so a rested console isn't fresh.)
2. Open the "Y2JB - Linux" tile.
3. The console **enters rest by itself**: 28 s after Cross in our runs.
4. **Wait for rest to settle.** A PS press 16 s into rest woke it straight back to rest; 45 s worked every time.
5. **Press PS**, which bt-waker's page does (it counts as the controller's PS button).
6. Linux boots, about 20 s later, to its login prompt at about 150 W, with the PS5's discovery silent. Whether SSH
   answers depends on the distro's network setup, so "silent discovery plus running power" was the reliable check.

Steps we used: `fresh_boot` (a verified restart if jailbroken), `open`, `wait` (console rest/awake/offline, a port, seconds,
or plug watts), `press_ps` (bt-waker) and `press`. Each is a named path the user can trigger.

---

## 4. Findings that may help the docs

- **bt-waker after a crash:** with the console crashed fully off (0 W), the page got `Page Timeout` until mains power was
  cut for 15 s. From a clean shutdown or rest it wakes at once.
- **Power menu focus:** with "Restart PS5" remembered, the PS5 showed the focused row only by its description line
  ("Close all games and apps, and restart your PS5"), with no outline. The menu also opens on the last row used, so
  "press down N times" macros land on the wrong option.
- **"Your PS5 wasn't turned off properly":** after an unclean power-off this screen blocks Remote Play entirely. An LG TV
  with HDMI-CEC (`/dev/cec0` on a rooted webOS TV) can send Select to the PS5's logical address whatever input the TV
  shows, which clears it with no one at the console.
- **LG SimpLink Auto Power Sync** puts the PS5 into rest when the TV turns off, once HDMI Device Link is on.
