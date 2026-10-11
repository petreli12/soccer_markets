# API notes for pm-edge-soccer

Researched 2026-10-11 against the live official docs and verified with unauthenticated
probes of the production APIs on the same day. Everything here is **read-only public market
data**. No endpoint listed below requires trading credentials, and the tool must never call
any order, portfolio, or account endpoint.

Sources:
- Kalshi: https://docs.kalshi.com (OpenAPI v3.34.0), fee schedule PDF https://kalshi.com/docs/kalshi-fee-schedule.pdf (7 Jul 2026 update)
- Polymarket: https://docs.polymarket.com (Gamma + CLOB OpenAPI), https://docs.polymarket.com/trading/fees
- The Odds API: https://the-odds-api.com/liveapi/guides/v4/ and https://the-odds-api.com/sports-odds-data/bookmaker-apis.html
  (Note: `theoddsapi.com`, without hyphens, is a *different* company with different pricing. Ignore it.)

---

## 1. Kalshi Trade API v2

### Base URL and auth
- REST: `https://external-api.kalshi.com/trade-api/v2` (recommended). `https://api.elections.kalshi.com/trade-api/v2` is still supported.
- All endpoints below are public: **no auth headers needed**. Verified live.
- WebSocket requires authentication even for public channels, so we **do not use it** (it would need an API key). We poll REST instead.

### Rate limits
- Documented limits are token buckets for *authenticated* users: Basic tier = 200 read tokens/s, default cost 10 tokens/request, so about 20 req/s sustained with a 3 s burst bucket.
- **Unauthenticated limits are not published.** Back off exponentially on HTTP 429. A 429 response has body `{"error": "too many requests"}` and no `Retry-After` header.
- Observed 2026-10-11: requests spaced evenly at 10/s drew 9 × 429 in about 250 calls. The default is set to 6 req/s.

### Soccer series (verified live)
| League | Series ticker | fee_type | fee_multiplier |
|---|---|---|---|
| EPL | `KXEPLGAME` | `quadratic_with_maker_fees` | 1 |
| UCL | `KXUCLGAME` | `quadratic_with_maker_fees` | 1 |

Future NBA would be a different series ticker, read from config.

### Endpoints we use
| Purpose | Endpoint | Key params | Notes |
|---|---|---|---|
| Series metadata (fees) | `GET /series/{series_ticker}` or `GET /series?category=Sports&tags=Soccer` | | Returns `fee_type` and `fee_multiplier` |
| List match events | `GET /events` | `series_ticker`, `status` (`unopened`/`open`/`closed`/`settled`), `with_nested_markets=true`, `with_milestones=true`, `min_close_ts`, `limit` (max 200), `cursor` | Events are never archived, but nested markets older than the historical cutoff are omitted |
| List markets | `GET /markets` | `series_ticker`, `event_ticker`, `status`, `tickers`, `min_close_ts`/`max_close_ts`, `limit` (max 1000), `cursor` | |
| Candlesticks (live tier) | `GET /series/{series_ticker}/markets/{ticker}/candlesticks` | `start_ts`, `end_ts` (unix s, required), `period_interval` in {1, 60, 1440} | |
| Candlesticks (archived) | `GET /historical/markets/{ticker}/candlesticks` | same params | For markets settled before `market_settled_ts` cutoff |
| | | | **Undocumented limit (verified):** a request range may cover at most **5,000 candles**, otherwise you get 400 `"max candlesticks: 5000"`. Chunk long ranges. |
| Historical cutoff | `GET /historical/cutoff` | | On 2026-10-11: `market_settled_ts = 2026-08-12T00:00:00Z` |
| Archived markets | `GET /historical/markets`, `GET /historical/markets/{ticker}` | | |
| Order book | `GET /markets/{ticker}/orderbook` | `depth` (optional) | |
| Batch order books | `GET /markets/orderbooks` | `tickers` **repeated** (`?tickers=A&tickers=B`), max 100 | Works unauthenticated (verified). Comma-joined tickers silently return one empty book. |

Pagination: cursor-based. Pass `cursor` back until it comes back empty.

### Match structure (how soccer is modelled)
One **event** per match, e.g. `KXEPLGAME-26OCT25MUNBOU`, with `mutually_exclusive: true`
(`collateral_return_type: MECNET`) and **three separate binary yes/no markets**:

