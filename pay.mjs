// Pay an Arc x402 v2 endpoint. The private key is injected into this process environment.
import { randomBytes } from "node:crypto";
import { Wallet, verifyTypedData } from "ethers";

const url = process.env.TARGET_URL;
const key = process.env.ARC_PAYER_PRIVATE_KEY;
if (!url || !key) throw new Error("TARGET_URL and ARC_PAYER_PRIVATE_KEY are required");
const account = new Wallet(key.trim());
const r1 = await fetch(url, { redirect: "error" });
if (r1.status !== 402) {
  console.log(JSON.stringify({ step: "challenge", status: r1.status,
    body: (await r1.text()).slice(0, 400), note: "not a 402; nothing paid" }));
  process.exit(0);
}
const hdr = r1.headers.get("PAYMENT-REQUIRED");
const reqs = hdr ? JSON.parse(Buffer.from(hdr, "base64").toString("utf8")) : await r1.clone().json();
const accept = reqs.accepts?.[0];
if (!accept || accept.network !== "eip155:5042" || accept.scheme !== "exact" ||
    accept.asset.toLowerCase() !== "0x3600000000000000000000000000000000000000") {
  throw new Error("402 did not request exact Arc mainnet USDC");
}
const now = Math.floor(Date.now() / 1000);
const authorization = {
  from: account.address, to: accept.payTo, value: String(accept.amount),
  validAfter: String(now - 60), validBefore: String(now + (accept.maxTimeoutSeconds ?? 120)),
  nonce: "0x" + randomBytes(32).toString("hex"),
};
const domain = { name: accept.extra?.name ?? "USDC", version: accept.extra?.version ?? "2",
  chainId: 5042, verifyingContract: accept.asset };
const types = { TransferWithAuthorization: [
  { name: "from", type: "address" }, { name: "to", type: "address" },
  { name: "value", type: "uint256" }, { name: "validAfter", type: "uint256" },
  { name: "validBefore", type: "uint256" }, { name: "nonce", type: "bytes32" },
] };
const signature = await account.signTypedData(domain, types, authorization);
if (verifyTypedData(domain, types, authorization, signature).toLowerCase() !== account.address.toLowerCase())
  throw new Error("local signature recovery failed");
const paymentPayload = { x402Version: 2, resource: reqs.resource, accepted: accept,
  payload: { authorization, signature } };
const r2 = await fetch(url, { redirect: "error", headers: {
  "PAYMENT-SIGNATURE": Buffer.from(JSON.stringify(paymentPayload)).toString("base64"),
  "User-Agent": "opdevio-agent/1.0 (+https://opdevio.xyz)",
} });
const text = await r2.text();
let transaction = null, explorer = null;
try { const body = JSON.parse(text); transaction = body.payment?.transaction ?? null; explorer = body.payment?.explorer ?? null; } catch {}
console.log(JSON.stringify({ step: "settle", status: r2.status,
  paymentResponse: r2.headers.get("PAYMENT-RESPONSE"), transaction, explorer, body: text.slice(0, 2000) }));
if (r2.status !== 200) process.exitCode = 1;
