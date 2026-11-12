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
| Volleyball | **Team** → add a team, then players (jersey #, name, position). **New** picks the team and you tap the starting six in serve order. Court cells show number + name; `SUB ⇄#4` marks a player who came in for #4. The **bench** strip under the court lists everyone not on court (`only for #4` = may only return for #4). Sub: tap `Sub`, tap the player going OUT, tap the green bench player coming IN (`+ #` for someone not on the roster). Pairing and subs-per-set limit are warned, not blocked. Tap a score half to award a rally; tap a player, then a stat: Kill/Ace/Block score for home, Err for away, Dig is stat-only. Serve and rotation are automatic; `Serve ⇄` fixes the server without rotating. Stats lists names and every substitution. Autosaves every action. |
| Counter | Big `+`; tap a row to select, tap the selected row to rename, swipe to delete. |
| Grades | Classes are weighted (weight = % of final) or points-based. Add future work with a blank score to get floor/ceiling and "need on rest". Scale and prior GPA/credits under `Scale`. |
| Network Scan | Single-threaded TCP-connect sweep (no ICMP on iOS), 64 connects in flight. Tap a host for a live latency graph. Allow Local Network on first run. |
| BLE Scan | Raw CoreBluetooth via objc_util. Rescans every 2 s for fresh RSSI. Tap rows to pin them to the graph; ⓘ shows adv data. |
| Classify | **Camera** or **Photo**, then Apple's Vision framework runs on the phone (no network, no key). **Labels**: whole-image classes (~1,300 built in) with confidence bars. **Objects**: detected regions (salient objects, animals, people, faces) boxed on the photo, each named by classifying its crop. **Copy** exports results; **Tag** sends the photo and detections to Tagger. iOS 13+. |
| Tagger | **Camera**/**Photo**, then drag on the photo to box an object: the crop is classified and a tag picker opens (suggestions, your recent tags, or type one). **Detect** proposes boxes automatically; dashed = unconfirmed until you **Tag** it. Tap a box to select, drag to move, corner dot to resize. Autosaves to a library with thumbnails; **Tags** filters by tag. **More**: annotated copy to Photos, JSON export, whole-photo labels. Stored in `data/tagger/<id>/`. |

## Diagnostics

`data/logs/pockettools.log` (flushed per record) and `data/logs/crash.log` (faulthandler stacks on native crashes), both viewable under **Logs**. The launcher flags a session that didn't close cleanly.

**Auto-upload:** Logs → Upload → Settings, paste a fine-grained GitHub token scoped to one repo with *Contents: read and write*. After that the log tail plus crash dumps are committed to `logs/<device>.log` on the `device-logs` branch (created from `main` on first upload) on every launch, a few seconds after any error (max once a minute), and on close. The token stays in `data/logupload/config.json`. Logs include app activity (scanned hosts, BLE names), so if this repo is public, point the upload at a private repo instead.

Deep link: Shortcuts → Run Pythonista Script → `launcher.py` with argument `volley`, `count`, `grades`, `net`, `ble`, `classify` or `tag`.
