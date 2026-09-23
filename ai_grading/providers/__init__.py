"""Provider contract and offline implementation."""

from .base import ILLMProvider
from .fake import FakeProvider

__all__ = ["ILLMProvider", "FakeProvider"]

