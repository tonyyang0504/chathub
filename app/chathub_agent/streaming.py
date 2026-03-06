"""Async wrapper for synchronous AI providers."""

import asyncio


class StreamingProvider:
    """Wraps a sync AIProvider to provide async chat completion."""

    def __init__(self, provider):
        self.provider = provider

    async def chat_completion_async(
        self,
        messages,
        tools=None,
        tool_choice=None,
        temperature=None,
        max_tokens=None,
        **kwargs,
    ):
        """Run sync provider.chat_completion in a thread to avoid blocking the event loop."""
        return await asyncio.to_thread(
            self.provider.chat_completion,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
