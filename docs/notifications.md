# Notifications

Everything goes through one script, `script.ps5_autojb_notify`, using the Home Assistant companion app for Android.
Every message has the tag `ps5-autojb`, so each one replaces the last instead of stacking.

| When | Title | Kind |
|---|---|---|
| An automatic pass starts | PS5 jailbreak starting | status (expires after 5 min) |
| A verified restart | PS5 restarting | status |
| A Bluetooth wake after a crash | PS5 waking up | status |
| A plug power cycle | PS5 power cycle | status |
| Back up, retry queued | PS5 retry queued | status |
| The warning screen and no TV press | PS5 needs X | ask (high importance) |
| Jailbroken | PS5 jailbreak ready (with a fresh screenshot) | done |
| Stopped (anything unclear, or a limit reached) | PS5 jailbreak needs attention (Run jailbreak button) | ask |
| Each step of a run path | PS5 · Linux (the path's name) | status |
| A run path finished | PS5 · Linux is ready | done |
| A run path stopped | PS5 · Linux stopped | ask |

The Run jailbreak button sends `mobile_app_notification_action` with `PS5_AUTOJB_RUN`, and the package's
automation `ps5_autojb_run_from_notification` presses `button.ps5_autojb_launch`.

## On the TV

`script.ps5_autojb_tv_notice` gets each message's title and message, plus `image` (the screenshot's URL) on the ready
message. It does nothing until you wire it up. For example, with the "Notifications for Android TV / Fire TV"
integration on a Shield:

```yaml
  ps5_autojb_tv_notice:
    sequence:
    - action: notify.your_android_tv    # EDIT: your TV's notify service
      data:
        title: '{{ title }}'
        message: '{{ message }}'
        data: "{{ {'image': {'url': image}} if image | default('') else {} }}"
```

## Turning the PS5 off

`script.ps5_autojb_full_shutdown` asks the launcher for a full shutdown, never rest mode. The launcher navigates the
Power menu by OCR and selects Turn Off PS5 only after two matching reads. The request is valid for 30 seconds and
used once. Put it behind a confirmation in your dashboard.
