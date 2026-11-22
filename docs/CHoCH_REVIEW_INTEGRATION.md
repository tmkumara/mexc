# CHoCH review on the existing Telegram bot

The signal bot creates one Telegram application in `bot.build_app()` and
polls through the updater in `main.py`. The review integration mounts
namespaced commands and `choch_review:` callbacks on that application.
It uses the existing Telegram bot and token. The signal and report handlers,
scheduler, strategy, and trading configuration continue to use their existing
code paths. Do not start the standalone review poller with the live token.

The integration is disabled unless `MEXC_REVIEW_TELEGRAM_ALLOWED_USER_IDS`
contains explicitly configured numeric user IDs. Review handlers validate both
private chat and sender ID. An authorized private `/start` or `/help` gains
a short `/review` hint. All other welcome and help content stays the same.
The review handlers and live handlers share the same polling process.

## Configure before an approved deployment

Verify the numeric Telegram ID through an existing trusted bot administration
channel or Telegram Developer tools. Do not launch another poller or call
`getUpdates` independently on the active live bot token. If the numeric ID is
unavailable, keep review disabled until it is verified.

The default database is `/var/lib/mexc-choch-review/review.sqlite3`, outside
the release checkout. The deployed `mexc-bot` service runs as root. At the
approved deployment, provision the private directory if missing.

```bash
sudo install -d -m 0700 -o root -g root /var/lib/mexc-choch-review
sudo test -r /var/lib/mexc-choch-review && sudo test -w /var/lib/mexc-choch-review
```

If the service user changes, use that user and its group for provisioning
and run the access check as that user. Keep any existing database intact.
The first startup creates a new database with mode 0600. The integration
checks that both the directory and any existing database are private. The
optional storage override `MEXC_REVIEW_STORAGE_PATH` must be absolute and
outside the checkout.

Keep the allowlist in a dedicated systemd drop-in or add it to the existing
deployment environment without replacing existing values. The deployment
workflow regenerates the live `.env`, so do not rely on a manual edit to
that file surviving. For a drop-in, run `sudo systemctl edit mexc-bot` and add
the following, substituting the verified IDs. Apply it with
`sudo systemctl daemon-reload`. Do not restart the process until deployment
is approved.

```ini
[Service]
Environment="MEXC_REVIEW_TELEGRAM_ALLOWED_USER_IDS=<verified numeric IDs>"
```

No extra Telegram token is required. The optional `MEXC_REVIEW_QUEUE_PATH`
must point to a trusted prepared CSV beside its chart manifest and PNG folder.
Missing or unreadable review inputs disable review at startup and allow live
handlers to register normally.

## Deploy and verify after approval

1. In the integration checkout run
   `python -m unittest discover -s tests -p test_integrated_telegram_review.py`.
   From `review_toolkit` also run
   `python -m unittest discover -s tests -p test_telegram_review.py`.
2. Confirm the reviewed branch diff contains only the integration, adapter,
   documentation, and tests. Commit it, fast-forward or merge it into GitHub
   `main`, then watch the existing **Deploy to Server** workflow. It will
   restart the existing bot and dashboard. There is no separate service,
   poller, or workflow change.
3. On the server verify `git -C /opt/signals rev-parse HEAD` matches the
   approved commit. Check `systemctl is-active mexc-bot mexc-dashboard` and
   confirm `systemctl show mexc-bot -p MainPID -p NRestarts` reports one live
   main process and no unexpected restart loop. Check that the review directory
   and database are private and accessible by the service user.
4. Verify existing `/start`, `/help`, `/status`, and one performance command
   with the live bot. In an authorized private chat, use `/review` and verify
   the image and both classifications. Use `/review_progress` before and after
   a genuine review or use `/review_skip` for a navigation check. A reviewer
   can undo a deliberate test decision with `/review_undo`. Check from an
   unauthorized account or group that review is inaccessible. Verify normal
   signal delivery through its next scheduled outcome.
5. After a separately approved restart, reopen the case or run
   `/review_progress` to confirm persistence.

## Disable without restarting the bot

Create the marker beside the active SQLite file. Review authorization is
checked per interaction. Existing signal commands and scheduled delivery
continue through the same process.

```bash
sudo touch /var/lib/mexc-choch-review/disabled
```

Remove the marker only when re-enabling review is approved. For a startup
rollback, set `MEXC_REVIEW_ENABLED=0` or remove the allowlist in the service
environment before the next planned restart. Both rollback paths retain the
SQLite data. Keep the database and any exports private.
