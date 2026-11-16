#!/usr/bin/env python3
"""Send a clickable announcement to everyone on the Minecraft server (runs on the host, via the container's rcon-cli)"""
import datetime
import json
import os
import subprocess
import sys

CONTAINER = "minecraft"

# name -> (text before the link, link, text after the link)
MESSAGES = {
    "dashboard": ("Stats & map: ", "https://mcdash.heyim.link", ""),
    "whitelist": ("Whitelist a friend: ", "https://mcdash.heyim.link/whitelist", ""),
    "invite": ("Invite a friend: enter their username at ", "https://mcdash.heyim.link/whitelist",
               ", then they join mc.heyim.link"),
    "rules": ("Reminder: please read the server rules with ", "/rules", " (click it)"),
}

# The event banner shown on the dashboard, also announced in game until its time ("until") passes.
ANNOUNCEMENT_PATH = os.path.expanduser("~/minecraft/mcdash-data/announcement.json")


def announcement_json():
    """tellraw JSON for the current announcement, or None when there is none or it is over."""
    try:
        with open(ANNOUNCEMENT_PATH, encoding="utf-8") as f:
            a = json.load(f)
        if datetime.datetime.now(datetime.timezone.utc) >= datetime.datetime.fromisoformat(a["until"]):
            return None
    except (OSError, ValueError, KeyError):
        return None
    return json.dumps([
        {"text": "» ", "color": "gold", "bold": True},
        {"text": a["title"] + ": ", "color": "gold", "bold": True},
        {"text": a["text"], "color": "yellow", "bold": False},
    ])


def tellraw(text, url, after=""):
    # a "/command" is clickable and runs for the player; anything else opens as a link
    if url.startswith("/"):
        click = {"action": "run_command", "command": url}
        hover = "Click to run " + url
    else:
        click = {"action": "open_url", "url": url}
        hover = "Click to open"
    return json.dumps([
        {"text": "» ", "color": "gold", "bold": True},
        {"text": text, "color": "yellow", "bold": False},
        {"text": url, "color": "aqua", "underlined": True, "bold": False,
         "click_event": click,
         "hover_event": {"action": "show_text", "value": hover}},
        {"text": after, "color": "yellow", "bold": False},
    ])


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "dashboard"
    if name == "announcement":
        # "announcement --show" prints the message without sending it
        msg = announcement_json()
        if msg is None:
            print("No current announcement")
            sys.exit(0)
        if sys.argv[2:] == ["--show"]:
            print(msg)
            sys.exit(0)
        result = subprocess.run(["podman", "exec", CONTAINER, "rcon-cli", f"tellraw @a {msg}"],
                                capture_output=True, text=True)
        if result.returncode != 0 or "Invalid" in result.stdout:
            sys.exit(f"Failed to send broadcast: {result.stdout.strip()} {result.stderr.strip()}")
        print("Announcement sent")
        sys.exit(0)
    if name not in MESSAGES:
        sys.exit(f"Unknown message '{name}'. Choose one of: {', '.join(MESSAGES)}")
    text, url, after = MESSAGES[name]
    result = subprocess.run(["podman", "exec", CONTAINER, "rcon-cli", f"tellraw @a {tellraw(text, url, after)}"],
                            capture_output=True, text=True)
    if result.returncode != 0 or "Invalid" in result.stdout:
        sys.exit(f"Failed to send broadcast: {result.stdout.strip()} {result.stderr.strip()}")
    print(f"Broadcast sent: {text}{url}{after}")
