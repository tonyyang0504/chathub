# Building ChatHub Executable

This guide explains how to build ChatHub as a standalone Windows executable.

## Prerequisites

1. **Python 3.10+** - Installed and in PATH
2. **Git** - For cloning the repository
3. **Inno Setup 6** (optional) - For creating installer
   - Download from: https://jrsoftware.org/isinfo.php

## Quick Build (Windows)

```powershell
# Clone the repository
git clone https://github.com/tonyyang0504/chathub.git
cd chathub

# Run the build script
python build_windows.py
```

The executable will be created in `dist/ChatHub/ChatHub.exe`.

## Build Options

```powershell
# Build without installer (faster, no Inno Setup needed)
python build_windows.py --skip-installer

# Build without pre-installing Playwright browsers
# (browsers will download on first run)
python build_windows.py --skip-playwright

# Clean build (removes previous build artifacts)
python build_windows.py --clean
```

## Manual Build Steps

If the build script doesn't work, follow these manual steps:

### Step 1: Install Dependencies

```powershell
pip install -r requirements.txt
pip install pyinstaller pystray pillow
```

### Step 2: Install Playwright Browser

```powershell
python -m playwright install chromium
```

### Step 3: Build with PyInstaller

```powershell
pyinstaller --clean --noconfirm chathub.spec
```

### Step 4: Create Installer (Optional)

1. Open Inno Setup Compiler
2. Open `installer/chathub_setup.iss`
3. Click Build > Compile

## Output Files

After building:

```
dist/
├── ChatHub/              # Standalone application folder
│   ├── ChatHub.exe       # Main executable
│   ├── app/              # Application files
│   └── ...               # Dependencies
└── installer/            # Installer (if created)
    └── ChatHub_Setup_1.0.0.exe
```

## Distribution

### Option 1: Portable (No Installation)
1. Copy the entire `dist/ChatHub` folder
2. Run `ChatHub.exe`

### Option 2: Installer
1. Share `dist/installer/ChatHub_Setup_x.x.x.exe`
2. User runs installer
3. Application installs to Program Files
4. Desktop/Start Menu shortcuts created

## Troubleshooting

### "DLL not found" errors
- Ensure Visual C++ Redistributable is installed
- Download from: https://aka.ms/vs/17/release/vc_redist.x64.exe

### Playwright browser issues
- Run: `python -m playwright install chromium`
- Or let the app download browsers on first run

### Antivirus false positives
- PyInstaller executables may trigger antivirus warnings
- This is a false positive - the code is open source
- Add exception in antivirus settings if needed

### Build fails with "spec file not found"
- Ensure you're running from the project root directory
- Check that `chathub.spec` exists

## Application Data

When running the installed application:

- **Windows**: Data stored in `%APPDATA%\ChatHub\`
  - Database: `app.db`
  - Sessions: `sessions/`
  - Logs: `logs/`

## Features of the Packaged App

The packaged application includes:

1. **System Tray Icon**
   - Shows running status (green = running, gray = stopped)
   - Right-click menu for controls

2. **Auto-Start Server**
   - Server starts automatically when launched
   - Opens browser to dashboard

3. **Easy Controls**
   - Start/Stop/Restart server
   - Open dashboard in browser
   - Open data folder
   - Exit application

4. **Portable Data**
   - All data in AppData folder
   - Easy backup and restore
   - Survives app updates
