"""Backend-only Vertex client ownership; no API keys or legacy config fallback."""

from contextlib import contextmanager
import logging
import threading

import google.auth
from google.auth import impersonated_credentials
from google.auth.exceptions import GoogleAuthError
from google import genai
from google.genai import types

from .GeminiProvider import GeminiProvider
from .ILLMProvider import ProviderError


_auth_log_lock = threading.Lock()
_auth_log_threads: dict[int, int] = {}


def _other_auth_threads(record):
    with _auth_log_lock:
        return record.thread not in _auth_log_threads


@contextmanager
def _private_auth_logs():
    """Google auth debug records include headers; omit only this call's records."""
    thread_id = threading.get_ident()
    logger = logging.getLogger("google.auth.transport.requests")
    with _auth_log_lock:
        if not _auth_log_threads:
            logger.addFilter(_other_auth_threads)
        _auth_log_threads[thread_id] = _auth_log_threads.get(thread_id, 0) + 1
    try:
        yield
    finally:
        with _auth_log_lock:
            _auth_log_threads[thread_id] -= 1
            if not _auth_log_threads[thread_id]:
                del _auth_log_threads[thread_id]
            # Keep one filter until all calls finish; mutating a list of per-call
            # filters during logging could skip another thread's protection.
            if not _auth_log_threads:
                logger.removeFilter(_other_auth_threads)


class VertexProvider:
    @_private_auth_logs()
    def __init__(
        self,
        model: str,
        *,
        project: str,
        location: str,
        service_account: str,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 4096,
    ) -> None:
        self._client = None
        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            # This local prototype deliberately accepts keyless impersonation ADC.
            if (not isinstance(credentials, impersonated_credentials.Credentials)
                    or credentials.service_account_email != service_account):
                raise ValueError("Impersonation ADC required")
            endpoint = ("https://aiplatform.googleapis.com" if location == "global"
                        else f"https://{location}-aiplatform.googleapis.com")
            self._client = genai.Client(
                vertexai=True, credentials=credentials,
                project=project, location=location,
                http_options=types.HttpOptions(
                    api_version="v1", base_url=endpoint,
                    timeout=max(1, int(timeout_seconds * 1000)),
                    retry_options=types.HttpRetryOptions(attempts=1),
                    client_args={"trust_env": False, "follow_redirects": False},
                    async_client_args={"trust_env": False, "follow_redirects": False},
                ),
            )
            self._provider = GeminiProvider(
                model=model, client=self._client,
                timeout_seconds=timeout_seconds,
                max_output_tokens=max_output_tokens,
            )
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise ProviderError(
                "backend_auth_unavailable", "Backend Vertex authentication is unavailable.",
            ) from None

    @_private_auth_logs()
    def generate_feedback(self, request):
        try:
            return self._provider.generate_feedback(request)
        except GoogleAuthError:
            raise ProviderError(
                "backend_auth_unavailable", "Backend Vertex authentication is unavailable.",
            ) from None

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
