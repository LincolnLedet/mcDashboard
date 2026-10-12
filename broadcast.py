#!/usr/bin/env python3
"""Send a broadcast message to the Minecraft server (runs on the host, via the container's rcon-cli)"""
import subprocess
import sys

CONTAINER = "minecraft"

# Default message - can be overridden via command line
DEFAULT_MESSAGE = "Check out our server dashboard at https://mcdash.heyim.link or the server map at https://map.heyim.link"

if __name__ == "__main__":
    message = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MESSAGE
    # Use 'say' command - shows as [Server] in chat
    result = subprocess.run(["podman", "exec", CONTAINER, "rcon-cli", f"say {message}"])
    if result.returncode == 0:
        print(f"Broadcast sent: {message}")
    else:
        print("Failed to send broadcast")
        sys.exit(1)
