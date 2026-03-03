"""
Device QA Monitor (Playwright Edition)
=======================================
Monitors devices after power-on by scraping the GPX admin dashboard.

Detection: Checks LAST REPORT column. En dash means not reporting.
Any timestamp means reporting. Status column is ignored.

Auto-detect (--auto): Scans Devices page filtered by Inventory for
devices with no Last Report. Monitors them until they report or timeout.

Timeouts: Road Wired, AssetTrack Wired, Protect Plus get 150 min.
All other types get 20 min. Configurable in config.yaml.

Usage:
    python monitor.py --auto --visible        # first run (watch browser)
    python monitor.py --auto                  # headless
    python monitor.py --serials 275741 275740
    python monitor.py --csv devices.csv
"""

import argparse
import csv
import time
import sys
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
import requests
from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PWTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("device_qa_monitor")

# Characters GPX uses for "no report" — includes en dash and em dash
NO_REPORT_VALUES = ("-", "\u2013", "\u2014", "", "N/A", "None", "Never")


def load_config(path="config.yaml"):
    p = Path(path)
    if not p.exists():
        logger.error(f"Config not found: {path}. Copy config.example.yaml to config.yaml")
        sys.exit(1)
    with open(p) as f:
        config = yaml.safe_load(f)
    for key in ["gpx_username", "gpx_password", "slack_webhook_url"]:
        if not config.get(key):
            logger.error(f"Missing required config: {key}")
            sys.exit(1)
    return config


def get_timeout_for_device(device_type, config):
    slow_kw = config.get("slow_device_keywords", ["road wired", "assettrack wired", "protect plus"])
    slow_min = config.get("slow_device_timeout_minutes", 150)
    default_min = config.get("default_timeout_minutes", 20)
    if device_type:
        dt = device_type.lower()
        for kw in slow_kw:
            if kw.lower() in dt:
                return slow_min
    return default_min


def format_duration(seconds):
    if seconds < 60: return f"{int(seconds)}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60: return f"{m}m {s}s"
    return f"{m // 60}h {m % 60}m"


def clean_last_report(raw):
    """
    Clean the Last Report cell value from GPX.
    GPX format: 'Mar 3, 2026\\xa0 @ 1:08 PM\\n110 Shields Park Drive, ...'
    We want just: 'Mar 3, 2026 @ 1:08 PM'
    """
    if not raw:
        return raw
    # Take only the first line (before address)
    first_line = raw.split("\n")[0].strip()
    # Replace non-breaking spaces with regular spaces
    first_line = first_line.replace("\xa0", " ")
    # Collapse multiple spaces
    first_line = re.sub(r"\s+", " ", first_line).strip()
    return first_line


# ---------------------------------------------------------------------------
# GPX Dashboard
# ---------------------------------------------------------------------------

