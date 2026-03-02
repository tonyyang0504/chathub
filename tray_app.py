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
import logging
import traceback
from pathlib import Path
from datetime import datetime

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

# Setup logging to file for debugging (especially important when console is hidden)
LOG_DIR = DATA_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / 'tray_app.log'

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

logger.info(f"=== ChatHub Tray App Starting ===")
logger.info(f"IS_FROZEN: {IS_FROZEN}")
logger.info(f"BASE_DIR: {BASE_DIR}")
logger.info(f"APP_DIR: {APP_DIR}")
logger.info(f"DATA_DIR: {DATA_DIR}")
logger.info(f"Python: {sys.executable}")
logger.info(f"Version: {sys.version}")

# Set environment variables for the app
os.environ['CHATHUB_DATA_DIR'] = str(DATA_DIR)
os.environ['DATABASE_URL'] = f"sqlite:///{DATA_DIR / 'app.db'}"
logger.info(f"DATABASE_URL: {os.environ['DATABASE_URL']}")

# Import pystray after setting up paths
try:
    logger.info("Importing pystray and PIL...")
    import pystray
    from PIL import Image, ImageDraw
    logger.info("pystray and PIL imported successfully")
except ImportError as e:
    logger.error(f"Failed to import pystray/PIL: {e}")
    logger.error(traceback.format_exc())
    print("Error: Required packages not found.")
    print("Please install: pip install pystray pillow")
    sys.exit(1)


class ChatHubTray:
    """System tray application for ChatHub."""

    def __init__(self):
        self.server_process = None
        self.server_running = False
        self.server_port = int(os.environ.get('PORT', 8000))
        self.server_host = os.environ.get('HOST', '0.0.0.0')  # Match config.py default
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
            logger.info("Server already running, skipping start")
            return

        logger.info("Starting server...")
        self.status_text = "Starting..."
        self.update_icon()

        self._server_start_error = None

        def run_server():
            try:
                logger.info("run_server thread started")
                # Determine the script/module to run
                if IS_FROZEN:
                    logger.info("Running as frozen executable, importing app.main...")
                    # When frozen, import and run directly
                    try:
                        import uvicorn
                        logger.info("uvicorn imported successfully")
                    except Exception as e:
                        logger.error(f"Failed to import uvicorn: {e}")
                        logger.error(traceback.format_exc())
                        self._server_start_error = f"Import uvicorn failed: {e}"
                        return

                    try:
                        from app.main import app
                        logger.info("app.main imported successfully")
                    except Exception as e:
                        logger.error(f"Failed to import app.main: {e}")
                        logger.error(traceback.format_exc())
                        self._server_start_error = f"Import app.main failed: {e}"
                        return

                    # Run in a way that can be stopped
                    # When frozen, sys.stdout/stderr are None, so disable uvicorn's default logging
                    logger.info(f"Creating uvicorn config: host={self.server_host}, port={self.server_port}")
                    config = uvicorn.Config(
                        app,
                        host=self.server_host,
                        port=self.server_port,
                        log_level="info",
                        log_config=None  # Disable default logging to avoid 'NoneType' has no 'isatty' error
                    )
                    server = uvicorn.Server(config)
                    self.server_process = server
                    logger.info("Starting uvicorn server...")
                    server.run()
                    logger.info("uvicorn server stopped")
                else:
                    logger.info("Running as script, using subprocess")
                    # When running as script, use subprocess
                    env = os.environ.copy()
                    env['HOST'] = self.server_host
                    env['PORT'] = str(self.server_port)

                    # Log file for subprocess output
                    log_file = LOG_DIR / 'server.log'
                    logger.info(f"Server output will be logged to: {log_file}")

                    with open(log_file, 'w', encoding='utf-8') as f:
                        self.server_process = subprocess.Popen(
                            [sys.executable, 'run.py'],
                            cwd=str(APP_DIR),
                            env=env,
                            stdout=f,
                            stderr=subprocess.STDOUT,
                            # Don't use CREATE_NO_WINDOW so we can debug
                        )
                        logger.info(f"Subprocess started with PID: {self.server_process.pid}")
                        # Don't wait() - let it run in background
                        # Just keep the thread alive while server runs
                        self.server_process.wait()

            except Exception as e:
                logger.error(f"Server error: {e}")
                logger.error(traceback.format_exc())
                self._server_start_error = str(e)
                self.server_running = False
                self.status_text = f"Error: {str(e)[:30]}"
                self.update_icon()

        # Start server in background thread
        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()

        # Wait for server to start and check if it's actually running
        logger.info("Waiting for server to start...")
        time.sleep(5)  # Give server more time to start

        # Check if there was a startup error
        if self._server_start_error:
            logger.error(f"Server failed to start: {self._server_start_error}")
            self.server_running = False
            self.status_text = f"Error: {self._server_start_error[:30]}"
            self.update_icon()
            return

        # Verify server is actually responding
        # Note: Connect to 127.0.0.1 even if server binds to 0.0.0.0 (can't connect TO 0.0.0.0)
        import socket
        try:
            check_host = '127.0.0.1' if self.server_host == '0.0.0.0' else self.server_host
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((check_host, self.server_port))
            sock.close()
            if result == 0:
                logger.info(f"Server is listening on {self.server_host}:{self.server_port} (checked via {check_host})")
                self.server_running = True
                self.status_text = f"Running on port {self.server_port}"
                self.update_icon()
                # Auto-open browser
                self.open_dashboard()
            else:
                logger.error(f"Server not responding on port {self.server_port} (connect result: {result})")
                self.server_running = False
                self.status_text = "Failed to start"
                self.update_icon()
        except Exception as e:
            logger.error(f"Error checking server status: {e}")
            self.server_running = False
            self.status_text = f"Error: {str(e)[:30]}"
            self.update_icon()

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
        # Use localhost for browser even if server binds to 0.0.0.0
        browser_host = 'localhost' if self.server_host == '0.0.0.0' else self.server_host
        url = f"http://{browser_host}:{self.server_port}"
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
    logger.info("main() called")

    # Handle Windows-specific setup
    if sys.platform == 'win32':
        # Hide console window if running as GUI
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 0
            )
            logger.info("Console window hidden")
        except Exception as e:
            logger.warning(f"Could not hide console window: {e}")

    # Create and run the tray app
    logger.info("Creating ChatHubTray instance...")
    app = ChatHubTray()
    logger.info("ChatHubTray instance created")

    try:
        logger.info("Starting tray app...")
        app.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, quitting...")
        app.quit_app()
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}")
        logger.error(traceback.format_exc())
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.error(traceback.format_exc())
        # Keep a simple message box for fatal errors on Windows
        if sys.platform == 'win32':
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"ChatHub failed to start:\n\n{str(e)}\n\nCheck logs at:\n{LOG_FILE}",
                    "ChatHub Error",
                    0x10  # MB_ICONERROR
                )
            except:
                pass
        sys.exit(1)
