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
                max_connections=100,
                max_keepalive_connections=20
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



        """Downloads a file from IPFS using its CID via the /cat endpoint (uses POST)."""
         # Re-check connection before proceeding
        if not self.is_connected():
            log.error("IPFS API not connected. Cannot download.")
            return None

        if not os.path.exists(destination_dir):
             try: os.makedirs(destination_dir, exist_ok=True)
             except OSError as e: log.error(f"Could not create download dir {destination_dir}: {e}"); return None

        log.info(f"Attempting to download file from IPFS CID: {ipfs_hash} via requests...")
        destination_path = os.path.join(destination_dir, ipfs_hash)

        try:
            params = {'arg': ipfs_hash}
            # Use stream=True and POST for /cat
            with self.session.post(f"{self.api_base_url}/cat", params=params, stream=True, timeout=300) as response:
                response.raise_for_status()
                downloaded_size = 0
                with open(destination_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded_size += len(chunk)

            if os.path.exists(destination_path) and downloaded_size > 0:
                 log.info(f"File downloaded successfully ({downloaded_size} bytes) to: {destination_path}")
                 return destination_path
            elif os.path.exists(destination_path):
                 log.warning(f"Downloaded file for CID {ipfs_hash} but it is empty."); 
                 try: os.remove(destination_path); 
                 except OSError: pass; return None
            else:
                 log.error(f"IPFS cat completed for CID {ipfs_hash}, but file not found at {destination_path}"); return None

        except requests.exceptions.HTTPError as http_err:
            status = http_err.response.status_code
            resp_text = http_err.response.text
            if status == 500:
                 try:
                      error_data = http_err.response.json(); msg = error_data.get("Message", "N/A")
                      if "object not found" in msg.lower() or "dag node not found" in msg.lower(): log.error(f"CID {ipfs_hash} not found on IPFS node(s). API response: {msg}")
                      else: log.error(f"IPFS API Error ({status}) downloading CID {ipfs_hash}: {msg}")
                 except ValueError: log.error(f"IPFS API Error ({status}) downloading CID {ipfs_hash}. Non-JSON error: {resp_text}")
            else: log.error(f"HTTP Error {status} downloading from IPFS API (CID: {ipfs_hash}): {resp_text}", exc_info=False)
            return None
        except requests.exceptions.RequestException as e: log.error(f"Network error downloading from IPFS API (CID: {ipfs_hash}): {e}", exc_info=True); return None
        except Exception as e: log.error(f"Unexpected error during IPFS download (CID: {ipfs_hash}): {e}", exc_info=True); return None