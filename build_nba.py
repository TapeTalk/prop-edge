#!/usr/bin/env python3
"""
Kalshi NBA player props vs. sportsbook consensus  (Phase 1)

What this does, in order:
  1. Pulls every OPEN Kalshi NBA player-prop market (no key needed).
  2. If Kalshi has no NBA props today, it stops there and spends ZERO odds-API quota.
  3. Otherwise it asks PropLine (free key) for sportsbook odds, but ONLY for the
     games and stat types Kalshi actually lists.
  4. Matches player + stat + line, removes the vig, averages books, compares with
     Kalshi's ask price plus Kalshi's taker fee, and writes data/nba.json.

Uses only the Python standard library, so GitHub Actions needs nothing installed.
Never invents numbers: anything missing is left empty and labeled.
"""

import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone(timedelta(hours=-5))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
OUT_FILE = os.path.join(DATA_DIR, "nba.json")
QUOTA_FILE = os.path.join(DATA_DIR, "quota.json")

# ---------------------------------------------------------------- settings
KALSHI_HOSTS = [
    "https://external-api.kalshi.com/trade-api/v2",   # current production host
    "https://api.elections.kalshi.com/trade-api/v2",  # older host, still supported
]

# Kalshi series ticker -> (our stat key, label, PropLine / Odds API market key)
SERIES = {
    "KXNBAPTS": ("points", "Points", "player_points"),
    "KXNBAREB": ("rebounds", "Rebounds", "player_rebounds"),
    "KXNBAAST": ("assists", "Assists", "player_assists"),
    "KXNBA3PT": ("threes", "3-Pointers", "player_threes"),
    "KXNBAPRA": ("pra", "Pts+Reb+Ast", "player_points_rebounds_assists"),
    "KXNBAPR": ("pr", "Pts+Reb", "player_points_rebounds"),
    "KXNBAPA": ("pa", "Pts+Ast", "player_points_assists"),
    "KXNBARA": ("ra", "Reb+Ast", "player_rebounds_assists"),
}

PROPLINE_BASE = "https://api.prop-line.com/v1"
PROPLINE_DAILY_LIMIT_FALLBACK = 1000      # free tier, used only if headers don't say
PROPLINE_SAFETY_RESERVE = 25              # never spend the last N requests

ODDSAPI_BASE = "https://api.the-odds-api.com/v4"
ODDSAPI_SAFETY_RESERVE = 40               # keep at least this many credits

SPORT_KEY = "basketball_nba"

# Books that are NOT independent two-way sportsbook prices; never used in the fair line.
EXCLUDED_BOOKS = {
    "kalshi", "polymarket", "prizepicks", "underdog", "sleeper", "dabble",
    "betr", "parlayplay", "chalkboard",
}

DEFAULT_ORDER_SIZE = 10    # contracts; Kalshi rounds the fee up per order
TAKER_RATE = 0.07

# Must match .github/workflows/nba.yml (UTC). Used only to show "next refresh".
SCHEDULE_UTC = [(15, 7), (22, 7), (23, 37)]

NBA_TEAMS = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}
TEAM_ALIASES = {
    "BRK": "BKN", "BK": "BKN", "CHO": "CHA", "GS": "GSW", "NO": "NOP", "NOR": "NOP",
    "NY": "NYK", "PHO": "PHX", "SA": "SAS", "UTAH": "UTA", "UTH": "UTA", "WSH": "WAS",
}
ALL_ABBRS = sorted(set(NBA_TEAMS) | set(TEAM_ALIASES), key=len, reverse=True)
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

# ---------------------------------------------------------------- helpers
def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url, headers=None, timeout=30, retries=2):
    """Returns (status, headers_dict_lowercase, parsed_json_or_None, error_text)."""
    h = {"User-Agent": "kalshi-props-dashboard/1.0", "Accept": "application/json"}
    h.update(headers or {})
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                return r.status, hdrs, json.loads(body) if body else None, None
        except urllib.error.HTTPError as e:
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if e.code in (429,) or e.code < 500:
                return e.code, hdrs, None, body[:500]
            last_err = f"HTTP {e.code}: {body[:300]}"
        except Exception as e:  # network error / timeout / bad JSON
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (attempt + 1))
    return 0, {}, None, last_err


def to_float(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm_name(name):
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)", " ", s)          # "Tarik Skubal (DET)" -> "Tarik Skubal"
    s = s.lower().replace(".", "").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z\s-]", " ", s).replace("-", " ")
    parts = [p for p in s.split() if p not in {"jr", "sr", "ii", "iii", "iv", "v"}]
    return " ".join(parts)


