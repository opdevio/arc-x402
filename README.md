# Arc x402 snapshot API

This is a small paid API for live Arc mainnet data. It returns the latest block, recent block time, USDC supply and an estimate of the USDC gas cost for a simple transfer.

The paid endpoint is `GET https://opdevio.xyz/arc/v1/snapshot`. It charges 0.01 USDC on Arc mainnet (chain ID 5042). An unpaid request returns HTTP 402 with x402 v2 payment terms. The endpoint uses the Arcus facilitator to verify and settle EIP-3009 payments. The payer's USDC goes directly to the published payee address; the facilitator pays transaction gas.

Install ethers 6, then run the client with Node.js 20 or newer:

```sh
npm install
TARGET_URL=https://opdevio.xyz/arc/v1/snapshot PK_FILE=/path/to/protected-key-file node pay.mjs
```

The key file must contain the payer's EVM private key. Keep it outside the repository. The client prints the API response and settlement transaction.

A free preview is available at `GET https://opdevio.xyz/arc/v1/snapshot/preview`. It returns the latest block number, but not the paid snapshot.

The public USDC contract address is `0x3600000000000000000000000000000000000000`. Arc USDC uses six decimals for payments. The Arc explorer is [explorer.arc.io](https://explorer.arc.io).
