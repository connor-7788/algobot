"""
Signed HTTP transport for the Kalshi Trade API v2.

Auth (docs.kalshi.com/getting_started/api_keys, read Oct 10, 2026):
  headers KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (ms), KALSHI-ACCESS-SIGNATURE
  message = timestamp + METHOD + path, where path includes /trade-api/v2 and
  excludes the query string. RSA keys sign with RSA-PSS / SHA-256 / MGF1-SHA-256,
  salt length = digest length. Ed25519 keys (now Kalshi's recommended type) sign
  the message directly. The key type is detected from the parsed PEM.

Rate limits: token buckets for reads and writes (Basic tier defaults in config).
429 and 5xx retry with exponential backoff plus full jitter. Kalshi's 429s do
not carry Retry-After, so we never wait on a header.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

log = logging.getLogger("kalshi.http")


class KalshiError(Exception):
    def __init__(self, status: int, message: str, body: object = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


class KalshiTransportError(Exception):
    """Network failure or timeout after all retries. The request may or may
    not have reached Kalshi."""


class KalshiSigner:
    def __init__(self, key_id: str, private_key):
        self.key_id = key_id
        self.private_key = private_key

    @classmethod
    def from_settings(cls, settings) -> "KalshiSigner | None":
        key_id = settings.kalshi_api_key_id
        pem = settings.kalshi_private_key_pem
        if not pem and settings.kalshi_private_key_path:
            with open(settings.kalshi_private_key_path, "r") as f:
                pem = f.read()
        if not key_id or not pem:
            return None
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
            raise ValueError(f"unsupported Kalshi key type: {type(key).__name__}")
        return cls(key_id, key)

    @staticmethod
    def message(timestamp_ms: str, method: str, path: str) -> bytes:
        return f"{timestamp_ms}{method.upper()}{path.split('?', 1)[0]}".encode("utf-8")

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        msg = self.message(timestamp_ms, method, path)
        if isinstance(self.private_key, Ed25519PrivateKey):
            sig = self.private_key.sign(msg)
        else:
            sig = self.private_key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float):
        self.rate = rate_per_s
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, cost: float) -> None:
        cost = min(cost, self.capacity)
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                wait = (cost - self.tokens) / self.rate
            time.sleep(wait)


class KalshiHTTP:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, settings, signer: KalshiSigner | None = None,
                 session: requests.Session | None = None, sleep=time.sleep):
        self.settings = settings
        self.base = settings.kalshi_base_url.rstrip("/")
        self.base_path = urlparse(self.base).path            # "/trade-api/v2"
        self.signer = signer
        self.session = session or requests.Session()
        self.sleep = sleep
        self.read_bucket = TokenBucket(settings.rate_read_tokens_per_s, settings.rate_read_tokens_per_s)
        self.write_bucket = TokenBucket(settings.rate_write_tokens_per_s, settings.rate_write_tokens_per_s)

    @property
    def authenticated(self) -> bool:
        return self.signer is not None

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, write: bool = False, cost: int | None = None,
                auth: bool = True, retry_on: set[int] | None = None) -> dict:
        """Send a request and return parsed JSON ({} for empty bodies).

        `path` is relative to the API base, e.g. "/markets". Raises KalshiError
        for non-retryable HTTP errors and KalshiTransportError when retries on
        network errors run out (outcome unknown)."""
        method = method.upper()
        url = self.base + path
        full_path = self.base_path + path
        bucket = self.write_bucket if write else self.read_bucket
        cost = cost if cost is not None else self.settings.rate_default_cost
        retry_statuses = self.RETRY_STATUSES if retry_on is None else retry_on
        data = json.dumps(body) if body is not None else None

        last_exc: Exception | None = None
        for attempt in range(self.settings.http_max_retries + 1):
            bucket.acquire(cost)
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if auth:
                if not self.signer:
                    raise KalshiError(401, "no Kalshi credentials configured")
                headers.update(self.signer.headers(method, full_path))
            try:
                resp = self.session.request(method, url, params=params, data=data,
                                            headers=headers, timeout=self.settings.http_timeout_s)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                log.warning("%s %s transport error (attempt %d): %s", method, path, attempt + 1, e)
                self._backoff(attempt)
                continue

            if resp.status_code in retry_statuses and attempt < self.settings.http_max_retries:
                log.warning("%s %s -> %d, retrying (attempt %d)", method, path,
                            resp.status_code, attempt + 1)
                self._backoff(attempt)
                continue
            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = resp.text
                msg = payload.get("message") if isinstance(payload, dict) else str(payload)
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                    msg = payload["error"].get("message", msg)
                raise KalshiError(resp.status_code, str(msg)[:300], payload)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {}

        raise KalshiTransportError(f"{method} {path} failed after retries: {last_exc}")

    def _backoff(self, attempt: int) -> None:
        cap = min(self.settings.http_backoff_max_s, self.settings.http_backoff_base_s * (2 ** attempt))
        self.sleep(random.uniform(0, cap))
