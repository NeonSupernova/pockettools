# pockettools

A Pythonista 3 launcher hosting small iPhone tools in one process.

## Install

Paste into the Pythonista console:

```python
import requests; exec(requests.get('https://raw.githubusercontent.com/NeonSupernova/pockettools/main/install.py').text)
```

Swipe Pythonista away, reopen, and run `pockettools/launcher.py`.

## Updates

On every launch a background thread asks GitHub for the head of `main` (ETag-conditional, so it doesn't eat the anonymous rate limit). A new commit is downloaded pinned to its SHA, every `.py` is compiled before anything is replaced, files swap in one by one with `os.replace`, and the previous version is kept in `data/update/backup/`. It takes effect on the next launch. **Update** on the launcher checks on demand; `AUTO_UPDATE = False` in `launcher.py` pins the current version. `data/` is never touched.

## Apps

| App | Notes |
|---|---|
| Volleyball | Tap a score half to award a rally. Tap a player, then a stat: Kill/Ace/Block also score for home, Err scores for away, Dig is stat-only. Serve and home rotation are automatic on side-out; `Serve ⇄` fixes the server without rotating. Sub: tap `Sub`, then the player going out. Autosaves every action. |
| Counter | Big `+`; tap a row to select, tap the selected row to rename, swipe to delete. |
| Grades | Classes are weighted (weight = % of final) or points-based. Add future work with a blank score to get floor/ceiling and "need on rest". Scale and prior GPA/credits under `Scale`. |
| Network Scan | Single-threaded TCP-connect sweep (no ICMP on iOS), 64 connects in flight. Tap a host for a live latency graph. Allow Local Network on first run. |
| BLE Scan | Raw CoreBluetooth via objc_util. Rescans every 2 s for fresh RSSI. Tap rows to pin them to the graph; ⓘ shows adv data. |

## Diagnostics

`data/logs/pockettools.log` (flushed per record) and `data/logs/crash.log` (faulthandler stacks on native crashes), both viewable under **Logs**. The launcher flags a session that didn't close cleanly.

Deep link: Shortcuts → Run Pythonista Script → `launcher.py` with argument `volley`, `count`, `grades`, `net` or `ble`.
