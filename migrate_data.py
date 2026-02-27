#!/usr/bin/env python3
"""
ChatHub Data Migration Script

Migrates data from the old Python-based installation to the new packaged version.

Old location (Python version):
  - Database: <project>/data/app.db
  - Sessions: <project>/data/sessions/
  - Config: <project>/.env

New location (Packaged version on Windows):
  - Database: %APPDATA%/ChatHub/app.db
  - Sessions: %APPDATA%/ChatHub/sessions/
  - Config: %APPDATA%/ChatHub/.env

Usage:
  python migrate_data.py --source "C:\path\to\old\chathub"

Or run interactively:
  python migrate_data.py
"""

import os
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime


def get_appdata_dir():
    """Get the AppData directory for ChatHub."""
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            return Path(appdata) / 'ChatHub'
    # Fallback for non-Windows or missing APPDATA
    return Path.home() / '.chathub'


def find_old_installation():
    """Try to find the old ChatHub installation."""
    possible_paths = []

    # Common locations
    if sys.platform == 'win32':
        # Check common Windows paths
        drives = ['C:', 'D:', 'E:']
        for drive in drives:
            possible_paths.extend([
                Path(drive) / 'chathub',
                Path(drive) / 'ChatHub',
                Path(drive) / 'Users' / os.environ.get('USERNAME', '') / 'chathub',
                Path(drive) / 'Users' / os.environ.get('USERNAME', '') / 'Documents' / 'chathub',
                Path(drive) / 'Projects' / 'chathub',
            ])

    # Current directory and parent
    possible_paths.extend([
        Path.cwd(),
        Path.cwd().parent,
    ])

    # Check each path for data/app.db
    for path in possible_paths:
        db_path = path / 'data' / 'app.db'
        if db_path.exists():
            return path

    return None


def get_data_stats(source_dir):
    """Get statistics about the data to be migrated."""
    stats = {
        'database': False,
        'database_size': 0,
        'sessions': 0,
        'env_file': False,
        'logs': 0,
    }

    # Check database
    db_path = source_dir / 'data' / 'app.db'
    if db_path.exists():
        stats['database'] = True
        stats['database_size'] = db_path.stat().st_size / (1024 * 1024)  # MB

    # Check sessions
    sessions_dir = source_dir / 'data' / 'sessions'
    if sessions_dir.exists():
        stats['sessions'] = len(list(sessions_dir.glob('bot_*')))

    # Check .env
    env_path = source_dir / '.env'
    if env_path.exists():
        stats['env_file'] = True

    # Check logs
    logs_dir = source_dir / 'logs'
    if logs_dir.exists():
        stats['logs'] = sum(1 for _ in logs_dir.rglob('*.log'))

    return stats


def migrate_data(source_dir, target_dir, include_logs=False, backup=True):
    """Migrate data from source to target directory."""
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)

    print(f"\n{'='*60}")
    print(f"  ChatHub Data Migration")
    print(f"{'='*60}")
    print(f"\nSource: {source_dir}")
    print(f"Target: {target_dir}")

    # Get stats
    stats = get_data_stats(source_dir)

    print(f"\nData to migrate:")
    print(f"  - Database: {'Yes' if stats['database'] else 'No'} ({stats['database_size']:.1f} MB)")
    print(f"  - Bot sessions: {stats['sessions']}")
    print(f"  - Environment file: {'Yes' if stats['env_file'] else 'No'}")
    if include_logs:
        print(f"  - Log files: {stats['logs']}")

    if not stats['database']:
        print("\nError: No database found in source directory!")
        print(f"Expected: {source_dir / 'data' / 'app.db'}")
        return False

    # Create backup if target exists
    if backup and target_dir.exists():
        backup_dir = target_dir.parent / f'ChatHub_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        print(f"\nBacking up existing data to: {backup_dir}")
        shutil.copytree(target_dir, backup_dir)

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Migrate database
    print("\nMigrating database...")
    source_db = source_dir / 'data' / 'app.db'
    target_db = target_dir / 'app.db'
    shutil.copy2(source_db, target_db)
    print(f"  Copied: {source_db.name}")

    # Migrate sessions
    source_sessions = source_dir / 'data' / 'sessions'
    target_sessions = target_dir / 'sessions'
    if source_sessions.exists():
        print("\nMigrating bot sessions...")
        if target_sessions.exists():
            shutil.rmtree(target_sessions)
        shutil.copytree(source_sessions, target_sessions)
        session_count = len(list(target_sessions.glob('bot_*')))
        print(f"  Copied: {session_count} bot session(s)")

    # Migrate .env file
    source_env = source_dir / '.env'
    target_env = target_dir / '.env'
    if source_env.exists():
        print("\nMigrating environment file...")
        shutil.copy2(source_env, target_env)
        print(f"  Copied: .env")

    # Migrate logs (optional)
    if include_logs:
        source_logs = source_dir / 'logs'
        target_logs = target_dir / 'logs'
        if source_logs.exists():
            print("\nMigrating logs...")
            if target_logs.exists():
                shutil.rmtree(target_logs)
            shutil.copytree(source_logs, target_logs)
            log_count = sum(1 for _ in target_logs.rglob('*.log'))
            print(f"  Copied: {log_count} log file(s)")

    print(f"\n{'='*60}")
    print(f"  Migration Complete!")
    print(f"{'='*60}")
    print(f"\nData location: {target_dir}")
    print(f"\nYou can now run the new ChatHub application.")
    print("Your bots, conversations, and settings have been preserved.")

    return True


def main():
    parser = argparse.ArgumentParser(
        description='Migrate ChatHub data to new installation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python migrate_data.py --source "C:\\chathub"
  python migrate_data.py --source "C:\\chathub" --include-logs
  python migrate_data.py  # Interactive mode
        """
    )
    parser.add_argument(
        '--source', '-s',
        help='Path to old ChatHub installation'
    )
    parser.add_argument(
        '--target', '-t',
        help='Path to new data directory (default: %%APPDATA%%/ChatHub)'
    )
    parser.add_argument(
        '--include-logs',
        action='store_true',
        help='Also migrate log files'
    )
    parser.add_argument(
        '--no-backup',
        action='store_true',
        help='Skip backup of existing data'
    )

    args = parser.parse_args()

    # Determine source directory
    source_dir = args.source
    if not source_dir:
        # Try to find automatically
        print("Searching for old ChatHub installation...")
        source_dir = find_old_installation()

        if source_dir:
            print(f"Found: {source_dir}")
            response = input("Use this location? [Y/n]: ").strip().lower()
            if response == 'n':
                source_dir = None

        if not source_dir:
            source_dir = input("Enter path to old ChatHub installation: ").strip()
            source_dir = source_dir.strip('"').strip("'")  # Remove quotes if present

    source_dir = Path(source_dir)

    if not source_dir.exists():
        print(f"Error: Source directory not found: {source_dir}")
        sys.exit(1)

    # Determine target directory
    target_dir = args.target
    if not target_dir:
        target_dir = get_appdata_dir()
    target_dir = Path(target_dir)

    # Confirm migration
    print(f"\nThis will migrate data:")
    print(f"  From: {source_dir}")
    print(f"  To:   {target_dir}")

    if not args.no_backup and target_dir.exists():
        print(f"\nExisting data in target will be backed up.")

    response = input("\nProceed with migration? [Y/n]: ").strip().lower()
    if response == 'n':
        print("Migration cancelled.")
        sys.exit(0)

    # Run migration
    success = migrate_data(
        source_dir,
        target_dir,
        include_logs=args.include_logs,
        backup=not args.no_backup
    )

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
