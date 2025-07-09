import os
import logging
import time
import httpx

log = logging.getLogger(__name__)
IPFS_API_URL = os.getenv("IPFS_API_URL", "/ip4/127.0.0.1/tcp/5002")

class IpfsInterface:
    def __init__(self, retries=3, delay=5):
        self.api_base_url = self._parse_api_url(IPFS_API_URL)
        if not self.api_base_url:
            raise ValueError(
                f"Invalid IPFS_API_URL format: {IPFS_API_URL}. Expected format like /ip4/127.0.0.1/tcp/5001"
            )
        self.retries = retries
        self.delay = delay
        self.client = httpx.Client(
            http2=True,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5
            ),
            timeout=60.0
        )
        if not self.is_connected():
            connected = False
            for attempt in range(self.retries):
                log.warning(
                    f"Initial IPFS connection failed. Retrying in {self.delay}s... "
                    f"(Attempt {attempt+1}/{self.retries})"
                )
                time.sleep(self.delay)
                if self.is_connected():
                    connected = True
                    break
            if not connected:
                raise ConnectionError(
                    f"Failed to connect to IPFS API at {self.api_base_url} "
                    f"after {self.retries} attempts."
                )

    def upload_bytes(self, data_bytes, name="file"):
        files = {'file': (name, data_bytes)}
        for attempt in range(self.retries):
            try:
                resp = self.client.post(
                    f"{self.api_base_url}/add",
                    files=files,
                    params={'pin': 'true', 'cid-version': 1},
                )
                resp.raise_for_status()
                return resp.json()['Hash']
            except httpx.ReadTimeout:
                log.warning(f"IPFS upload timed out on attempt {attempt+1}/{self.retries}, retrying in {self.delay}s…")
                time.sleep(self.delay)
        # after retries exhausted
        raise httpx.ReadTimeout(f"upload_bytes failed after {self.retries} attempts")

    def cat_bytes(self, cid):
        resp = self.client.post(
            f"{self.api_base_url}/cat",
            params={'arg': cid}
        )
        resp.raise_for_status()
        return resp.content

    def _parse_api_url(self, multiaddr_api_spec):
        try:
            parts = multiaddr_api_spec.strip('/').split('/')
            if len(parts) == 4 and parts[0] == 'ip4' and parts[2] == 'tcp':
                ip_addr = parts[1]
                port = parts[3]
                return f"http://{ip_addr}:{port}/api/v0"
            else:
                log.error(f"Cannot parse IPFS API multiaddr: {multiaddr_api_spec}")
                return None
        except Exception as e:
            log.error(f"Error parsing IPFS API multiaddr '{multiaddr_api_spec}': {e}")
            return None

    def is_connected(self):
        if not self.api_base_url:
            return False
        try:
            resp = self.client.post(f"{self.api_base_url}/id", timeout=10)
            resp.raise_for_status()
            return True
        except httpx.RequestError as e:
            status_code = e.response.status_code if e.response is not None else "N/A"
            log.warning(f"IPFS connection check failed (Status: {status_code}): {e}")
            return False
        except Exception as e:
            log.warning(f"IPFS connection check encountered unexpected error: {e}")
            return False
