"""Provider contract and offline implementation."""

from .ILLMProvider import ILLMProvider
from .FakeProvider import FakeProvider

__all__ = ["ILLMProvider", "FakeProvider"]