class GPXDashboard:
    def __init__(self, config):
        self.config = config
        self.base_url = config.get("gpx_base_url", "https://admin.gpx.co").rstrip("/")
        self.username = config["gpx_username"]
        self.password = config["gpx_password"]
        self.pw = None
        self.browser = None
        self.page = None

    def start(self):
        logger.info("Launching browser...")
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=self.config.get("headless", True))
        self.page = self.browser.new_page()
        self._login()

    def stop(self):
        if self.browser: self.browser.close()
        if self.pw: self.pw.stop()
        logger.info("Browser closed.")

    def _login(self):
        url = f"{self.base_url}/login"
        logger.info(f"Logging in at {url}...")
        self.page.goto(url, wait_until="networkidle")

        for sel in ['input[name="email"]', 'input[name="username"]', 'input[type="email"]',
                     'input[id="email"]', 'input[id="username"]']:
            try:
                el = self.page.wait_for_selector(sel, timeout=3000)
                if el: el.fill(self.username); break
            except PWTimeout: continue

        for sel in ['input[name="password"]', 'input[type="password"]', 'input[id="password"]']:
            try:
                el = self.page.wait_for_selector(sel, timeout=3000)
                if el: el.fill(self.password); break
            except PWTimeout: continue

        for sel in ['button[type="submit"]', 'input[type="submit"]',
                     'button:has-text("Log in")', 'button:has-text("Login")', 'button:has-text("Sign in")']:
            try:
                el = self.page.wait_for_selector(sel, timeout=3000)
                if el: el.click(); break
            except PWTimeout: continue

        try: self.page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout: pass

        try:
            self.page.wait_for_selector(
                ':has-text("Hello"),:has-text("Devices"),nav,.sidebar,aside', timeout=10000)
            logger.info("Logged in to GPX successfully.")
        except PWTimeout:
            logger.error("Login failed. Check credentials. Run with --visible.")
            sys.exit(1)

    # ------------------------------------------------------------------
    # Navigate to Inventory-filtered Devices page
    # ------------------------------------------------------------------

    def go_to_inventory_devices(self):
        """Navigate to the Devices page and apply the Inventory filter."""
        self.page.goto(f"{self.base_url}/devices", wait_until="networkidle", timeout=20000)
        time.sleep(1.5)
        self._apply_inventory_filter()

    def _apply_inventory_filter(self):
        try:
            # Try select dropdown
            for sel in ['select:near(:text("Status"))', 'select:has(option:text("Inventory"))',
                        '[class*="filter"] select']:
                try:
                    el = self.page.wait_for_selector(sel, timeout=2000)
                    if el:
                        el.select_option(label="Inventory")
                        logger.info("Applied Inventory filter (select).")
                        try: self.page.wait_for_load_state("networkidle", timeout=10000)
                        except PWTimeout: pass
                        time.sleep(1.5)
                        return
                except PWTimeout: continue

            # Try clickable dropdown
            for sel in [':text("Status")', 'button:has-text("Status")',
                        '[class*="filter"]:has-text("Status")']:
                try:
                    el = self.page.wait_for_selector(sel, timeout=2000)
                    if el:
                        el.click()
                        time.sleep(0.5)
                        for opt in [':text("Inventory")', 'option:text("Inventory")',
                                    'li:text("Inventory")', '[role="option"]:text("Inventory")']:
                            try:
                                o = self.page.wait_for_selector(opt, timeout=2000)
                                if o:
                                    o.click()
                                    logger.info("Applied Inventory filter (click).")
                                    try: self.page.wait_for_load_state("networkidle", timeout=10000)
                                    except PWTimeout: pass
                                    time.sleep(1.5)
                                    return
                            except PWTimeout: continue
                except PWTimeout: continue

            # URL fallback
            logger.warning("Filter UI not found. Trying URL parameter...")
            self.page.goto(f"{self.base_url}/devices?status=Inventory",
                           wait_until="networkidle", timeout=20000)
            time.sleep(1.5)
        except Exception as e:
            logger.warning(f"Filter error: {e}")

    # ------------------------------------------------------------------
    # Scan the Inventory table (used for both auto-detect and monitoring)
    # ------------------------------------------------------------------

    def scan_inventory_table(self, target_ids=None):
        """
        Scan the Inventory-filtered Devices table across all pages.

        Returns a dict: {device_id: {"has_report": bool, "last_report": str, "device_type": str}}

        If target_ids is provided, only tracks those devices and stops scanning
        once all have been found (or pages run out).
        """
        results = {}
        page_num = 1
        max_pages = self.config.get("auto_detect_max_pages", 10)
        target_set = set(target_ids) if target_ids else None
        found_targets = set()

        while page_num <= max_pages:
            if page_num > 1:
                # Already on page 1 from go_to_inventory_devices
                pass

            time.sleep(1)
            page_devices = self._read_table_page()

            if page_devices is None:
                logger.warning(f"Could not read table on page {page_num}.")
                break

            unreported = 0
            reported = 0
            for d in page_devices:
                results[d["id"]] = d
                if d["has_report"]:
                    reported += 1
                else:
                    unreported += 1

                if target_set and d["id"] in target_set:
                    found_targets.add(d["id"])

            logger.info(f"  Page {page_num}: {unreported} unreported, {reported} already reported")

            # If tracking specific devices and we found them all, stop
            if target_set and found_targets == target_set:
                logger.info("Found all target devices.")
                break

            # If no unreported on this page and no target_ids, stop
            # (newest first, so remaining pages are all older/reported)
            if not target_set and unreported == 0 and len(page_devices) > 0:
                logger.info("All devices on this page have reported. Done scanning.")
                break

            # Go to next page
            if not self._go_next_page():
                logger.info("No more pages.")
                break
            page_num += 1

        return results

    def _read_table_page(self):
        """Read all device rows on the current table page."""
        try:
            # GPX uses <a> tags as table rows, not <tr>
            rows = self.page.query_selector_all("table tbody a")
            if not rows:
                rows = self.page.query_selector_all("a:has(td)")
            if not rows:
                return None

            devices = []
            for row in rows:
                cells = row.query_selector_all("td")
                if len(cells) < 3:
                    continue

                device_name = cells[0].inner_text().strip()
                last_report_raw = cells[1].inner_text().strip()
                device_type = cells[2].inner_text().strip()

                # Check status column (index 6) to skip Reserved
                status = ""
                if len(cells) >= 7:
                    status = cells[6].inner_text().strip().lower()
                if "reserved" in status:
                    continue
                if status and "inventory" not in status:
                    continue

                has_report = last_report_raw not in NO_REPORT_VALUES
                last_report_clean = clean_last_report(last_report_raw) if has_report else None

                devices.append({
                    "id": device_name,
                    "device_type": device_type,
                    "has_report": has_report,
                    "last_report": last_report_clean,
                })
            return devices
        except Exception as e:
            logger.warning(f"Table read error: {e}")
            return None

    def _go_next_page(self):
        for sel in ['button:has-text("Next")', 'a:has-text("Next")',
                    '[aria-label="Next"]', ':text("Next")']:
            try:
                btn = self.page.wait_for_selector(sel, timeout=2000)
                if btn and btn.is_enabled():
                    btn.click()
                    try: self.page.wait_for_load_state("networkidle", timeout=10000)
                    except PWTimeout: pass
                    time.sleep(1)
                    return True
            except PWTimeout: continue
        return False

    # ------------------------------------------------------------------
    # Go back to page 1 of the filtered table
    # ------------------------------------------------------------------

    def go_to_first_page(self):
        """Navigate back to page 1 of the Inventory-filtered Devices list."""
        # Try clicking page 1 button
        for sel in ['a:has-text("1")', 'button:has-text("1")']:
            try:
                btns = self.page.query_selector_all(sel)
                for btn in btns:
                    txt = btn.inner_text().strip()
                    if txt == "1":
                        btn.click()
                        try: self.page.wait_for_load_state("networkidle", timeout=10000)
                        except PWTimeout: pass
                        time.sleep(1)
                        return
            except: continue
        # Fallback: re-navigate
        self.go_to_inventory_devices()


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def send_slack(url, text, blocks=None):
    payload = {"text": text}
    if blocks: payload["blocks"] = blocks
    try: requests.post(url, json=payload, timeout=10).raise_for_status()
    except requests.exceptions.RequestException as e: logger.error(f"Slack failed: {e}")

