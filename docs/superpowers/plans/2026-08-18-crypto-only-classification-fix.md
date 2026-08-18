# Crypto-Only Classification Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop tokenized-stock futures contracts (`TESLA_USDT`, `SOXL_USDT`, and similar) from entering the coin pool when `CRYPTO_FUTURES_ONLY=true`, without narrowing the legitimate crypto universe.

**Architecture:** `coin_scanner._is_crypto_symbol` currently does a substring search of `_NON_CRYPTO_KEYWORDS` (index/commodity/metal terms) against the *whole* contract symbol string. It has zero coverage for individual equity/ETF tickers, so `TESLA_USDT` and `SOXL_USDT` both passed the filter and fired live signals (`[SIGNAL] Confirmed #17 TESLA_USDT LONG`, `#2`/`#35 SOXL_USDT`, per `mexc_bot.log`). Fix: add a second, exact-match check against a maintained set of known tokenized-stock/ETF base coins, keyed off the base-coin token (`symbol` minus the `_USDT` suffix) rather than a whole-string substring search — exact matching avoids the substring-collision risk a blocklist otherwise carries (e.g. a hypothetical coin symbol that happens to contain "OIL" or "NAS" as a substring). The existing index/commodity/metal keyword list is left untouched — it's a different, lower-collision-risk category and already deployed correctly.

**Tech Stack:** Python, pytest (`monkeypatch`).

## Global Constraints

- Do not touch `_NON_CRYPTO_KEYWORDS` or its existing substring-match behavior — it stays as-is, this plan adds a second, independent check alongside it.
- `_is_crypto_symbol` must still return `True` unconditionally when `CRYPTO_FUTURES_ONLY` is `False` (existing behavior, do not change).
- No live behavior change beyond narrowing the pool — do not touch `coin_scanner`'s ranking/scoring logic, only the crypto/non-crypto classification gate.
- This is a blocklist, not a perfect classifier — MEXC lists new tokenized-stock contracts periodically and this list will need future additions. Document that limitation in the code comment; do not attempt to build a general classifier against `/contract/detail` metadata (its schema doesn't expose an asset-class field to key off, confirmed by inspecting `mexc_client.get_all_contracts`'s raw passthrough).
- Run `python -m pytest -v` and confirm passing before committing.

---

## File Structure

**New files:**
- `tests/test_coin_scanner_crypto_filter.py` — tests for `_is_crypto_symbol` covering both the existing keyword path and the new stock-ticker path.

**Modified files:**
- `coin_scanner.py` — add `_STOCK_TICKER_BASE_COINS` and the exact-match check inside `_is_crypto_symbol`.

---

## Task 1: Exact-match stock-ticker blocklist

**Files:**
- Modify: `coin_scanner.py:98-137`
- Test: `tests/test_coin_scanner_crypto_filter.py`

**Interfaces:**
- Consumes: `config.CRYPTO_FUTURES_ONLY`, `config.QUOTE_CURRENCY` (both already imported in `coin_scanner.py`).
- Produces: `coin_scanner._is_crypto_symbol(symbol: str) -> bool` — same signature as today, callers (`_is_contract_active` and four other call sites already in the file) are unaffected.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coin_scanner_crypto_filter.py
import config
import coin_scanner


def test_known_index_keyword_still_blocked(monkeypatch):
    monkeypatch.setattr(config, "CRYPTO_FUTURES_ONLY", True)
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("NAS100_USDT") is False


def test_tesla_is_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("TESLA_USDT") is False


def test_soxl_is_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("SOXL_USDT") is False


def test_tsla_ticker_variant_is_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("TSLA_USDT") is False


def test_legitimate_coin_with_similar_prefix_not_blocked(monkeypatch):
    # exact base-coin match must not false-positive on a real coin whose
    # symbol merely starts with a blocked ticker's letters
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("METAVERSE_USDT") is True


def test_real_crypto_coin_not_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("DOGE_USDT") is True
    assert coin_scanner._is_crypto_symbol("BTC_USDT") is True


