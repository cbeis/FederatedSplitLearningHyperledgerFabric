import os
import logging
import time
from typing import Optional
import httpx

log = logging.getLogger(__name__)
IPFS_API_URL = os.getenv("IPFS_API_URL", "/ip4/127.0.0.1/tcp/5002")


class IpfsInterface:
    def __init__(self, retries: int = 3, delay: int = 5):
        self.api_base_url = self._parse_api_url(IPFS_API_URL)
        if not self.api_base_url:
            raise ValueError(
                f"Invalid IPFS_API_URL format: {IPFS_API_URL}. "
                "Expected format like /ip4/127.0.0.1/tcp/5001"
            )
        self.retries = retries
        self.delay = delay
        self.client = httpx.Client(
            http2=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            timeout=60.0,
        )

        if not self.is_connected():
            for attempt in range(1, self.retries + 1):
                log.warning(
                    f"IPFS connection failed (attempt {attempt}/{self.retries}). "
                    f"Retrying in {self.delay}s..."
                )
                time.sleep(self.delay)
                if self.is_connected():
                    break
            else:
                raise ConnectionError(
                    f"Failed to connect to IPFS API at {self.api_base_url} "
                    f"after {self.retries} attempts."
                )

    def upload_bytes(self, data_bytes: bytes, name: str = "file") -> str:
        files = {"file": (name, data_bytes)}
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.client.post(
                    f"{self.api_base_url}/add",
                    files=files,
                    params={"pin": "true", "cid-version": 1},
                )
                resp.raise_for_status()
                return resp.json()["Hash"]
            except httpx.ReadTimeout:
                log.warning(
                    f"Upload timed out (attempt {attempt}/{self.retries}). "
                    f"Retrying in {self.delay}s..."
                )
                time.sleep(self.delay)
        raise httpx.ReadTimeout(
            f"upload_bytes failed after {self.retries} attempts"
        )

    def cat_bytes(self, cid: str) -> bytes:
        resp = self.client.post(
            f"{self.api_base_url}/cat", params={"arg": cid}
        )
        resp.raise_for_status()
        return resp.content

    def is_connected(self) -> bool:
        try:
            resp = self.client.post(f"{self.api_base_url}/id", timeout=10.0)
            resp.raise_for_status()
            return True
        except httpx.RequestError as e:
            status = (
                e.response.status_code
                if getattr(e, "response", None)
                else "N/A"
            )
            log.warning(f"IPFS connection check failed (Status: {status}): {e}")
            return False
        except Exception as e:
            log.warning(f"Unexpected error during IPFS check: {e}")
            return False

    @staticmethod
    def _parse_api_url(multiaddr: str) -> Optional[str]:
        parts = multiaddr.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "ip4" and parts[2] == "tcp":
            ip, port = parts[1], parts[3]
            return f"http://{ip}:{port}/api/v0"
        log.error(f"Cannot parse IPFS API multiaddr: {multiaddr}")
        return None
