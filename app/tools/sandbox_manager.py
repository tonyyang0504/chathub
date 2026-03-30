"""
Docker Sandbox Lifecycle Manager
Creates, inspects, publishes, and cleans up Docker containers for Tool Builder sandbox sessions.
Auto-installs Docker if not present (macOS via Colima, Linux via rootless, Windows via winget).

Docker is deferred: create() is lightweight (no Docker). Call start_preview() to trigger
Docker install + image build + container launch on demand (when user clicks Preview).
"""

import logging
import os
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX_IMAGE = "chathub-sandbox-v2"
SANDBOX_DB_DIR = PROJECT_ROOT / ".sandbox-data"
PORT_RANGE = range(9001, 9100)
CONTAINER_PREFIX = "tb-sandbox-"
MAX_AGE_SECONDS = 30 * 60  # 30 minutes


def _docker_bin() -> Optional[str]:
    """Dynamic lookup of docker binary — not cached so it finds newly installed docker."""
    import sys
    candidates = [
        shutil.which("docker"),
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        str(Path.home() / ".local/bin/docker"),          # rootless Linux
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",  # Windows
    ]
    return next((p for p in candidates if p and Path(p).exists()), None)


@dataclass
class SandboxInfo:
    """Information about an active sandbox session."""
    session_id: str
    user_id: int
    branch: str
    container_id: str = ""
    container_name: str = ""
    preview_port: int = 0
    preview_url: str = ""
    db_snapshot_dir: Optional[Path] = None
    worktree_path: Optional[Path] = None


