"""
Launcher for the user_story_analyzer statistical software (Streamlit).

Usage:
    python start_analyzer.py

Works from any location within the project:
the script automatically finds the user_story_analyzer folder
relative to its own location, starts Streamlit and opens the browser.
"""

import subprocess
import sys
import time
import webbrowser
import threading
from pathlib import Path


PORT = 8501
URL = f"http://localhost:{PORT}"


def open_browser_when_ready():
    """Wait a few seconds and open the browser on the Streamlit URL."""
    # Give Streamlit a few seconds to start.
    time.sleep(3)
    print(f"[INFO] Opening browser at {URL}")
    webbrowser.open(URL)


def main():
    script_dir = Path(__file__).resolve().parent

    app_dir = script_dir / "software_stats" / "user_story_analyzer"
    app_file = app_dir / "app.py"

    if not app_dir.exists():
        print(f"[ERROR] Folder not found: {app_dir}")
        sys.exit(1)
    if not app_file.exists():
        print(f"[ERROR] File app.py not found in: {app_dir}")
        sys.exit(1)

    print(f"[INFO] Starting Streamlit from: {app_dir}")
    print(f"[INFO] To stop the app: CTRL+C in this window\n")

    # The browser opens from a separate thread so Streamlit is not blocked.
    threading.Thread(target=open_browser_when_ready, daemon=True).start()

    # headless=true prevents Streamlit from opening a second browser tab.
    try:
        subprocess.run(
            [
                "streamlit", "run", "app.py",
                "--server.port", str(PORT),
                "--server.headless", "true",
            ],
            cwd=app_dir,
            check=True,
        )
    except FileNotFoundError:
        print("[ERROR] 'streamlit' not found in PATH.")
        print("        Install it with: pip install streamlit")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[INFO] Streamlit stopped by the user.")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Streamlit terminated with error: {e.returncode}")
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()