| Market ticker suffix | `yes_sub_title` | Outcome |
|---|---|---|
| `-{HOME_ABBR}` e.g. `-MUN` | "Manchester United" | HOME |
| `-{AWAY_ABBR}` e.g. `-BOU` | "Bournemouth" | AWAY |
| `-TIE` | "Tie" | DRAW |

- Event `title` is `"{Home} vs {Away}"` (e.g. "Manchester United vs Bournemouth") and `sub_title` is `"MUN vs BOU (Oct 25)"`. I checked home-first ordering against Polymarket's explicit `teams[].ordering` for four EPL fixtures on 24 Oct, and all matched. Matching will still cross-check against the sportsbook `home_team`.
- `product_metadata.competition` = `"EPL"`.
- Outcome mapping rule: the `-TIE` suffix maps to DRAW. Otherwise compare `yes_sub_title` with the title halves (home first). The ticker abbreviation is the fallback.
- **Kickoff time:** use the milestone `start_date` (from `with_milestones=true`, joined on `primary_event_tickers`), e.g. `2026-10-25T16:30:00Z`.
  **Do not** use the market's `expected_expiration_time` or `occurrence_datetime`. Those are **kickoff + 3 h** (e.g. `19:30Z`), and `close_time` is about kickoff + 48 h.
- Milestone `details.status` (`not_started`, ...) can tag in-play state in live data.

### Response shapes
Prices are fixed-point **dollar strings** (`"0.5300"`, up to 4 dp in quotes, up to 6 in responses). Sizes are `_fp` strings with 2 dp (`"80.19"`). Contracts pay $1.

Market (abridged):
```json
{"ticker":"KXEPLGAME-26OCT25MUNBOU-MUN","event_ticker":"KXEPLGAME-26OCT25MUNBOU",
 "yes_sub_title":"Manchester United","status":"active",
 "yes_bid_dollars":"0.5300","yes_ask_dollars":"0.5700","no_bid_dollars":"...","no_ask_dollars":"...",
 "yes_bid_size_fp":"...","yes_ask_size_fp":"...","last_price_dollars":"...",
 "volume_fp":"80.19","open_interest_fp":"...","result":"", "close_time":"...",
 "price_level_structure":"linear_cent","price_ranges":[{"start":"0.0000","end":"1.0000","step":"0.0100"}]}
```
Market status values: `initialized, inactive, active, closed, determined, disputed, amended, finalized`. The `status` *filter* values are different: `unopened, open, paused, closed, settled`.

Order book: **bids only**, for both YES and NO, as `[price_dollars, count_fp]`, **sorted ascending, so the best bid is the last element**:
```json
{"orderbook_fp":{"yes_dollars":[["0.4900","185.00"],["0.5300","200.00"]],
                 "no_dollars":[["0.4200","3647.00"],["0.4300","460.84"]]}}
```
Asks are implied:
- YES ask = 1 − best NO bid, with size equal to that NO bid's size
- NO ask = 1 − best YES bid

Top-5 ask depth on YES therefore comes from the top 5 NO bids.

Live candlestick (period 1 min):
```json
{"end_period_ts":1791682500,
 "yes_bid":{"open_dollars":"0.3100","high_dollars":"0.3100","low_dollars":"0.3100","close_dollars":"0.3100"},
 "yes_ask":{"open_dollars":"0.3200","high_dollars":"...","low_dollars":"...","close_dollars":"0.3200"},
 "price":{"open_dollars":null,"close_dollars":null,"mean_dollars":null,"previous_dollars":"0.3300"},
 "volume_fp":"5.96","open_interest_fp":"87898.97"}
```
The **historical** candlestick endpoint uses different field names: `open/high/low/close` (no `_dollars` suffix), `mean`, `previous`, `volume`, and `open_interest`. The parser must handle both.

Observations:
- Candles carry **real bid and ask OHLC**. That means historical Kalshi gives YES ask and (as 1 − YES bid) NO ask, not just a mid.
- Candles are **sparse** (1,832 candles over about 3,060 minutes for LIV–MCI), so forward-fill is required.
- `price.*` is null when there were no trades in that minute.

