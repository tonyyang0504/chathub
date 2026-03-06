"""Message queue with collect/steer/followup modes for agent communication."""

import asyncio
from enum import Enum


class QueueMode(str, Enum):
    COLLECT = "collect"
    STEER = "steer"
    FOLLOWUP = "followup"


class MessageQueue:
    """
    Queue for user messages arriving while the agent is running.

    Modes:
    - COLLECT: Buffer messages, drain them later as a batch.
    - STEER: Inject a message to redirect the agent mid-execution.
    - FOLLOWUP: Queue messages for sequential processing after current task.
    """

    def __init__(self, mode: QueueMode = QueueMode.COLLECT):
        self.mode = mode
        self._buffer: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._steer_event: asyncio.Event = asyncio.Event()
        self._steer_message: str = ""

    async def enqueue(self, message: str) -> str:
        """Add a message according to the current mode."""
        if self.mode == QueueMode.COLLECT:
            self._buffer.append(message)
            return "buffered"
        elif self.mode == QueueMode.STEER:
            self._steer_message = message
            self._steer_event.set()
            return "injected"
        else:  # FOLLOWUP
            await self._queue.put(message)
            return "queued"

    def drain(self) -> list[str]:
        """Drain all buffered messages (COLLECT mode). Returns list and clears buffer."""
        messages = self._buffer.copy()
        self._buffer.clear()
        return messages

    async def wait_for_steer(self, timeout: float | None = None) -> str | None:
        """Wait for a steer message. Returns None on timeout."""
        try:
            await asyncio.wait_for(self._steer_event.wait(), timeout=timeout)
            self._steer_event.clear()
            return self._steer_message
        except asyncio.TimeoutError:
            return None

    async def next_followup(self) -> str | None:
        """Get the next followup message, or None if empty."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
