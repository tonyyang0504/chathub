#!/usr/bin/env python3
"""
Build script for creating ChatHub Windows executable and installer.

This script:
1. Installs required build dependencies
2. Installs Playwright browsers
3. Runs PyInstaller to create executable
4. Optionally creates installer using Inno Setup

Usage:
    python build_windows.py [--skip-installer]

Requirements:
    - Python 3.10+
    - pip
    - Inno Setup (optional, for creating installer)
"""

import os
import sys
import subprocess
import shutil
import argparse
from pathlib import Path


def run_command(cmd, description, cwd=None):
    """Run a command and handle errors."""
    print(f"\n{'='*60}")
    print(f"  {description}")
    print(f"{'='*60}")
    print(f"Command: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    print()

    result = subprocess.run(
        cmd,
        cwd=cwd,
        shell=isinstance(cmd, str),
        capture_output=False
    )

    if result.returncode != 0:
        print(f"\nError: {description} failed with code {result.returncode}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Build ChatHub Windows executable")
    parser.add_argument('--skip-installer', action='store_true',
                        help="Skip creating the installer (requires Inno Setup)")
    parser.add_argument('--skip-playwright', action='store_true',
                        help="Skip installing Playwright browsers")
    parser.add_argument('--clean', action='store_true',
                        help="Clean build directories before building")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent
    dist_dir = project_dir / 'dist'
    build_dir = project_dir / 'build'

    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║                  ChatHub Build Script                      ║
    ║         Building Windows Executable and Installer          ║
    ╚═══════════════════════════════════════════════════════════╝
    """)

    # Clean if requested
    if args.clean:
        print("Cleaning build directories...")
        for d in [dist_dir, build_dir]:
            if d.exists():
                shutil.rmtree(d)
                print(f"  Removed: {d}")

    # Step 1: Install build dependencies
    print("\n[Step 1/5] Installing build dependencies...")
    build_deps = ['pyinstaller', 'pystray', 'pillow']
    if not run_command(
        [sys.executable, '-m', 'pip', 'install', '--upgrade'] + build_deps,
        "Installing build dependencies"
    ):
        sys.exit(1)

    # Step 2: Install application dependencies
    print("\n[Step 2/5] Installing application dependencies...")
    requirements_file = project_dir / 'requirements.txt'
    if requirements_file.exists():
        if not run_command(
            [sys.executable, '-m', 'pip', 'install', '-r', str(requirements_file)],
            "Installing requirements.txt"
        ):
            sys.exit(1)

    # Step 3: Install Playwright browsers
    if not args.skip_playwright:
        print("\n[Step 3/5] Installing Playwright Chromium browser...")
        if not run_command(
            [sys.executable, '-m', 'playwright', 'install', 'chromium'],
            "Installing Playwright Chromium"
        ):
            print("Warning: Playwright browser installation failed.")
            print("The application will download browsers on first run.")
    else:
        print("\n[Step 3/5] Skipping Playwright browser installation...")

    # Step 4: Run PyInstaller
    print("\n[Step 4/5] Building executable with PyInstaller...")
    spec_file = project_dir / 'chathub.spec'

    if not spec_file.exists():
        print(f"Error: Spec file not found: {spec_file}")
        sys.exit(1)

    if not run_command(
        [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', str(spec_file)],
        "Running PyInstaller",
        cwd=str(project_dir)
    ):
        sys.exit(1)

    # Verify build output
    exe_path = dist_dir / 'ChatHub' / 'ChatHub.exe'
    if not exe_path.exists():
        print(f"Error: Expected executable not found: {exe_path}")
        sys.exit(1)

    print(f"\n  Executable created: {exe_path}")
    print(f"  Size: {exe_path.stat().st_size / (1024*1024):.1f} MB")

    # Step 5: Create installer (optional)
    if not args.skip_installer:
        print("\n[Step 5/5] Creating installer with Inno Setup...")

        # Find Inno Setup compiler
        inno_paths = [
            Path(os.environ.get('PROGRAMFILES(X86)', '')) / 'Inno Setup 6' / 'ISCC.exe',
            Path(os.environ.get('PROGRAMFILES', '')) / 'Inno Setup 6' / 'ISCC.exe',
            Path('C:/Program Files (x86)/Inno Setup 6/ISCC.exe'),
            Path('C:/Program Files/Inno Setup 6/ISCC.exe'),
        ]

        iscc_path = None
        for path in inno_paths:
            if path.exists():
                iscc_path = path
                break

        if iscc_path:
            iss_file = project_dir / 'installer' / 'chathub_setup.iss'
            if iss_file.exists():
                # Create installer output directory
                installer_output = dist_dir / 'installer'
                installer_output.mkdir(parents=True, exist_ok=True)

                if run_command(
                    [str(iscc_path), str(iss_file)],
                    "Creating installer"
                ):
                    # Find the created installer
                    for installer in installer_output.glob('ChatHub_Setup_*.exe'):
                        print(f"\n  Installer created: {installer}")
                        print(f"  Size: {installer.stat().st_size / (1024*1024):.1f} MB")
                else:
                    print("Warning: Installer creation failed.")
            else:
                print(f"Warning: Inno Setup script not found: {iss_file}")
        else:
            print("Warning: Inno Setup not found. Skipping installer creation.")
            print("Install Inno Setup from: https://jrsoftware.org/isinfo.php")
    else:
        print("\n[Step 5/5] Skipping installer creation...")

    # Summary
    print(f"""
    ╔═══════════════════════════════════════════════════════════╗
    ║                    Build Complete!                         ║
    ╚═══════════════════════════════════════════════════════════╝

    Output files:
      Executable: {dist_dir / 'ChatHub' / 'ChatHub.exe'}

    To distribute:
      1. Copy the entire 'dist/ChatHub' folder to the target machine
      2. Run 'ChatHub.exe' to start the application

    Or use the installer (if created):
      Run the installer from 'dist/installer/'

    """)


if __name__ == '__main__':
    main()
