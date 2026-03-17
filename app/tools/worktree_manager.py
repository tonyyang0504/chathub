"""
Git Worktree Lifecycle Manager
Creates, inspects, merges, and cleans up git worktrees for Tool Builder sandbox sessions.
"""

import logging
import shutil
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
    base_commit: str = ""  # SHA the worktree branched from


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

        # Record the commit SHA the worktree branched from
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        base_commit = head_result.stdout.strip() if head_result.returncode == 0 else ""

        info = WorktreeInfo(
            session_id=session_id,
            user_id=user_id,
            path=worktree_path,
            branch=branch,
            base_commit=base_commit
        )
        self._active[user_id] = info
        logger.info(f"Created worktree at {worktree_path} on branch {branch}")
        return info

    def get_info(self, user_id: int) -> Optional[WorktreeInfo]:
        """Get the active worktree info for a user."""
        return self._active.get(user_id)

    def get_status(self, session_id: str) -> dict:
        """Get worktree diff stats and log relative to base commit."""
        info = self._find_by_session(session_id)
        if not info:
            return {"error": "Worktree not found"}

        wt = str(info.path)
        base = info.base_commit or "HEAD~1"

        # Get diff stat
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", f"{base}..HEAD"],
            capture_output=True, text=True, cwd=wt
        )

        # Get log
        log = subprocess.run(
            ["git", "log", f"{base}..HEAD", "--oneline"],
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
        """Get list of files changed relative to base commit (committed + uncommitted + untracked)."""
        info = self._find_by_session(session_id)
        if not info:
            return []

        wt = str(info.path)
        base = info.base_commit or "HEAD~1"

        # Committed changes vs base commit
        committed = subprocess.run(
            ["git", "diff", "--name-only", f"{base}..HEAD"],
            capture_output=True, text=True, cwd=wt
        )
        # Uncommitted changes (working tree)
        uncommitted = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=wt
        )
        # Staged but not yet committed
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True, text=True, cwd=wt
        )
        # Untracked new files (never git add'ed)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=wt
        )
        files = set()
        for r in [committed, uncommitted, staged, untracked]:
            if r.returncode == 0:
                files.update(f for f in r.stdout.strip().split("\n") if f)
        return sorted(files)

    def get_file_diff(self, session_id: str, file_path: str = None) -> str:
        """Get the actual diff content. If file_path given, diff for that file only."""
        info = self._find_by_session(session_id)
        if not info:
            return ""

        base = info.base_commit or "HEAD~1"
        cmd = ["git", "diff", f"{base}..HEAD"]
        if file_path:
            cmd.extend(["--", file_path])

        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(info.path)
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def merge(self, session_id: str, tool_name: str = None) -> MergeResult:
        """Apply worktree changes into main working tree and commit.

        If tool_name is provided, copies files into app/tools/custom/{tool_name}/
        instead of mirroring the worktree structure into PROJECT_ROOT.
        """
        info = self._find_by_session(session_id)
        if not info:
            return MergeResult(success=False, message="Worktree not found")

        branch = info.branch

        # Check if there are actually changes to merge
        changed = self.get_changed_files(session_id)
        if not changed:
            self.discard(session_id)
            return MergeResult(success=False, message="No changes to merge")

        if tool_name:
            # Plugin mode: copy all changed files into app/tools/custom/{tool_name}/
            custom_dir = PROJECT_ROOT / "app" / "tools" / "custom" / tool_name
            custom_dir.mkdir(parents=True, exist_ok=True)
            copied = []
            prefix = f"app/tools/custom/{tool_name}/"
            for rel_path in changed:
                src = info.path / rel_path
                if rel_path.startswith(prefix):
                    # Strip the prefix — rel_path already includes the custom tool dir
                    inner_path = rel_path[len(prefix):]
                    dst = custom_dir / inner_path
                else:
                    # File outside the custom tool dir — copy preserving original structure
                    dst = PROJECT_ROOT / rel_path
                try:
                    if src.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src), str(dst))
                        copied.append(str(dst.relative_to(PROJECT_ROOT)))
                    # Don't delete files in plugin mode — only add
                except Exception as e:
                    logger.warning(f"Failed to copy {rel_path}: {e}")
        else:
            # Legacy mode: copy to PROJECT_ROOT preserving structure
            copied = []
            for rel_path in changed:
                src = info.path / rel_path
                dst = PROJECT_ROOT / rel_path
                try:
                    if src.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src), str(dst))
                        copied.append(rel_path)
                    elif dst.exists():
                        dst.unlink()  # file deleted in worktree
                        copied.append(rel_path)
                except Exception as e:
                    logger.warning(f"Failed to copy {rel_path}: {e}")

        if not copied:
            self.discard(session_id)
            return MergeResult(success=False, message="No files could be applied")

        # Stage and commit the applied changes
        subprocess.run(
            ["git", "add", "--"] + copied,
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        commit_msg = f"Tool Builder: publish {session_id[:8]}"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )

        # Get commit hash
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        commit_hash = head.stdout.strip() if head.returncode == 0 else ""

        # Clean up worktree and branch
        self._cleanup(info)

        return MergeResult(
            success=True,
            message="Changes published successfully",
            merged_branch=branch,
            commit_hash=commit_hash,
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
