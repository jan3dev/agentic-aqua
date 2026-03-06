# Design: `lbtc_pay_invoice` - Pay Lightning Invoices from Liquid Bitcoin

**Date:** 2026-03-05
**Status:** Draft
**Based on:** `research_boltz_lightning_pay_20260305.md`

---

## 1. Architecture Overview

```
                        aqua-mcp
                     +-----------+
                     | server.py |  (MCP tool registration)
                     +-----+-----+
                           |
                     +-----+-----+
                     |  tools.py  |  (tool: lbtc_pay_invoice)
                     +-----+-----+
                           |
            +--------------+--------------+
            |                             |
      +-----+-----+               +------+------+
      |  boltz.py  |  (NEW)       | wallet.py   |  (existing)
      +-----+-----+               +------+------+
            |                             |
            v                             v
    Boltz REST API              LWK (send L-BTC to lockup)
    api.boltz.exchange          Electrum/Esplora
```

### Key Design Decision: No MuSig2 in v1

In a submarine swap, the lockup address has three spend paths:
1. **Key path** (MuSig2): Cooperative 2-of-2 signature from User + Boltz (private, cheap)
2. **Claim leaf** (script path): Boltz's key + preimage (hash lock) - Boltz can claim unilaterally
3. **Refund leaf** (script path): User's key + timeout (time lock) - User can refund after expiry