def sblock(text):
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

def notify_online(did, elapsed, last_seen, dtype, config):
    t = f"\n:gear: Type: {dtype}" if dtype else ""
    r = f"\n:satellite: Last report: {last_seen}" if last_seen else ""
    send_slack(config["slack_webhook_url"], f"Device {did} reporting ({elapsed})",
        sblock(f":white_check_mark: Device `{did}` is now *reporting*\n:stopwatch: {elapsed}{t}{r}"))

def notify_timeout(did, mins, dtype, config):
    t = f"\n:gear: Type: {dtype}" if dtype else ""
    send_slack(config["slack_webhook_url"], f"Device {did} NOT reporting after {mins}m",
        sblock(f":rotating_light: Device `{did}` *NOT reported* after {mins} min{t}\n"
               f":warning: Needs QA investigation"))

def notify_started(serials, device_types, config, auto_detected=False):
    count = len(serials)
    preview = ", ".join(serials[:5])
    if count > 5: preview += f", ... (+{count-5} more)"
    mode = "Auto-detected" if auto_detected else "Manual"
    type_counts = {}
    for dt in device_types.values():
        name = dt or "Unknown"
        type_counts[name] = type_counts.get(name, 0) + 1
    type_line = ""
    if type_counts:
        type_line = "\n:gear: Types: " + ", ".join(f"{v}x {k}" for k, v in type_counts.items())
    send_slack(config["slack_webhook_url"], f"QA Monitor started — {count} device(s)",
        sblock(f":clipboard: *QA Monitor Started* ({mode})\nTracking *{count}* device(s)\n"
               f"Unreported Devices: `{preview}`{type_line}"))

