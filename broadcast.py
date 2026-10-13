#!/usr/bin/env python3
"""Send a clickable announcement to everyone on the Minecraft server (runs on the host, via the container's rcon-cli)"""
import json
import subprocess
import sys

CONTAINER = "minecraft"

# name -> (text before the link, link, text after the link)
MESSAGES = {
    "dashboard": ("Stats & map: ", "https://mcdash.heyim.link", ""),
    "whitelist": ("Whitelist a friend: ", "https://mcdash.heyim.link/whitelist", ""),
    "invite": ("Invite a friend: enter their username at ", "https://mcdash.heyim.link/whitelist",
               ", then they join mc.heyim.link"),
}


def tellraw(text, url, after=""):
    return json.dumps([
        {"text": "» ", "color": "gold", "bold": True},
        {"text": text, "color": "yellow", "bold": False},
        {"text": url, "color": "aqua", "underlined": True, "bold": False,
         "click_event": {"action": "open_url", "url": url},
         "hover_event": {"action": "show_text", "value": "Click to open"}},
        {"text": after, "color": "yellow", "bold": False},
    ])


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "dashboard"
    if name not in MESSAGES:
        sys.exit(f"Unknown message '{name}'. Choose one of: {', '.join(MESSAGES)}")
    text, url, after = MESSAGES[name]
    result = subprocess.run(["podman", "exec", CONTAINER, "rcon-cli", f"tellraw @a {tellraw(text, url, after)}"],
                            capture_output=True, text=True)
    if result.returncode != 0 or "Invalid" in result.stdout:
        sys.exit(f"Failed to send broadcast: {result.stdout.strip()} {result.stderr.strip()}")
    print(f"Broadcast sent: {text}{url}{after}")