class SandboxManager:
    """Manages Docker containers as isolated sandboxes for Tool Builder sessions.

    create() is lightweight — no Docker work.
    start_preview() triggers Docker install + image build + container on demand.
    """

    def __init__(self):
        self._active: dict[int, SandboxInfo] = {}
        self._image_ready = False

    # ------------------------------------------------------------------
    # Docker detection & auto-install
    # ------------------------------------------------------------------

    def _docker_env(self) -> dict:
        """Build env dict with correct DOCKER_HOST for Colima/rootless sockets."""
        env = os.environ.copy()
        # Colima socket (macOS)
        colima_sock = Path.home() / ".colima/default/docker.sock"
        if colima_sock.exists():
            env["DOCKER_HOST"] = f"unix://{colima_sock}"
        # Rootless Docker socket (Linux)
        try:
            rootless_sock = Path(f"/run/user/{os.getuid()}/docker.sock")
            if rootless_sock.exists():
                env["DOCKER_HOST"] = f"unix://{rootless_sock}"
        except AttributeError:
            pass  # Windows has no getuid
        return env

    def _docker_ready(self) -> bool:
        """Check if docker daemon responds to 'docker ps'."""
        import sys
        bin_ = _docker_bin()
        if not bin_:
            return False
        env = self._docker_env()
        # On Windows, Docker uses a named pipe — no DOCKER_HOST needed
        if sys.platform == "win32":
            env.pop("DOCKER_HOST", None)
        try:
            r = subprocess.run([bin_, "ps"], capture_output=True, env=env, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def _ensure_docker_available(self):
        """Auto-installs Docker if not present. Called only when preview is requested."""
        if self._docker_ready():
            return
        logger.info("Docker not found — attempting auto-install...")
        self._install_docker()
        self._wait_for_docker(timeout=180)
        self._image_ready = False  # force image rebuild check after install

    def _install_docker(self):
        import sys
        if sys.platform == "darwin":
            self._install_docker_macos()
        elif sys.platform.startswith("linux"):
            self._install_docker_linux()
        elif sys.platform == "win32":
            self._install_docker_windows()
        else:
            raise RuntimeError(f"Unsupported platform: {sys.platform}")

    def _install_docker_macos(self):
        brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
        if not brew or not Path(brew).exists():
            brew = "/usr/local/bin/brew"
        if not Path(brew).exists():
            raise RuntimeError(
                "Homebrew is required to auto-install Docker on macOS. "
                "Install from https://brew.sh"
            )
        # Install colima + docker CLI (no sudo, no GUI, no license prompts)
        subprocess.run([brew, "install", "colima", "docker"], check=True)
        # Start Colima VM (takes ~30s). If a stale VM exists, delete and retry.
        colima = shutil.which("colima") or "/opt/homebrew/bin/colima"
        result = subprocess.run(
            [colima, "start", "--cpu", "2", "--memory", "2"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            logger.warning("Colima start failed — deleting stale VM and retrying...")
            subprocess.run([colima, "delete", "-f"], capture_output=True)
            subprocess.run(
                [colima, "start", "--cpu", "2", "--memory", "2"],
                check=True
            )

    def _install_docker_linux(self):
        # Rootless install — no sudo required
        script = subprocess.run(
            ["curl", "-fsSL", "https://get.docker.com/rootless"],
            capture_output=True, text=True, check=True
        ).stdout
        script_path = Path("/tmp/install-docker-rootless.sh")
        script_path.write_text(script)
        script_path.chmod(0o755)
        subprocess.run(["sh", str(script_path)], check=True)
        # Install systemd user unit if available
        setup_tool = Path.home() / "bin/dockerd-rootless-setuptool.sh"
        if setup_tool.exists():
            subprocess.run([str(setup_tool), "install"], check=True)
        # Start rootless daemon
        subprocess.Popen(
            [str(Path.home() / "bin/dockerd-rootless.sh")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def _install_docker_windows(self):
        # Try winget first (built into Windows 10 1709+ and Windows 11)
        winget = shutil.which("winget")
        if winget:
            subprocess.run([
                winget, "install", "-e", "--id", "Docker.DockerDesktop",
                "--accept-package-agreements", "--accept-source-agreements",
                "--silent"
            ], check=True)
            # Docker Desktop auto-starts after install; _wait_for_docker polls
            return
        # Fallback: download installer via PowerShell
        subprocess.run([
            "powershell", "-Command",
            "Invoke-WebRequest -Uri 'https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe' "
            "-OutFile $env:TEMP\\DockerInstaller.exe; "
            "Start-Process $env:TEMP\\DockerInstaller.exe "
            "-ArgumentList 'install','--quiet','--accept-license' -Wait"
        ], check=True)

    def _wait_for_docker(self, timeout: int = 180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._docker_ready():
                logger.info("Docker is ready")
                return
            time.sleep(3)
        raise RuntimeError(
            "Docker was installed but failed to start within 3 minutes. "
            "Try restarting and clicking New Session again."
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, session_id: str, user_id: int) -> SandboxInfo:
        """Create a lightweight session record. No Docker work performed."""
        # Discard existing sandbox for this user
        if user_id in self._active:
            try:
                self.discard(self._active[user_id].session_id)
            except Exception as e:
                logger.warning(f"Failed to discard old sandbox for user {user_id}: {e}")

        from app.tools.worktree_manager import worktree_manager
        try:
            wt_info = worktree_manager.create(session_id, user_id)
            info = SandboxInfo(
                session_id=session_id, user_id=user_id,
                branch=wt_info.branch, worktree_path=wt_info.path
            )
        except Exception as e:
            logger.warning(f"Failed to create worktree for session {session_id}: {e}")
            timestamp = int(time.time())
            branch = f"tool-builder/{user_id}/{timestamp}"
            info = SandboxInfo(session_id=session_id, user_id=user_id, branch=branch)

        self._active[user_id] = info
        logger.info(f"Created lightweight sandbox session {session_id[:8]} for user {user_id}")
        return info

    def start_preview(self, session_id: str) -> SandboxInfo:
        """Trigger Docker install + image build + container on demand. Blocks until ready."""
        info = self._find_by_session(session_id)
        if not info:
            raise RuntimeError("Session not found")
        if info.preview_url:
            return info  # already running
        self._ensure_docker_available()
        self._launch_container(info)
        return info

    def get_info(self, user_id: int) -> Optional[SandboxInfo]:
        return self._active.get(user_id)

    def get_status(self, session_id: str) -> dict:
        info = self._find_by_session(session_id)
        if not info:
            return {"error": "Sandbox not found"}
        return self._docker_status(info)

    def get_changed_files(self, session_id: str) -> list[str]:
        info = self._find_by_session(session_id)
        if not info:
            return []
        # Always use the host worktree for file detection — it has full git
        # access. Docker only mounts the worktree dir, so refs like 'main'
        # aren't accessible inside the container.
        from app.tools.worktree_manager import worktree_manager
        return worktree_manager.get_changed_files(session_id)

    def run_command(self, session_id: str, command: list[str], timeout: int = 60) -> dict:
        info = self._find_by_session(session_id)
        if not info:
            return {"error": "Sandbox not found", "returncode": -1}
        if not info.container_name:
            return {"error": "Preview not started — click Preview to launch Docker container", "returncode": -1}
        bin_ = _docker_bin()
        if not bin_:
            return {"error": "Docker not available", "returncode": -1}
        try:
            result = subprocess.run(
                [bin_, "exec", info.container_name] + command,
                capture_output=True, text=True, timeout=timeout,
                env=self._docker_env()
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

    def publish(self, session_id: str, tool_name: str = None, commit_prefix: str = None) -> dict:
        """Publish sandbox changes to the host repo.
        If tool_name is provided, files are copied into app/tools/custom/{tool_name}/.
        If commit_prefix is provided, uses it for the merge commit message.
        """
        info = self._find_by_session(session_id)
        if not info:
            return {"success": False, "message": "Sandbox not found"}
        if not info.container_name:
            return self._host_publish(info, tool_name=tool_name, commit_prefix=commit_prefix)
        return self._docker_publish(info, tool_name=tool_name, commit_prefix=commit_prefix)

    def discard(self, session_id: str) -> None:
        info = self._find_by_session(session_id)
        if not info:
            return
        self._docker_cleanup(info)
        logger.info(f"Discarded sandbox for session {session_id}")

    # ------------------------------------------------------------------
    # Docker implementation
    # ------------------------------------------------------------------

    def ensure_image(self):
        """Check if chathub-sandbox image exists, build if not. Called on first start_preview()."""
        if self._image_ready:
            return
        bin_ = _docker_bin()
        if not bin_:
            raise RuntimeError("Docker binary not found after install")

        env = self._docker_env()
        result = subprocess.run(
            [bin_, "images", "-q", SANDBOX_IMAGE],
            capture_output=True, text=True, env=env
        )
        if result.stdout.strip():
            self._image_ready = True
            return

        logger.info("Building Docker sandbox image (first time)...")
        result = subprocess.run(
            [bin_, "build", "-t", SANDBOX_IMAGE, "."],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), env=env
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to build sandbox image: {result.stderr.strip()}")
        self._image_ready = True
        logger.info("Sandbox image built successfully")

    def _launch_container(self, info: SandboxInfo):
        """Build image if needed and start the Docker container. Mutates info in place."""
        self.ensure_image()

        bin_ = _docker_bin()
        short_id = info.session_id[:8]
        container_name = f"{CONTAINER_PREFIX}{short_id}"
        preview_port = self._find_free_port()

        # Copy DB snapshot for the sandbox
        SANDBOX_DB_DIR.mkdir(parents=True, exist_ok=True)
        db_snapshot_dir = SANDBOX_DB_DIR / f"db-{short_id}"
        db_snapshot_dir.mkdir(parents=True, exist_ok=True)
        source_db = PROJECT_ROOT / "data" / "app.db"
        if source_db.exists():
            shutil.copy2(str(source_db), str(db_snapshot_dir / "app.db"))

        # Prevent bot auto-recovery in sandbox (Playwright browsers not installed in image)
        snapshot_db = db_snapshot_dir / "app.db"
        if snapshot_db.exists():
            import sqlite3
            try:
                conn = sqlite3.connect(str(snapshot_db))
                conn.execute("UPDATE bot_profiles SET is_running = 0")
                conn.commit()
                conn.close()
                logger.info("Marked all bots as stopped in sandbox DB snapshot")
            except Exception as e:
                logger.warning(f"Could not reset bot states in snapshot DB: {e}")

        secret_key = f"sandbox-{uuid.uuid4().hex[:16]}"

        cmd = [
            bin_, "run", "-d",
            "--name", container_name,
            "-v", f"{info.worktree_path or PROJECT_ROOT}:/app",
            "-v", f"{db_snapshot_dir}:/app/data",
            "-p", f"{preview_port}:8000",
            "-e", "DATABASE_URL=sqlite:///./data/app.db",
            "-e", f"SECRET_KEY={secret_key}",
            "-e", "COOKIE_NAME=sandbox_access_token",
            "-e", "SANDBOX_MODE=true",
            "-e", "PORT=8000",
            "-e", "DEBUG=true",
            "--memory=512m", "--cpus=1",
            "--label", f"tb-session={info.session_id}",
            "--label", f"tb-user={info.user_id}",
        ]

        # Overlay COOKIE_NAME-aware auth files so container uses sandbox_access_token
        # (individual file mounts take precedence over the directory mount)
        auth_overlays = [
            "app/config.py",
            "app/auth/routes.py",
            "app/auth/utils.py",
            "app/tools/routes.py",
            "app/tools/__init__.py",
        ]
        for rel in auth_overlays:
            src = PROJECT_ROOT / rel
            if src.exists():
                cmd.extend(["-v", f"{src}:/app/{rel}:ro"])

        cmd.append(SANDBOX_IMAGE)

        result = subprocess.run(cmd, capture_output=True, text=True, env=self._docker_env())
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start sandbox container: {result.stderr.strip()}")

        info.container_id = result.stdout.strip()[:12]
        info.container_name = container_name
        info.preview_port = preview_port
        info.preview_url = f"http://localhost:{preview_port}"
        info.db_snapshot_dir = db_snapshot_dir
        logger.info(f"Launched Docker container {container_name} on port {preview_port}")

        logger.info(f"Container {container_name} started, waiting for HTTP /health on port {preview_port}...")
        try:
            self._wait_for_http("127.0.0.1", preview_port, timeout=60)
        except RuntimeError as e:
            logs = self._get_container_logs(container_name)
            raise RuntimeError(
                f"{e}\n\nContainer logs (last 60 lines):\n{logs}"
            ) from None
        logger.info(f"Sandbox app ready on port {preview_port}")

    @staticmethod
    def _wait_for_http(host: str, port: int, timeout: int = 60):
        """Block until /health returns non-5xx or timeout expires."""
        import urllib.request
        import urllib.error
        deadline = time.time() + timeout
        url = f"http://{host}:{port}/health"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status < 500:
                        return
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return  # server is up, returned a non-5xx HTTP error
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(
            f"Sandbox app did not become ready within {timeout}s on port {port}. "
            "Check Docker logs for errors."
        )

    def _get_container_logs(self, container_name: str, tail: int = 60) -> str:
        """Retrieve recent container logs for error diagnostics."""
        bin_ = _docker_bin()
        if not bin_:
            return "(docker not found)"
        try:
            result = subprocess.run(
                [bin_, "logs", "--tail", str(tail), container_name],
                capture_output=True, text=True, env=self._docker_env(), timeout=10
            )
            output = (result.stdout + result.stderr).strip()
            return output or "(no output)"
        except Exception as e:
            return f"(could not retrieve logs: {e})"

    @staticmethod
    def _find_free_port() -> int:
        for port in PORT_RANGE:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError("No free ports in sandbox range 9001-9099")

    def _docker_status(self, info: SandboxInfo) -> dict:
        if not info.container_name:
            return {
                "session_id": info.session_id, "branch": info.branch,
                "container_name": "", "container_status": "not_started",
                "preview_url": info.preview_url, "preview_port": info.preview_port,
                "diff_stat": "", "changed_files": self.get_changed_files(info.session_id),
            }
        bin_ = _docker_bin()
        env = self._docker_env()
        result = subprocess.run(
            [bin_, "inspect", "--format", "{{.State.Status}}", info.container_name],
            capture_output=True, text=True, env=env
        )
        container_status = result.stdout.strip() if result.returncode == 0 else "unknown"
        # Use worktree for diff stats (Docker git may not have access to main ref)
        from app.tools.worktree_manager import worktree_manager
        wt_status = worktree_manager.get_status(info.session_id) if info.worktree_path else {}
        diff_stat = wt_status.get("diff_stat", "")
        return {
            "session_id": info.session_id, "branch": info.branch,
            "container_name": info.container_name, "container_status": container_status,
            "preview_url": info.preview_url, "preview_port": info.preview_port,
            "diff_stat": diff_stat,
            "changed_files": self.get_changed_files(info.session_id),
        }

    def _host_publish(self, info: SandboxInfo, tool_name: str = None, commit_prefix: str = None) -> dict:
        """Publish worktree branch into main repo when no Docker container was launched."""
        from app.tools.worktree_manager import worktree_manager
        # Auto-commit any uncommitted changes the agent left in the worktree
        worktree_manager.run_command(info.session_id, ["git", "add", "-A"])
        worktree_manager.run_command(
            info.session_id,
            ["git", "commit", "-m", f"{commit_prefix or 'Tool Builder: publish'} from session {info.session_id[:8]}"]
        )  # OK if this fails (nothing new to commit)
        result = worktree_manager.merge(info.session_id, tool_name=tool_name, commit_prefix=commit_prefix)
        self._active.pop(info.user_id, None)
        return {
            "success": result.success,
            "message": result.message,
            "commit_hash": result.commit_hash,
            "merged_branch": result.merged_branch,
            "changed_files": [],
        }

    def _docker_publish(self, info: SandboxInfo, tool_name: str = None, commit_prefix: str = None) -> dict:
        bin_ = _docker_bin()
        env = self._docker_env()
        changed_files = self.get_changed_files(info.session_id)
        if not changed_files:
            self.discard(info.session_id)
            return {"success": False, "message": "No changes to publish"}

        subprocess.run([bin_, "exec", info.container_name, "git", "add", "-A"],
                       capture_output=True, text=True, env=env)
        subprocess.run([bin_, "exec", info.container_name, "git", "commit", "-m",
                        f"Tool Builder: sandbox {info.session_id[:8]}"],
                       capture_output=True, text=True, env=env)

        # Copy changed files from container into the isolated worktree (not PROJECT_ROOT)
        target_root = info.worktree_path or PROJECT_ROOT
        for f in changed_files:
            host_path = target_root / f
            host_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [bin_, "cp", f"{info.container_name}:/app/{f}", str(host_path)],
                capture_output=True, text=True, env=env
            )

        # Commit in worktree then merge into main
        from app.tools.worktree_manager import worktree_manager
        worktree_manager.run_command(info.session_id, ["git", "add", "-A"])
        worktree_manager.run_command(
            info.session_id,
            ["git", "commit", "-m", f"Tool Builder: sandbox {info.session_id[:8]}"]
        )
        merge_result = worktree_manager.merge(info.session_id, tool_name=tool_name)
        self._docker_cleanup(info)
        return {
            "success": merge_result.success,
            "message": merge_result.message,
            "commit_hash": merge_result.commit_hash,
            "merged_branch": merge_result.merged_branch,
            "changed_files": changed_files,
        }

    def _docker_cleanup(self, info: SandboxInfo):
        bin_ = _docker_bin()
        env = self._docker_env()
        if bin_ and info.container_name:
            subprocess.run([bin_, "stop", info.container_name], capture_output=True, text=True, env=env)
            subprocess.run([bin_, "rm", "-f", info.container_name], capture_output=True, text=True, env=env)
        if info.db_snapshot_dir and info.db_snapshot_dir.exists():
            shutil.rmtree(str(info.db_snapshot_dir), ignore_errors=True)
        from app.tools.worktree_manager import worktree_manager
        worktree_manager.discard(info.session_id)
        self._active.pop(info.user_id, None)

    def _cleanup_stale_docker(self):
        bin_ = _docker_bin()
        if not bin_:
            return
        env = self._docker_env()
        result = subprocess.run(
            [bin_, "ps", "-a", "--filter", f"name={CONTAINER_PREFIX}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, env=env
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        for name in result.stdout.strip().split("\n"):
            if name:
                subprocess.run([bin_, "stop", name], capture_output=True, text=True, env=env)
                subprocess.run([bin_, "rm", "-f", name], capture_output=True, text=True, env=env)

    # ------------------------------------------------------------------
    # File write/commit helpers (for fallback TOOL.md generation)
    # ------------------------------------------------------------------

    def write_file(self, session_id: str, rel_path: str, content: str) -> bool:
        """Write a file into the sandbox worktree."""
        info = self._find_by_session(session_id)
        if not info or not info.worktree_path:
            return False
        target = info.worktree_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True

    def commit_file(self, session_id: str, rel_path: str, message: str) -> bool:
        """Stage and commit a single file in the sandbox worktree."""
        info = self._find_by_session(session_id)
        if not info or not info.worktree_path:
            return False
        from app.tools.worktree_manager import worktree_manager
        worktree_manager.run_command(session_id, ["git", "add", rel_path])
        result = worktree_manager.run_command(
            session_id, ["git", "commit", "-m", message]
        )
        return result.get("returncode", -1) == 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_by_session(self, session_id: str) -> Optional[SandboxInfo]:
        for info in self._active.values():
            if info.session_id == session_id:
                return info
        return None

    def _cleanup_stale(self):
        """Clean up leftover containers on startup."""
        try:
            self._cleanup_stale_docker()
        except Exception as e:
            logger.warning(f"Docker stale cleanup failed: {e}")


# Singleton
sandbox_manager = SandboxManager()

# Cleanup stale containers on import
try:
    sandbox_manager._cleanup_stale()
except Exception as e:
    logger.warning(f"Stale sandbox cleanup failed: {e}")
