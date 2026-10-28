"""Small chat-completions adapters; no provider SDK dependency."""
from .client import ChatResult, HttpChatClient, LLMError, MockChatClient

__all__ = ["ChatResult", "HttpChatClient", "LLMError", "MockChatClient"]
