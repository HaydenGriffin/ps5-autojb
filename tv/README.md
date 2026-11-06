# Press X from the TV: the "wasn't turned off properly" screen

After an unclean power-off (a crash, or a power cut), the PS5 boots to "Your PS5 wasn't turned off properly" and
waits for X. Remote Play doesn't work on that screen, so the launcher can't press it. The TV can, over **HDMI-CEC**.

[`ps5-cec.py`](ps5-cec.py) runs on a **rooted LG webOS TV** (Python 2.7, `/dev/cec0`). It finds the device that names
itself "PlayStation 5" on CEC and sends User Control Pressed "Select". The PS5 obeys it **whatever input the TV is
showing**, so a recovery at night doesn't have to switch the picture.

It does two things only: `probe` (print the PS5's CEC address) and `x`. Anything else is refused without touching
the bus.

## Setup

1. On the PS5: Settings → System → HDMI → **Enable HDMI Device Link: on**. Leave **One-Touch Play off** (otherwise every
   wake turns the TV on and switches input), and **Power Off Link off**.
   Note: with Device Link on, the TV remote's arrows and OK reach the PS5 while its input is showing.
2. On the TV (root shell): `mkdir -p /home/root/autojb` and copy `ps5-cec.py` there (`/home` survives reboots).
   Check with `python /home/root/autojb/ps5-cec.py probe` → `ps5 4`.
3. On the machine running the launcher, create a key for the launcher's user in `<state dir>/tv-cec/id_ed25519`, and
   pin the TV's host key in `<state dir>/tv-cec/known_hosts`. Compare the fingerprint with one you already trust.
4. On the TV, add one **forced-command** line to `/home/root/.ssh/authorized_keys`:

   ```
   command="/usr/bin/python /home/root/autojb/ps5-cec.py",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... ps5-autojb
   ```

   That key can only run the script; the script only takes `probe` or `x`.
5. Set `"tv_host"` in `autojb.json`. Prove it from the launcher's user: `ssh ... probe` prints `ps5 4`, and
   `ssh ... id` is refused.

## When it presses

Only after the launcher's own crash or power-cut recovery, and only when Remote Play is refused (a busy Remote Play
session is never mistaken for the warning). It presses at most twice, then asks you. The press is blind (the
launcher can't see the screen), and those conditions make the warning screen near-certain. If the TV is in standby and
unreachable, you're asked straight away.
