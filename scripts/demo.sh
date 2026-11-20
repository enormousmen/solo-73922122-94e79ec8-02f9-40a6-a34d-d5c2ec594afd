#!/usr/bin/env bash
# Demo: compress a table that fits the new device, then one that does not.
# Usage: ./scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8000}"

read -r -d '' ROUTES <<'JSON' || true
[
  {"prefix": "10.0.0.0/8", "action": "NH1"},
  {"prefix": "10.1.0.0/16", "action": "NH2"},
  {"prefix": "10.2.0.0/16", "action": "NH1"},
  {"prefix": "10.3.0.0/16", "action": "drop"},
  {"prefix": "192.168.0.0/16", "action": "NH2"}
]
JSON

echo "=== 1) capacity sufficient (capacity=4, drill: NH2 fails) ==="
curl -sS -X POST "$BASE/compress" -H 'Content-Type: application/json' -d "{
  \"routes\": $ROUTES,
  \"capacity\": 4,
  \"failed_nexthop\": \"NH2\"
}" | python3 -m json.tool

echo
echo "=== 2) capacity insufficient (capacity=3): only the minimum count is reported ==="
curl -sS -X POST "$BASE/compress" -H 'Content-Type: application/json' -d "{
  \"routes\": $ROUTES,
  \"capacity\": 3,
  \"failed_nexthop\": \"NH2\"
}" | python3 -m json.tool

echo
echo "=== 3) recursive static-route resolution: a loop falls back to a shorter cover ==="
RESOLVE_RESP=$(curl -sS -X POST "$BASE/resolve" -H 'Content-Type: application/json' -d '{
  "connected": [
    {"prefix": "192.168.10.0/24", "interface": "eth1"},
    {"prefix": "192.168.10.128/25", "interface": "eth2"},
    {"prefix": "10.0.0.0/8", "interface": "eth0"}
  ],
  "static_routes": [
    {"prefix": "172.16.1.0/24", "next_hop": "172.16.2.1"},
    {"prefix": "172.16.2.0/24", "next_hop": "172.16.1.1"},
    {"prefix": "172.16.0.0/16", "next_hop": "10.9.9.9"},
    {"prefix": "172.17.0.0/16", "next_hop": "172.16.1.254"},
    {"prefix": "172.18.0.0/16", "next_hop": "192.168.10.200"},
    {"prefix": "172.19.0.0/16", "next_hop": "203.0.113.7"},
    {"prefix": "172.20.0.0/16", "next_hop": "172.20.5.5"}
  ]
}')
echo "$RESOLVE_RESP" | python3 -m json.tool

echo
echo "=== 4) hand the generated routes straight to /compress ==="
echo "$RESOLVE_RESP" | python3 -c '
import json, sys, urllib.request
body = json.load(sys.stdin)
req = urllib.request.Request(
    "'"$BASE"'/compress",
    data=json.dumps({
        "routes": body["routes"],
        "capacity": 16,
        "failed_nexthop": "eth0:10.9.9.9",
    }).encode(),
    headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req).read().decode())
' | python3 -m json.tool
