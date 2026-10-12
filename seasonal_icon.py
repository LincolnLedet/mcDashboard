#!/usr/bin/env python3
"""Change the Minecraft server icon and MOTD colors based on the current season"""
import shutil
import subprocess
import os
from datetime import datetime

SERVER_DIR = os.environ.get("MC_SERVER_DIR", os.path.expanduser("~/minecraft/server"))
ICONS_DIR = f"{SERVER_DIR}/icons"
SERVER_ICON = f"{SERVER_DIR}/server-icon.png"
SEASON_FILE = f"{SERVER_DIR}/current_season.txt"
# Read by the minecraft container (EnvironmentFile=) on every start.
MOTD_ENV_FILE = os.environ.get("MOTD_ENV_FILE", os.path.expanduser("~/minecraft/motd.env"))

MOTD_NAME = "mc.heyim.link"
MOTD_SUBTITLE = "Hosted on the East Coast"

# Colors picked from each season's tree icon; the name cycles through them letter by letter.
SEASON_COLORS = {
    "spring": ["#3FA34D", "#FF7EB6", "#5CC85C", "#FFB3D1"],  # green leaves, pink blossoms
    "summer": ["#1E9E2F", "#3FCF4A", "#FF2A2A", "#2DB83A"],  # full green, red apples
    "fall":   ["#FF2A00", "#FF7A00", "#FFC400", "#FF5000"],  # red, orange, yellow leaves
    "winter": ["#8B5A2B", "#D7ECFF", "#A8743F", "#FFFFFF"],  # bare brown branches, snow
}
SUBTITLE_COLOR = {"spring": "§a", "summer": "§2", "fall": "§6", "winter": "§b"}


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


def hex_code(color):
    """#RRGGBB -> legacy hex color code §x§R§R§G§G§B§B"""
    return "§x" + "".join(f"§{c}" for c in color.lstrip("#").lower())


def build_motd(season):
    colors = SEASON_COLORS[season]
    name = "".join(f"{hex_code(colors[i % len(colors)])}§l{ch}" for i, ch in enumerate(MOTD_NAME))
    # Literal \n: the container writes it into server.properties, where it becomes a line break.
    return f"{name}§r\\n{SUBTITLE_COLOR[season]}{MOTD_SUBTITLE}"


def read_file(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return None


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def update_icon():
    season = get_season()
    last_season = (read_file(SEASON_FILE) or "").strip()
    motd_env = f"MOTD={build_motd(season)}\n"

    print(f"Current season: {season}")
    print(f"Last recorded season: {last_season}")

    if season == last_season and read_file(MOTD_ENV_FILE) == motd_env:
        print("Season unchanged, no restart needed.")
        return

    source_icon = f"{ICONS_DIR}/{season}.png"
    print(f"Applying {season}: copying {source_icon} and updating MOTD colors")

    shutil.copy2(source_icon, SERVER_ICON)
    write_file(MOTD_ENV_FILE, motd_env)
    write_file(SEASON_FILE, season)

    # Restart Minecraft server to apply the new icon and MOTD
    print("Restarting Minecraft server...")
    subprocess.run(["systemctl", "--user", "restart", "minecraft"], check=True)
    print("Done! Server restarted with new seasonal icon.")


if __name__ == "__main__":
    update_icon()