**Verified (2026-10-11): a missing 1-minute candle means "nothing changed".** Test set: 12 markets (4 settled EPL matches × 3 outcomes), window T−48h to T+3h around kickoff.
- 4,394 gaps covering 17,171 missing minutes.
- In **100%** of gaps, the previous candle's `yes_bid.close` equalled the next candle's `yes_bid.open`, and the same held for `yes_ask`.
- **Zero** trades from `GET /markets/trades` fell inside a gap. A first pass counted 24, but all of them were artefacts of truncating fractional-second timestamps; they belong to the following candle's window. Candle `end_period_ts` is the inclusive end of a `(end−60, end]` window.
- Consequence: Kalshi candles are forward-filled **without a staleness limit**, bounded to T−48h through T+3h around kickoff (`normalize.staleness.kalshi_historical: null` in config). This applies to candle history only. Live Kalshi snapshots still use the 5-minute staleness limit, because a missed poll is not proof of no change.
- Re-check at any time with `pm-edge verify-kalshi-gaps --league EPL --matches 4`, which reproduced the result above exactly.
- Archived markets (settled before the cutoff) were also checked. The 10 EPL matches from 20–26 May 2026 resolved via `/historical/markets?event_ticker=` and `/historical/markets/{ticker}/candlesticks`.

### Taker fee (Kalshi)
From the fee schedule (7 Jul 2026), general "quadratic" fee:

```
fee_dollars = round_up( M × 0.07 × C × P × (1 − P) )
```
- P = execution price in dollars, C = contracts, M = series `fee_multiplier` (1 for KXEPLGAME and KXUCLGAME).
- Maker fees (series type `quadratic_with_maker_fees`) use 0.0175 in place of 0.07. Not relevant here, because we model taking liquidity.
- Rounding: "rounds up such that fee + position cost is rounded to a centicent" ($0.0001) for direct members. Non-direct (FCM-cleared) balances are aligned to $0.01. The model is per order: compute the raw fee, then round up to a configurable increment (default $0.01, which is conservative). At 50¢ with 100 contracts the fee is $1.75.
- Events can override fees with `fee_type_override` and `fee_multiplier_override`. The ingest step should read these and prefer them when present.

---

## 2. Polymarket: Gamma API (metadata) and CLOB API (prices and books)

### Base URLs and auth
- Gamma: `https://gamma-api.polymarket.com`. Public, no auth.
- CLOB: `https://clob.polymarket.com`. Market-data endpoints are public (`security: []`). Trading endpoints need L2 auth, which we never use.

### Rate limits (Cloudflare, per IP, sliding window; excess requests are throttled/queued, not rejected)
| API | Endpoint | Limit |
|---|---|---|
| Gamma | general | 4,000 / 10 s |
| Gamma | `/events` | 500 / 10 s |
| Gamma | `/markets` | 300 / 10 s |
| CLOB | `/book` | 1,500 / 10 s |
| CLOB | `/books` (POST batch) | 500 / 10 s |
| CLOB | `/prices-history` | 1,000 / 10 s |
| CLOB | general | 9,000 / 10 s |

### Soccer discovery (verified live)
`GET /sports` returns league metadata:
| League | `sport` | Gamma `series` id | primary tag id | ordering |
|---|---|---|---|---|
| EPL | `epl` | `10188` | 306 | `home` |
| UCL | `ucl` | `10204` | 1234 | `home` |

`ordering: home` means titles are "Home vs. Away".

`GET /events?series_id=10188&closed=false&limit=100` lists events. Each match has **several events**:
- `epl-ast-mac-2026-10-24` is the **full-time 3-way result (the one we want)**
- `...-halftime-result`, `...-second-half-result`, `...-exact-score`, `...-first-to-score`, `...-total-corners`, `...-more-markets`, `...-player-props`

Select the event whose markets all have `sportsMarketType == "moneyline"`, count exactly 3, and whose slug has no suffix after the date.

Useful Gamma params: `series_id`, `tag_id`, `closed`, `active`, `start_date_min`/`max`, `end_date_min`/`max`, `limit`, `offset` (or the keyset endpoint with `after_cursor`).
- Event `startDate` is the **listing** time, about 2 weeks before the match. Event `endDate` equals kickoff. Ingest therefore filters on `end_date_min`/`max`, then re-checks kickoff client-side.
- Verified 2026-10-11: 59 closed EPL base events had kickoffs in the last 60 days, one per Kalshi match.
  - Each fixture also has up to 10 sibling events (halftime-result, exact-score, corners and so on), and all are ignored.
  - All 59 mapped via `opticOddsSelectionLine`.
  - Moneyline markets have `orderPriceMinTickSize` **0.001** (Kalshi uses 0.01) and `orderMinSize` 5.