def test_returns_true_unconditionally_when_filter_disabled(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", False)
    assert coin_scanner._is_crypto_symbol("TESLA_USDT") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_coin_scanner_crypto_filter.py -v`
Expected: `test_known_index_keyword_still_blocked`, `test_legitimate_coin_with_similar_prefix_not_blocked`, and `test_real_crypto_coin_not_blocked` PASS already (existing behavior); `test_tesla_is_blocked`, `test_soxl_is_blocked`, `test_tsla_ticker_variant_is_blocked` FAIL (current bug — these currently return `True`).

- [ ] **Step 3: Add the stock-ticker blocklist and exact-match check**

In `coin_scanner.py`, immediately after the existing `_NON_CRYPTO_KEYWORDS` tuple (ends at line 125 today), add:

```python
# Tokenized single-stock / equity-ETF perpetuals MEXC lists alongside real
# crypto pairs. These carry no shared substring with real coin tickers, so
# they're matched by EXACT base-coin equality (symbol minus the quote
# suffix), not the substring search _NON_CRYPTO_KEYWORDS uses above --
# substring matching a bare 3-5 letter ticker like "META" or "AMD" against
# the whole symbol would false-positive on real coins that merely contain
# those letters. This list is a known-incomplete blocklist: MEXC adds new
# tokenized-stock listings periodically, and each one needs adding here.
_NON_CRYPTO_BASE_COINS = frozenset({
    "TESLA", "TSLA", "SOXL", "SOXS", "NVDA", "AAPL", "AMZN", "GOOGL",
    "GOOG", "META", "MSFT", "AMD", "COIN", "MSTR", "PLTR", "HOOD",
    "NFLX", "DIS", "BA", "SPY", "QQQ", "TQQQ", "SQQQ", "GME", "AMC",
    "CRCL", "IBIT",
})
```

Then change `_is_crypto_symbol` (currently):

```python
def _is_crypto_symbol(symbol: str) -> bool:
    """
    Returns False for MEXC stock/index/commodity style contracts.
    Always returns True when CRYPTO_FUTURES_ONLY=False.
    """
    if not CRYPTO_FUTURES_ONLY:
        return True

    upper = symbol.upper()
    return not any(keyword in upper for keyword in _NON_CRYPTO_KEYWORDS)
```

to:

```python
def _is_crypto_symbol(symbol: str) -> bool:
    """
    Returns False for MEXC stock/index/commodity style contracts.
    Always returns True when CRYPTO_FUTURES_ONLY=False.
    """
    if not CRYPTO_FUTURES_ONLY:
        return True

    upper = symbol.upper()
    base_coin = upper.rsplit("_", 1)[0]
    if base_coin in _NON_CRYPTO_BASE_COINS:
        return False
    return not any(keyword in upper for keyword in _NON_CRYPTO_KEYWORDS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_coin_scanner_crypto_filter.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -v`
Expected: no regressions — `_is_crypto_symbol`'s other call sites (`_is_contract_active` and the four filtering sites in `coin_scanner.py`) are unaffected since the function signature and default behavior for non-listed symbols are unchanged.

- [ ] **Step 6: Compile check**

Run: `python -m py_compile coin_scanner.py`
Expected: no output (success).

- [ ] **Step 7: Commit**

```bash
git add coin_scanner.py tests/test_coin_scanner_crypto_filter.py
git commit -m "fix: block tokenized-stock tickers from CRYPTO_FUTURES_ONLY pool

TESLA_USDT and SOXL_USDT both fired live signals despite
CRYPTO_FUTURES_ONLY=true -- the existing _NON_CRYPTO_KEYWORDS list only
covers index/commodity/metal terms, never individual equity tickers.
Adds an exact base-coin-match blocklist alongside the existing substring
check."
```

---

## Self-Review

- **Spec coverage:** Priority 8 of `architecture.txt` asked to "inspect the crypto-only classification logic" and "fix the classification/filtering bug without breaking legitimate MEXC symbols" — Task 1 does both: the bug is fixed (TESLA/SOXL blocked) and legitimate symbols are protected by using exact match instead of a broader substring rule for the new list.
- **Placeholder scan:** No TBDs — the blocklist is a concrete list, the code diff is exact, tests have real assertions.
- **Type consistency:** `_is_crypto_symbol(symbol: str) -> bool` signature unchanged; no other function signatures introduced.
- **Known limitation carried forward to the roadmap:** this is a blocklist, not a classifier — flagged in both the code comment and this plan's Global Constraints, not silently glossed over.