**Critical insight:** Boltz holds the preimage after paying the Lightning invoice and CAN claim via script path (#2) without any cooperation from the client. MuSig2 cooperative signing (#1) is an optimization for privacy and fees, not a requirement.

**v1 approach:** Skip MuSig2. Boltz claims via script path. The swap completes successfully. Trade-off: slightly higher on-chain fee (~few sats more) and the swap scripts are visible on-chain.

**v2 (future):** Add MuSig2 cooperative signing for key path spend.

This eliminates the hardest technical challenge and all platform-specific crypto dependencies.

---

## 2. New File: `src/aqua_mcp/boltz.py`

### 2.1 Module Structure

```python
"""Boltz Exchange integration for submarine swaps (L-BTC -> Lightning)."""

# Dependencies: stdlib only (urllib, json, hashlib, secrets, time, dataclasses)
# No new pip dependencies required for v1

BOLTZ_API = {
    "mainnet": "https://api.boltz.exchange",
    "testnet": "https://api.testnet.boltz.exchange",
}

@dataclass
class SwapInfo:
    """Holds all data for an active/completed submarine swap."""
    swap_id: str
    address: str                    # Liquid lockup address
    expected_amount: int            # Sats to send (invoice + fees)
    claim_public_key: str           # Boltz's public key (hex)
    swap_tree: dict                 # Taproot swap tree
    timeout_block_height: int       # After this, user can refund
    refund_private_key: str         # Ephemeral key (hex) - for refund recovery
    refund_public_key: str          # Corresponding pubkey (hex)
    invoice: str                    # Original BOLT11 invoice
    status: str                     # Current swap status
    network: str                    # mainnet/testnet
    created_at: str                 # ISO timestamp
    lockup_txid: str | None = None  # Liquid txid after sending
    preimage: str | None = None     # Set after successful claim
    claim_txid: str | None = None   # Boltz's claim txid

class BoltzClient:
    """HTTP client for Boltz API v2."""

    def __init__(self, network: str = "mainnet"):
        self.base_url = BOLTZ_API[network]
        self.network = network

    def get_submarine_pairs(self) -> dict:
        """GET /v2/swap/submarine - fetch available pairs, fees, limits."""

    def create_submarine_swap(
        self,
        invoice: str,
        refund_public_key: str,  # hex-encoded compressed pubkey
    ) -> dict:
        """POST /v2/swap/submarine - create a new swap."""

    def get_swap_status(self, swap_id: str) -> dict:
        """POST /v2/swap/{swap_id} - get current swap status."""

    def get_claim_details(self, swap_id: str) -> dict:
        """GET /v2/swap/submarine/{swap_id}/claim - get preimage after invoice paid."""

    def poll_swap_status(
        self,
        swap_id: str,
        target_statuses: set[str],
        failure_statuses: set[str],
        timeout_seconds: int = 300,
        poll_interval: int = 5,
    ) -> str:
        """Poll swap status until target or failure state reached."""


def generate_keypair() -> tuple[str, str]:
    """Generate ephemeral secp256k1 keypair for refund.

    Returns (private_key_hex, public_key_hex).
    Uses coincurve for key derivation.
    """

def verify_preimage(preimage_hex: str, expected_hash_hex: str) -> bool:
    """Verify SHA256(preimage) == expected_hash. Pure stdlib."""
```

### 2.2 HTTP Implementation

Use `urllib.request` (already used in `tools.py:lw_tx_status`) for consistency. No new HTTP dependency.

```python
def _api_request(self, method: str, path: str, body: dict | None = None) -> dict:
    """Make HTTP request to Boltz API."""
    url = f"{self.base_url}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "aqua-mcp",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())
```

### 2.3 Key Generation

New dependency: `coincurve>=21.0.0` (cross-platform: Linux, macOS ARM64/x86, Windows).

```python
import coincurve
import secrets

def generate_keypair() -> tuple[str, str]:
    """Generate ephemeral secp256k1 keypair."""
    privkey = secrets.token_bytes(32)
    pubkey = coincurve.PublicKey.from_secret(privkey)
    return privkey.hex(), pubkey.format(compressed=True).hex()
```

**Why coincurve?** It's the only cross-platform Python library with proper secp256k1. Needed because raw private key bytes must be multiplied on the secp256k1 curve to derive the public key - this is not possible with stdlib.

### 2.4 Swap Data Persistence

Store swap data in `~/.aqua-mcp/swaps/{swap_id}.json` for refund recovery.

```
~/.aqua-mcp/
 +-- swaps/                 # NEW directory
 |    +-- {swap_id}.json    # SwapInfo serialized
 +-- wallets/
 +-- cache/
 +-- config.json
```

Storage changes in `storage.py`:
- Add `swaps_dir` property (lazy mkdir)
- Add `save_swap(swap: SwapInfo)` method
- Add `load_swap(swap_id: str) -> SwapInfo | None`
- Add `list_swaps() -> list[str]`

File permissions: `0o600` (same as wallet files) - contains private refund key.

---

## 3. Tool Interface

### 3.1 Tool: `lbtc_pay_invoice`

**Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `invoice` | string | YES | - | BOLT11 Lightning invoice |
| `wallet_name` | string | no | "default" | Liquid wallet to pay from |
| `passphrase` | string | no | None | Wallet passphrase (if encrypted) |

**Return value (success):**
```json
{
  "swap_id": "abc123",
  "status": "transaction.claimed",
  "invoice_amount_sats": 50000,
  "total_paid_sats": 50069,
  "boltz_fee_sats": 50,
  "miner_fee_sats": 19,
  "lockup_txid": "abcdef...",
  "preimage": "deadbeef...",
  "explorer_url": "https://blockstream.info/liquid/tx/abcdef...",
  "refund_info": "Swap data saved. If issues arise, swap ID: abc123"
}
```

**Return value (failure):**
```json
{
  "error": {
    "code": "SWAP_FAILED",
    "message": "Boltz could not pay the Lightning invoice",
    "swap_id": "abc123",
    "status": "invoice.failedToPay",
    "refund_info": "Your L-BTC is locked. Refund available after block 2500000. Swap ID: abc123"
  }
}
```

### 3.2 Tool: `lbtc_swap_status` (secondary tool)

Check status of an existing swap. Useful for the AI assistant to follow up on swaps.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `swap_id` | string | YES | Boltz swap ID |

**Return value:**
```json
{
  "swap_id": "abc123",
  "status": "transaction.claimed",
  "lockup_txid": "abcdef...",
  "preimage": "deadbeef...",
  "timeout_block_height": 2500000,
  "network": "mainnet"
}
```

---

## 4. Execution Flow

### 4.1 Happy Path (lbtc_pay_invoice)

```
Step  Action                                 Time
----  ------                                 ----
 1    Validate invoice format (lnbc prefix)  instant
 2    Get pair info from Boltz API           ~200ms
      -> Verify L-BTC/BTC pair exists
      -> Get fee structure and limits
 3    Generate ephemeral keypair             instant
 4    Create submarine swap via API          ~500ms
      -> Send: invoice, from=L-BTC, to=BTC, refundPublicKey
      -> Receive: id, address, expectedAmount, swapTree, timeout
 5    Save swap data to disk                 instant
 6    Check L-BTC balance >= expectedAmount  ~2s (Liquid sync)
 7    Send L-BTC to lockup address           ~2s (build + sign + broadcast)
      -> Use WalletManager.send() internally
      -> Save lockup_txid to swap data
 8    Poll swap status every 5s              ~60-120s
      -> Wait for: transaction.mempool
      -> Wait for: invoice.pending
      -> Wait for: transaction.claimed
 9    Fetch claim details (preimage)         ~200ms
      -> GET /v2/swap/submarine/{id}/claim
      -> Verify SHA256(preimage) matches
10    Return success result                  instant
                                             -------
                                   Total:    ~1-3 min
```

### 4.2 Error Scenarios

| Scenario | Detection | Action |
|----------|-----------|--------|
| Invalid invoice | Step 1 | Raise ValueError immediately |
| Amount below minimum (1000 sats) | Step 2 | Raise ValueError |
| Amount above maximum (25M sats) | Step 2 | Raise ValueError |
| Insufficient L-BTC balance | Step 6 | Raise ValueError with balance info |
| Watch-only wallet | Step 6 | Raise ValueError |
| Passphrase required | Step 7 | Raise ValueError |
| Boltz can't pay invoice | Step 8 (`invoice.failedToPay`) | Return error with refund info |
| Swap expires during polling | Step 8 (`swap.expired`) | Return error with refund info |
| Network timeout during polling | Step 8 | Raise after max retries, save state |
| Polling timeout (>5 min) | Step 8 | Return partial status, swap continues in background |

### 4.3 Polling Strategy

```python
TERMINAL_SUCCESS = {"transaction.claimed"}
TERMINAL_FAILURE = {"invoice.failedToPay", "swap.expired", "transaction.lockupFailed"}
POLL_INTERVAL = 5      # seconds between status checks
POLL_TIMEOUT = 300     # 5 minutes max wait

def poll_swap_status(self, swap_id, timeout=POLL_TIMEOUT):
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        status = self.get_swap_status(swap_id)["status"]
        if status != last_status:
            logger.info(f"Swap {swap_id}: {status}")
            last_status = status
        if status in TERMINAL_SUCCESS:
            return status
        if status in TERMINAL_FAILURE:
            raise SwapFailedError(swap_id, status)
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Swap {swap_id} still in progress: {last_status}")
```

Use HTTP polling (not WebSocket) because:
- Simpler implementation (no new dependencies)
- Current tool framework is synchronous
- 5s interval is fine for 1-2 minute swaps
- Liquid blocks are ~1 minute, so faster polling adds no value

---

## 5. Integration Points

### 5.1 Changes to `tools.py`

```python
# New import
from .boltz import BoltzClient, generate_keypair, verify_preimage, SwapInfo

# New tool function
def lbtc_pay_invoice(
    invoice: str,
    wallet_name: str = "default",
    passphrase: str | None = None,
) -> dict[str, Any]:
    """Pay a Lightning invoice using L-BTC via Boltz submarine swap."""
    ...

def lbtc_swap_status(swap_id: str) -> dict[str, Any]:
    """Check the status of a Boltz submarine swap."""
    ...

# Add to TOOLS dict
TOOLS = {
    ...
    "lbtc_pay_invoice": lbtc_pay_invoice,
    "lbtc_swap_status": lbtc_swap_status,
}
```

### 5.2 Changes to `server.py`

Add to `TOOL_SCHEMAS`:

```python
"lbtc_pay_invoice": {
    "description": "Pay a Lightning invoice using Liquid Bitcoin (L-BTC) via Boltz submarine swap. Sends L-BTC from your wallet, Boltz pays the Lightning invoice. Fees: ~0.1% + 19 sats. Limits: 1,000 - 25,000,000 sats.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "invoice": {
                "type": "string",
                "description": "BOLT11 Lightning invoice to pay (lnbc...)",
            },
            "wallet_name": {
                "type": "string",
                "description": "Liquid wallet to pay from",
                "default": "default",
            },
            "passphrase": {
                "type": "string",
                "description": "Passphrase to decrypt mnemonic (if encrypted)",
            },
        },
        "required": ["invoice"],
    },
},
"lbtc_swap_status": {
    "description": "Check the status of a Boltz submarine swap",
    "inputSchema": {
        "type": "object",
        "properties": {
            "swap_id": {
                "type": "string",
                "description": "Boltz swap ID",
            },
        },
        "required": ["swap_id"],
    },
},
```

Add MCP prompt for `pay_lightning`:

```python
Prompt(
    name="pay_lightning",
    description="Pay a Lightning invoice using Liquid Bitcoin",
    arguments=[
        PromptArgument(name="wallet_name", description="Wallet name", required=False),
    ],
),
```

Update server instructions to include Lightning payment workflow.

### 5.3 Changes to `storage.py`

```python
class Storage:
    def __init__(self, ...):
        ...
        self.swaps_dir = self.base_dir / "swaps"

    def _ensure_dirs(self):
        ...
        self.swaps_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.swaps_dir, 0o700)

    def save_swap(self, swap: SwapInfo):
        """Save swap data for recovery."""
        path = self.swaps_dir / f"{swap.swap_id}.json"
        # Use same atomic write pattern as save_wallet

    def load_swap(self, swap_id: str) -> SwapInfo | None:
        """Load swap data."""
        path = self.swaps_dir / f"{swap_id}.json"
        if not path.exists():
            return None
        with open(path) as f:
            return SwapInfo(**json.load(f))

    def list_swaps(self) -> list[str]:
        """List all swap IDs."""
        return [p.stem for p in self.swaps_dir.glob("*.json")]
```

### 5.4 Changes to `pyproject.toml`

```toml
dependencies = [
    "lwk>=0.8.0",
    "mcp>=1.0.0",
    "cryptography>=42.0.0",
    "bdkpython>=2.2.0",
    "coincurve>=21.0.0",        # NEW - secp256k1 for keypair generation
]
```

### 5.5 Changes to `CLAUDE.md` / `AGENTS.md`

Update tool count (16 -> 18), add Boltz tools to table, add swap data to storage section.

---

## 6. Dependencies Analysis

### New: `coincurve>=21.0.0`

| Platform | Wheel Available |
|----------|----------------|
| Linux x86_64 | manylinux |
| Linux ARM64 | manylinux |
| macOS ARM64 (Apple Silicon) | macosx_11_0_arm64 |
| macOS x86_64 | macosx_10_13_x86_64 |
| Windows ARM64 | win_arm64 |
| Windows x86_64 | via source |

Full cross-platform support. Pure C extension wrapping libsecp256k1. No Rust toolchain needed.

### NOT adding (v1 simplification)

- `httpx` - not needed, using `urllib.request` (already in use)
- `websockets` - not needed, using HTTP polling
- `bolt11` - not needed, Boltz API handles invoice validation
- `secp256k1-zkp` - not needed without MuSig2

---

## 7. Security Design

### 7.1 Private Key Handling

The ephemeral refund private key:
- Generated per-swap using `secrets.token_bytes(32)` (CSPRNG)
- Stored in `~/.aqua-mcp/swaps/{id}.json` with `0o600` permissions
- Only used if swap fails and user needs to manually refund
- NOT sent to Boltz API (only the public key is sent)

### 7.2 Preimage Verification

After Boltz claims and the claim details become available:

```python
import hashlib

def verify_preimage(preimage_hex: str, payment_hash_hex: str) -> bool:
    preimage = bytes.fromhex(preimage_hex)
    computed = hashlib.sha256(preimage).hexdigest()
    return computed == payment_hash_hex.lower()
```

This proves the Lightning invoice was actually paid. For v1 (no cooperative claim), this is informational rather than gating, but still important for the user to verify.

### 7.3 Amount Validation

Before sending L-BTC to lockup:
1. Verify `expectedAmount` from Boltz is reasonable: `invoice_amount * 1.005 + miner_fees` (approximately)
2. Check user has sufficient L-BTC balance
3. The amount Boltz requests includes all fees

### 7.4 Refund Safety

If swap fails, the user's L-BTC is locked until `timeoutBlockHeight`. The refund private key stored on disk allows recovery. The AI assistant should inform the user:
- The swap ID for reference
- That funds are recoverable after the timeout
- To keep the `~/.aqua-mcp/swaps/` directory intact

---

## 8. Testing Strategy

### 8.1 Unit Tests (`tests/test_boltz.py`)

```python
# Test BoltzClient with mocked HTTP responses
class TestBoltzClient:
    def test_get_submarine_pairs(self, mock_urlopen)
    def test_create_submarine_swap(self, mock_urlopen)
    def test_get_swap_status(self, mock_urlopen)
    def test_get_claim_details(self, mock_urlopen)
    def test_poll_swap_status_success(self, mock_urlopen)
    def test_poll_swap_status_failure(self, mock_urlopen)
    def test_poll_swap_status_timeout(self, mock_urlopen)
    def test_api_error_handling(self, mock_urlopen)

# Test keypair generation
class TestKeypair:
    def test_generate_keypair_valid_pubkey()
    def test_generate_keypair_unique()

# Test preimage verification
class TestPreimage:
    def test_verify_preimage_valid()
    def test_verify_preimage_invalid()

# Test swap persistence
class TestSwapStorage:
    def test_save_and_load_swap(self, tmp_path)
    def test_list_swaps(self, tmp_path)
```

### 8.2 Integration Test (`tests/test_tools.py` additions)

```python
# Test full lbtc_pay_invoice flow with mocked Boltz API + mocked lw_send
class TestLbtcPayInvoice:
    def test_pay_invoice_happy_path(self, mock_boltz, mock_lw_send)
    def test_pay_invoice_insufficient_balance(self, mock_boltz)
    def test_pay_invoice_invalid_invoice(self)
    def test_pay_invoice_swap_failure(self, mock_boltz, mock_lw_send)
    def test_pay_invoice_watch_only_wallet(self)
    def test_pay_invoice_passphrase_required(self)
```

### 8.3 Mock Fixtures

```python
MOCK_SUBMARINE_PAIRS = {
    "L-BTC/BTC": {
        "rate": 1.0,
        "fees": {"percentage": 0.1, "minerFees": 19},
        "limits": {"maximal": 25000000, "minimal": 1000, "maximalZeroConf": 500000},
    }
}

MOCK_SWAP_RESPONSE = {
    "id": "test_swap_123",
    "address": "lq1qqexampleaddress",
    "expectedAmount": 50069,
    "claimPublicKey": "03" + "ab" * 32,
    "swapTree": {"claimLeaf": {...}, "refundLeaf": {...}},
    "timeoutBlockHeight": 2500000,
}

MOCK_CLAIM_DETAILS = {
    "preimage": "aa" * 32,
    "transactionHash": "bb" * 32,
    "pubNonce": "cc" * 33,
}
```

---

## 9. File Changes Summary

| File | Action | Changes |
|------|--------|---------|
| `src/aqua_mcp/boltz.py` | **CREATE** | BoltzClient, SwapInfo, generate_keypair, verify_preimage |
| `src/aqua_mcp/tools.py` | EDIT | Add `lbtc_pay_invoice`, `lbtc_swap_status`, update TOOLS dict |
| `src/aqua_mcp/server.py` | EDIT | Add tool schemas, prompt, update server instructions |
| `src/aqua_mcp/storage.py` | EDIT | Add swaps_dir, save_swap, load_swap, list_swaps |
| `pyproject.toml` | EDIT | Add `coincurve>=21.0.0` dependency |
| `tests/test_boltz.py` | **CREATE** | Unit tests for boltz module |
| `tests/test_tools.py` | EDIT | Add integration tests for new tools |
| `CLAUDE.md` | EDIT | Update tool count, add swap tools + storage docs |

---

## 10. Future Enhancements (Out of Scope for v1)

1. **MuSig2 cooperative claiming** - Better privacy and lower fees
2. **Reverse submarine swaps** - Receive Lightning -> L-BTC (tool: `lbtc_receive_lightning`)
3. **Automated refund tool** - `lbtc_refund_swap` for failed swaps
4. **Chain swaps** - BTC <-> L-BTC via Boltz
5. **Invoice generation** - Pay from BTC wallet directly (BTC -> Lightning)
6. **Fee estimation tool** - `lbtc_estimate_lightning_fee` to show costs before committing
7. **WebSocket status streaming** - Replace polling for real-time updates

---

## 11. Open Questions for Review

1. **Tool naming**: `lbtc_pay_invoice` vs `lw_pay_invoice` vs `boltz_pay_invoice`?
   - Recommendation: `lbtc_pay_invoice` (matches the L-BTC asset, clear intent)

2. **Blocking duration**: The tool may block for 1-3 minutes. Is this acceptable for MCP?
   - Current `btc_send` and `lw_send` already block for sync + broadcast (~5-10s)
   - Alternative: Split into `lbtc_create_swap` + `lbtc_poll_swap` for async flow

3. **Network support**: Should we support testnet swaps?
   - Boltz has a testnet API. Worth adding for development/testing
   - Derive network from wallet's stored network setting

4. **Error recovery**: If the tool crashes mid-swap (after sending L-BTC but before confirming):
   - Swap data is persisted to disk after step 5 (before sending)
   - User can use `lbtc_swap_status` to check and Boltz will still complete the swap
   - Worst case: wait for timeout and refund using stored private key