### Match structure
The match event is a **negative-risk** event (`negRisk: true`, with a `negRiskMarketID`) containing **three binary Yes/No markets**:

| `question` | `groupItemTitle` | `marketMetadata.opticOddsSelectionLine` | Outcome |
|---|---|---|---|
| "Will Aston Villa FC win on 2026-10-24?" | "Aston Villa FC" | `home` | HOME |
| "Will Aston Villa FC vs. Manchester City FC end in a draw?" | "Draw (Aston Villa FC vs. Manchester City FC)" | `draw` | DRAW |
| "Will Manchester City FC win on 2026-10-24?" | "Manchester City FC" | `away` | AWAY |

Outcome mapping rule:
1. Use `marketMetadata.opticOddsSelectionLine` (home/draw/away) when present.
2. **`marketMetadata` is missing on some UCL markets** (verified). Fallback: a `groupItemTitle` starting with "Draw" maps to DRAW. Otherwise match `groupItemTitle` against event `teams[]`, where each team has `ordering: "home"|"away"`, `name`, `abbreviation`, `alias`.
3. Anything that doesn't resolve is sent to the review file. Never guess.

Kickoff: event `startTime` / market `gameStartTime` (e.g. `"2026-10-24 11:30:00+00"`). Market `endDate` is also kickoff.

Team names are long form ("Manchester City FC"), with `alias` ("Man City") and `abbreviation` ("mac") in `teams[]`.

### Token ids (important: Protocol V2)
Every market has a `version`:
- `"v1"`: outcome token ids are in `clobTokenIds`, a **JSON-encoded string** (`"[\"6988...\", \"2359...\"]"`), index-aligned with `outcomes` (also a JSON string, `"[\"Yes\", \"No\"]"`).
- `"v2"`: use `positionIds`, a real array of decimal strings.

Choose by `version` even when both are present (the docs say so explicitly). The selected id is the `token_id` / `market` param for CLOB. All soccer moneyline markets seen today are v1, but the code must handle v2.

For each outcome we keep the **Yes** token (buy the outcome) and the **No** token (buy against the outcome).

### Endpoints we use
| Purpose | Endpoint | Params |
|---|---|---|
| Order book | `GET /book?token_id=...` | |
| Batch order books | `POST /books` | body `[{"token_id": "..."}, ...]` |
| Price history | `GET /prices-history` | `market` = token id (required), `startTs`, `endTs` (unix s), `fidelity` (minutes, default 1) **or** `interval` in {`1m`,`1h`,`6h`,`1d`,`1w`,`max`,`all`} |
| Per-market CLOB params | `GET /clob-markets/{condition_id}` | Returns `gst` (game start time), `fd` = fee details `{r: rate, e: exponent, to: takerOnly}`, `mts` tick, `mos` min size |

### Response shapes
Book:
```json
{"market":"0xffc7...","asset_id":"7348...","timestamp":"1791745396451","hash":"...",
 "bids":[{"price":"0.01","size":"1479.1"}, ..., {"price":"0.52","size":"83.37"}],
 "asks":[{"price":"0.99","size":"1605.34"}, ..., {"price":"0.58","size":"325"}],
 "min_order_size":"5","tick_size":"0.01","neg_risk":true,"last_trade_price":"0.420"}
```
**This conflicts with the docs.** The docs say bids are sorted descending and asks ascending. The live API returns **bids ascending and asks descending, so the best price is the last element of each**. `timestamp` is in **milliseconds**. The implementation will not rely on ordering: best bid = max price, best ask = min price, then sort to take the top 5 levels.

Price history:
```json
{"history":[{"t":1791559831,"p":0.325},{"t":1791559885,"p":0.325}]}
```
- About 1 point per minute at `fidelity=1`. Verified 7+ days back for a match settled today (10,218 points).
- **`p` is a single price, and the docs don't say whether it is midpoint or last trade. There is no bid/ask history.** Historical cross-venue gaps involving Polymarket are therefore labelled `approximate`, which is what you planned.
- Verified 2026-10-11:
  - **No-token history is exactly `1 − p` of Yes-token history** at every shared timestamp, so ingest fetches only the Yes token per outcome.
  - Points are not minute-aligned (e.g. `:16`, `:13` seconds).
  - A whole T−48h…T+3h window (about 3,000 points) comes back in one request.
  - Across the 59-match EPL backfill, history starts the full 48 h before kickoff. Before kickoff there are only 42 gaps longer than 5 min, and the longest is 23 min.
