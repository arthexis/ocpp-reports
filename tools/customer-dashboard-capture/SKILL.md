---
name: customer-dashboard-capture
description: Capture authenticated configured customer Watchtower dashboards on desktop Chromium or an authorized USB Android browser, with private credentials and provenance manifests. Use for dashboard screenshots and browser acceptance evidence.
---

# Customer dashboard captures

Use `customer-charger-ops` for production context. Screenshots are read-only dashboard access; do not restart servers, rotate credentials, install phone software, or change source scopes to obtain them.

## One-command capture and upload

```bash
ocpp-dashboard-shots 108
ocpp-dashboard-shots https://github.com/OWNER/REPO/pull/123
ocpp-dashboard-shots 123 --repo OWNER/REPO
ocpp-dashboard-shots 108 --check
```

`108` defaults to `arthexis/ocpp-reports`. Both issue and PR URLs are supported. The command requires an authorized, unlocked Android device, captures each configured tenant on actual Android and desktop Chromium, verifies titles/scopes and displayed phone URL/title, then uploads the complete set of PNGs and their manifest to a unique evidence branch and posts an illustrated comment. Missing, unauthorized, locked, or interrupted Android capture stops with exit code 2 and no upload. An already unlocked sleeping phone may be woken; keyguard is never dismissed. No emulation fallback or partial publication is allowed.

The standalone CLI uses the installed authenticated `gh` command, passing upload bodies through stdin. It verifies server-stored blob digests before creating the evidence branch/comment. `--serial` selects among connected devices; `--output` sets the evidence root. Private unique run directories and a `receipt.json` record the artifacts, evidence commit/branch, and final comment URL. On an upload failure, inspect the receipt before rerunning to avoid duplicate evidence; there is no automatic retry of mutations. An exclusive local lock prevents simultaneous runs from switching the phone between dashboards.

The canonical source is now in `tools/customer-dashboard-capture/` in ocpp-reports. The installed wrappers must be updated only after separately authorized deployment. The implementation lives in `scripts/shots.py`, with the existing capture library in `scripts/capture.py`. Its installed wrapper is `/home/arthe/.local/bin/ocpp-dashboard-shots`. Verify stop/publication behavior with the launcher's Python environment: `python -m unittest -v test_shots` from `scripts/`.

## Individual captures

First try the Browser skill's supported connection. When none is available, the host's existing Playwright/Chromium launcher provides authenticated desktop access. Do not inspect browser profiles/cookies. This helper reads the dedicated per-tenant integrated-login password in memory and verifies the expected title and source scope before capturing.

```bash
/home/arthe/.local/share/ocpp-dashboard-launcher/venv/bin/python \
  /home/arthe/.codex/skills/customer-dashboard-capture/scripts/capture.py \
  --mode desktop --site all --output /home/arthe/screenshots/customer-watchtower
```

`--mode mobile-emulated` is a responsive Chromium viewport, **not physical Android acceptance**. `--mode android` requires exactly one authorized `adb devices -l` entry (or `--serial` selecting one). If no phone is attached or its state is unauthorized, ask the operator to connect/unlock/authorize it while continuing desktop work. Never infer readiness from an old device record or USB enumeration.

Android mode opens the public dashboard URL in the installed Chrome browser, uses a temporary local ADB port forward and same-origin integrated form login, validates the rendered page and source scope, then captures the actual phone display using `adb exec-out screencap -p`. It removes its port forward afterward. Require an unlocked phone; do not bypass a lock screen. It does not install applications or place secrets in URLs, arguments, files on the phone, or console output.

Use the same command with `--mode android`. Android mode needs an available Chrome DevTools socket; report absence rather than claiming an emulated capture came from the phone. Revalidate the current attachment each run. The path was field validated on a Samsung SM-G950F, Android 9, Chrome 101 on 2026-10-30. Use native DOM reads for title/scope checks because recent Playwright locator visibility checks require `element.checkVisibility`, absent in this browser. Always bring the controlled tab to the foreground and wait for visibility/paint before native screencap: a background tab can pass HTTP/DOM checks while the phone still displays about:blank.

Each run writes private PNG files and a JSON manifest with UTC time, origin, capture mode, viewport, and image digest. Dashboard summaries are appropriate; credentials, auth dialogs, notification panels and full customer event/report records are not. The automated workflow checks Chrome foreground focus plus the actual displayed URL/title through Android UI hierarchy before native capture; desktop captures end before record tables. Visually inspect images when diagnosing or changing the capture implementation. Avoid opening transaction detail pages for a summary capture.

Upload only when requested. Prefer an attachment-capable GitHub browser/API. The current GitHub connector can post comments but has no issue image-upload operation; if unavailable, an isolated evidence branch in the same repository can hold reviewed PNGs and the manifest, linked from the issue comment. Do not modify main or open a deployment PR for evidence. Verify uploaded blob digests and comment links, and distinguish desktop, emulation and real Android provenance in the comment.

## Integrated-login accounts

Basic Auth credentials no longer work. The operator must privately provision
`~/.config/ocpp-dashboard/integrated-accounts.json` (owner-only regular file,
mode 0600), with the following placeholder shape:

```json
{"sites":{"tenant-a":{"origin":"https://tenant-a.example.test","title":"Tenant A Dashboard","source_ids":["source-a"],"username":"<user>","password":"<private>"}}}
```

Do not commit credentials or pass them in CLI arguments. `--check` only verifies
local account-file availability, not successful login. Sessions remain in
nonpersistent browser contexts; dashboard title and source scope are checked
before any capture or publication.