def american_to_prob(price):
    p = to_float(price)
    if p is None or p == 0:
        return None
    if -100 < p < 100:      # decimal odds slipped through
        return 1.0 / p if p > 1 else None
    return 100.0 / (p + 100.0) if p > 0 else (-p) / ((-p) + 100.0)


def kalshi_fee_per_contract(price, contracts=DEFAULT_ORDER_SIZE, waived=False):
    """Kalshi taker fee = round_up(0.07 * C * P * (1-P)) to the cent, spread over C."""
    if price is None or waived:
        return 0.0
    raw = TAKER_RATE * contracts * price * (1 - price)
    total = math.ceil(round(raw * 100, 6)) / 100.0
    return total / contracts


def next_runs(n=3):
    out, base = [], now_utc()
    for day in range(0, 3):
        d = (base + timedelta(days=day)).date()
        for hh, mm in SCHEDULE_UTC:
            t = datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)
            if t > base:
                out.append(t)
    return [iso(t) for t in sorted(out)[:n]]


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- Kalshi
def parse_event_ticker(event_ticker):
    """KXNBAPTS-26OCT28NYKBOS -> (date 2026-10-28, 'NYK', 'BOS'). Teams may be None."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)", event_ticker or "")
    if not m:
        return None, None, None
    yy, mon, dd, teams = m.groups()
    game_date = None
    if mon in MONTHS:
        try:
            game_date = f"20{yy}-{MONTHS[mon]:02d}-{int(dd):02d}"
        except ValueError:
            game_date = None
    for a in ALL_ABBRS:
        if teams.startswith(a):
            rest = teams[len(a):]
            if rest in NBA_TEAMS or rest in TEAM_ALIASES:
                return (game_date,
                        TEAM_ALIASES.get(a, a),
                        TEAM_ALIASES.get(rest, rest))
    return game_date, None, None


NAME_PATTERNS = [
    re.compile(r"^\s*(?P<name>(?:[^\W\d_]|['.\- ])+?)\s*[:\-–]\s*(?P<n>\d+(?:\.\d+)?)\s*\+"),
    re.compile(r"^\s*Will\s+(?P<name>(?:[^\W\d_]|['.\- ])+?)\s+(?:score|record|have|get|make|hit|grab|tally)\s+"
               r"(?:at least\s+|over\s+|more than\s+)?(?P<n>\d+(?:\.\d+)?)\s*\+?", re.I),
    re.compile(r"^\s*(?P<name>(?:[^\W\d_]|['.\- ])+?)\s+(?P<n>\d+(?:\.\d+)?)\s*\+"),
]


def parse_kalshi_market(m):
    """Return (player, threshold_n, over_line) or None if the market can't be read safely.
    threshold_n means YES wins if stat >= n. over_line is the sportsbook-style line (n - 0.5)."""
    texts = [m.get("yes_sub_title"), m.get("title"), m.get("subtitle")]
    cs = m.get("custom_strike")
    if isinstance(cs, dict):
        texts += [str(v) for v in cs.values() if isinstance(v, str)]
    name, n_text = None, None
    for t in texts:
        if not t:
            continue
        for pat in NAME_PATTERNS:
            mm = pat.search(t)
            if mm:
                cand = mm.group("name").strip(" :-–")
                if len(cand.split()) >= 2 and not cand.lower().startswith("will "):
                    name, n_text = cand, mm.group("n")
                    break
        if name:
            break
    if not name:
        return None

    # Prefer Kalshi's structured strike if present.
    floor = to_float(m.get("floor_strike"))
    stype = (m.get("strike_type") or "").lower()
    if floor is not None and stype == "greater":              # stat > floor
        threshold = math.floor(floor) + 1
    elif floor is not None and stype == "greater_or_equal":   # stat >= floor
        threshold = math.ceil(floor)
    else:
        threshold = math.ceil(float(n_text))                  # "25+" -> 25
    return name, threshold, threshold - 0.5


def fetch_kalshi_nba():
    """All open NBA player-prop markets across SERIES. Returns (markets, meta)."""
    meta = {"ok": False, "host": None, "errors": [], "per_series": {}}
    for host in KALSHI_HOSTS:
        markets, failed = [], False
        for series in SERIES:
            cursor, n = "", 0
            for _page in range(20):
                q = {"series_ticker": series, "status": "open", "limit": 1000}
                if cursor:
                    q["cursor"] = cursor
                status, _h, js, err = http_get(f"{host}/markets?{urllib.parse.urlencode(q)}")
                if status != 200 or not isinstance(js, dict):
                    meta["errors"].append(f"{series} on {host}: {err or status}")
                    failed = True
                    break
                page = js.get("markets") or []
                for mk in page:
                    mk["_series"] = series
                markets += page
                n += len(page)
                cursor = js.get("cursor") or ""
                if not cursor or not page:
                    break
                time.sleep(0.15)
            meta["per_series"][series] = n
            if failed:
                break
            time.sleep(0.15)
        if not failed:
            meta.update(ok=True, host=host)
            return markets, meta
        meta["per_series"] = {}
    return [], meta


# ---------------------------------------------------------------- odds providers
class Quota:
    """Our own request counter + whatever the provider's headers say."""

    def __init__(self):
        self.store = load_json(QUOTA_FILE, {})
        today = now_utc().strftime("%Y-%m-%d")
        month = now_utc().strftime("%Y-%m")
        pl = self.store.setdefault("propline", {})
        if pl.get("date_utc") != today:
            pl.update(date_utc=today, requests_today=0)
        oa = self.store.setdefault("oddsapi", {})
        if oa.get("month") != month:
            oa.update(month=month, requests_month=0)

    def record(self, provider, headers, status):
        p = self.store[provider]
        if provider == "propline":
            p["requests_today"] = p.get("requests_today", 0) + 1
        else:
            p["requests_month"] = p.get("requests_month", 0) + 1
        quota_hdrs = {k: v for k, v in headers.items()
                      if any(w in k for w in ("ratelimit", "quota", "remaining", "requests-", "reset", "limit"))}
        if quota_hdrs:
            p["last_headers"] = quota_hdrs
        p["last_status"] = status
        p["last_checked"] = iso(now_utc())
        rem = self.remaining(provider)
        if rem is not None:
            p["remaining"] = rem
        lim = self.limit(provider)
        if lim is not None:
            p["limit"] = lim

    def _hdr_num(self, provider, *needles):
        hdrs = self.store[provider].get("last_headers") or {}
        for k, v in hdrs.items():
            if all(n in k for n in needles):
                f = to_float(str(v).split(",")[0].strip())
                if f is not None:
                    return int(f)
        return None

    def remaining(self, provider):
        return self._hdr_num(provider, "remaining")

    def limit(self, provider):
        v = self._hdr_num(provider, "limit")
        if v is None and provider == "propline":
            return None
        return v

    def propline_left(self):
        rem = self.remaining("propline")
        if rem is not None and self.store["propline"].get("date_utc") == now_utc().strftime("%Y-%m-%d"):
            return rem
        return PROPLINE_DAILY_LIMIT_FALLBACK - self.store["propline"].get("requests_today", 0)

    def save(self):
        save_json(QUOTA_FILE, self.store)