- **Historical Polymarket spread cannot be measured.** Neither Gamma (`bestBid`/`bestAsk` are current only) nor CLOB exposes past quotes. Spread comparisons come from live books, and the step 6 collector records them going forward.

Market fields we keep: `id, conditionId, question, groupItemTitle, sportsMarketType, version, outcomes, clobTokenIds, positionIds, negRisk, gameStartTime, bestBid, bestAsk, liquidityClob, feeSchedule, feesEnabled, clearBookOnStart, secondsDelay, orderMinSize, orderPriceMinTickSize, marketMetadata, closed`.

### In-play mechanics (affects the in-play bucket)
- `clearBookOnStart: true`: all resting orders are wiped at kickoff, so expect a liquidity gap right after kickoff.
- `secondsDelay: 1`: marketable orders are delayed in-play. CLOB field `itode` signals a 250 ms taker delay.

### Taker fee (Polymarket)
Docs formula (makers are never charged):
```
fee_usdc = C × feeRate × p × (1 − p)
```
- Sports `feeRate = 0.05`. The 100-share fee at 50¢ is $1.25. Fees are rounded to 5 dp, and anything below 0.00001 rounds to 0.
- Live markets expose `feeSchedule: {"rate": 0.05, "exponent": 1, "takerOnly": true, "rebateRate": 0.15}` and `feeType: "sports_fees_v3"`. CLOB `fd` exposes `{r, e}`.
- **The exponent is not explained in the docs.** I implement the generalised form `C × rate × (p(1−p))^exponent`, which equals the documented formula when exponent = 1 (all soccer markets seen today), and I read rate and exponent per market rather than hardcoding them.
- A tiered Taker Rebate Program exists. It is **ignored** (conservative), but there is a config flag for it.

---

## 3. The Odds API v4 (sportsbooks)

### Base URL and auth
- `https://api.the-odds-api.com` (IPv6 host: `https://ipv6-api.the-odds-api.com`).
- Auth is an `apiKey` query parameter, read from `.env` as `ODDS_API_KEY`. This is a data-subscription key, not a trading credential.

### Quota and rate limits
- Every response carries headers `x-requests-remaining`, `x-requests-used`, and `x-requests-last` (cost of that call). The client logs them and stops before going below a configurable floor.
- Burst limit: HTTP 429 means "space requests over several seconds".
- Plans listed on the-odds-api.com today: free 500 credits/mo, 20K credits for $30/mo, 100K for $59/mo, 5M for $119/mo, 15M for $249/mo. **The historical endpoints need a paid plan.**

### Sport keys
`soccer_epl`, `soccer_uefa_champs_league`. NBA later would be `basketball_nba`.

### Endpoints we use
| Purpose | Endpoint | Cost |
|---|---|---|
| In-season sports | `GET /v4/sports` | free |
| Events (ids, teams, kickoff) | `GET /v4/sports/{sport}/events` (`commenceTimeFrom`/`To`) | free |
| Live odds | `GET /v4/sports/{sport}/odds?regions=..&markets=h2h&oddsFormat=decimal` | **1 per market per region** |
| Historical odds snapshot | `GET /v4/historical/sports/{sport}/odds?...&date=ISO8601` | **10 per market per region** |
| Historical events | `GET /v4/historical/sports/{sport}/events?date=...` | 1 (0 if empty) |
| Scores (optional, in-play tag) | `GET /v4/sports/{sport}/scores?daysFrom=1` | 2 with `daysFrom`, else 1 |

- Use the `bookmakers=pinnacle,...` param instead of `regions`. **Up to 10 bookmakers count as 1 region**, so Pinnacle (region `eu`) plus 9 UK/US books costs 1 credit per live call and 10 per historical snapshot.
- Pinnacle caveat from the bookmaker page: "Odds are from public website which may incur a delay."
- Interesting extra: the Odds API also lists `kalshi` and `polymarket` as bookmakers in region `us_ex`. We don't rely on it, but it could serve as an independent sanity check.
- Empty responses cost nothing.

