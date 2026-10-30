"""Call a friend's browser-engine A2A agent over Tailscale. Needs only `pip install httpx`.

    export BE_A2A_URL=http://100.x.y.z:8765      # the owner's Tailscale IP
    export BE_A2A_TOKEN=...                       # the owner sends you this privately
    python examples/a2a_client.py "cheapest BLR to DEL flight on 5 Nov on skyscanner.co.in"
"""
import json
import os
import sys
import uuid

import httpx

url = os.environ.get("BE_A2A_URL", "http://127.0.0.1:8765").rstrip("/")
token = os.environ["BE_A2A_TOKEN"]
task = " ".join(sys.argv[1:]) or "What is the top story on news.ycombinator.com right now?"

card = httpx.get(f"{url}/.well-known/agent-card.json", timeout=10).json()
print(f"-> {card['name']}: {card.get('description', '')[:100]}\n")

req = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
    "kind": "message", "role": "user", "messageId": uuid.uuid4().hex,
    "parts": [{"kind": "text", "text": task}]}}}
r = httpx.post(f"{url}/", json=req, headers={"Authorization": f"Bearer {token}"}, timeout=600)
r.raise_for_status()
body = r.json()
if "error" in body:
    sys.exit(json.dumps(body["error"], indent=1))
res = body["result"]
texts = [p.get("text", "") for a in res.get("artifacts", []) for p in a.get("parts", [])]
print("\n".join(t for t in texts if t) or json.dumps(res, indent=1)[:2000])