def next_utc_midnight():
    t = now_utc()
    return iso(datetime(t.year, t.month, t.day, tzinfo=timezone.utc) + timedelta(days=1))


def event_et_date(ev):
    ct = ev.get("commence_time")
    try:
        dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        return dt.astimezone(ET).strftime("%Y-%m-%d"), iso(dt)
    except Exception:
        return None, None


def match_events(events, games):
    """games: {(date, away_abbr, home_abbr) or (date,None,None)} -> event list from provider."""
    wanted, unresolved_dates = {}, set()
    for (gdate, a, b) in games:
        if not a:
            unresolved_dates.add(gdate)
            continue
        names = {NBA_TEAMS[a], NBA_TEAMS[b]}
        for ev in events:
            d, _ = event_et_date(ev)
            if d == gdate and {ev.get("home_team"), ev.get("away_team")} == names:
                wanted[(gdate, a, b)] = ev
                break
    extra = [ev for ev in events if event_et_date(ev)[0] in unresolved_dates]
    return wanted, extra


def fetch_odds(provider, key, quota, games, market_keys):
    """Returns (events_with_odds: list, meta). Same schema for PropLine and The Odds API."""
    meta = {"source": provider, "ok": False, "limit_hit": False, "errors": [], "events_fetched": 0}
    if provider == "propline":
        base, hdr, auth_q = PROPLINE_BASE, {"X-API-Key": key}, {}
    else:
        base, hdr, auth_q = ODDSAPI_BASE, {}, {"apiKey": key}

    q = dict(auth_q)
    status, h, events, err = http_get(f"{base}/sports/{SPORT_KEY}/events?{urllib.parse.urlencode(q)}", hdr)
    quota.record(provider, h, status)
    if status == 429:
        meta["limit_hit"] = True
        meta["errors"].append("Free request limit reached.")
        return [], meta
    if status in (401, 403):
        meta["errors"].append(f"Key rejected (HTTP {status}). Check the GitHub secret. {err or ''}")
        return [], meta
    if status != 200 or not isinstance(events, list):
        meta["errors"].append(f"Events list failed: {err or status}")
        return [], meta

    wanted, extra = match_events(events, games)
    targets = list(wanted.values()) + [e for e in extra if e not in wanted.values()]
    unmatched = [g for g in games if g[1] and g not in wanted]
    if unmatched:
        meta["errors"].append("No sportsbook event found for Kalshi game(s): " +
                              ", ".join(f"{g[1]}@{g[2]} {g[0]}" for g in unmatched))

    out = []
    for ev in targets:
        if provider == "propline" and quota.propline_left() <= PROPLINE_SAFETY_RESERVE:
            meta["limit_hit"] = True
            meta["errors"].append("Stopped early to protect the free daily limit.")
            break
        if provider == "oddsapi":
            rem = quota.remaining("oddsapi")
            cost = len(market_keys)                    # markets x 1 region
            if rem is not None and rem - cost < ODDSAPI_SAFETY_RESERVE:
                meta["limit_hit"] = True
                meta["errors"].append("Stopped early to protect the free monthly credits.")
                break
        q = dict(auth_q)
        q["markets"] = ",".join(market_keys)
        q["oddsFormat"] = "american"
        if provider == "oddsapi":
            q["regions"] = "us"
        url = f"{base}/sports/{SPORT_KEY}/events/{urllib.parse.quote(str(ev.get('id')))}/odds?{urllib.parse.urlencode(q)}"
        status, h, js, err = http_get(url, hdr)
        quota.record(provider, h, status)
        if status == 429:
            meta["limit_hit"] = True
            meta["errors"].append("Free request limit reached mid-run.")
            break
        if status != 200 or not isinstance(js, dict):
            meta["errors"].append(f"Odds for {ev.get('away_team')} @ {ev.get('home_team')}: {err or status}")
            continue
        out.append(js)
        time.sleep(0.4)
    meta["events_fetched"] = len(out)
    meta["ok"] = len(out) > 0
    return out, meta