### Historical snapshot semantics
- `date` returns the closest snapshot **at or before** the requested time. Snapshots exist every **5 minutes** (since Sep 2022).
- The response wraps `/odds` data with `timestamp`, `previous_timestamp`, and `next_timestamp`.
- A single league-wide snapshot covers every match in the league at that moment, so one call serves a whole matchday.

### Response shape (h2h, decimal)
```json
{"id":"...","sport_key":"soccer_epl","commence_time":"2026-10-24T11:30:00Z",
 "home_team":"Aston Villa","away_team":"Manchester City",
 "bookmakers":[{"key":"pinnacle","title":"Pinnacle","last_update":"...",
   "markets":[{"key":"h2h","last_update":"...",
     "outcomes":[{"name":"Aston Villa","price":3.9},{"name":"Manchester City","price":1.85},{"name":"Draw","price":3.8}]}]}]}
```
- Soccer h2h is **3-way**: outcomes are the home team name, the away team name, and `"Draw"`. Outcome mapping uses `home_team`/`away_team` equality, with `"Draw"` mapping to DRAW.
- Draw label confirmed as exactly `"Draw"` on a live call (see §5.1). Outcome order is **not** fixed, so map by name, never by position.
- Odds API team names are a third naming convention ("Manchester City" vs Kalshi "Manchester City" vs Polymarket "Manchester City FC"). The alias table covers it.

### Implied probability and de-vig
- Implied probability is q_i = 1 / decimal_odds_i, and the overround is Σq − 1.
- **Power method (default):** find k such that Σ q_i^k = 1, then p_i = q_i^k. Solve with bracketed root-finding (k > 1 when overround > 0).
- **Multiplicative fallback:** p_i = q_i / Σq. Used when the power solve fails to converge, or when selected in config.

### Budget estimate for backfill (EPL + UCL, 60 days)
A full 5-minute history would be 60 × 288 × 10 = **172,800 credits per league**, which is too expensive. The plan instead samples around kickoffs, with the schedule configurable:
- hourly from T−48h to T−3h
- every 5 min from T−3h to T+2h
- de-duplicated across matches that share a timestamp

That is about 105 snapshots × 10 credits ≈ **1,050 credits per kickoff slot**. Roughly 25–35 kickoff slots in 60 days across both leagues comes to about **30–40K credits**, which needs the 100K plan. The CLI will print an estimate and require `--max-credits` before spending.

Live collector: polling both leagues every 5 min only while a match is inside the 48 h window costs at most about 576 credits/day (17K/month), and in practice far less. The interval is configurable.

---

## 4. Where the docs differ from the brief, and what I did

1. **Kalshi order books have no asks.** Executable asks are derived (YES ask = 1 − best NO bid). Depth "top 5 levels" on the ask side comes from the opposite side's bids.
2. **Polymarket book ordering is the reverse of what its docs say** (verified live). The code sorts explicitly instead of trusting order.
3. **Polymarket price history has no bid/ask.** Historical Polymarket edges can only be approximate. Kalshi history *does* have bid/ask, so Kalshi-only intra-venue overround in history can use real asks (still labelled approximate, because candles are 1-minute closes).
4. **Kalshi archive split.** On 2026-10-11 the cutoff is 2026-08-12, so a 60-day backfill lands exactly on the boundary. Ingest checks `/historical/cutoff` and routes each market to `/series/.../candlesticks` or `/historical/markets/.../candlesticks`, with a different field schema for each.
5. **Kalshi kickoff is not on the market object.** Use milestone `start_date`. The market's `expected_expiration_time` is kickoff + 3 h and would break the 2-hour kickoff-match rule.
6. **Kalshi WebSocket requires auth**, so the collector uses REST polling only. That keeps us credential-free at the cost of up to one polling interval of latency.
7. **Odds API historical is paid-only, 10 credits/snapshot, 5-min granularity.** Sharp-divergence episodes in historical data can't be resolved below 5 minutes, and duration estimates inherit that coarseness.
8. **"Buy the opposite side on the other venue"** works directly, because both prediction venues list a NO contract for each outcome. So the cross-venue executable condition is
   `ask_YES(outcome, A) + fee_A + ask_NO(outcome, B) + fee_B < 1`.
   The sportsbook side is never treated as executable. It is reference-only (sharp divergence).
9. **Intra-venue overround.** Because both venues use three mutually exclusive yes/no markets, buying all three YES at the ask pays exactly $1. The edge is `1 − Σ(ask_YES + fee)`.
   - The mirror trade (buying all three NOs pays $2) is also checked: `2 − Σ(ask_NO + fee)`.
   - Polymarket neg-risk conversion is not modelled.
