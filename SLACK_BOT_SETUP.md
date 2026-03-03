# Slack Bot Setup Guide

This lets your team trigger the QA monitor by @mentioning it in Slack.
For example: @QA Monitor run

## Step 1: Install the extra package

In your terminal (with the venv activated):

    uv pip install slack-bolt

## Step 2: Create the Slack App

1. Go to https://api.slack.com/apps
2. Click "Create New App" then "From scratch"
3. Name it "QA Monitor" and pick your workspace
4. Click "Create App"

## Step 3: Enable Socket Mode

1. In the left sidebar click "Socket Mode"
2. Toggle "Enable Socket Mode" ON
3. It will ask you to create an App-Level Token
4. Name it "qa-monitor-socket" and give it the scope "connections:write"
5. Click "Generate"
6. Copy the token (starts with xapp-). This is your slack_app_token.
7. Paste it into config.yaml as slack_app_token

## Step 4: Set Bot Permissions

1. In the left sidebar click "OAuth & Permissions"
2. Scroll down to "Scopes" then "Bot Token Scopes"
3. Add these scopes:
    - app_mentions:read
    - chat:write
    - channels:history
4. Scroll back up and click "Install to Workspace"
5. Click "Allow"
6. Copy the "Bot User OAuth Token" (starts with xoxb-)
7. Paste it into config.yaml as slack_bot_token

## Step 5: Enable Event Subscriptions

1. In the left sidebar click "Event Subscriptions"
2. Toggle "Enable Events" ON
3. Expand "Subscribe to bot events"
4. Click "Add Bot User Event"
5. Add: app_mention
6. Click "Save Changes"

## Step 6: Invite the Bot to Your Channel

In Slack, go to the channel you want to use (e.g. #fulfillment-qa) and type:

    /invite @QA Monitor

## Step 7: Start the Bot

In your terminal:

    .venv\Scripts\activate
    python slack_bot.py

You should see:
    QA Monitor Slack bot starting...
    Listening for @mentions. Press Ctrl+C to stop.

Leave this running. It will listen for @mentions all day.

## Usage

In Slack, type any of these:

    @QA Monitor run                     Auto-detect unreported devices
    @QA Monitor run 274722 274721       Monitor specific devices
    @QA Monitor status                  Check current progress
    @QA Monitor stop                    Cancel the current run
    @QA Monitor help                    Show all commands

## Tips

Keep the bot running all day:
    Just leave the terminal open with slack_bot.py running.
    If you close it, the bot goes offline.

Run it alongside the command line:
    You can still use "python monitor.py --auto" separately.
    The slack bot is just another way to trigger it.

If the bot disconnects:
    Just run "python slack_bot.py" again.

Your config.yaml should now have these Slack fields filled in:
    slack_webhook_url: "https://hooks.slack.com/services/..."
    slack_bot_token: "xoxb-..."
    slack_app_token: "xapp-..."
