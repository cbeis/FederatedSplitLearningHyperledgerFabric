#!/usr/bin/env bash
set -e
# clean up function
cleanup() {
  echo "⏹️  Killing processes…"
  kill "${pids[@]}" 2>/dev/null || true
}
trap cleanup EXIT SIGINT SIGTERM

# Build once
echo "🔨 Building project…"
npm run build

echo "🚀 Starting server on HTTP=3000 WS=8080"
ORG_NAME=Admin PEER_NAME=peer0 \
MSP_ID=AdminMSP \
PEER_ENDPOINT=localhost:7050 PEER_HOST_ALIAS=peer0.admin.example.com \
CRYPTO_PATH="$PWD/test-network/organizations/peerOrganizations/admin.example.com" \
PORT=3000 WS_PORT=8080 \
node dist/server.js &
pids+=($!)

# Define as many clients as you like here: user→org mapping
CLIENTS=(
  "User1:org1"
  "User1:org2"
)

# Base ports
http_port=3001
ws_port=8081

for entry in "${CLIENTS[@]}"; do
  # split "User1:org1" → user=User1, org=org1
  IFS=: read -r user org <<< "$entry"
  peer="peer0"   # or make this part of your pair if you need peer1, peer2, etc.

  # derive the rest
  msp="${org^}MSP"  # org1→Org1MSP, org2→Org2MSP
  if [[ $org == "org1" ]]; then
    endpoint="localhost:7051"
  else
    endpoint="localhost:9051"
  fi
  alias="${peer}.${org}.example.com"
  crypto="$PWD/test-network/organizations/peerOrganizations/${org}.example.com"

  echo "🤖  Starting client for $user (@${org}) on HTTP=${http_port} WS=${ws_port}"
  ORG_NAME=$org PEER_NAME=$peer \
  MSP_ID=$msp \
  PEER_ENDPOINT=$endpoint PEER_HOST_ALIAS=$alias \
  CRYPTO_PATH=$crypto \
  USER_NAME=$user \
  PORT=$http_port WS_PORT=$ws_port \
  node dist/client.js &

  pids+=($!)
  ((http_port++))
  ((ws_port++))
done

# ─── Wait for everyone ────────────────────────────────────────────────────────
wait
echo "✅ All done."