def notify_complete(summary, config):
    p, f, t, d = summary["passed"], summary["failed"], summary["total"], summary["duration"]
    e = ":tada:" if f == 0 else ":warning:"
    send_slack(config["slack_webhook_url"], f"QA Complete — {p}/{t} passed, {f} failed ({d})",
        sblock(f"{e} *QA Monitor Complete*\n:white_check_mark: Passed: *{p}/{t}*\n"
               f":x: Failed: *{f}*\n:stopwatch: {d}"))
    if summary.get("failed_serials"):
        items = "\n".join(f"  \u2022 `{s}`" for s in summary["failed_serials"])
        send_slack(config["slack_webhook_url"], f"Failed: {', '.join(summary['failed_serials'])}",
            sblock(f"*Devices needing investigation:*\n{items}"))

def notify_no_devices(config):
    send_slack(config["slack_webhook_url"], "QA Monitor — no unreported devices found",
        sblock(":mag: *QA Monitor*\nNo unreported Inventory devices found. "
               "All devices are already reporting or none are provisioned yet."))


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def load_from_csv(filepath):
    path = Path(filepath)
    if not path.exists(): logger.error(f"CSV not found: {filepath}"); sys.exit(1)
    serials = []
    with open(path) as f:
        reader = csv.DictReader(f)
        headers = [h.lower().strip() for h in (reader.fieldnames or [])]
        col = None
        for c in ["device_name","device_id","serial","serial_number","sn","device_serial","id"]:
            if c in headers: col = reader.fieldnames[headers.index(c)]; break
        if col is None:
            logger.warning("No recognized ID column — using first column.")
            f.seek(0); r2 = csv.reader(f); next(r2)
            serials = [row[0].strip() for row in r2 if row and row[0].strip()]
        else:
            for row in reader:
                v = row.get(col, "").strip()
                if v: serials.append(v)
    logger.info(f"Loaded {len(serials)} device ID(s) from {filepath}")
    return serials

def get_interactive():
    print("\nEnter device IDs (one per line, blank line to finish):\n")
    serials = []
    while True:
        line = input("  Device ID: ").strip()
        if not line: break
        serials.append(line)
    return serials


# ---------------------------------------------------------------------------
# Monitor Loop
# ---------------------------------------------------------------------------

