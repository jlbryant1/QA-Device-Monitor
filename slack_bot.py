"""
QA Monitor Slack Bot
====================
Runs in the background and listens for @mentions in Slack.
When someone mentions the bot, it triggers the device QA monitor.

Commands (mention the bot + one of these):
    @QA Monitor run          - Auto-detect and monitor unreported devices
    @QA Monitor run 274722 274721  - Monitor specific devices
    @QA Monitor status       - Show current monitoring status
    @QA Monitor stop         - Stop the current monitoring run

Setup:
    1. Create a Slack app with Socket Mode (see README)
    2. Add SLACK_BOT_TOKEN and SLACK_APP_TOKEN to config.yaml
    3. Run: python slack_bot.py

Requires: pip install slack-bolt
"""

import re
import threading
import time
import logging
from datetime import datetime

import yaml
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Import the monitor components
from monitor import (
    GPXDashboard, load_config, get_timeout_for_device, format_duration,
    notify_online, notify_timeout, notify_started, notify_complete,
    NO_REPORT_VALUES, clean_last_report,
)
from datetime import timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("qa_slack_bot")


# ---------------------------------------------------------------------------
# Global state for tracking active monitor runs
# ---------------------------------------------------------------------------

class MonitorState:
    def __init__(self):
        self.running = False
        self.stop_requested = False
        self.thread = None
        self.pending_count = 0
        self.passed_count = 0
        self.failed_count = 0
        self.total_count = 0
        self.started_at = None
        self.passed = []
        self.failed = []

    def reset(self):
        self.running = False
        self.stop_requested = False
        self.thread = None
        self.pending_count = 0
        self.passed_count = 0
        self.failed_count = 0
        self.total_count = 0
        self.started_at = None
        self.passed = []
        self.failed = []


state = MonitorState()


# ---------------------------------------------------------------------------
# Monitor runner (runs in a background thread)
# ---------------------------------------------------------------------------

def run_monitor(config, channel_id, app, device_ids=None):
    """
    Run the QA monitor. Called in a background thread.
    Posts updates to the Slack channel.
    """
    state.running = True
    state.stop_requested = False
    state.started_at = datetime.now()

    dash = GPXDashboard(config)

    try:
        dash.start()
        dash.go_to_inventory_devices()

        if device_ids:
            # Manual mode: monitor specific devices
            logger.info(f"Monitoring specific devices: {device_ids}")
            # Scan table to get device types
            table_data = dash.scan_inventory_table(target_ids=device_ids)
            device_list = []
            for did in device_ids:
                d = table_data.get(did, {})
                device_list.append({
                    "id": did,
                    "device_type": d.get("device_type"),
                })
        else:
            # Auto mode: find unreported devices
            logger.info("Auto-detecting unreported devices...")
            table_data = dash.scan_inventory_table()
            device_list = [
                {"id": did, "device_type": d["device_type"]}
                for did, d in table_data.items()
                if not d["has_report"]
            ]

        if not device_list:
            app.client.chat_postMessage(
                channel=channel_id,
                text=":mag: No unreported Inventory devices found. All devices are already reporting."
            )
            state.reset()
            dash.stop()
            return

        # Set up tracking
        poll_interval = config.get("poll_interval_seconds", 30)
        pending = {}
        device_types = {}

        for item in device_list:
            did = item["id"]
            dtype = item.get("device_type")
            timeout = get_timeout_for_device(dtype, config) if dtype else config.get("default_timeout_minutes", 20)
            pending[did] = {"start": datetime.now(), "type": dtype, "timeout": timeout}
            device_types[did] = dtype

        state.total_count = len(pending)
        state.pending_count = len(pending)
        state.passed = []
        state.failed = []

        # Build start message
        count = len(pending)
        preview = ", ".join(list(pending.keys())[:5])
        if count > 5:
            preview += f", ... (+{count - 5} more)"

        type_counts = {}
        for dt in device_types.values():
            name = dt or "Unknown"
            type_counts[name] = type_counts.get(name, 0) + 1
        type_summary = ", ".join(f"{v}x {k}" for k, v in type_counts.items())

        app.client.chat_postMessage(
            channel=channel_id,
            text=f":clipboard: *QA Monitor Started*\n"
                 f"Tracking *{count}* device(s)\n"
                 f"Devices: `{preview}`\n"
                 f":gear: Types: {type_summary}\n"
                 f"_Polling every {poll_interval}s..._"
        )


        # --- Monitor loop ---
        while pending and not state.stop_requested:
            logger.info(f"Checking {len(pending)} pending device(s)...")
            dash.go_to_first_page()
            table_data = dash.scan_inventory_table(target_ids=list(pending.keys()))
            now = datetime.now()

            for did in list(pending.keys()):
                if state.stop_requested:
                    break

                info = pending[did]
                device_data = table_data.get(did)

                if device_data and device_data.get("has_report"):
                    elapsed = format_duration((now - info["start"]).total_seconds())
                    last_seen = device_data.get("last_report", "")
                    logger.info(f"  [+] {did} REPORTING ({elapsed})")
                    state.passed.append(did)
                    state.passed_count += 1
                    del pending[did]
                    state.pending_count = len(pending)
                    notify_online(did, elapsed, last_seen, info.get("type"), config)

                elif did in pending and now - info["start"] > timedelta(minutes=info["timeout"]):
                    logger.warning(f"  [X] {did} TIMED OUT ({info['timeout']}m)")
                    state.failed.append(did)
                    state.failed_count += 1
                    del pending[did]
                    state.pending_count = len(pending)
                    notify_timeout(did, info["timeout"], info.get("type"), config)

            state.pending_count = len(pending)
            done = state.passed_count + state.failed_count
            logger.info(f"Progress: {done}/{state.total_count} | "
                        f"{state.passed_count} passed | {state.failed_count} failed | "
                        f"{len(pending)} pending")

            if pending and not state.stop_requested:
                time.sleep(poll_interval)

        # --- Done ---
        duration = format_duration((datetime.now() - state.started_at).total_seconds())

        if state.stop_requested:
            app.client.chat_postMessage(
                channel=channel_id,
                text=f":stop_sign: *QA Monitor Stopped*\n"
                     f":white_check_mark: Passed: {state.passed_count} | "
                     f":x: Failed: {state.failed_count} | "
                     f":hourglass: Remaining: {len(pending)}\n"
                     f":stopwatch: Ran for: {duration}"
            )
        else:
            emoji = ":tada:" if state.failed_count == 0 else ":warning:"
            app.client.chat_postMessage(
                channel=channel_id,
                text=f"{emoji} *QA Monitor Complete*\n"
                     f":white_check_mark: Passed: *{state.passed_count}/{state.total_count}*\n"
                     f":x: Failed: *{state.failed_count}*\n"
                     f":stopwatch: Total time: {duration}"
            )
            if state.failed:
                failed_list = "\n".join(f"  \u2022 `{s}`" for s in state.failed)
                app.client.chat_postMessage(
                    channel=channel_id,
                    text=f"*Devices needing investigation:*\n{failed_list}"
                )


    except Exception as e:
        logger.error(f"Monitor error: {e}")
        app.client.chat_postMessage(
            channel=channel_id,
            text=f":x: *Monitor Error*\n```{str(e)}```"
        )
    finally:
        dash.stop()
        state.reset()


