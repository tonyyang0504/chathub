"""
Git Worktree Lifecycle Manager
Creates, inspects, merges, and cleans up git worktrees for Tool Builder sandbox sessions.
"""

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKTREES_DIR = PROJECT_ROOT / ".worktrees"


@dataclass
class WorktreeInfo:
    """Information about a created worktree."""
    session_id: str
    user_id: int
    path: Path
    branch: str


@dataclass
class MergeResult:
    """Result of merging a worktree branch."""
    success: bool
    message: str
    merged_branch: str = ""
    commit_hash: str = ""


class WorktreeManager:
    """Manages git worktree lifecycle for tool builder sessions."""

    def __init__(self):
        self._active: dict[int, WorktreeInfo] = {}  # user_id -> WorktreeInfo

    def create(self, session_id: str, user_id: int) -> WorktreeInfo:
        """Create a new git worktree for a tool builder session.

        One active worktree per user enforced — previous one is discarded.
        """
        # Discard existing worktree for this user
        if user_id in self._active:
            try:
                self.discard(self._active[user_id].session_id)
            except Exception as e:
                logger.warning(f"Failed to discard old worktree for user {user_id}: {e}")

        short_id = session_id[:8]
        timestamp = int(time.time())
        branch = f"tool-builder/{user_id}/{timestamp}"
        worktree_path = WORKTREES_DIR / f"tb-{short_id}"

        # Ensure worktrees directory exists
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

        # Create worktree with new branch
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree_path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {result.stderr.strip()}")

        info = WorktreeInfo(
            session_id=session_id,
            user_id=user_id,
            path=worktree_path,
            branch=branch
        )
        self._active[user_id] = info
        logger.info(f"Created worktree at {worktree_path} on branch {branch}")
        return info

    def get_info(self, user_id: int) -> Optional[WorktreeInfo]:
        """Get the active worktree info for a user."""
        return self._active.get(user_id)

    def get_status(self, session_id: str) -> dict:
        """Get worktree diff stats and log relative to main."""
        info = self._find_by_session(session_id)
        if not info:
            return {"error": "Worktree not found"}

        wt = str(info.path)

        # Get diff stat
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", "main..HEAD"],
            capture_output=True, text=True, cwd=wt
        )

        # Get log
        log = subprocess.run(
            ["git", "log", "main..HEAD", "--oneline"],
            capture_output=True, text=True, cwd=wt
        )

        return {
            "session_id": info.session_id,
            "branch": info.branch,
            "path": str(info.path),
            "diff_stat": diff_stat.stdout.strip() if diff_stat.returncode == 0 else "",
            "log": log.stdout.strip() if log.returncode == 0 else "",
        }

    def get_changed_files(self, session_id: str) -> list[str]:
        """Get list of files changed relative to main."""
        info = self._find_by_session(session_id)
        if not info:
            return []

        result = subprocess.run(
            ["git", "diff", "--name-only", "main..HEAD"],
            capture_output=True, text=True, cwd=str(info.path)
        )
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().split("\n") if f]

    def get_file_diff(self, session_id: str, file_path: str = None) -> str:
        """Get the actual diff content. If file_path given, diff for that file only."""
        info = self._find_by_session(session_id)
        if not info:
            return ""

        cmd = ["git", "diff", "main..HEAD"]
        if file_path:
            cmd.extend(["--", file_path])

        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(info.path)
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def merge(self, session_id: str) -> MergeResult:
        """Merge worktree branch into main and clean up."""
        info = self._find_by_session(session_id)
        if not info:
            return MergeResult(success=False, message="Worktree not found")

        branch = info.branch

        # Check if there are actually changes to merge
        changed = self.get_changed_files(session_id)
        if not changed:
            self.discard(session_id)
            return MergeResult(success=False, message="No changes to merge")

        # Merge from main repo
        result = subprocess.run(
            ["git", "merge", branch, "--no-edit"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        if result.returncode != 0:
            return MergeResult(
                success=False,
                message=f"Merge failed: {result.stderr.strip()}",
                merged_branch=branch
            )

        # Get merged commit hash
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        commit_hash = head.stdout.strip() if head.returncode == 0 else ""

        # Clean up worktree and branch
        self._cleanup(info)

        return MergeResult(
            success=True,
            message="Branch merged successfully",
            merged_branch=branch,
            commit_hash=commit_hash
        )

    def discard(self, session_id: str) -> None:
        """Remove worktree and delete branch without merging."""
        info = self._find_by_session(session_id)
        if not info:
            return
        self._cleanup(info)
        logger.info(f"Discarded worktree for session {session_id}")

    def run_command(self, session_id: str, command: list[str], timeout: int = 60) -> dict:
        """Run an arbitrary command in the worktree directory."""
        info = self._find_by_session(session_id)
        if not info:
            return {"error": "Worktree not found", "returncode": -1}

        try:
            result = subprocess.run(
                command,
                capture_output=True, text=True,
                cwd=str(info.path),
                timeout=timeout
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out", "returncode": -1}
        except Exception as e:
            return {"error": str(e), "returncode": -1}

    def _find_by_session(self, session_id: str) -> Optional[WorktreeInfo]:
        """Find worktree info by session ID."""
        for info in self._active.values():
            if info.session_id == session_id:
                return info
        return None

    def _cleanup(self, info: WorktreeInfo):
        """Remove worktree and delete branch."""
        user_id = info.user_id

        # Remove worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(info.path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )

        # Delete branch
        subprocess.run(
            ["git", "branch", "-D", info.branch],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )

        # Prune worktree list
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )

        # Remove from active tracking
        if user_id in self._active and self._active[user_id].session_id == info.session_id:
            del self._active[user_id]


# Singleton
worktree_manager = WorktreeManager()