def monitor_devices(dash, device_list, config, auto_detected=False):
    """
    Monitor devices by repeatedly scanning the Inventory table.
    Uses the same browser session (dash) — no second login needed.
    """
    poll_interval = config.get("poll_interval_seconds", 30)
    pending = {}
    device_types = {}

    for item in device_list:
        if isinstance(item, dict):
            did, dtype = item["id"], item.get("device_type")
        else:
            did, dtype = str(item), None
        timeout = get_timeout_for_device(dtype, config) if dtype else config.get("default_timeout_minutes", 20)
        pending[did] = {"start": datetime.now(), "type": dtype, "timeout": timeout}
        device_types[did] = dtype

    passed, failed = [], []
    total = len(pending)
    if total == 0:
        logger.info("No devices to monitor.")
        return

    logger.info(f"Monitoring {total} device(s) | Poll: {poll_interval}s")
    logger.info(f"Default timeout: {config.get('default_timeout_minutes', 20)}m | "
                f"Slow timeout: {config.get('slow_device_timeout_minutes', 150)}m")
    for did, info in pending.items():
        logger.info(f"  {did}: Type={info['type'] or 'Unknown'} | Timeout={info['timeout']}m")

    notify_started(list(pending.keys()), device_types, config, auto_detected)
    batch_start = datetime.now()

    while pending:
        logger.info(f"Checking {len(pending)} pending device(s)...")

        # Go back to page 1 of the Inventory table and scan
        dash.go_to_first_page()
        table_data = dash.scan_inventory_table(target_ids=list(pending.keys()))

        now = datetime.now()
        pending_ids = list(pending.keys())

        for did in pending_ids:
            info = pending[did]
            device_data = table_data.get(did)

            if not device_data:
                elapsed_so_far = format_duration((now - info["start"]).total_seconds())
                logger.info(f"  [-] {did}: Not found in table ({elapsed_so_far} elapsed)")
            elif device_data.get("has_report"):
                # Device has reported!
                elapsed = format_duration((now - info["start"]).total_seconds())
                last_seen = device_data.get("last_report", "")
                logger.info(f"  [+] {did} REPORTING ({elapsed}) — {last_seen}")
                passed.append(did)
                del pending[did]
                notify_online(did, elapsed, last_seen, info.get("type"), config)
            else:
                elapsed_so_far = format_duration((now - info["start"]).total_seconds())
                logger.info(f"  [-] {did}: No report yet ({elapsed_so_far} elapsed)")

            # Check timeout (whether found in table or not)
            if did in pending and now - info["start"] > timedelta(minutes=info["timeout"]):
                logger.warning(f"  [X] {did} TIMED OUT ({info['timeout']}m)")
                failed.append(did)
                del pending[did]
                notify_timeout(did, info["timeout"], info.get("type"), config)

        done = len(passed) + len(failed)
        logger.info(f"Progress: {done}/{total} | {len(passed)} passed | "
                    f"{len(failed)} failed | {len(pending)} pending")

        if pending:
            logger.info(f"Next check in {poll_interval}s...")
            time.sleep(poll_interval)

    duration = format_duration((datetime.now() - batch_start).total_seconds())
    logger.info("=" * 50)
    logger.info(f"COMPLETE — {len(passed)} passed, {len(failed)} failed ({duration})")
    if failed: logger.info(f"Failed: {', '.join(failed)}")
    logger.info("=" * 50)
    notify_complete({"total": total, "passed": len(passed), "failed": len(failed),
                     "failed_serials": failed, "duration": duration}, config)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="QA Monitor — GPX dashboard via Playwright")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--auto", action="store_true", help="Auto-detect unreported Inventory devices")
    group.add_argument("--csv", help="CSV file with device IDs")
    group.add_argument("--serials", nargs="+", help="Device IDs to monitor")
    group.add_argument("--interactive", action="store_true", help="Enter IDs manually")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--visible", action="store_true", help="Show browser for debugging")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.visible: config["headless"] = False

    # Start browser (one login for everything)
    dash = GPXDashboard(config)
    dash.start()

    try:
        if args.auto:
            # Auto-detect: scan Inventory table for unreported devices
            dash.go_to_inventory_devices()
            logger.info("Scanning for unreported devices...")
            table_data = dash.scan_inventory_table()

            # Filter to unreported only
            unreported = [
                {"id": did, "device_type": d["device_type"]}
                for did, d in table_data.items()
                if not d["has_report"]
            ]

            if not unreported:
                logger.info("No unreported Inventory devices found.")
                notify_no_devices(config)
                sys.exit(0)

            logger.info(f"Found {len(unreported)} unreported device(s):")
            for d in unreported:
                logger.info(f"  {d['id']} ({d.get('device_type', 'Unknown')})")

            monitor_devices(dash, unreported, config, auto_detected=True)

        else:
            # Manual mode
            if args.csv: serials = load_from_csv(args.csv)
            elif args.serials: serials = args.serials
            else: serials = get_interactive()
            if not serials: logger.error("No device IDs provided."); sys.exit(1)

            seen, unique = set(), []
            for s in serials:
                if s not in seen: seen.add(s); unique.append(s)
            if len(unique) < len(serials):
                logger.warning(f"Removed {len(serials)-len(unique)} duplicate(s)")

            # For manual mode, navigate to Inventory page for table scanning
            dash.go_to_inventory_devices()
            monitor_devices(dash, unique, config)

    finally:
        dash.stop()


if __name__ == "__main__":
    main()