# ---------------------------------------------------------------------------
# Slack Bot Setup
# ---------------------------------------------------------------------------

def create_app(config):
    bot_token = config.get("slack_bot_token")
    app_token = config.get("slack_app_token")

    if not bot_token or not app_token:
        logger.error("Missing slack_bot_token or slack_app_token in config.yaml")
        logger.error("See README for Slack app setup instructions.")
        raise SystemExit(1)

    app = App(token=bot_token)

    @app.event("app_mention")
    def handle_mention(event, say):
        """Handle @QA Monitor mentions in Slack."""
        text = event.get("text", "").lower()
        channel = event.get("channel")
        user = event.get("user")

        # Strip the bot mention to get the command
        # Text looks like: "<@U1234567> run" or "<@U1234567> run 274722 274721"
        command = re.sub(r"<@\w+>\s*", "", text).strip()

        if command.startswith("run"):
            if state.running:
                say(":warning: A monitor run is already in progress. "
                    "Use `@QA Monitor status` to check progress or `@QA Monitor stop` to cancel.")
                return

            # Check for specific device IDs after "run"
            parts = command.split()
            device_ids = None
            if len(parts) > 1:
                device_ids = [p.strip() for p in parts[1:] if p.strip().isdigit()]
                if not device_ids:
                    device_ids = None

            if device_ids:
                say(f":rocket: Starting QA monitor for {len(device_ids)} specific device(s)...")
            else:
                say(":rocket: Starting QA monitor in auto-detect mode... scanning for unreported devices.")

            # Run in background thread
            state.thread = threading.Thread(
                target=run_monitor,
                args=(config, channel, app, device_ids),
                daemon=True,
            )
            state.thread.start()

        elif command.startswith("status"):
            if not state.running:
                say(":zzz: No monitor run is active.")
                return

            elapsed = format_duration((datetime.now() - state.started_at).total_seconds())
            say(f":bar_chart: *Monitor Status*\n"
                f":white_check_mark: Passed: {state.passed_count}\n"
                f":x: Failed: {state.failed_count}\n"
                f":hourglass: Pending: {state.pending_count}\n"
                f"Total: {state.total_count} | Running for: {elapsed}")

        elif command.startswith("stop"):
            if not state.running:
                say(":zzz: No monitor run is active.")
                return

            state.stop_requested = True
            say(":stop_sign: Stopping monitor after current check completes...")

        elif command.startswith("help") or command == "":
            say(":wave: *QA Monitor Commands*\n"
                "\u2022 `@QA Monitor run` — Auto-detect and monitor unreported devices\n"
                "\u2022 `@QA Monitor run 274722 274721` — Monitor specific devices\n"
                "\u2022 `@QA Monitor status` — Check current run progress\n"
                "\u2022 `@QA Monitor stop` — Stop the current run")

        else:
            say(f":thinking_face: I don't understand `{command}`. "
                f"Try `@QA Monitor help` for available commands.")

    return app, app_token


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config("config.yaml")

    app, app_token = create_app(config)

    logger.info("QA Monitor Slack bot starting...")
    logger.info("Listening for @mentions. Press Ctrl+C to stop.")

    handler = SocketModeHandler(app, app_token)
    handler.start()


if __name__ == "__main__":
    main()
