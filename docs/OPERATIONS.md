# Unattended operation

Public account identifiers let the monitor run while the hardware wallet is
offline. Provider uptime remains an external dependency.

## Continuous Telegram bot

Copy and edit the example service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/hwr-bot.service.example \
  ~/.config/systemd/user/hwr-bot.service
systemctl --user daemon-reload
systemctl --user enable --now hwr-bot.service
systemctl --user status hwr-bot.service
```

Enable user lingering if the bot must run while you are logged out:

```bash
loginctl enable-linger "$USER"
```

## Weekly one-shot report

Copy the service and timer:

```bash
cp deploy/systemd/hwr-check.service.example \
  ~/.config/systemd/user/hwr-check.service
cp deploy/systemd/hwr-check.timer.example \
  ~/.config/systemd/user/hwr-check.timer
systemctl --user daemon-reload
systemctl --user enable --now hwr-check.timer
systemctl --user list-timers hwr-check.timer
```

The example timer runs Sundays at 20:00 local time. Edit `OnCalendar` to
change the schedule. It passes `--no-prompt`, so each scheduled check uses a
zero top-up and cannot wait for interactive input. Telegram delivery is the
default and requires no send flag.

## Logs

```bash
journalctl --user -u hwr-bot.service
journalctl --user -u hwr-check.service
```

Reports contain portfolio values but not wallet identifiers. Treat logs as
financially sensitive.
