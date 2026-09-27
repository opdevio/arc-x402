#!/usr/bin/env python3
"""Pay-per-call Arc chain snapshot, settled in USDC on Arc mainnet over x402.

An unpaid GET gets 402 and x402 v2 terms naming Arc mainnet USDC. A paid one is
verified by an Arc facilitator, the snapshot is built, the payment is settled,
and the settlement receipt is checked against the Arc RPC before the body is
served. The facilitator relays the transaction, so this service holds no key
and pays no gas. Standard library only.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

X402_VERSION = 2
NETWORK = os.environ.get("ARC_NETWORK", "eip155:5042")
CHAIN_ID = int(NETWORK.split(":")[1])
RPC = os.environ.get("ARC_RPC", "https://rpc.mainnet.arc.io")
FACILITATOR = os.environ.get("ARC_FACILITATOR", "https://facilitator.arcusnetwork.co")
# Arc's USDC predeploy. The ERC-20 interface uses 6 decimals; the native gas
# balance behind it uses 18. Amounts here are always the 6-decimal units.
ASSET = os.environ.get("ARC_ASSET", "0x3600000000000000000000000000000000000000")
PAY_TO = os.environ.get("ARC_PAY_TO", "")
AMOUNT = os.environ.get("ARC_AMOUNT", "10000")  # 0.01 USDC
ORIGIN = os.environ.get("ARC_ORIGIN", "https://opdevio.xyz")
EXPLORER = os.environ.get("ARC_EXPLORER", "https://explorer.arc.io")
PORT = int(os.environ.get("PORT", "8090"))
SETTLEMENT_LOG = os.environ.get("ARC_SETTLEMENT_LOG", "/data/settlements.jsonl")

PAID = "/arc/v1/snapshot"
PREVIEW = "/arc/v1/snapshot/preview"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# One settlement per nonce at a time. The USDC contract rejects a reused nonce
# on-chain; this only stops two concurrent requests racing the facilitator.
_inflight = set()
_inflight_lock = threading.Lock()


def requirements():
    return {
        "scheme": "exact",
        "network": NETWORK,
        "asset": ASSET,
        "payTo": PAY_TO,
        "amount": AMOUNT,
        "maxTimeoutSeconds": 120,
        "resource": ORIGIN + PAID,
        "description": "Live Arc mainnet snapshot: latest block, block time, "
                       "gas price in USDC, USDC supply.",
        "mimeType": "application/json",
        "extra": {"name": "USDC", "version": "2"},
    }


def _post(url, body, timeout=75):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json",
                                          "User-Agent": "opdevio-arcx402/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": "http_%d" % e.code}
    except Exception as exc:
        return {"error": type(exc).__name__}


def rpc(method, params):
    out = _post(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, 20)
    if "result" not in out:
        raise RuntimeError("rpc %s failed: %s" % (method, out.get("error")))
    return out["result"]


def snapshot():
    """Build the paid body. Raises if Arc cannot be read, so nothing is charged."""
    head = rpc("eth_getBlockByNumber", ["latest", False])
    number = int(head["number"], 16)
    prev = rpc("eth_getBlockByNumber", [hex(number - 100), False])
    gas_price = int(rpc("eth_gasPrice", []), 16)
    supply = int(rpc("eth_call", [{"to": ASSET, "data": "0x18160ddd"}, "latest"]), 16)
    elapsed = int(head["timestamp"], 16) - int(prev["timestamp"], 16)
    # Gas is paid in native USDC, 18 decimals: gas price in wei is USDC * 1e-18.
    transfer_fee = gas_price * 21000 / 1e18
    return {
        "network": NETWORK,
        "chain_id": int(rpc("eth_chainId", []), 16),
        "block": {"number": number, "hash": head["hash"],
                  "timestamp": int(head["timestamp"], 16),
                  "transactions": len(head.get("transactions") or []),
                  "gas_used": int(head["gasUsed"], 16)},
        "avg_block_time_seconds_last_100": round(elapsed / 100, 3),
        "gas_price_wei": gas_price,
        "simple_transfer_fee_usdc": round(transfer_fee, 8),
        "usdc_total_supply": supply / 1e6,
        "source": RPC,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def receipt_pays_us(tx, value):
    """Best-effort check that an Arc receipt transferred USDC to PAY_TO.

    Arcus only reports success after mining. RPC lag must not turn a completed
    facilitator settlement into a denied response, so missing or unavailable
    receipt data is inconclusive rather than a payment failure.
    """
    try:
        rc = rpc("eth_getTransactionReceipt", [tx])
    except RuntimeError:
        return None
    if not rc:
        return None
    if rc.get("status") != "0x1":
        return False
    want_to = "0x" + PAY_TO.lower()[2:].rjust(64, "0")
    for log in rc.get("logs") or []:
        topics = log.get("topics") or []
        if (log.get("address", "").lower() == ASSET.lower() and len(topics) == 3
                and topics[0] == TRANSFER_TOPIC and topics[2].lower() == want_to
                and int(log.get("data") or "0x0", 16) >= value):
            return True
    return False


def record(tx, payer):
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "amount": AMOUNT, "asset": ASSET, "network": NETWORK,
           "transaction": tx, "self_payment": bool(payer and payer.lower() == PAY_TO.lower())}
    try:
        with open(SETTLEMENT_LOG, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        print("SETTLEMENT RECORD FAILED %s tx=%s" % (type(exc).__name__, tx), flush=True)
    print("SETTLED amount=%s network=%s tx=%s" % (AMOUNT, NETWORK, tx), flush=True)


def b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode("ascii")


class Handler(BaseHTTPRequestHandler):
    server_version = "opdevio-arcx402/1.0"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.command, self.path.split("?")[0]), flush=True)

    def _send(self, code, body, headers=None):
        data = json.dumps(body, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "PAYMENT-REQUIRED, PAYMENT-RESPONSE")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _need(self, msg):
        payload = {"x402Version": X402_VERSION, "error": msg,
                   "resource": {"url": ORIGIN + PAID, "mimeType": "application/json",
                                "description": requirements()["description"]},
                   "accepts": [requirements()]}
        return self._send(402, payload, {"PAYMENT-REQUIRED": b64(payload)})

    do_HEAD = lambda self: self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path in ("/arc", "/arc/health"):
            return self._send(200, {"ok": True, "network": NETWORK, "asset": ASSET,
                                    "payTo": PAY_TO, "amount": AMOUNT,
                                    "paid": ORIGIN + PAID, "free_preview": ORIGIN + PREVIEW,
                                    "facilitator": FACILITATOR})
        if path == PREVIEW:
            try:
                s = snapshot()
            except RuntimeError as exc:
                return self._send(503, {"error": "arc_unreachable", "detail": str(exc)})
            return self._send(200, {"preview": True, "block": {"number": s["block"]["number"]},
                                    "paid_endpoint": ORIGIN + PAID})
        if path != PAID:
            return self._send(404, {"error": "not_found", "routes": [PAID, PREVIEW, "/arc/health"]})

        header = self.headers.get("PAYMENT-SIGNATURE") or self.headers.get("X-PAYMENT")
        if not header:
            return self._need("payment required")
        try:
            payload = json.loads(base64.b64decode(header).decode())
            auth = payload["payload"]["authorization"]
            nonce = auth["nonce"]
        except Exception:
            return self._need("malformed PAYMENT-SIGNATURE header")

        reqs = requirements()
        body = {"x402Version": X402_VERSION, "paymentPayload": payload, "paymentRequirements": reqs}
        verify = _post(FACILITATOR + "/verify", body, 30)
        if not verify.get("isValid"):
            return self._need(verify.get("invalidReason") or verify.get("error") or "payment not valid")

        with _inflight_lock:
            if nonce in _inflight:
                return self._need("payment already being settled")
            _inflight.add(nonce)
        try:
            # Build before settling: a snapshot we cannot produce is not charged.
            try:
                out = snapshot()
            except RuntimeError as exc:
                return self._send(503, {"error": "arc_unreachable", "detail": str(exc),
                                        "charged": False}, {"Retry-After": "30"})
            settle = _post(FACILITATOR + "/settle", body, 75)
            tx = settle.get("transaction") or ""
            if not settle.get("success") or not tx:
                return self._need(settle.get("errorReason") or settle.get("error") or "settlement failed")
            receipt_check = receipt_pays_us(tx, int(AMOUNT))
            if receipt_check is False:
                # The facilitator claimed success, but the receipt does not pay us: no body, no record.
                print("SETTLEMENT RECEIPT MISMATCH tx=%s" % tx, flush=True)
                return self._send(502, {"error": "settlement_receipt_mismatch", "transaction": tx,
                                        "explorer": EXPLORER + "/tx/" + tx})
            if receipt_check is None:
                print("SETTLEMENT RECEIPT UNAVAILABLE tx=%s" % tx, flush=True)
            record(tx, settle.get("payer"))
            out["payment"] = {"transaction": tx, "explorer": EXPLORER + "/tx/" + tx,
                              "amount": AMOUNT, "asset": ASSET, "network": NETWORK}
            return self._send(200, out, {"PAYMENT-RESPONSE": b64(settle)})
        finally:
            with _inflight_lock:
                _inflight.discard(nonce)


if __name__ == "__main__":
    if not PAY_TO:
        raise SystemExit("ARC_PAY_TO is required")
    print("listening on :%d network=%s amount=%s facilitator=%s" % (PORT, NETWORK, AMOUNT, FACILITATOR), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