10. **Polymarket fee exponent** isn't documented. I read it per market and use the generalised formula, which reduces to the documented one at exponent 1.
11. **Polymarket wipes the book at kickoff** (`clearBookOnStart`) and adds an in-play order delay. Both matter for interpreting the "in-play" bucket and the 2-minute human-reaction criterion.
12. **Settlement equivalence (verified from rules text, 2026-10-11).** Both venues settle the match result on **90 minutes plus stoppage time, excluding extra time and penalties**. That includes UCL qualifying second legs, where Kalshi titles carry a ": Regulation Time Moneyline" suffix that ingest strips. Sportsbook h2h for soccer uses the same 90-minute convention. Two tail differences remain and are not modelled:
    - **Postponement:** Polymarket stays open until the game is played. Kalshi's rules refer to the game "originally scheduled" for a date.
    - **Cancellation:** if a game is cancelled outright, Polymarket resolves the Draw market to Yes.

    A cross-venue pair can therefore lose on both legs in a postponement or cancellation. This is a rare tail risk; the report should mention it.
13. **Kalshi abbreviations are not unique.** `CEL` is used for both Celtic and NK Celje in UCL. Matching never uses abbreviations; it uses names and the alias table only.

---

## 5. Review decisions (2026-10-11)

These were approved on review and are reflected in `config.yaml`.

### 5.1 Odds API plan and key
- **Free tier now (500 credits/month)**, upgrading to the 100K plan for one month before the historical backfill. Nothing in steps 2–4 depends on the key.
- On free tier, the live collector polls sportsbooks every 30 min, and only while a matched match is inside the lookahead window (`odds.live_poll_interval_seconds`). It also stops when `x-requests-remaining` falls below `odds.credit_floor`.
- Draw label **confirmed, 2026-10-11**. One call: `GET /v4/sports/soccer_epl/odds?regions=eu&markets=h2h&bookmakers=pinnacle&oddsFormat=decimal`. It cost 1 credit; quota headers afterwards showed `x-requests-used: 1` and `x-requests-remaining: 499`.
  - It returned 21 EPL events, every one with a Pinnacle h2h market.
  - Across all events, the only outcome name that isn't a team is exactly `"Draw"`.
  - Team outcome names equal `home_team` / `away_team` exactly (e.g. "Coventry City", "Newcastle United").
  - Outcome order varies by event. For Everton vs Chelsea it was `[Chelsea, Everton, Draw]`, away team first. Mapping must use name equality, never position.
  - Pinnacle's `last_update` was current, equal to the call time, so the "may incur a delay" note did not show up on this call.

### 5.2 Forward-fill staleness, per venue
| Series | Staleness limit | Reason |
|---|---|---|
| Kalshi candles (historical) | none, bounded to T−48h…T+3h | verified in §1: a missing candle means no change |
| Kalshi live snapshots | 5 min | a missed poll is not proof of no change |
| Polymarket (history and live) | 5 min | history semantics unverified, see §5.5 |
| Sportsbooks | 5 min | |

All of these are configurable under `normalize.staleness_minutes`.

### 5.3 Pinnacle staleness for sharp divergence
- A comparison counts only if Pinnacle's market `last_update` is within `analysis.sharp.max_pinnacle_age_minutes` (default 5) of the comparison timestamp.
- Discarded comparisons are counted per match and reported (total, and the share of all candidate comparisons).

### 5.4 Reference trade size
Every net edge is evaluated at two sizes:
1. **Reference notional:** `analysis.reference_notional_usd` (default $100) on each leg.
2. **Live only, maximum book-supported size:** the largest size fillable at the quoted best price on both legs, bounded by best-level depth.

Per-order fee rules at the evaluated size:
- **Kalshi:** fee = `ceil_to(0.07 × M × C × P × (1−P), increment)`, applied once per order (per leg) at the evaluated contract count C. The default increment is $0.01.
- **Polymarket:** fee = `round(C × rate × (p(1−p))^exponent, 5 dp)`.
- **Polymarket minimum size:** `min_order_size` (e.g. 5 shares, from the book or `orderMinSize`) is enforced. Below it, the edge is reported as not executable.
- **Kalshi contract granularity:** contracts are 0.01 granular, so a $ notional converts to contracts by flooring to 0.01.

