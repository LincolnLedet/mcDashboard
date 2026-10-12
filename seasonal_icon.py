#!/usr/bin/env python3
"""Change Minecraft server icon based on the current season"""
import shutil
import subprocess
import os
from datetime import datetime

SERVER_DIR = os.environ.get("MC_SERVER_DIR", os.path.expanduser("~/minecraft/server"))
ICONS_DIR = f"{SERVER_DIR}/icons"
SERVER_ICON = f"{SERVER_DIR}/server-icon.png"
SEASON_FILE = f"{SERVER_DIR}/current_season.txt"

def get_season():
    """Determine current season based on date (Northern Hemisphere)"""
    today = datetime.now()
    month = today.month
    day = today.day

    # Spring: March 20 - June 20
    # Summer: June 21 - September 22
    # Fall: September 23 - December 20
    # Winter: December 21 - March 19

    if (month == 3 and day >= 20) or month in [4, 5] or (month == 6 and day <= 20):
        return "spring"
    elif (month == 6 and day >= 21) or month in [7, 8] or (month == 9 and day <= 22):
        return "summer"
    elif (month == 9 and day >= 23) or month in [10, 11] or (month == 12 and day <= 20):
        return "fall"
    else:
        return "winter"

def get_last_season():
    """Get the last recorded season"""
    if os.path.exists(SEASON_FILE):
        with open(SEASON_FILE, 'r') as f:
            return f.read().strip()
    return None

def save_season(season):
    """Save the current season"""
    with open(SEASON_FILE, 'w') as f:
        f.write(season)

def update_icon():
    season = get_season()
    last_season = get_last_season()

    print(f"Current season: {season}")
    print(f"Last recorded season: {last_season}")

    if season == last_season:
        print("Season unchanged, no restart needed.")
        return

    source_icon = f"{ICONS_DIR}/{season}.png"
    print(f"Season changed! Copying {source_icon} to {SERVER_ICON}")

    shutil.copy2(source_icon, SERVER_ICON)
    save_season(season)

    # Restart Minecraft server to apply the new icon
    print("Restarting Minecraft server...")
    subprocess.run(["systemctl", "--user", "restart", "minecraft"], check=True)
    print("Done! Server restarted with new seasonal icon.")

if __name__ == "__main__":
    update_icon()