def index_book_lines(odds_events):
    """-> {(player_norm, market_key): {book_key: {point: {'over':price,'under':price,...}}}}"""
    idx = {}
    for ev in odds_events:
        for bk in ev.get("bookmakers") or []:
            bkey = (bk.get("key") or "").lower()
            if bkey in EXCLUDED_BOOKS:
                continue
            for mk in bk.get("markets") or []:
                mkey = mk.get("key")
                if mk.get("period") not in (None, "", "full", "game"):
                    continue
                for o in mk.get("outcomes") or []:
                    if o.get("dfs_odds_type") not in (None, "standard"):
                        continue
                    pm = o.get("payout_multiplier")
                    if pm not in (None, 1, 1.0):
                        continue
                    side = (o.get("name") or "").lower()
                    if side not in ("over", "under"):
                        continue
                    pt = to_float(o.get("point"))
                    player = norm_name(o.get("description") or "")
                    if pt is None or not player:
                        continue
                    slot = idx.setdefault((player, mkey), {}).setdefault(bkey, {}).setdefault(pt, {
                        "title": bk.get("title") or bkey,
                        "updated": bk.get("last_update") or mk.get("last_update")})
                    slot[side] = o.get("price")
    return idx


# ---------------------------------------------------------------- core
def side_numbers(ask, fair, waived):
    if ask is None or fair is None or ask <= 0 or ask >= 1:
        return None
    fee = kalshi_fee_per_contract(ask, waived=waived)
    cost = ask + fee
    return {"ask": round(ask, 4), "fee": round(fee, 4), "cost": round(cost, 4),
            "fair": round(fair, 4), "edge": round(fair - cost, 4),
            "roi": round((fair - cost) / cost, 4) if cost > 0 else None}


