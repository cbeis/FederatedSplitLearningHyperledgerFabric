#!/usr/bin/env bash
set -euo pipefail

# 1) Point to a dedicated test repo
export IPFS_PATH="$HOME/ipfs-test"

# 2) Wipe and init fresh
rm -rf "$IPFS_PATH"
echo "Initializing fresh IPFS repo at $IPFS_PATH"
ipfs init

# 3) Remove all public bootstrap peers
echo "Removing all bootstrap peers"
ipfs bootstrap rm --all

# 4) Restrict swarm to localhost only
echo "Configuring swarm to localhost:4001"
ipfs config --json Addresses.Swarm '["/ip4/127.0.0.1/tcp/4001"]'

# 5) Move the HTTP Gateway off 8080 → 9090
echo "Configuring HTTP Gateway on localhost:9090"
ipfs config --json Addresses.Gateway '["/ip4/127.0.0.1/tcp/9090"]'

# 6) Configure API on 5002
echo "Configuring API on localhost:5002"
ipfs config --json Addresses.API '"/ip4/127.0.0.1/tcp/5002"'

# 7) Turn off MDNS discovery
echo "Disabling mDNS discovery"
ipfs config --json Discovery.MDNS.Enabled false

# 8) Start the daemon in offline mode
echo "Starting ipfs daemon (--offline)..."
ipfs daemon --offline &
DAEMON_PID=$!
echo "Daemon PID=$DAEMON_PID. Run your tests now. (Press Ctrl-C to stop & clean up.)"

# 9) On interrupt, shut down daemon and delete repo
cleanup() {
  echo
  echo "Stopping daemon (PID $DAEMON_PID)..."
  kill "$DAEMON_PID" 2>/dev/null || true
  wait "$DAEMON_PID"   2>/dev/null || true

  echo "Deleting test repo at $IPFS_PATH"
  rm -rf "$IPFS_PATH"

  echo "Cleanup complete."
  exit 0
}
trap cleanup SIGINT

# 10) Keep script alive until user hits Ctrl-C
while true; do
  sleep 1
done