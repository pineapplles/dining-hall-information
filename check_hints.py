"""Watch the Hints and Tracker sections of The Veritas Search; push a phone alert on change.

Env vars:
  NTFY_TOPIC        (required) your private ntfy topic
  INTERVAL_SECONDS  if set (min 20), run forever, checking this often. Unset = check once and exit.
  SITE_URL          default https://www.theveritassearch.com/
  HINTS_SELECTOR    optional CSS selector for the hints container
  TRACKER_SELECTOR  optional CSS selector for the tracker container
  IGNORE_REGEX      optional regex stripped before comparing (countdowns, timestamps)
  STATE_FILE        default state/state.json
  DEBUG_DUMP        if set, writes extracted text + screenshots to ./debug/
"""
import difflib
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = os.environ.get("SITE_URL", "https://www.theveritassearch.com/")
IGNORE = os.environ.get("IGNORE_REGEX", "").strip()
TOPIC = os.environ["NTFY_TOPIC"]
STATE = Path(os.environ.get("STATE_FILE", "state/state.json"))
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "0"))
FAIL_ALERT_AFTER = 10  # consecutive failures before you get a "watcher is broken" ping

SECTIONS = {
    "hints": {"label": "Hints", "selector": os.environ.get("HINTS_SELECTOR", "").strip()},
    "tracker": {"label": "Tracker", "selector": os.environ.get("TRACKER_SELECTOR", "").strip()},
}


def notify(title, body, priority="4"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Click": URL, "Priority": priority, "Tags": "mag"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # never let a notify hiccup kill the loop
        print(f"notify failed: {e}", file=sys.stderr)


def normalize(text):
    if IGNORE:
        text = re.sub(IGNORE, "", text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1200)


def extract(page, label, selector):
    """Open the section on a fresh page load and return its text."""
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    settle(page)
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)

    clicked = False
    for role in ("tab", "link", "button"):
        t = page.get_by_role(role, name=pattern)
        if t.count():
            t.first.click()
            clicked = True
            break
    if not clicked:
        t = page.get_by_text(pattern)
        if t.count() and not selector:
            # Could be a heading of an on-page section rather than a nav item.
            sec = t.first.locator("xpath=ancestor::section[1]")
            if sec.count():
                return sec.first.inner_text()
            t.first.click()
            clicked = True
    if clicked:
        settle(page)

    if selector:
        loc = page.locator(selector).first
        loc.wait_for(timeout=20000)
        return loc.inner_text()
    if not clicked:
        raise RuntimeError(f"Could not find a '{label}' tab/section. Set a CSS selector for it.")
    main = page.locator("main")
    return (main.first if main.count() else page.locator("body")).inner_text()


def load_state():
    if not STATE.exists():
        return {}
    data = json.loads(STATE.read_text())
    if "hash" in data:  # old single-section format
        data = {"hints": data}
    return data


def check_section(page, key, cfg, state):
    raw = extract(page, cfg["label"], cfg["selector"])
    if os.environ.get("DEBUG_DUMP"):
        Path("debug").mkdir(exist_ok=True)
        Path(f"debug/{key}.txt").write_text(raw)
        page.screenshot(path=f"debug/{key}.png", full_page=True)

    text = normalize(raw)
    if len(text) < 20:
        raise RuntimeError("extraction looks empty")
    digest = hashlib.sha256(text.encode()).hexdigest()

    if key not in state:
        state[key] = {"hash": digest, "text": text}
        notify(f"{cfg['label']} watcher is running",
               "Baseline saved. You'll get a ping when this section changes.", "3")
        print(f"[{key}] baseline saved")
        return
    if state[key]["hash"] == digest:
        print(f"[{key}] no change")
        return

    added = [ln[2:] for ln in difflib.ndiff(state[key]["text"].splitlines(), text.splitlines())
             if ln.startswith("+ ")]
    notify(f"Veritas Search: {cfg['label'].lower()} updated",
           "\n".join(added)[:600] or "Content changed.")
    state[key] = {"hash": digest, "text": text}
    print(f"[{key}] CHANGED, notified")


def run_cycle(fails):
    state = load_state()
    any_fail = False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1800})
        for key, cfg in SECTIONS.items():
            try:
                check_section(page, key, cfg, state)
                if fails.get(key, 0) >= FAIL_ALERT_AFTER:
                    notify(f"{cfg['label']} watcher recovered", "Working again.", "3")
                fails[key] = 0
            except Exception as e:
                any_fail = True
                fails[key] = fails.get(key, 0) + 1
                print(f"[{key}] ERROR ({fails[key]}x): {e}", file=sys.stderr)
                if fails[key] == FAIL_ALERT_AFTER:
                    notify(f"{cfg['label']} watcher is failing",
                           f"{FAIL_ALERT_AFTER} failed checks in a row: {str(e)[:200]}")
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
        browser.close()
    return any_fail


def main():
    fails = {}
    if not INTERVAL:
        return 1 if run_cycle(fails) else 0
    interval = max(20, INTERVAL)
    while True:
        try:
            run_cycle(fails)
        except Exception as e:  # browser crash etc.; keep going
            print(f"cycle crashed: {e}", file=sys.stderr)
        time.sleep(interval + random.uniform(0, 5))


if __name__ == "__main__":
    sys.exit(main())