### 5.5 Polymarket `/prices-history` semantics (to be measured)
- The live collector also stores `last_trade_price` (from the book) and the book midpoint `(best_bid + best_ask)/2`.
- Analysis step `pm-edge verify-pm-history` aligns collected minutes with `/prices-history?fidelity=1` points for the same tokens. It reports the mean absolute difference and exact-match rate against midpoint and against last trade.
- Historical Polymarket edges are labelled with the winner, e.g. `price_basis = "midpoint"` or `"last_trade"`, or `"unknown"` until enough live data exists (threshold in config).

### 5.6 In-play and time buckets
- Buckets: `>24h`, `24h–2h`, `2h–kickoff` (**headline**), and `in-play`.
- In-play rows are tagged and **excluded from the PASS/FAIL decision box**. They get a separate report section.
- The bucket boundaries are configurable.
- **Lineup window** (added 2026-10-11): `analysis.focus_windows` → `lineup`, 75 to 45 minutes before kickoff. EPL lineups are announced about 60 minutes before kickoff. The window overlaps the `2h–kickoff` bucket and is reported separately, never instead of it. Cross-venue gaps and venue lag after price moves are both broken out for this window.

### 5.7 us_ex cross-check (off by default)
- `odds.us_ex_crosscheck.enabled: false`. When on, `kalshi,polymarket` are appended to the sportsbook poll's `bookmakers` list. This is free while the list stays at 10 or fewer books; each extra group of 10 books costs 1 more credit per call (2 per call at 11–20 books, ×10 for historical).
- Those prices are stored as venue `odds_us_ex` and compared with our direct Kalshi and Polymarket prices for the same minute. They are **never** used as an edge source.

### 5.8 Kalshi live snapshots vs served candles (step 6, added 2026-10-11)
This is the live re-check of the forward-fill assumption in §5.2.
- The collector stores every Kalshi orderbook snapshot it takes, already the plan under `data/live/venue=kalshi/date=.../`. Each row carries its poll timestamp, the best YES bid and ask derived from the book, and the top-5 depth.
- Once a collected match settles, `pm-edge verify-kalshi-candles` fetches Kalshi's candles for the same tickers and window. For each snapshot it takes the candle whose window `(end−60 s, end]` contains it. It reports:
  - The share of snapshots whose best bid/ask equals that candle's close, and separately whether the value lies between the candle's low and high.
  - **The key check:** for snapshots falling in minutes with **no** candle, the share whose quote equals the forward-filled previous close. Anything below about 100% means a missing candle does not always mean "no change". Kalshi's `kalshi_historical` staleness would then move from none to a finite limit.
  - Any snapshot quote that differs from the forward-filled value, listed with ticker, timestamp and gap length.
- Results go into this section and drive `normalize.staleness_minutes.kalshi_historical`.

### 5.9 Matching (step 4)
- `data/team_aliases.csv` (columns `alias, canonical, note`) is the only versioned file under `data/`. It covers every variant seen on Kalshi, Polymarket and the Odds API for EPL and UCL: 58 canonical teams.
- **Lookup normalisation:** case-folded; accents stripped, with ø→o; apostrophes and backticks removed; `&` becomes "and"; other punctuation becomes a space. So `Be`er Sheva`, `Be'er Sheva` and `Beer Sheva` are the same key.
- **Unknown names:** these go to rapidfuzz `token_sort_ratio`. A score of 90 or more is accepted **and** written to the review file as `fuzzy_accepted`. A score of 80–90 is written as a suggestion and the event is left unmatched. Below 80, the event is recorded as unresolved. `WRatio` is not used, because its partial matching inflates scores like "Inter" against "Inter Milan B".
- **Pairing:** events pair when the canonical home and away teams are identical and kickoffs are within 120 min. `match_id = {league}_{kickoff date UTC}_{home}_{away}`, using the earliest kickoff in the pair.
- **Review kinds:** `unresolved_team`, `fuzzy_accepted`, `home_away_swapped` (never paired), `kickoff_mismatch` (including same-day kickoffs too far apart), `duplicate_venue_event`, `unpaired`.
- **Result, 2026-10-11:** EPL 59/59 and UCL 32/32 paired on both venues, with all names resolved by exact alias. Kickoffs are identical, and settled results agree outcome by outcome in all 91 matches. The review file is empty.
