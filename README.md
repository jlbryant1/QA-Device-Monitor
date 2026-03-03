# Device QA Monitor - Setup Guide

## Step 1: Create Your Config File

In the project folder (device QA Monitor), make a copy of the example config:

    copy config.example.yaml config.yaml

Open config.yaml in VS Code and fill in your details:

    gpx_username: "james@yourcompany.com"
    gpx_password: "yourpassword"
    slack_webhook_url: "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

Save the file.


## Step 2: Create a Slack Webhook

1. Go to https://api.slack.com/apps
2. Click Create New App, then From scratch
3. Name it QA Device Monitor and pick your workspace
4. In the left sidebar click Incoming Webhooks
5. Toggle it ON
6. Click Add New Webhook to Workspace
7. Select the channel you want alerts in (e.g. #fulfillment-qa)
8. Click Allow
9. Copy the Webhook URL and paste it into config.yaml
10. Save config.yaml


## Step 3: Activate the Virtual Environment

Every time you open a new terminal in VS Code you need to activate the
virtual environment first:

    .venv\Scripts\activate

You should see (.venv) appear at the start of your terminal prompt.
That means you are good to go.

If you ever get python not found or module not found errors,
it probably means the venv is not activated. Run activate again.


## Step 4: First Test Run

Run the monitor with --visible so you can watch the browser:

    python monitor.py --auto --visible

What should happen:
- A Chromium browser window opens
- It goes to admin.gpx.co/login and logs in
- It goes to the Devices page and filters by Inventory status
- It scans the table for devices where Last Report is -
- It starts monitoring those devices
- As devices report you get Slack notifications
- When all devices are done you get a summary

Watch the browser and terminal output to make sure login works,
the Inventory filter gets applied, and devices are being found.


## Step 5: Run for Real (Headless)

Once the --visible test works, run without the browser window:

    python monitor.py --auto


## Other Ways to Run

    python monitor.py --serials 275741 275740 275739
    python monitor.py --csv devices.csv
    python monitor.py --interactive


## Quick Reference

Every time you want to run the monitor:

    1. Open VS Code
    2. Open terminal with Ctrl + backtick
    3. .venv\Scripts\activate
    4. python monitor.py --auto


## Timeouts

The monitor reads the TYPE column and sets timeouts automatically:

    Road Wired, AssetTrack Wired, Protect Plus:  150 min (2.5 hours)
    All other device types:                       20 min

Change these in config.yaml:

    default_timeout_minutes: 20
    slow_device_timeout_minutes: 150


## Troubleshooting

Login fails:
    Run with --visible to watch the browser.
    Double-check username and password in config.yaml.

No unreported Inventory devices found:
    All Inventory devices already have a Last Report timestamp.
    Provision some new devices first then run the monitor.

Slack notifications not arriving:
    Check slack_webhook_url in config.yaml is correct.
    Make sure the Slack app is installed to the right channel.

Module not found errors:
    Activate the venv: .venv\Scripts\activate
    Reinstall: uv pip install playwright pyyaml requests

Python not found:
    Activate the venv: .venv\Scripts\activate
