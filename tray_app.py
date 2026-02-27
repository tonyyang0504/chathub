#!/usr/bin/env python3
"""
ChatHub System Tray Application

Provides a Windows system tray interface to:
- Start/Stop the ChatHub server
- Open the dashboard in browser
- View server status
- Exit the application
"""

import os
import sys
import subprocess
import threading
import webbrowser
import time
import signal
from pathlib import Path

# Determine if running as frozen executable (PyInstaller)
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    BASE_DIR = Path(sys._MEIPASS)
    APP_DIR = Path(sys.executable).parent
    IS_FROZEN = True
else:
    # Running as script
    BASE_DIR = Path(__file__).resolve().parent
    APP_DIR = BASE_DIR
    IS_FROZEN = False

# Set up data directory in AppData for Windows
if sys.platform == 'win32':
    DATA_DIR = Path(os.environ.get('APPDATA', '')) / 'ChatHub'
else:
    DATA_DIR = APP_DIR / 'data'

# Ensure data directory exists
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Set environment variables for the app
os.environ['CHATHUB_DATA_DIR'] = str(DATA_DIR)
os.environ['DATABASE_URL'] = f"sqlite:///{DATA_DIR / 'app.db'}"

# Import pystray after setting up paths
try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("Error: Required packages not found.")
    print("Please install: pip install pystray pillow")
    sys.exit(1)


class ChatHubTray:
    """System tray application for ChatHub."""

    def __init__(self):
        self.server_process = None
        self.server_running = False
        self.server_port = int(os.environ.get('PORT', 8000))
        self.server_host = os.environ.get('HOST', '127.0.0.1')
        self.icon = None
        self.status_text = "Stopped"

    def create_icon_image(self, running=False):
        """Create a simple icon image."""
        # Create a 64x64 image
        size = 64
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        # Draw a circle - green if running, gray if stopped
        color = (76, 175, 80, 255) if running else (158, 158, 158, 255)
        margin = 4
        draw.ellipse([margin, margin, size - margin, size - margin], fill=color)

        # Draw "CH" text in white
        try:
            from PIL import ImageFont
            # Try to use a system font
            font = ImageFont.truetype("arial.ttf", 20)
        except:
            font = ImageFont.load_default()

        # Center the text
        text = "CH"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (size - text_width) // 2
        y = (size - text_height) // 2 - 2
        draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)

        return image

    def update_icon(self):
        """Update the tray icon based on server status."""
        if self.icon:
            self.icon.icon = self.create_icon_image(self.server_running)
            self.icon.title = f"ChatHub - {self.status_text}"

    def start_server(self, icon=None, item=None):
        """Start the ChatHub server."""
        if self.server_running:
            return

        self.status_text = "Starting..."
        self.update_icon()

        def run_server():
            try:
                # Determine the script/module to run
                if IS_FROZEN:
                    # When frozen, import and run directly
                    import uvicorn
                    from app.main import app

                    # Run in a way that can be stopped
                    config = uvicorn.Config(
                        app,
                        host=self.server_host,
                        port=self.server_port,
                        log_level="info"
                    )
                    server = uvicorn.Server(config)
                    self.server_process = server
                    server.run()
                else:
                    # When running as script, use subprocess
                    env = os.environ.copy()
                    env['HOST'] = self.server_host
                    env['PORT'] = str(self.server_port)

                    self.server_process = subprocess.Popen(
                        [sys.executable, 'run.py'],
                        cwd=str(APP_DIR),
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    self.server_process.wait()

            except Exception as e:
                print(f"Server error: {e}")
                self.server_running = False
                self.status_text = f"Error: {str(e)[:30]}"
                self.update_icon()

        # Start server in background thread
        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()

        # Wait a moment for server to start
        time.sleep(2)

        self.server_running = True
        self.status_text = f"Running on port {self.server_port}"
        self.update_icon()

        # Auto-open browser
        self.open_dashboard()

    def stop_server(self, icon=None, item=None):
        """Stop the ChatHub server."""
        if not self.server_running:
            return

        self.status_text = "Stopping..."
        self.update_icon()

        try:
            if self.server_process:
                if hasattr(self.server_process, 'should_exit'):
                    # Uvicorn server
                    self.server_process.should_exit = True
                elif hasattr(self.server_process, 'terminate'):
                    # Subprocess
                    self.server_process.terminate()
                    self.server_process.wait(timeout=5)
        except Exception as e:
            print(f"Error stopping server: {e}")
            if hasattr(self.server_process, 'kill'):
                self.server_process.kill()

        self.server_running = False
        self.server_process = None
        self.status_text = "Stopped"
        self.update_icon()

    def restart_server(self, icon=None, item=None):
        """Restart the ChatHub server."""
        self.stop_server()
        time.sleep(1)
        self.start_server()

    def open_dashboard(self, icon=None, item=None):
        """Open the dashboard in the default browser."""
        url = f"http://{self.server_host}:{self.server_port}"
        webbrowser.open(url)

    def open_data_folder(self, icon=None, item=None):
        """Open the data folder in file explorer."""
        if sys.platform == 'win32':
            os.startfile(str(DATA_DIR))
        elif sys.platform == 'darwin':
            subprocess.run(['open', str(DATA_DIR)])
        else:
            subprocess.run(['xdg-open', str(DATA_DIR)])

    def quit_app(self, icon=None, item=None):
        """Quit the application."""
        self.stop_server()
        if self.icon:
            self.icon.stop()

    def create_menu(self):
        """Create the system tray menu."""
        return pystray.Menu(
            pystray.MenuItem(
                "ChatHub",
                None,
                enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Open Dashboard",
                self.open_dashboard,
                default=True
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start Server",
                self.start_server,
                visible=lambda item: not self.server_running
            ),
            pystray.MenuItem(
                "Stop Server",
                self.stop_server,
                visible=lambda item: self.server_running
            ),
            pystray.MenuItem(
                "Restart Server",
                self.restart_server,
                visible=lambda item: self.server_running
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Open Data Folder",
                self.open_data_folder
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Exit",
                self.quit_app
            )
        )

    def run(self):
        """Run the system tray application."""
        # Create the icon
        self.icon = pystray.Icon(
            "ChatHub",
            self.create_icon_image(False),
            "ChatHub - Stopped",
            self.create_menu()
        )

        # Auto-start server
        threading.Thread(target=self.start_server, daemon=True).start()

        # Run the icon (this blocks)
        self.icon.run()


def main():
    """Main entry point."""
    # Handle Windows-specific setup
    if sys.platform == 'win32':
        # Hide console window if running as GUI
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 0
            )
        except:
            pass

    # Create and run the tray app
    app = ChatHubTray()

    try:
        app.run()
    except KeyboardInterrupt:
        app.quit_app()


if __name__ == "__main__":
    main()
