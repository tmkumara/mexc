# EMA MEXC Futures Historical Kline Contract

Task 16–19 research report. No application code, tests, databases, or
production systems were modified.

## Sources

### DOCUMENTED — official MEXC Futures API documentation

- [MEXC Contract API — K-line data](https://mexcdevelop.github.io/apidocs/contract_v1_en/#k-line-data)
- [MEXC official API demo — futures market API](https://github.com/mexcdevelop/mexc-api-demo/blob/main/python/futures/api/src/market_api.py)
- [MEXC official Postman collection](https://github.com/mexcdevelop/mexc-api-postman/blob/main/MEXC%20V1%20contract.postman_collection.json)

The repository’s [mexc_client.py](../mexc_client.py),
[backtest/fetch_data.py](../backtest/fetch_data.py), and `AGENTS.md` agree with
the endpoint and interval mappings below, but the official documentation is the
primary source for limits.

## Payload fields

### DOCUMENTED

The parser now uses the official response fields as its only supported
contract:

```json
{
  "success": true,
  "data": {
    "time": [],
    "open": [],
    "high": [],
    "low": [],
    "close": [],
    "vol": []
  }
}
```

The `realOpen`/`realHigh`/`realLow`/`realClose`/`realVolume` family is not
supported by the standalone EMA parser. It came from the legacy repository
implementation and is not required by the new EMA bot, so it was intentionally
not retained as backward compatibility.

The official Postman collection contains a sample with millisecond-looking
request values, but the official K-line parameter table and endpoint example
specify Unix seconds. The EMA parser and client follow the official K-line
parameter table; live verification remains blocked.

## Endpoint and parameters

### DOCUMENTED

```text
GET https://contract.mexc.com/api/v1/contract/kline/{symbol}
```

Documented request parameters:

| Parameter | Meaning |
|---|---|
| `symbol` | Contract symbol, for example `SOL_USDT` |
| `interval` | `Min15` or `Min60` for this EMA implementation |
| `start` | Optional Unix timestamp in seconds |
| `end` | Optional Unix timestamp in seconds |

The public market-data endpoint does not require API credentials. No private
account or trading endpoint is involved.

The current EMA client maps:

| EMA timeframe | MEXC interval |
|---|---|
| `15m` | `Min15` |
| `1h` | `Min60` |

## Historical limits

### DOCUMENTED

The official documentation states that a single request returns at most 2,000
candles. If the requested time range and interval would produce more than that,
the API returns only 2,000 candles and the caller must make multiple requests.

The documentation also states:

- `start` only: data from `start` through the current system time.
- `end` only: the 2,000 candles closest to `end`.
- Neither boundary: the 2,000 candles closest to the current system time.

This client should always provide both explicit boundaries. It must not use
implicit current-time behavior.

There is no documented maximum calendar-date age in the K-line section. The
documentation does not state that 15m and 1h have different historical
availability limits.

## Timestamp and boundary semantics

### DOCUMENTED

- Request timestamps are Unix seconds.
- Response `time` values are documented as time-window timestamps in Unix
  seconds.
- The endpoint uses start and end time parameters.
- The official documentation does not explicitly state whether `start` and
  `end` are inclusive or exclusive.

### UNKNOWN

- Whether `start` is inclusive.
- Whether `end` is inclusive.
- Whether response timestamps represent candle-open time in every case.
- Whether responses are always ascending or may be reverse chronological.
- Whether a range with no available historical data is distinguishable from a
  malformed or truncated response solely from the response body.

The local adapter treats response timestamps as candle-open timestamps and
accepts a fully ascending or fully descending array, but this behavior has not
been live-verified against the endpoint.

## Rate limits

### DOCUMENTED

The official K-line endpoint documents a rate limit of 20 requests per 2
seconds. The client should remain below that limit and apply bounded retries for
429 and 5xx responses.

## Live verification status

### OBSERVED — local DNS/connectivity blocked

One fixed public `Min15` request was attempted and then live verification was
stopped as required:

```text
GET https://contract.mexc.com/api/v1/contract/kline/SOL_USDT
    ?interval=Min15&start=1735776000&end=1735784100
```

Before making any HTTP request, the requested Windows checks were run once.

`Resolve-DnsName contract.mexc.com` failed with:

```text
contract.mexc.com : Access is denied.
```

`Test-NetConnection contract.mexc.com -Port 443` reported:

```text
TcpTestSucceeded: False
RemoteAddress: <none>
```

Because DNS and TCP connectivity failed, no HTTP request was attempted.

The earlier Task 17 request from this environment had also failed before an
HTTP response with:

```text
System.Net.Http.HttpRequestException:
No such host is known. (contract.mexc.com:443)
```

No Min15 or Min60 request, retry loop, adjacent-boundary check, large-range
request, parser pass, or raw fixture capture was performed in Task 19.

Consequently, this report makes no live claims about:

- returned candle counts;
- first or last timestamps;
- response ordering;
- start/end inclusivity;
- adjacent-range overlap;
- behavior for a range above 2,000 candles;
- SOL_USDT historical availability.

The parser was therefore not tested against an actual MEXC response in Tasks
17–19. No `tests/fixtures/ema_mexc_live_sample.json` file was created.

## Recommended pagination strategy

### INFERRED — based on the documented 2,000-candle cap

Use deterministic time pagination with a conservative maximum of 1,900
candles per request:

| Timeframe | Conservative request size | Approximate covered span |
|---|---:|---:|
| `15m` | 1,900 candles | 19 days 19 hours 45 minutes between first and last opens |
| `1h` | 1,900 candles | 79 days 3 hours between first and last opens |

The 1,900 size leaves margin below the documented 2,000 cap while preserving a
simple boundary check. It is not an official API limit.

For a requested range:

1. Align the requested start and end to candle-open boundaries.
2. Request at most 1,900 expected candle opens at a time.
3. Set the next request start to the previous request end boundary. This
   intentionally permits one known boundary overlap if MEXC treats `end` as
   inclusive.
4. Concatenate responses in UTC timestamp order.
5. Deduplicate only an exact timestamp repeated at the planned boundary.
6. Reject any other duplicate timestamp.
7. Verify every expected candle-open timestamp in the requested range is
   present before returning.
8. Reject a response that does not advance the pagination cursor or returns
   more than the requested range.

Task 21B below confirms inclusive boundaries for the tested `SOL_USDT`
request pair. Other symbols, historical periods, and undocumented edge cases
remain subject to the continuity checks in the client.

## Risks and unresolved questions

### UNKNOWN

- Earlier verification attempts lacked live access; Task 21B now records a
  bounded successful live observation for `SOL_USDT` below.
- Documentation does not specify start/end inclusivity.
- Documentation does not specify response ordering.
- Documentation does not specify whether a symbol’s listing date or delisting
  creates an explicit response distinction.
- Documentation does not specify whether the 2,000-candle cap is applied before
  or after time-bound filtering in every edge case.

### OBSERVED locally

- The existing repository parser uses Unix seconds.
- The existing parser sorts returned candles ascending.
- The existing backtest fetcher already splits broad ranges into time windows,
  but it does not establish the official 2,000-candle semantics.
- The new EMA client currently rejects incomplete coverage instead of silently
  accepting a truncated response.
- Task 20's deterministic mocked tests verify 1,900-candle windows, one planned
  shared boundary between adjacent windows, strict overlap handling, and final
  contiguous coverage checks. These are local implementation observations, not
  live exchange observations.

## Task 21B live verification

### OBSERVED — public requests on 2026-10-19

Requests were made to the public endpoint for `SOL_USDT` using fixed UTC
historical ranges and no credentials:

- `Min15`, `2026-10-01T00:00:00Z` through `2026-10-01T02:15:00Z`: HTTP 200,
  `success=true`, `code=0`, 10 candles.
- `Min60`, `2026-10-01T00:00:00Z` through `2026-10-01T09:00:00Z`: HTTP 200,
  `success=true`, `code=0`, 10 candles.

The actual response data fields were:

```text
time, open, close, high, low, vol, amount,
realOpen, realClose, realHigh, realLow
```

The documented fields are present. `time` values are Unix seconds. Both
responses were ascending, with first timestamps at the requested starts and
last timestamps at the requested ends. The parser and validator accepted both
responses without modification.

### OBSERVED — boundary behavior

Two adjacent `Min15` requests were made:

- `2026-10-01T00:00:00Z` through `2026-10-01T01:00:00Z`
- `2026-10-01T01:00:00Z` through `2026-10-01T02:00:00Z`

Each returned five candles. The shared `2026-10-01T01:00:00Z` candle appeared
in both responses. This demonstrates inclusive start and inclusive end behavior
for this observed request pair.

### OBSERVED — request above 2,000 candles

A single `Min15` request for `2026-09-01T00:00:00Z` through
`2026-09-21T20:00:00Z` requested 2,001 expected candles. It returned 2,001
candles, from the exact requested first timestamp through the exact requested
last timestamp, with ascending ordering and no observed truncation.

This does not disprove the documented 2,000-candle limit for every request
shape, but it means the limit was not observed in this bounded test.

### OBSERVED — live parser and pagination compatibility

The live `Min15` and `Min60` responses passed through
`parse_mexc_futures_candles()` and `validate_candles()` successfully. A live
`EmaMexcClient.fetch_candles()` request from `2026-09-01T00:00:00Z` through
`2026-09-20T20:00:00Z` returned 1,905 unique ascending candles, confirming the
current 1,900-window pagination and planned-overlap deduplication worked for a
range crossing the pagination boundary.

No application code or live fixture was modified during this verification.

## Smallest recommended Task 22

Add focused regression tests for the now-observed inclusive boundaries and
documented-field payload shape, using sanitized mocked responses only. Keep
the 1,900-candle safety window unless a broader API-limit study is explicitly
approved; the live test did not establish a reliable hard maximum above 2,000.