def build():
    started = now_utc()
    notes = []
    prev = load_json(OUT_FILE, {})

    kalshi_markets, kmeta = fetch_kalshi_nba()
    k_updated = iso(now_utc()) if kmeta["ok"] else None

    props, unparsed, games = [], [], set()
    for m in kalshi_markets:
        series = m["_series"]
        stat, label, mkey = SERIES[series]
        parsed = parse_kalshi_market(m)
        gdate, away, home = parse_event_ticker(m.get("event_ticker"))
        if not parsed:
            if len(unparsed) < 40:
                unparsed.append({"ticker": m.get("ticker"), "yes_sub_title": m.get("yes_sub_title"),
                                 "title": m.get("title")})
            continue
        player, threshold, line = parsed
        games.add((gdate, away, home))
        waiver = m.get("fee_waiver_expiration_time")
        waived = False
        if waiver:
            try:
                waived = datetime.fromisoformat(waiver.replace("Z", "+00:00")) > now_utc()
            except Exception:
                waived = False
        yb, ya = to_float(m.get("yes_bid_dollars")), to_float(m.get("yes_ask_dollars"))
        nb, na = to_float(m.get("no_bid_dollars")), to_float(m.get("no_ask_dollars"))
        props.append({
            "ticker": m.get("ticker"), "event_ticker": m.get("event_ticker"), "series": series,
            "player": player, "player_key": norm_name(player), "stat": stat, "stat_label": label,
            "market_key": mkey, "threshold": threshold, "line": line,
            "game": {"date": gdate, "away": away, "home": home},
            "close_time": m.get("close_time"),
            "kalshi": {
                "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na,
                "yes_ask_size": to_float(m.get("yes_ask_size_fp")),
                "yes_bid_size": to_float(m.get("yes_bid_size_fp")),
                "spread": round(ya - yb, 4) if ya is not None and yb is not None and yb > 0 else None,
                "last": to_float(m.get("last_price_dollars")),
                "volume": to_float(m.get("volume_fp")),
                "volume_24h": to_float(m.get("volume_24h_fp")),
                "open_interest": to_float(m.get("open_interest_fp")),
                "fee_waived": waived,
            },
        })

    # ---------------- odds
    quota = Quota()
    odds_meta = {"source": None, "ok": False, "limit_hit": False, "errors": [], "events_fetched": 0,
                 "updated_at": None}
    odds_events = []
    cache_path = os.path.join(CACHE_DIR, f"odds_{started.astimezone(ET).strftime('%Y-%m-%d')}.json")

    if not props:
        notes.append("Kalshi lists no open NBA player props right now, so no odds were requested (0 quota used).")
    else:
        market_keys = sorted({p["market_key"] for p in props})
        pl_key = os.environ.get("PROPLINE_API_KEY", "").strip()
        oa_key = os.environ.get("ODDS_API_KEY", "").strip()
        if pl_key:
            odds_events, odds_meta = fetch_odds("propline", pl_key, quota, games, market_keys)
        else:
            odds_meta["errors"].append("PROPLINE_API_KEY secret is not set.")
        if not odds_meta["ok"] and oa_key:
            notes.append("PropLine gave no odds this run; tried The Odds API backup.")
            first_errors = odds_meta["errors"]
            odds_events, odds_meta = fetch_odds("oddsapi", oa_key, quota, games, market_keys)
            odds_meta["errors"] = ["PropLine: " + e for e in first_errors] + odds_meta["errors"]
        if odds_meta["ok"]:
            odds_meta["updated_at"] = iso(now_utc())
            save_json(cache_path, {"saved_at": odds_meta["updated_at"], "source": odds_meta["source"],
                                   "events": odds_events})
        else:
            cached = load_json(cache_path, None)
            if cached and cached.get("events"):
                odds_events = cached["events"]
                odds_meta["updated_at"] = cached.get("saved_at")
                odds_meta["stale"] = True
                notes.append(f"Using cached sportsbook odds from {cached.get('saved_at')} (UTC). "
                             "They may be out of date.")
        if odds_meta["limit_hit"]:
            nxt = next_utc_midnight() if odds_meta["source"] == "propline" else None
            odds_meta["limit_message"] = ("Free limit reached — next refresh at " +
                                          (nxt or "the start of next month"))
    quota.save()

    # ---------------- match + edges
    idx = index_book_lines(odds_events)
    for p in props:
        books_at_line, mismatches = [], []
        by_book = idx.get((p["player_key"], p["market_key"]), {})
        for bkey, lines in by_book.items():
            exact = lines.get(p["line"])
            if exact and exact.get("over") is not None and exact.get("under") is not None:
                po, pu = american_to_prob(exact["over"]), american_to_prob(exact["under"])
                if po and pu:
                    fo = po / (po + pu)
                    books_at_line.append({"book": bkey, "title": exact["title"],
                                          "over_price": exact["over"], "under_price": exact["under"],
                                          "vig": round(po + pu - 1, 4),
                                          "fair_over": round(fo, 4), "fair_under": round(1 - fo, 4),
                                          "updated": exact.get("updated")})
                    continue
            two_sided = sorted(pt for pt, v in lines.items() if "over" in v and "under" in v)
            if two_sided:
                mismatches.append({"book": bkey, "title": next(iter(lines.values()))["title"],
                                   "lines": two_sided})
        p["books"] = sorted(books_at_line, key=lambda b: b["title"])
        p["mismatches"] = mismatches
        if books_at_line:
            fo = sum(b["fair_over"] for b in books_at_line) / len(books_at_line)
            p["consensus"] = {"fair_over": round(fo, 4), "n_books": len(books_at_line)}
            p["match_status"] = "matched"
            k = p["kalshi"]
            p["sides"] = {
                "YES": side_numbers(k["yes_ask"], fo, k["fee_waived"]),
                "NO": side_numbers(k["no_ask"], 1 - fo, k["fee_waived"]),
            }
        else:
            p["consensus"] = None
            p["sides"] = None
            p["match_status"] = "line_mismatch" if mismatches else ("no_books" if odds_events else "no_odds")

    counts = {s: sum(1 for p in props if p["match_status"] == s)
              for s in ("matched", "line_mismatch", "no_books", "no_odds")}

    out = {
        "sport": "NBA",
        "phase": 1,
        "generated_at": iso(now_utc()),
        "run_seconds": round((now_utc() - started).total_seconds(), 1),
        "settings": {"order_size": DEFAULT_ORDER_SIZE, "taker_rate": TAKER_RATE},
        "sources": {
            "kalshi": {"ok": kmeta["ok"], "updated_at": k_updated, "host": kmeta["host"],
                       "markets": len(kalshi_markets), "per_series": kmeta["per_series"],
                       "errors": kmeta["errors"][:10]},
            "odds": odds_meta,
            "stats": {"ok": False, "note": "Player stats and context arrive in Phase 3."},
        },
        "quota": {
            "propline": {
                "used_today_by_this_site": quota.store["propline"].get("requests_today", 0),
                "remaining": quota.store["propline"].get("remaining"),
                "limit": quota.store["propline"].get("limit") or PROPLINE_DAILY_LIMIT_FALLBACK,
                "resets_at": next_utc_midnight(),
                "last_checked": quota.store["propline"].get("last_checked"),
            },
            "oddsapi": {
                "used_this_month_by_this_site": quota.store["oddsapi"].get("requests_month", 0),
                "remaining": quota.store["oddsapi"].get("remaining"),
                "last_checked": quota.store["oddsapi"].get("last_checked"),
            },
        },
        "next_scheduled_runs": next_runs(),
        "counts": counts,
        "props": props,
        "unparsed_kalshi": unparsed,
        "notes": notes,
    }

    # Never wipe a good board with an empty one because Kalshi had a hiccup.
    if not kmeta["ok"] and prev.get("props"):
        prev.setdefault("sources", {}).setdefault("kalshi", {})
        prev["sources"]["kalshi"]["errors"] = kmeta["errors"][:10]
        prev["sources"]["kalshi"]["ok"] = False
        prev["notes"] = ["Kalshi could not be reached on the last run; showing the previous board."] + \
            (prev.get("notes") or [])
        prev["last_attempt_at"] = iso(now_utc())
        prev["quota"] = out["quota"]
        prev["next_scheduled_runs"] = out["next_scheduled_runs"]
        out = prev

    save_json(OUT_FILE, out)
    # prune old odds caches (keep 3 days)
    try:
        for fn in sorted(os.listdir(CACHE_DIR))[:-3]:
            os.remove(os.path.join(CACHE_DIR, fn))
    except FileNotFoundError:
        pass
    log(f"Kalshi markets: {len(kalshi_markets)} | props parsed: {len(props)} | unparsed: {len(unparsed)}")
    log(f"Odds source: {odds_meta.get('source')} ok={odds_meta.get('ok')} events={odds_meta.get('events_fetched')}")
    log(f"Match counts: {counts}")
    for e in (kmeta["errors"] + odds_meta.get("errors", []))[:10]:
        log("  ! " + e)


if __name__ == "__main__":
    build()
