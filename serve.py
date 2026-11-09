#!/usr/bin/env python3
"""
Static file server for the terminal + a same-origin Yahoo Finance proxy.

Why: Yahoo has the data but blocks browser CORS. This server fetches Yahoo
SERVER-SIDE and re-serves it from the same origin as the page, so the browser
fetch is same-origin (no CORS, no third-party proxy, no API key).

Run:  python serve.py 8777
Then open:  http://localhost:8777/Equidex.html
"""
import sys, os, json, gzip, time, re, socket, urllib.request, urllib.parse, urllib.error, email.utils, http.cookiejar
import datetime, threading, functools, webbrowser, csv, html
import xml.etree.ElementTree as ET
import order_book
import rpo
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import math

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))   # display all times in IST

# When frozen by PyInstaller the static files (HTML, PNGs) are unpacked into a temp dir (sys._MEIPASS);
# when run as a plain script they sit next to serve.py. Serve from whichever it is.
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# --- diagnostics log ------------------------------------------------------------------------------
# File-only on purpose: the frozen exe runs --windowed, where sys.stdout/sys.stderr are None, so any
# console output would raise. A handler is ALWAYS attached (file, else NullHandler) so Python's
# logging.lastResort (which writes to stderr) can never be reached. Logging must never break serving.
import logging, logging.handlers
LOG = logging.getLogger("equidex")
LOG.propagate = False
def _init_log():
    try:
        d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EQUIDEX")
        os.makedirs(d, exist_ok=True)
        # delay=True: open the file lazily on first emit, not at import, so a transient sharing
        # violation at startup (e.g. a previous instance still closing) can't leave logging
        # permanently disabled via the NullHandler fallback below.
        h = logging.handlers.RotatingFileHandler(os.path.join(d, "serve.log"),
                                                 maxBytes=1_000_000, backupCount=2, encoding="utf-8", delay=True)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(h); LOG.setLevel(logging.INFO)
    except Exception:
        LOG.addHandler(logging.NullHandler())
_init_log()
def _err(e):
    # one-line, status-aware description: "HTTPError 429: ..." / "URLError: timed out" / "ValueError: ..."
    code = getattr(e, "code", None)
    return "%s%s: %s" % (type(e).__name__, (" %s" % code) if code else "", str(e)[:200])

# --- fast failover across a host's addresses ------------------------------------------------------
# socket.create_connection tries a host's addresses in DNS order, each with the caller's timeout -
# and an address that never answers is abandoned only when Windows gives up on it, after ~21 s. On
# this network one of GitHub's four CDN addresses (185.199.109.133) is unreachable, so whenever DNS
# lists it first every raw/release-asset download stalled 21 s before trying the next (measured:
# a 42 KB archive shard took 21.7 s; the other three addresses connect in 0.01-0.03 s).
# Here each address gets a short CONNECT timeout and the next is tried; an address that timed out
# is tried last for the next 10 minutes. The last address still gets the caller's full timeout, so a
# host that is merely slow to accept is never refused. Once connected, the caller's timeout governs
# the reads exactly as before. http.client (hence urllib) looks the function up at call time.
_CONNECT_TRY_S = 3.0
_DEAD_ADDR = {}            # ip -> monotonic time its connect last timed out
_DEAD_FOR_S = 600
def _fast_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **_kw):
    host, port = address[0], address[1]
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not infos:
        raise OSError("getaddrinfo returned no addresses for %s" % host)
    now = time.monotonic()
    dead = lambda i: now - _DEAD_ADDR.get(i[4][0], -1e9) < _DEAD_FOR_S
    infos = [i for i in infos if not dead(i)] + [i for i in infos if dead(i)]
    full = socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    err = None
    for n, (fam, typ, proto, _, sa) in enumerate(infos):
        last = n == len(infos) - 1
        s = None
        try:
            s = socket.socket(fam, typ, proto)
            s.settimeout(full if last or full is not None and full <= _CONNECT_TRY_S else _CONNECT_TRY_S)
            if source_address:
                s.bind(source_address)
            s.connect(sa)
            s.settimeout(full)
            _DEAD_ADDR.pop(sa[0], None)
            return s
        except OSError as e:
            err = e
            if s is not None:
                s.close()
            if isinstance(e, TimeoutError) and not last:
                if sa[0] not in _DEAD_ADDR:
                    LOG.warning("connect %s (%s:%s) timed out; trying the next address, and this one last for %d min",
                                sa[0], host, port, _DEAD_FOR_S // 60)
                _DEAD_ADDR[sa[0]] = time.monotonic()
    raise err
socket.create_connection = _fast_create_connection

APP_PAGE = "Equidex.html"

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
YBASE = "https://query1.finance.yahoo.com/v8/finance/chart/"
GNEWS = "https://news.google.com/rss/search"
NSE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
MCX_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
NSE_CACHE = None   # (fetched_ts, list) of NSE equities — refreshed daily for new listings
MCX_CACHE = None   # (fetched_ts, list) of MCX futures — refreshed daily for new contracts
US_CACHE = None    # (fetched_ts, list) of all US-listed common stocks — refreshed daily for new IPOs
EARN_CACHE = {}    # symbol -> (fetched_ts, rows) for quarterly earnings
YFUND_CACHE = {}   # symbol -> (fetched_ts, dict) for Yahoo quoteSummary fundamentals
ANALYST_CACHE = {} # symbol -> (fetched_ts, dict) for Yahoo analyst recommendations / price targets
IND_FIN_CACHE = {} # symbol -> (fetched_ts, dict) for Indian financial statements (Screener.in)
IND_EARN_CACHE = {} # symbol -> (fetched_ts, list) for Indian quarterly EPS history (Screener.in)
OPT_CACHE = {}     # (symbol, date) -> (fetched_ts, dict) for Yahoo option chains
MOVERS_CACHE = {}  # (mode, cap, region) -> (fetched_ts, list) for market-wide movers
PREMOVERS_CACHE = {}  # region -> (fetched_ts, dict) for extended-hours (pre-market / after-hours) movers
IN_MA_CACHE = {}   # symbol -> (fetched_ts, dict) for Indian M&A / insider / announcements (NSE)
IN_MADEALS_CACHE = None  # (fetched_ts, list) market-wide Indian M&A wire (NSE announcements)
DIV_CACHE = {}     # symbol -> (fetched_ts, dict) for dividend history (Yahoo chart events)
SPLIT_CACHE = {}   # symbol -> (fetched_ts, [{date, num, den}]) split history for the portfolio tracker
PEERS_CACHE = {}   # symbol -> (fetched_ts, dict) for relative-value peer comps (Yahoo screener)
IPO_CACHE = None   # (fetched_ts, dict) recent + upcoming IPOs (Nasdaq IPO calendar)
EARNCAL_CACHE = None  # (fetched_ts, list) upcoming earnings calendar (Nasdaq earnings calendar)
ECON_CACHE = None  # (fetched_ts, list) US economic indicators (BLS + World Bank, keyless)
CORR_CACHE = {}    # (symbols,range) -> (fetched_ts, dict) correlation matrix from daily returns
SP_CACHE = None    # (fetched_ts, list) of recent S&P 500 constituent additions (Wikipedia)
HOL_CACHE = None   # (fetched_ts, {"us": [...], "in": [...], "inSource": str}) exchange holidays, 24h


def _us_holidays(year):
    """NYSE full-day closures for a year from the exchange's published rules (observed-date shifts included).
    Early closes (1 pm) are not modelled. Source of the rules: NYSE holidays & trading hours page."""
    D = datetime.date
    def observed(d):                       # Saturday -> Friday, Sunday -> Monday
        if d.weekday() == 5: return d - datetime.timedelta(days=1)
        if d.weekday() == 6: return d + datetime.timedelta(days=1)
        return d
    def nth_weekday(month, weekday, n):    # n-th (1-based) given weekday of the month
        d = D(year, month, 1)
        d += datetime.timedelta(days=(weekday - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)
    def last_weekday(month, weekday):
        d = D(year, month + 1, 1) - datetime.timedelta(days=1) if month < 12 else D(year, 12, 31)
        return d - datetime.timedelta(days=(d.weekday() - weekday) % 7)
    def easter():                          # Anonymous Gregorian algorithm
        a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4; f = (b + 8) // 25
        g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30; i = c // 4; k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31; day = ((h + l - 7 * m + 114) % 31) + 1
        return D(year, month, day)
    days = [observed(D(year, 1, 1)), nth_weekday(1, 0, 3), nth_weekday(2, 0, 3), easter() - datetime.timedelta(days=2),
            last_weekday(5, 0), observed(D(year, 6, 19)), observed(D(year, 7, 4)), nth_weekday(9, 0, 1),
            nth_weekday(11, 3, 4), observed(D(year, 12, 25))]
    # New Year's Day falling on a Saturday is NOT observed on the prior Friday by NYSE
    if D(year, 1, 1).weekday() == 5:
        days[0] = None
    return sorted(d.isoformat() for d in days if d and d.year == year)


IDXM_CACHE = {}    # index code -> (fetched_ts, {"asof","src","rows":[{"s","n","w"?}]}) constituent lists, 24h
IDXM_PAGES = {     # Wikipedia fallbacks: the dedicated "List of … companies" pages carry the tickers
    "DJI": "List_of_Dow_Jones_Industrial_Average_companies",
    "NDX": "List_of_NASDAQ-100_companies",
    "SENSEX": "List_of_BSE_SENSEX_companies",   # the only keyless list: BSE's own API answers 403 to non-browsers
}
# Official, keyless, self-updating sources (probed 30 Sep 2026 with this plain UA):
#   SPX / DJI  State Street's daily SPY / DIA holdings workbook (weight %, shares, "As of" date)
#   NDX        Nasdaq's own index list API (101 rows, with market cap)
#   NIFTY      NSE's ind_nifty50list.csv (Company Name, Industry, Symbol, Series, ISIN)
IDXM_UA = {"User-Agent": "EQUIDEX/1.0 (keyless equity research; non-commercial)", "Accept": "*/*"}
IDXM_SSGA = {
    "SPX": "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx",
    "DJI": "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-dia.xlsx",
}
IDXM_NASDAQ = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
IDXM_NSE = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"


def _idxm_title(n):
    # "NVIDIA CORP" -> "Nvidia Corp"; tokens with digits or '&' (3M, AT&T) stay as written
    return " ".join(w if (any(ch.isdigit() for ch in w) or "&" in w) else w.capitalize() for w in n.split())


def _idxm_ssga(url):
    # State Street holdings workbook -> rows with weight %. Stdlib only: xlsx = zip of XML, cells are either
    # shared-string indexes (t="s") or inline numbers.
    import zipfile, io
    b = urllib.request.urlopen(urllib.request.Request(url, headers=IDXM_UA), timeout=25).read()
    z = zipfile.ZipFile(io.BytesIO(b))
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        x = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        ss = [html.unescape(re.sub(r"<[^>]+>", "", m)) for m in re.findall(r"<si>(.*?)</si>", x, re.S)]
    sh = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
    rows = []
    for rm in re.finditer(r"<row[^>]*>(.*?)</row>", sh, re.S):
        cells = []
        for cm in re.finditer(r'<c r="([A-Z]+)\d+"(?: [^>]*?t="(\w+)")?[^>]*>(?:<v>(.*?)</v>)?', rm.group(1)):
            col, typ, val = cm.groups()
            cells.append(ss[int(val)] if (typ == "s" and val) else (val or ""))
        rows.append(cells)
    asof, hdr = None, None
    for i, r in enumerate(rows):
        if r and r[0].startswith("Holdings:") and len(r) > 1:
            asof = r[1].replace("As of ", "").strip()
        if r and r[0] == "Name" and "Ticker" in r:
            hdr = i; break
    if hdr is None:
        raise RuntimeError("holdings header not found")
    h = rows[hdr]
    ci_n, ci_t, ci_w = h.index("Name"), h.index("Ticker"), (h.index("Weight") if "Weight" in h else -1)
    out = []
    for r in rows[hdr + 1:]:
        if len(r) <= max(ci_n, ci_t):
            continue
        sym, name = r[ci_t].strip().upper(), r[ci_n].strip()
        if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", sym) or re.search(r"CASH|DEPOSIT|US DOLLAR|FUTURE", name, re.I):
            continue
        row = {"s": sym, "n": _idxm_title(name)}
        if ci_w >= 0:
            try: row["w"] = round(float(r[ci_w]), 3)
            except (TypeError, ValueError): pass
        out.append(row)
    out.sort(key=lambda r: -(r.get("w") or 0))
    return {"asof": asof, "src": "State Street daily holdings", "rows": out}


def _idxm_nasdaq():
    b = urllib.request.urlopen(urllib.request.Request(IDXM_NASDAQ, headers=IDXM_UA), timeout=25).read()
    j = json.loads(b.decode("utf-8", "ignore"))
    rows = (((j.get("data") or {}).get("data") or {}).get("rows")) or []
    out = []
    for r in rows:
        sym = str(r.get("symbol", "")).strip().upper()
        if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", sym):
            continue
        name = re.sub(r"\s+(Class [A-C] )?(Common|Ordinary) (Stock|Shares)$", "", str(r.get("companyName", "")).strip())
        out.append({"s": sym, "n": name})
    if len(out) < 50:
        raise RuntimeError("only %d rows" % len(out))
    return {"asof": time.strftime("%d-%b-%Y"), "src": "Nasdaq index list", "rows": out}


def _idxm_nse():
    b = urllib.request.urlopen(urllib.request.Request(IDXM_NSE, headers=IDXM_UA), timeout=25).read()
    lines = [l for l in b.decode("utf-8", "ignore").splitlines() if l.strip()]
    rd = list(csv.reader(lines))
    hdr = [c.strip().lower() for c in rd[0]]
    ci_n, ci_s = hdr.index("company name"), hdr.index("symbol")
    out = [{"s": r[ci_s].strip().upper(), "n": r[ci_n].strip()} for r in rd[1:] if len(r) > max(ci_n, ci_s) and r[ci_s].strip()]
    if len(out) < 40:
        raise RuntimeError("only %d rows" % len(out))
    return {"asof": time.strftime("%d-%b-%Y"), "src": "NSE ind_nifty50list.csv", "rows": out}


def _idxm_wiki(page):
    # Wikipedia list page, table id="constituents"; the header row names the Symbol/Ticker and Company columns
    req = urllib.request.Request(f"https://en.wikipedia.org/wiki/{page}?action=raw", headers={"User-Agent": "Mozilla/5.0"})
    wt = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    i = wt.find('id="constituents"')
    if i < 0:
        raise RuntimeError("constituents table not found")
    tbl = wt[i:]
    end = tbl.find("\n|}")
    if end > 0:
        tbl = tbl[:end]
    def _cell(x):
        x = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", x, flags=re.S)
        x = re.sub(r"\{\{[A-Za-z ]*(?:link|Symbol)\|([^}|]+)[^}]*\}\}", r"\1", x)
        x = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", x)
        x = re.sub(r"<br\s*/?>", " ", x)
        x = re.sub(r"<[^>]*>", "", x).replace("'''", "").replace("''", "")
        x = re.sub(r"^\s*!?scope=\"(?:row|col)\"\s*\|", "", x.strip())
        return x.strip().lstrip("|!").strip()
    blocks = tbl.split("\n|-")
    # the header row sits before the first "|-" on some pages (SENSEX) and after it on others (Dow, Nasdaq-100)
    header, cs, cn, hi = [], -1, -1, -1
    for cand in range(min(3, len(blocks))):
        # the first block starts with the table's own attributes (id="constituents"…), which are not a column
        header = [_cell(c).lower() for c in re.split(r"\n[!|]|!!|\|\|", blocks[cand])
                  if c.strip() and 'id="constituents"' not in c and not c.strip().startswith("class=")]
        def _col(*names):
            for j, h in enumerate(header):
                if any(h.startswith(n) for n in names):
                    return j
            return -1
        cs, cn = _col("symbol", "ticker"), _col("company", "name", "security")
        if cs >= 0 and cn >= 0:
            hi = cand; break
    if hi < 0:
        raise RuntimeError("header columns not recognised: %r" % header[:6])
    out, seen = [], set()
    for blk in blocks[hi + 1:]:
        cells = [_cell(c) for c in re.split(r"\n[!|]|!!|\|\|", blk) if c.strip()]
        if len(cells) <= max(cs, cn):
            continue
        sym, name = re.sub(r"\.(BO|NS)$", "", cells[cs].upper()), cells[cn]
        if not re.match(r"^[A-Z][A-Z0-9.&\-]{0,11}$", sym) or sym in seen:
            continue
        seen.add(sym)
        out.append({"s": sym, "n": name})
    if len(out) < 10:
        raise RuntimeError("only %d rows parsed" % len(out))
    return {"asof": None, "src": "Wikipedia", "rows": out}
CONGRESS_CACHE = None  # (fetched_ts, list, ttl) congressional STOCK Act trades (LuxAlgo Market Trackers dumps)
GOVCON_CACHE = None    # (fetched_ts, list, ttl) federal contract awards (same source)
INSTTOT_CACHE = None   # (fetched_ts, list) per-filer 13F portfolio totals for the institutions list
CONGRESS_ALL_CACHE = None  # (fetched_ts, dict, ttl) every congress trade filed in the last 12 months
EXEC_CACHE = None          # (fetched_ts, list, ttl) executive-branch trades in the last 12 months (Open Cabinet)
GOVCON_ALL_CACHE = None    # (fetched_ts, dict, ttl) every listed-company contract signed in the last 12 months

# LuxAlgo Market Trackers publishes the US public record as free CC0 JSON dumps, parsed from PRIMARY
# sources (Senate eFD + House Clerk for congress trades, USAspending for contracts). Keyless, no quota:
# they are static files. Every row keeps a provenance.sourceUrl pointing at the government document,
# so nothing here has to be taken on trust — the UI links each row straight back to its filing.
MT_BASE = "https://raw.githubusercontent.com/LuxAlgo/market-trackers-data/main"
MT_UA = {"User-Agent": "EQUIDEX/1.0 (market-trackers-data consumer)"}
def _usa_dates(rows):
    """Attach awarded / start / end dates to contract rows from USAspending, in bulk.

    The Market Trackers dumps carry only actionDate (the transaction date, which tracks the performance
    START - not the award date). USAspending's award search returns the real dates for up to 100 award
    IDs per request: "Base Obligation Date" is the date the award was signed (checked equal to the
    award record's date_signed) and Start/End Date are the period of performance. Rows are matched on
    the generated award id, because a bare PIID can recur across agencies. A row with no match keeps
    None for the dates rather than borrowing actionDate.

    The API sheds load with 502/503s and slow reads, so each page is retried with backoff and only two
    requests run at once. Returns the number of chunks that still failed (0 = every lookup answered).
    """
    want = {r.get("awardKey") for r in rows if r.get("awardKey")}
    piids = sorted({r.get("piid") for r in rows if r.get("piid")})
    chunks = [piids[i:i + 100] for i in range(0, len(piids), 100)]
    hdr = dict(MT_UA); hdr["Content-Type"] = "application/json"

    def page_of(chunk, page):
        body = {"filters": {"award_type_codes": ["A", "B", "C", "D"], "award_ids": chunk},
                "fields": ["Start Date", "End Date", "Base Obligation Date", "generated_internal_id"],
                "limit": 100, "page": page}
        for attempt in range(4):
            try:
                req = urllib.request.Request("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                                             data=json.dumps(body).encode(), headers=hdr, method="POST")
                return json.loads(urllib.request.urlopen(req, timeout=45).read())
            except urllib.error.HTTPError as e:
                if attempt == 3 or (e.code < 500 and e.code != 429):
                    raise
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise
            time.sleep(1.5 * 2 ** attempt)            # 1.5s, 3s, 6s

    def run(chunk):
        got = {}
        try:
            for page in range(1, 11):                     # a PIID can match >1 award, so page through
                d = page_of(chunk, page)
                for x in d.get("results") or []:
                    gid = x.get("generated_internal_id")
                    if gid in want:
                        got[gid] = x
                if not (d.get("page_metadata") or {}).get("hasNext"):
                    break
        except Exception as e:
            LOG.warning("usaspending dates (%d ids) failed: %s", len(chunk), _err(e))
            return None
        return got

    found, failed = {}, 0
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        for got in ex.map(run, chunks):
            if got is None:
                failed += 1
            else:
                found.update(got)
    for r in rows:
        x = found.get(r.get("awardKey"))
        r["awarded"] = ((x.get("Base Obligation Date") or "")[:10] or None) if x else None
        r["start"] = ((x.get("Start Date") or "")[:10] or None) if x else None
        r["end"] = ((x.get("End Date") or "")[:10] or None) if x else None
    return failed


def _one_year_ago():
    return (datetime.date.today() - datetime.timedelta(days=365)).isoformat()


# --- technicals for the screener -------------------------------------------------------------------
# Yahoo's screener has no technical fields, so RSI / MACD / EMA / 52-week position are computed here from
# each candidate's own daily history (2 years, so a 200-day EMA has room to settle). Nothing is estimated:
# a stock without enough history simply fails a technical condition.
TECH_CACHE = {}   # symbol -> (fetched_ts, dict | None)


def _ema(vals, n):
    """Exponential moving average series, seeded with the simple average of the first n values."""
    if len(vals) < n:
        return []
    k = 2.0 / (n + 1)
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out                                    # out[i] belongs to vals[n - 1 + i]


def _technicals(sym):
    hit = TECH_CACHE.get(sym)
    if hit and time.time() - hit[0] < 900:        # daily bars move slowly; the last one tracks the session
        return hit[1]
    res = None
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            YBASE + urllib.parse.quote(sym) + "?range=2y&interval=1d",
            headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read())
        r = ((d.get("chart") or {}).get("result") or [None])[0] or {}
        q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        bars = [(c, h, l) for c, h, l in zip(q.get("close") or [], q.get("high") or [], q.get("low") or [])
                if c is not None]
        closes = [b[0] for b in bars]
        if len(closes) >= 30:
            px = closes[-1]
            res = {"px": px}
            # RSI 14, Wilder smoothing
            ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            g = sum(max(x, 0) for x in ch[:14]) / 14; l_ = sum(max(-x, 0) for x in ch[:14]) / 14
            for x in ch[14:]:
                g = (g * 13 + max(x, 0)) / 14; l_ = (l_ * 13 + max(-x, 0)) / 14
            res["rsi"] = 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)
            # MACD 12 / 26 / 9
            e12, e26 = _ema(closes, 12), _ema(closes, 26)
            macd = [a - b for a, b in zip(e12[14:], e26)]          # aligned from close index 25
            sig = _ema(macd, 9)
            if sig:
                hist = [m - s for m, s in zip(macd[8:], sig)]
                res["macd"], res["macdSig"], res["macdH"] = macd[-1], sig[-1], hist[-1]
                last = hist[-6:]                                    # a cross within the last 5 sessions
                res["crossUp"] = any(last[i - 1] <= 0 < last[i] for i in range(1, len(last)))
                res["crossDown"] = any(last[i - 1] >= 0 > last[i] for i in range(1, len(last)))
            for n in (20, 50, 200):
                e = _ema(closes, n)
                res["ema%d" % n] = e[-1] if e else None
            # 52-week range from the daily highs / lows of the last 252 sessions
            yr = bars[-252:]
            hi = max((b[1] for b in yr if b[1] is not None), default=None)
            lo = min((b[2] for b in yr if b[2] is not None), default=None)
            res["hi52pct"] = (hi - px) / hi * 100 if hi else None    # % below the 52-week high
            res["lo52pct"] = (px - lo) / lo * 100 if lo else None    # % above the 52-week low
            # the PRED tab's reasons, on adjusted closes with their timestamps (as the page parses them)
            ts = r.get("timestamp") or []
            adj = ((((r.get("indicators") or {}).get("adjclose") or [{}])[0]) or {}).get("adjclose")
            cl = q.get("close") or []
            pt, pa = [], []
            for i in range(min(len(ts), len(cl))):
                aa = adj[i] if adj and i < len(adj) else cl[i]
                if cl[i] is None or aa is None:
                    continue
                pt.append(ts[i]); pa.append(aa)
            res.update(_pred_signals(pt, pa))
    except Exception as e:
        LOG.warning("technicals %s failed: %s", sym, _err(e))
        return None                                   # not cached: a failed fetch is retried next time
    TECH_CACHE[sym] = (time.time(), res)
    return res


def _pred_signals(t, a):
    """The PRED tab's reasons to buy / sell, computed exactly as the page's computePrediction does, so a
    stock the screener returns for a reason shows that same reason in its PRED tab: adjusted closes, RSI =
    simple average of the last 14 moves, EMAs seeded with the first close, simple 50 / 200-day averages,
    position in the 52-week (252-session) close range, and the return over the last 91 calendar days."""
    n = len(a)
    if n < 35:
        return {}
    px = a[-1]
    sma = lambda k: sum(a[-k:]) / k if n >= k else None
    g = l_ = 0.0
    for i in range(n - 14, n):
        d = a[i] - a[i - 1]
        if d >= 0: g += d
        else: l_ -= d
    rsi = 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)

    def ema_s(v, k):
        m = 2.0 / (k + 1); out = [v[0]]
        for x in v[1:]:
            out.append(x * m + out[-1] * (1 - m))
        return out
    e12, e26 = ema_s(a, 12), ema_s(a, 26)
    line = [x - y for x, y in zip(e12, e26)]
    hist = line[-1] - ema_s(line, 9)[-1]
    w = a[-252:]
    hi, lo = max(w), min(w)
    target, idx = t[-1] - 91 * 86400, -1
    for i in range(n - 1, -1, -1):
        if t[i] <= target:
            idx = i; break
    return {"p_px": px, "p_sma50": sma(50), "p_sma200": sma(200), "p_rsi": rsi, "p_hist": hist,
            "p_pos": (px - lo) / (hi - lo) if hi > lo else None,
            "p_r3m": a[-1] / a[idx] - 1 if idx >= 0 else None}


def _pred_sig_ok(t, v):
    px, s50, s200 = t.get("p_px"), t.get("p_sma50"), t.get("p_sma200")
    rsi, h, pos, r3 = t.get("p_rsi"), t.get("p_hist"), t.get("p_pos"), t.get("p_r3m")
    have = lambda *x: all(y is not None for y in x)
    return {  # the same branches as computePrediction's reasons
        "above200": have(px, s200) and px > s200,      "below200": have(px, s200) and not px > s200,
        "above50":  have(px, s50) and px > s50,        "below50":  have(px, s50) and not px > s50,
        "golden":   have(s50, s200) and s50 > s200,    "death":    have(s50, s200) and not s50 > s200,
        "macdup":   have(h) and h > 0,                 "macddown": have(h) and not h > 0,
        "oversold": have(rsi) and rsi <= 30,           "overbought": have(rsi) and rsi >= 70,
        "rsiup":    have(rsi) and 55 <= rsi < 70,      "rsiweak":  have(rsi) and 30 < rsi < 45,
        "mom3up":   have(r3) and r3 > 0,               "mom3down": have(r3) and not r3 > 0,
        "nearhigh": have(pos) and pos >= 0.9,          "nearlow":  have(pos) and pos <= 0.1,
    }.get(v, False)


def _tech_ok(t, conds):
    """Every technical condition must hold; missing data fails the condition (never assumed)."""
    if not t:
        return False
    for key, op, v in conds:
        if key in ("rsi", "hi52", "lo52"):
            x = t.get({"rsi": "rsi", "hi52": "hi52pct", "lo52": "lo52pct"}[key])
            if x is None or (op == "gt" and not x > v) or (op == "lt" and not x < v):
                return False
        elif key == "sig":
            if not _pred_sig_ok(t, v):
                return False
        elif key == "macd":
            h, m = t.get("macdH"), t.get("macd")
            ok = {"above": h is not None and h > 0, "below": h is not None and h < 0,
                  "crossup": bool(t.get("crossUp")), "crossdown": bool(t.get("crossDown")),
                  "pos": m is not None and m > 0, "neg": m is not None and m < 0}.get(v, False)
            if not ok:
                return False
        elif key == "ema":
            px, e20, e50, e200 = t.get("px"), t.get("ema20"), t.get("ema50"), t.get("ema200")
            pairs = {"above20": (px, e20), "above50": (px, e50), "above200": (px, e200), "gold": (e50, e200),
                     "below20": (e20, px), "below50": (e50, px), "below200": (e200, px), "death": (e200, e50)}
            a, b = pairs.get(v, (None, None))
            if a is None or b is None or not a > b:
                return False
    return True


def _cg_row(r):
    """One congress disclosure row, in the shape the frontend reads."""
    m = r.get("member") or {}
    amt = r.get("amountRange") or {}
    return {"filed": r.get("filedAt"), "traded": r.get("transactedAt"),
            "member": m.get("name"), "party": m.get("party"), "state": m.get("state"),
            "chamber": r.get("chamber"), "ticker": r.get("ticker"),
            "asset": r.get("assetDescription"), "side": r.get("side"),
            "amount": amt.get("text"),
            "amtMin": amt.get("min"), "amtMax": amt.get("max"),
            "url": (r.get("provenance") or {}).get("sourceUrl")}



_HOUSE_FRESH_CACHE = None   # (fetched_ts, since, rows)
_HOUSE_FRESH_LOCK = threading.Lock()
_HOUSE_DOCS = {}            # House PTR DocID -> parsed rows (a filing never changes; an amendment is a new DocID)
_HOUSE_PARTY = {}           # (state, last name) -> party, from the dumps; the Clerk's filings don't state one ("" = ambiguous)
_HOUSE_ROW = re.compile(r"^(.*?)(?:^|\s)(P|E|S \(partial\)|S \(full\)|S)\s+(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}\s*(.*)$")
_HOUSE_AMOUNT = re.compile(r"\$[\d,]+(?:\.\d+)?(?:\s*-(?:\s*\$[\d,]+)?)?")


def _house_ptr_page(page):
    """A page's text with its layout kept. Page 1 of an electronic House PTR holds everything in a form
    XObject, which pypdf's layout mode skips, so read that form's content in the page's place."""
    text = page.extract_text(extraction_mode="layout") or ""
    if text.strip():
        return text
    from pypdf.generic import NameObject
    parts = []
    forms = (page.get("/Resources") or {}).get("/XObject") or {}
    for name in forms:
        form = forms[name].get_object()
        if form.get("/Subtype") == "/Form" and "/Resources" in form:
            page[NameObject("/Contents")] = form
            page[NameObject("/Resources")] = form["/Resources"]
            parts.append(page.extract_text(extraction_mode="layout") or "")
    return "\n".join(parts)


def _house_ptr_text(text, filing):
    """Conservative parser for the House PTR table. A row is the transaction type followed by the trade and
    notification dates, matched as a pattern because the columns drift by a character or two with the asset
    text. Scanned/unknown layouts are omitted; retain distinct rows, including identical trades.
    """
    in_table = False
    pending = None
    out = []
    def finish():
        if not pending:
            return
        asset, side, traded, amount = pending
        bounds = re.fullmatch(r"\$([\d,]+)\s*-\s*\$([\d,]+)", amount.strip())
        # some filers report the exact sum, not a band; the top band reads "Over $1,000,000"
        exact = re.fullmatch(r"\$([\d,]+(?:\.\d+)?)", amount.strip()) or re.search(r"\bOver \$([\d,]+)$", amount.strip())
        ticker = re.findall(r"\(([A-Z][A-Z0-9.]{0,5})\)", asset)
        if not bounds and not exact:
            return
        out.append({"filed": datetime.datetime.strptime(filing["FilingDate"], "%m/%d/%Y").date().isoformat(),
                    "traded": traded, "member": " ".join(filter(None, [filing.get("First"), filing.get("Last"), filing.get("Suffix")])),
                    "party": None, "state": filing.get("StateDst", "")[:2], "chamber": "house",
                    "ticker": ticker[-1] if ticker else None, "asset": asset.strip(), "side": side,
                    "amount": amount.strip(),
                    "amtMin": int(bounds[1].replace(",", "")) if bounds else float(exact[1].replace(",", "")),
                    "amtMax": int(bounds[2].replace(",", "")) if bounds else None,
                    "url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/%s/%s.pdf" % (filing["Year"], filing["DocID"])})
    for line in text.splitlines():
        if pending and "\x00" in line:
            finish(); pending = None
        if all(x in line for x in ("Asset", "Transaction", "Notification", "Amount")):
            if pending and not pending[3].endswith("-"):   # else the band wrapped across the page break
                finish(); pending = None
            in_table = True
            continue
        if not in_table:
            continue
        rest = line.strip()
        if rest.startswith("* For the complete"):
            finish(); pending = None; in_table = False
            continue
        row = _HOUSE_ROW.match(line)
        if row:
            finish(); pending = None
            try:
                traded = datetime.datetime.strptime(row[3], "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue
            amount = _HOUSE_AMOUNT.match(row[4].strip())
            pending = [re.sub(r"^(?:SP|JT|DC)\s+", "", row[1].strip()), {"P": "buy", "E": "exchange"}.get(row[2], "sell"),
                       traded, amount[0] if amount else row[4].strip()]
        elif pending and "\x00" not in line and rest and not rest.startswith(("Filing", "Subholding", "Type", "Date", "ID", "Gains", "$200")):
            tail = re.search(r"(?:^|\s)(\$[\d,]+)\s*$", line)
            if tail and (not pending[3] or pending[3].endswith("-")):   # the band wrapped onto this line
                pending[3] = (pending[3] + " " + tail[1]).strip()
                rest = line[:tail.start()].strip()
            if rest:
                pending[0] += " " + rest
    finish()
    return out


def _house_fresh(since):
    """Every House PTR filed on or after `since`, read from the Clerk's PDFs. Parsed filings are kept for the
    life of the process, so a refresh only downloads filings it has not seen."""
    global _HOUSE_FRESH_CACHE
    with _HOUSE_FRESH_LOCK:
        hit = _HOUSE_FRESH_CACHE
        if hit and time.time() - hit[0] < 1800 and hit[1] <= since:
            return hit[2]
        import io, zipfile
        from pypdf import PdfReader
        from concurrent.futures import ThreadPoolExecutor
        def filed(f):
            return datetime.datetime.strptime(f["FilingDate"], "%m/%d/%Y").date()
        filings = []
        for year in range(since.year, datetime.date.today().year + 1):
            url = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%dFD.ZIP" % year
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=MT_UA), timeout=30).read()
            archive = zipfile.ZipFile(io.BytesIO(raw))
            filings += [f for f in csv.DictReader(io.StringIO(archive.read("%dFD.txt" % year).decode("utf-8-sig")), delimiter="\t")
                        if f.get("FilingType") == "P" and filed(f) >= since]
        filings.sort(key=filed, reverse=True)
        def fetch(f):
            if f["DocID"] in _HOUSE_DOCS:
                return _HOUSE_DOCS[f["DocID"]]
            try:
                url = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/%s/%s.pdf" % (f["Year"], f["DocID"])
                data = urllib.request.urlopen(urllib.request.Request(url, headers=MT_UA), timeout=25).read()
                text = "\n".join(_house_ptr_page(page) for page in PdfReader(io.BytesIO(data)).pages)
                _HOUSE_DOCS[f["DocID"]] = rows = _house_ptr_text(text, f)
                return rows
            except Exception as exc:
                LOG.warning("House PTR %s: %s", f["DocID"], _err(exc)); return []
        with ThreadPoolExecutor(max_workers=2) as ex:
            rows = [r for part in ex.map(fetch, filings) for r in part]
        _HOUSE_FRESH_CACHE = (time.time(), since, rows)
        return rows


def _house_who(r):
    words = [w for w in re.split(r"[\s,]+", r.get("member") or "") if w.rstrip(".") not in ("Jr", "Sr", "II", "III", "IV")]
    return (r.get("state"), words[-1].lower() if words else "")


def _house_party(rows):
    return [dict(r, party=_HOUSE_PARTY.get(_house_who(r)) or None) if r.get("chamber") == "house" and not r.get("party") else r
            for r in rows]


def _with_fresh_house(rows):
    # The dumps can stop updating (they have nothing after early Sep 2026), so read back to where their House
    # filings end, with an overlap, rather than a fixed window that would leave a hole behind it.
    try:
        today = datetime.date.today()
        ends = [r["filed"] for r in rows if r.get("chamber") == "house" and r.get("filed")]
        since = today - datetime.timedelta(days=30)
        if ends:
            since = min(since, datetime.date.fromisoformat(max(ends)[:10]) - datetime.timedelta(days=14))
        fresh = _house_fresh(since)
        documents = {r["url"] for r in fresh}
        for r in rows:
            if r.get("chamber") == "house" and r.get("party"):
                who = _house_who(r)
                _HOUSE_PARTY[who] = r["party"] if _HOUSE_PARTY.get(who, r["party"]) == r["party"] else ""
        return _house_party([r for r in rows if r.get("url") not in documents] + fresh)
    except Exception as exc:
        LOG.warning("Fresh House filings unavailable: %s", _err(exc))
        return rows

def _mt_archive_tags():
    """Newest deep-history archive release per source, from the data repo's archives.json index.

    The live snapshot.json.gz files are a rolling window of recent ingestion (a few weeks), so a
    12-month view also needs the backfill releases, which hold the whole history in year shards.
    Returns {source: (tag, to_date)}.
    """
    idx = json.loads(urllib.request.urlopen(
        urllib.request.Request(MT_BASE + "/archives.json", headers=MT_UA), timeout=45).read())
    best = {}
    for rel in idx.get("releases") or []:
        src = rel.get("source")
        if src and rel.get("tag") and (src not in best
                                        or (rel.get("publishedAt") or "") > (best[src].get("publishedAt") or "")):
            best[src] = rel
    return {k: (v["tag"], v.get("to")) for k, v in best.items()}


def _mt_release_asset(tag, asset):
    url = "https://github.com/LuxAlgo/market-trackers-data/releases/download/%s/%s" % (tag, asset)
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=MT_UA), timeout=120).read()
    return json.loads(gzip.decompress(raw))


def _congress_all():
    """Every congress trade FILED in the last 12 months: the Senate eFD and House Clerk archive
    shards (by trade year) for the history, plus the live snapshot for the newest filings.

    Returns {"rows", "complete", "gaps"}. "complete" is False when a shard failed to download;
    "gaps" lists any stretch between an archive's end and the live window's start, where filings
    are missing from the source itself - the view states both instead of implying full coverage.
    """
    cut = _one_year_ago()
    tags = _mt_archive_tags()
    jobs = []
    for src in ("senate-efd", "house-clerk"):
        if src in tags:
            # shards are by TRADE year, and a trade can be filed up to ~45 days later - so the shard
            # for the year before the cutoff can still hold filings inside the window
            for y in range(int(cut[:4]) - 1, datetime.date.today().year + 1):
                jobs.append((src, tags[src][0], "snapshot-%d.json.gz" % y))
    failed = []

    def fetch(job):
        src, tag, asset = job
        try:
            return _mt_release_asset(tag, asset)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []                       # no trades that year in that chamber's archive
            failed.append(asset); LOG.warning("congress archive %s/%s: %s", tag, asset, _err(e))
        except Exception as e:
            failed.append(asset); LOG.warning("congress archive %s/%s: %s", tag, asset, _err(e))
        return []

    rows = {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        for part in ex.map(fetch, jobs):
            for r in part:
                rows[r.get("id")] = r
    live = _mt_snapshot("/congress/trades/snapshot.json.gz")
    for r in live:
        rows[r.get("id")] = r                   # the live copy wins: it has the latest parse
    gaps = []
    live_from = min((r.get("filedAt") or "9999") for r in live) if live else None
    for src in ("senate-efd", "house-clerk"):
        to = (tags.get(src) or (None, None))[1]
        if not to:
            gaps.append({"source": src, "from": cut, "to": live_from})
        elif live_from and to < live_from:
            gaps.append({"source": src, "from": to, "to": live_from})
    out = _with_fresh_house([_cg_row(r) for r in rows.values() if (r.get("filedAt") or "") >= cut])
    # Senate filings come only from the dumps (eFD refuses this client), so state where they end.
    today = datetime.date.today()
    senate_end = max((r["filed"] for r in out if r.get("chamber") == "senate" and r.get("filed")), default=None)
    if senate_end and senate_end < (today - datetime.timedelta(days=7)).isoformat():
        gaps.append({"source": "senate-efd", "from": senate_end, "to": today.isoformat()})
    out.sort(key=lambda x: ((x.get("filed") or ""), (x.get("traded") or "")), reverse=True)
    return {"rows": out, "complete": not failed, "gaps": gaps, "from": cut}


# --- executive-branch trades (Open Cabinet) ------------------------------------------------------
# Senior executive-branch officials (the President, Cabinet, agency heads) report trades to the Office
# of Government Ethics on Form 278-T, published only as individual PDFs - some typed, some scanned.
# Open Cabinet (github.com/tbrown034/open-cabinet) reads them into rows, each tied to its source PDF,
# page and row, with a verification status. We take the rows it marks checked / human-verified, dated in
# the last 12 months, and keep the link so any figure can be confirmed against the government PDF.
OC_CSV = "https://raw.githubusercontent.com/tbrown034/open-cabinet/main/public/data/all-transactions.csv"
_EXEC_LOCK = threading.Lock()


def _exec_all():
    global EXEC_CACHE
    hit = EXEC_CACHE
    if hit and time.time() - hit[0] < hit[2]:
        return hit[1]
    with _EXEC_LOCK:
        hit = EXEC_CACHE
        if hit and time.time() - hit[0] < hit[2]:
            return hit[1]
        import csv, io
        now = time.time()
        raw = urllib.request.urlopen(urllib.request.Request(OC_CSV, headers=MT_UA), timeout=120).read()
        text = raw.decode("utf-8", "replace")
        i = 0
        while text.startswith("#", i):          # the file opens with a comment line about how rows are checked
            i = text.index("\n", i) + 1
        cut = _one_year_ago()
        out = []
        for r in csv.DictReader(io.StringIO(text[i:])):
            if (r.get("historical_report") or "").lower() == "yes":
                continue
            if r.get("verificationState") not in ("checked", "human_verified"):
                continue                          # rows still under review stay out, as on open-cabinet.org
            d = (r.get("date") or "")[:10]
            if not d or d < cut:
                continue
            name = (r.get("official_name") or "").strip()
            if "," in name:                       # "Trump, Donald J." -> "Donald J. Trump"
                last, first = name.split(",", 1)
                name = (first.strip() + " " + last.strip()).strip()
            t = (r.get("type") or "").strip().lower()
            side = ("buy" if t.startswith(("purchase", "buy")) else "sell" if t.startswith(("sale", "sell"))
                    else "exchange" if t.startswith("exchange") else t)
            amt = r.get("amount_range") or ""
            nums = [int(x.replace(",", "")) for x in re.findall(r"\$([\d,]+)", amt)]
            tk = (r.get("resolved_ticker") or r.get("ticker") or "").strip().upper()
            if tk in ("N/A", "NA", "-", "NONE"):
                tk = ""
            url = r.get("source_filing_url") or ""
            pg = (r.get("source_page") or "").strip()
            if url and pg.isdigit():
                url += "#page=" + pg              # open the PDF at the page the row was read from
            out.append({"traded": d, "official": name, "title": r.get("official_title"),
                        "agency": r.get("agency"), "asset": r.get("description"), "ticker": tk or None,
                        "side": side, "amount": amt,
                        "amtMin": nums[0] if nums else None, "amtMax": nums[1] if len(nums) > 1 else None,
                        "late": (r.get("late_filing") or "").lower() == "yes",
                        "former": (r.get("former_official") or "").lower() == "yes",
                        "kind": r.get("source_kind"),
                        "verified": r.get("verificationState") == "human_verified",
                        "url": url or None})
        try:
            import executive_oge
            official = executive_oge.fresh_rows()
            documents = {url for r in official for url in [r["url"].split("#", 1)[0]] + r.get("sourceAliases", [])}
            out = [r for r in out if (r.get("url") or "").split("#", 1)[0] not in documents]
            out.extend(r for r in official if r["traded"] >= cut)
        except Exception as exc:
            LOG.warning("Official executive filings unavailable: %s", _err(exc))
        out.sort(key=lambda x: x["traded"] or "", reverse=True)
        EXEC_CACHE = (now, out, 1800)
        LOG.info("executive-branch list: %d trades in %.1fs", len(out), time.time() - now)
        return out


_CG_ALL_LOCK = threading.Lock()


def _congress_all_cached():
    global CONGRESS_ALL_CACHE
    hit = CONGRESS_ALL_CACHE
    if hit and time.time() - hit[0] < hit[2]:
        return hit[1]
    with _CG_ALL_LOCK:                                   # one build at a time (request or warm-up)
        hit = CONGRESS_ALL_CACHE
        if hit and time.time() - hit[0] < hit[2]:
            return hit[1]
        now = time.time()
        out = _congress_all()
        CONGRESS_ALL_CACHE = (now, out, 1800 if out["complete"] else 600)
        LOG.info("congress 12-month list: %d trades in %.0fs (complete=%s, gaps=%s)",
                 len(out["rows"]), time.time() - now, out["complete"], out["gaps"])
        return out


def _warm(lock, fn, name):
    """Run a slow list build in the background so its view opens without waiting; no-op while one
    is already running."""
    if lock.locked():
        return
    def go():
        try:
            fn()
        except Exception as e:
            LOG.warning("%s warm-up failed: %s", name, _err(e))
    threading.Thread(target=go, daemon=True, name=name + "-warm").start()


_GC_ALL_LOCK = threading.Lock()


def _govcon_chunk(mapping, limit=None):
    """Official USAspending awards; local UEI mapping supplies entity scope only."""
    names = {re.sub(r"[^A-Z0-9]", "", r["name"].upper()): r["tickers"] for r in mapping.values()}
    fields = ["Award ID", "Recipient Name", "Recipient UEI", "Award Amount", "Start Date", "End Date",
              "Base Obligation Date", "Awarding Agency", "Description", "generated_internal_id"]
    out, seen, complete = [], set(), False
    today = datetime.date.today().isoformat()
    for page in range(1, 21):
        body = {"filters": {"award_type_codes": ["A", "B", "C", "D"],
                "recipient_search_text": list(mapping),
                "time_period": [{"start_date": _one_year_ago(), "end_date": today}]},
                "fields": fields, "page": page, "limit": 100, "sort": "Base Obligation Date", "order": "desc"}
        for attempt in range(3):
            try:
                req = urllib.request.Request("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                    data=json.dumps(body).encode(), headers={**MT_UA, "Content-Type": "application/json"})
                data = json.loads(urllib.request.urlopen(req, timeout=45).read())
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2: raise
                time.sleep(1 + attempt)
        for r in data.get("results") or []:
            key = r.get("generated_internal_id")
            name = r.get("Recipient Name") or ""
            tickers = (mapping.get(r.get("Recipient UEI")) or {}).get("tickers") or names.get(re.sub(r"[^A-Z0-9]", "", name.upper()))
            awarded = r.get("Base Obligation Date")
            if not key or key in seen or not tickers or not awarded or not (_one_year_ago() <= awarded <= today): continue
            seen.add(key)
            out.append({"date": None, "awarded": awarded, "start": r.get("Start Date"), "end": r.get("End Date"),
                "recipient": name, "tickers": tickers, "agency": r.get("Awarding Agency"),
                "amount": r.get("Award Amount"), "desc": r.get("Description") or "", "naics": None,
                "url": "https://www.usaspending.gov/award/" + urllib.parse.quote(key, safe="_-")})
        if not (data.get("page_metadata") or {}).get("hasNext"):
            complete = True; break
        if limit and len(out) >= limit: break
    out.sort(key=lambda r: r["awarded"], reverse=True)
    return out[:limit] if limit else out, complete


_USA_GATE = threading.Semaphore(3)   # USAspending sheds load with 5xx: at most three requests at once


def _usa_call(path, body=None):
    """One USAspending API request (POST if there is a body), retried with backoff."""
    headers = {**MT_UA, "Content-Type": "application/json"} if body else MT_UA
    for attempt in range(4):
        try:
            with _USA_GATE:
                req = urllib.request.Request("https://api.usaspending.gov/api/v2/" + path,
                                             data=json.dumps(body).encode() if body else None, headers=headers)
                return json.loads(urllib.request.urlopen(req, timeout=45).read())
        except urllib.error.HTTPError as e:
            if attempt == 3 or (e.code < 500 and e.code != 429):
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 3:
                raise
        time.sleep(1.5 * 2 ** attempt)


def _legal_name(name):
    """A company name reduced to what must match: SEC's "BOEING CO" and USAspending's "THE BOEING COMPANY" are
    the same, but "BALL CORP" and "BALL LLC" are not, so the legal form stays in."""
    s = re.sub(r"/[A-Z]{2,}/?", " ", str(name or "").upper()).replace("&", " AND ")
    words = re.sub(r"[^A-Z0-9 ]", "", re.sub(r"[.,]", " ", s)).split()
    words = words[1:] if words[:1] == ["THE"] else words
    return "".join({"CORPORATION": "CORP", "INCORPORATED": "INC", "COMPANY": "CO", "LIMITED": "LTD"}.get(w, w) for w in words)


def _us_contracts(symbol, title):
    """Federal contracts of a US-listed company, from USAspending, for the Order total row and its tab.

    Scope: recipients whose legal name equals the SEC company name, every child USAspending links to them as
    parent, and the ticker's entries in govcon-recipients.json. A company whose contractor has another name and
    no parent link (Alphabet / Google LLC) therefore finds nothing rather than a guess.
    value = USAspending's own net contract obligations for the last 12 months (recipient profile); it covers
    earlier contracts too and can be negative, so it is not the sum of `rows`. rows = the contracts SIGNED in
    the last 365 days, newest first, capped at 100."""
    import pathlib
    from concurrent.futures import ThreadPoolExecutor
    mapping = json.loads(pathlib.Path(__file__).with_name("govcon-recipients.json").read_text(encoding="utf-8"))["recipients"]
    mapped = [uei for uei, r in mapping.items() if symbol in r["tickers"]]
    if not title and not mapped:
        return dict(value=None, status='unsupported', rows=[], note='Government contracts are available for US-listed companies.')

    def find(keyword):
        return _usa_call("recipient/", {"keyword": keyword, "award_type": "contracts", "limit": 50,
                                        "order": "desc", "sort": "amount"}).get("results") or []
    exact = []
    if title:
        full = " ".join(re.sub(r"[.,]", " ", re.sub(r"/[A-Z]{2,}/?", " ", title.upper())).split())
        core = " ".join(re.sub(r"\b(CORPORATION|CORP|INCORPORATED|INC|COMPANY|CO|LIMITED|LTD|PLC|THE)\b", " ", full).split())
        for keyword in dict.fromkeys(k for k in (full, core) if k):   # the core name can bury a small recipient under namesakes
            exact = [x for x in find(keyword) if x.get("uei") and _legal_name(x.get("name")) == _legal_name(title)]
            if exact:
                break
    parents = [x for x in exact if x.get("recipient_level") == "P"]
    with ThreadPoolExecutor(max_workers=3) as ex:
        kids = list(ex.map(lambda x: _usa_call("recipient/children/%s/?year=latest" % x["uei"]), parents))
    covered = {x["uei"] for x in parents} | {k["uei"] for part in kids for k in part if k.get("uei")}
    value = sum(x.get("amount") or 0 for x in parents)
    # a recipient outside those parents counts on its own: at its parent level if it has one
    for x in sorted((x for x in exact if x.get("recipient_level") != "P"), key=lambda x: x.get("recipient_level") != "R"):
        if x["uei"] not in covered:
            covered.add(x["uei"]); value += x.get("amount") or 0
    for uei in mapped:
        if uei not in covered:
            rows = [x for x in find(uei) if x.get("uei") == uei]
            rows.sort(key=lambda x: {"P": 0, "R": 1}.get(x.get("recipient_level"), 2))
            covered.add(uei)
            if rows:
                value += rows[0].get("amount") or 0
    name = title or (mapping[mapped[0]]["name"] if mapped else symbol)
    note = ('Net federal contract obligations in the last 12 months, across new and earlier contracts; '
            'the list shows contracts signed in the last 365 days. Company scope follows USAspending parent links, which can lag spin-offs and sales; '
            'subsidiaries it does not link to the parent are not included.')
    if not covered:
        return dict(value=None, currency='USD', status='none', source='USAspending', rows=[], name=name,
                    note='No federal contractor with this company name was found on USAspending. A subsidiary under another name would not be matched.')

    frm, today = _one_year_ago(), datetime.date.today().isoformat()
    ueis = sorted(covered)
    fields = ["Award ID", "Recipient Name", "Recipient UEI", "Award Amount", "End Date", "Base Obligation Date",
              "Awarding Agency", "Description", "generated_internal_id"]

    def newest(chunk):     # one page per chunk is enough: the newest 100 overall are among each chunk's newest 100
        return _usa_call("search/spending_by_award/", {"filters": {"award_type_codes": ["A", "B", "C", "D"],
                         "recipient_search_text": chunk, "time_period": [{"start_date": frm, "end_date": today}]},
                         "fields": fields, "page": 1, "limit": 100, "sort": "Base Obligation Date", "order": "desc"})
    with ThreadPoolExecutor(max_workers=3) as ex:
        pages = list(ex.map(newest, [ueis[i:i + 10] for i in range(0, len(ueis), 10)]))   # larger batches earn a 503
    rows, more = {}, False
    for d in pages:
        got = [r for r in d.get("results") or [] if r.get("Recipient UEI") in covered and r.get("generated_internal_id")
               and frm <= (r.get("Base Obligation Date") or "") <= today]
        rows.update((r["generated_internal_id"], r) for r in got)
        last = (d.get("results") or [{}])[-1].get("Base Obligation Date") or ""   # further pages only matter if this one ends inside the window
        more = more or bool((d.get("page_metadata") or {}).get("hasNext") and last >= frm)
    ordered = sorted(rows.values(), key=lambda r: r["Base Obligation Date"], reverse=True)
    shown = [{"date": r["Base Obligation Date"], "end": r.get("End Date"), "recipient": r.get("Recipient Name"),
              "agency": r.get("Awarding Agency"), "title": r.get("Description") or None, "amount": r.get("Award Amount"),
              "url": "https://www.usaspending.gov/award/" + urllib.parse.quote(r["generated_internal_id"], safe="_-")}
             for r in ordered[:100]]
    if not shown and not value:
        return dict(value=None, currency='USD', status='none', source='USAspending', rows=[], name=name, note=note)
    return dict(value=value, currency='USD', status='ok', source='USAspending', metric='federal_contract_obligations',
                count=len(shown), rows=shown, window='Last 12 months', truncated=more or len(ordered) > 100, name=name, note=note)


def _govcon_rows(limit=None):
    import pathlib
    from concurrent.futures import ThreadPoolExecutor
    mapping = json.loads(pathlib.Path(__file__).with_name("govcon-recipients.json").read_text(encoding="utf-8"))["recipients"]
    items = list(mapping.items())
    chunks = [dict(items[i:i + 10]) for i in range(0, len(items), 10)]
    def run(chunk):
        try: return _govcon_chunk(chunk, limit)
        except Exception as e:
            LOG.warning("USAspending recipient chunk failed: %s", _err(e))
            return None
    with ThreadPoolExecutor(max_workers=3) as pool: results = list(pool.map(run, chunks))
    available = [r for r in results if r is not None]
    if not available: raise RuntimeError("Official USAspending API unavailable")
    rows = {r["url"]: r for part, _ in available for r in part}
    out = sorted(rows.values(), key=lambda r: r["awarded"], reverse=True)
    return (out[:limit] if limit else out), len(available) == len(chunks) and all(c for _, c in available)


def _govcon_finish(out, failed):
    out.sort(key=lambda x: (x.get("awarded") or x.get("date") or ""), reverse=True)
    for r in out:
        r.pop("awardKey", None); r.pop("piid", None)
    return out, (600 if failed else 1800)     # a partial date lookup is retried in 10 minutes


def _govcon_all():
    """Every listed-company award signed in the last 12 months, newest first (cached)."""
    global GOVCON_ALL_CACHE
    hit = GOVCON_ALL_CACHE
    if hit and time.time() - hit[0] < hit[2]:
        return hit[1]
    with _GC_ALL_LOCK:                                   # one build at a time (request or warm-up)
        hit = GOVCON_ALL_CACHE
        if hit and time.time() - hit[0] < hit[2]:
            return hit[1]
        now = time.time()
        out, complete = _govcon_rows()
        ttl = 1800 if complete else 600
        res = {"rows": out, "complete": complete, "from": _one_year_ago(),
               "source": "USAspending", "feedFrom": _one_year_ago(), "feedTo": datetime.date.today().isoformat()}
        GOVCON_ALL_CACHE = (now, res, ttl)
        LOG.info("govcon 12-month list: %d awards in %.0fs (%d lookup chunks failed)",
                 len(out), time.time() - now, 0 if complete else 1)
        return res


def _govcon_card():
    """The 40 most recent listed-company awards, for the card."""
    global GOVCON_CACHE
    full = GOVCON_ALL_CACHE
    if full and time.time() - full[0] < full[2]:
        return full[1]["rows"][:40]                      # already dated and sorted: no extra lookups
    hit = GOVCON_CACHE
    if not (hit and time.time() - hit[0] < hit[2]):
        now = time.time()
        out, complete = _govcon_rows(limit=40)
        ttl = 1800
        GOVCON_CACHE = hit = (now, out, ttl)
    # ALL CONTRACTS needs a (at times multi-minute) USAspending date lookup: start it now
    _warm(_GC_ALL_LOCK, _govcon_all, "govcon")
    return hit[1]


_MT_RAW = {}   # path -> (fetched_ts, gzipped bytes): the card and its 12-month view share one download
def _mt_snapshot(path):
    hit = _MT_RAW.get(path)
    if hit and time.time() - hit[0] < 1800:
        raw = hit[1]
    else:
        raw = urllib.request.urlopen(urllib.request.Request(MT_BASE + path, headers=MT_UA), timeout=45).read()
        _MT_RAW[path] = (time.time(), raw)
    return json.loads(gzip.decompress(raw))
Y_OPENER = None    # shared cookie-jar opener for Yahoo (crumb auth)
Y_CRUMB = None
Y_LOCK = threading.Lock()   # serialize crumb (re)build across threads (ThreadingHTTPServer)
NSE_OPENER = None  # shared cookie-jar opener for nseindia.com (bot-cookie handshake)
NSE_LOCK = threading.Lock()

# Single-flight per request path (Batch 2.3): identical requests that arrive while one is still being
# computed wait for it, then answer from the cache it fills — instead of each one hitting upstream
# (3 concurrent cold /api/ma calls cost 3x the SEC traffic). Distinct paths never wait on each other.
_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()
def _path_lock(path):
    with _PATH_LOCKS_GUARD:
        if len(_PATH_LOCKS) > 256:   # bound the table; current holders keep their lock object
            _PATH_LOCKS.clear()
        lk = _PATH_LOCKS.get(path)
        if lk is None:
            lk = _PATH_LOCKS[path] = threading.Lock()
        return lk
YHOLD_CACHE = {}   # symbol -> (fetched_ts, dict) for institutional-holdings data
SHP_CACHE = {}     # symbol -> (fetched_ts, dict) for Indian shareholding pattern (Screener)
INST_CACHE = {}    # cik -> (fetched_ts, dict) for an institution's latest 13F portfolio
CUSIP_CACHE = {}   # cusip -> ticker (OpenFIGI), stable so cached for the process lifetime
RELS_CACHE = {}    # symbol -> (fetched_ts, dict) for parent/subsidiary relationships (SEC Ex-21)
MA_CACHE = {}      # symbol -> (fetched_ts, dict) for a company's M&A filing history (SEC)
MADEALS_CACHE = None   # (fetched_ts, list) recent market-wide M&A filings (EDGAR Form 425 full-text search)
FIN_CACHE = {}     # symbol -> (fetched_ts, dict) for financial statements (SEC XBRL company facts)
CIK_MAP = None     # (fetched_ts, {TICKER: cik}) SEC ticker->CIK directory, refreshed daily
SEC_TITLES = {}    # TICKER -> company name, from the same directory
SEC_UA = {"User-Agent": "stock research contact@example.com"}
# Major 13F filers (name, SEC CIK) — verified to file 13F-HR.
INSTITUTIONS = [
    {"name": "Berkshire Hathaway",      "cik": "1067983"},
    {"name": "BlackRock",               "cik": "1364742"},
    {"name": "Vanguard Group",          "cik": "102909"},
    {"name": "State Street",            "cik": "93751"},
    {"name": "Fidelity (FMR)",          "cik": "315066"},
    {"name": "Geode Capital",           "cik": "1214717"},
    {"name": "Morgan Stanley",          "cik": "895421"},
    {"name": "Bridgewater Associates",  "cik": "1350694"},
    {"name": "Renaissance Technologies","cik": "1037389"},
    {"name": "Citadel Advisors",        "cik": "1423053"},
    {"name": "Jane Street",             "cik": "1595888"},
    {"name": "Millennium Management",   "cik": "1273087"},
    {"name": "Tiger Global",            "cik": "1167483"},
    {"name": "Pershing Square (Ackman)","cik": "1336528"},
    {"name": "Third Point (Loeb)",      "cik": "1040273"},
    {"name": "Appaloosa (Tepper)",      "cik": "1006438"},
    {"name": "Scion Asset (Burry)",     "cik": "1649339"},
    {"name": "ARK Invest (Wood)",       "cik": "1697748"},
    {"name": "Baupost (Klarman)",       "cik": "1061768"},
    {"name": "Duquesne (Druckenmiller)","cik": "1536411"},
]
UNIVERSE_TTL = 20 * 3600   # re-pull instrument universes after ~20h so new IPOs/listings appear each day


def yahoo_qs(sym, modules):
    """Fetch Yahoo quoteSummary modules for a symbol using a shared crumb+cookie session."""
    global Y_OPENER, Y_CRUMB
    def session():
        global Y_OPENER, Y_CRUMB
        if Y_OPENER is not None and Y_CRUMB:
            return
        with Y_LOCK:                                   # only one thread rebuilds the crumb
            if Y_OPENER is not None and Y_CRUMB:
                return
            cj = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
            try:
                op.open("https://fc.yahoo.com", timeout=15)
            except Exception:
                pass
            crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
            Y_CRUMB = crumb; Y_OPENER = op
    def fetch():
        session()
        url = ("https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
               + urllib.parse.quote(sym) + "?modules=" + urllib.parse.quote(modules)
               + "&crumb=" + urllib.parse.quote(Y_CRUMB))
        return json.loads(Y_OPENER.open(url, timeout=15).read())
    try:
        d = fetch()
    except Exception as e:
        LOG.warning("yahoo_qs %s: first attempt failed (%s); refreshing crumb and retrying", sym, _err(e))
        Y_OPENER = None; Y_CRUMB = None   # crumb may have expired -> refresh once and retry
        d = fetch()
    return (d.get("quoteSummary", {}).get("result") or [{}])[0]


def yahoo_screener(field, value, region, size=20):
    """Top companies (by market cap) matching an industry OR sector via Yahoo's screener. Note: Yahoo's
    screener rejects industry display names containing ' - ' (e.g. 'Software - Infrastructure'), so the
    caller falls back to sector for those. Reuses the crumb session."""
    global Y_OPENER, Y_CRUMB
    if not value:
        return []
    def run():
        if Y_OPENER is None or not Y_CRUMB:
            yahoo_qs("AAPL", "price")   # warm the shared crumb+cookie session
        body = {"size": size, "offset": 0, "sortField": "intradaymarketcap", "sortType": "DESC",
                "quoteType": "EQUITY", "userId": "", "userIdType": "guid",
                "query": {"operator": "AND", "operands": [
                    {"operator": "EQ", "operands": [field, value]},
                    {"operator": "EQ", "operands": ["region", region]}]}}
        url = "https://query1.finance.yahoo.com/v1/finance/screener?crumb=" + urllib.parse.quote(Y_CRUMB)
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "Mozilla/5.0"}, method="POST")
        return json.loads(Y_OPENER.open(req, timeout=20).read())
    try:
        d = run()
    except Exception as e:
        LOG.warning("yahoo_screener %s=%s: first attempt failed (%s); refreshing crumb and retrying", field, value, _err(e))
        Y_OPENER = None; Y_CRUMB = None
        d = run()
    return ((d.get("finance", {}).get("result") or [{}])[0].get("quotes") or [])


# market-cap bands are currency-specific — Yahoo's screener market cap is in the market's local currency.
# US (USD): small 300M–2B, mid 2B–10B, large 10B–200B, mega 200B+.  India (INR): small ₹500cr–5,000cr,
# mid 5,000–20,000cr, large 20,000cr–2L cr, mega ₹2L cr+ (~$24B, ~top 40 names).  "" = whole market.
MOVER_CAPS = {
    "us": ({"small": (3e8, 2e9), "mid": (2e9, 1e10), "large": (1e10, 2e11), "mega": (2e11, None)}, 5e7, 2),
    "in": ({"small": (5e9, 5e10), "mid": (5e10, 2e11), "large": (2e11, 2e12), "mega": (2e12, None)}, 1e9, 10),
}
def yahoo_movers(mode, cap, region="us", size=15):
    """Market-wide top gainers / losers / most-active from Yahoo's screener, optionally within a market-cap
    band. Sorted by % change (gainers/losers) or day volume (most active). Keyless (shared crumb)."""
    global Y_OPENER, Y_CRUMB
    caps, capFloor, priceFloor = MOVER_CAPS.get(region, MOVER_CAPS["us"])
    def run():
        if Y_OPENER is None or not Y_CRUMB:
            yahoo_qs("AAPL", "price")   # warm the crumb+cookie session
        ops = [{"operator": "EQ", "operands": ["region", region]},
               {"operator": "GT", "operands": ["intradayprice", priceFloor]},   # skip penny stocks
               {"operator": "GT", "operands": ["dayvolume", 100000]}]           # skip illiquid names
        band = caps.get(cap)
        if band:
            ops.append({"operator": "GT", "operands": ["intradaymarketcap", band[0]]})
            if band[1]:
                ops.append({"operator": "LT", "operands": ["intradaymarketcap", band[1]]})
        else:
            ops.append({"operator": "GT", "operands": ["intradaymarketcap", capFloor]})
        sf, st = {"gainers": ("percentchange", "DESC"), "losers": ("percentchange", "ASC"),
                  "active": ("dayvolume", "DESC")}.get(mode, ("percentchange", "DESC"))
        body = {"size": size, "offset": 0, "sortField": sf, "sortType": st,
                "quoteType": "EQUITY", "userId": "", "userIdType": "guid",
                "query": {"operator": "AND", "operands": ops}}
        url = "https://query1.finance.yahoo.com/v1/finance/screener?crumb=" + urllib.parse.quote(Y_CRUMB)
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
        return json.loads(Y_OPENER.open(req, timeout=20).read())
    try:
        d = run()
    except Exception as e:
        LOG.warning("yahoo_movers %s: first attempt failed (%s); refreshing crumb and retrying", mode, _err(e))
        Y_OPENER = None; Y_CRUMB = None
        d = run()
    return ((d.get("finance", {}).get("result") or [{}])[0].get("quotes") or [])


def nse_get(path, raw=False):
    """GET nseindia.com/api/<path> as JSON. NSE gates its APIs behind a browser cookie, so we keep a
    warmed cookie-jar opener and re-handshake on failure. Returns parsed JSON (list/dict), or the raw
    bytes with raw=True (NSE's `csv=true` downloads)."""
    global NSE_OPENER
    def build():
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        op.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
            ("Accept", "*/*"), ("Accept-Language", "en-US,en;q=0.9"),
            ("Referer", "https://www.nseindia.com/")]
        try:
            op.open("https://www.nseindia.com/", timeout=20).read()   # sets the bot cookies
        except Exception as e:
            LOG.warning("nse_get: cookie warm-up failed: %s", _err(e))
            pass
        return op
    def fetch():
        global NSE_OPENER
        if NSE_OPENER is None:
            with NSE_LOCK:
                if NSE_OPENER is None:
                    NSE_OPENER = build()
        b = NSE_OPENER.open("https://www.nseindia.com/api/" + path, timeout=30).read()
        return b if raw else json.loads(b)
    try:
        return fetch()
    except Exception as e:
        LOG.warning("nse_get %s: first attempt failed (%s); rebuilding cookie session and retrying", path, _err(e))
        NSE_OPENER = None   # stale cookie -> rebuild once and retry
        return fetch()


PIT_XML_CACHE = {}   # NSE insider-filing XBRL url -> parsed trade rows (a filing never changes; a revision is a new filing)


def _pit_key(r):
    return (r["date"], str(r["owner"] or "").strip().lower(), r["type"], r["mode"], r["shares"], r["value"])


def _pit_same_trade(old, new):
    """Does `new`, a row of a later revision, correct the trade in `old`? Same person, and at most one of
    date / type / shares / value differs: the one being corrected. Mode is left out because revisions fix it
    too (HCLTECH, Sep 2026: Market Purchase corrected to Market Sale)."""
    if str(old["owner"] or "").strip().lower() != str(new["owner"] or "").strip().lower():
        return False
    return sum(old[k] == new[k] for k in ("date", "type", "shares", "value")) >= 3


def nse_pit_filings(sym, want=20):
    """SEBI PIT Regulation 7(2) insider trades filed through NSE's XBRL system, which replaced the
    corporates-pit feed (its last filings are dated 2 May 2026). The list endpoint gives one row per filing;
    the trades are in each filing's XBRL file, one Disclosure context per trade. Reads filings newest first
    until `want` trades are found. Returns (rows, complete) - complete is False if any filing failed to load.

    The same trade can be filed more than once, and each must count once. Companies resubmit a whole filing
    minutes apart (Bajaj Finance, 14 May 2026, three times), and most revisions leave prevAppId empty, so the
    original they correct has to be recognised from its rows (Infosys, 27 Jul 2026: value 8,472 corrected to
    817,911). See _pit_same_trade."""
    d = nse_get("corporates-pit-gg?index=equities&symbol=" + urllib.parse.quote(sym))
    filings = [f for f in ((d.get("data") if isinstance(d, dict) else d) or []) if f.get("xmlFileName")]
    replaced = {str(f.get("prevAppId")) for f in filings if f.get("prevAppId")}   # a revised filing supersedes its original
    filings = [f for f in filings if str(f.get("appId")) not in replaced]

    def when(f):
        try:
            return datetime.datetime.strptime(f.get("broadcastDateTime") or "", "%d-%b-%Y %H:%M:%S")
        except ValueError:
            return datetime.datetime.min
    filings.sort(key=when, reverse=True)

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def day(v):   # XBRL "2026-10-26" -> "26-Oct-2026", the format of the older feed
        try:
            return datetime.datetime.strptime(v, "%Y-%m-%d").strftime("%d-%b-%Y")
        except (TypeError, ValueError):
            return v

    def parse(url):
        if url in PIT_XML_CACHE:
            return PIT_XML_CACHE[url]
        if (urllib.parse.urlparse(url).hostname or "").lower() != "nsearchives.nseindia.com":
            return []
        x = NSE_OPENER.open(url, timeout=30).read().decode("utf-8", "replace")
        facts = {}
        for m in re.finditer(r"<([\w-]+):(\w+)\b([^>]*)>([^<]*)</\1:\2>", x):
            ctx = re.search(r'contextRef="([^"]+)"', m.group(3))
            if ctx and m.group(1) not in ("xbrli", "link", "xlink"):
                facts.setdefault(ctx.group(1), {})[m.group(2)] = html.unescape(m.group(4).strip())
        rows = []
        for ctx in sorted(facts, key=lambda c: int(re.sub(r"\D", "", c) or 0)):
            f = facts[ctx]
            if not f.get("NameOfThePerson"):
                continue
            inst = f.get("TypeOfInstrument") or ""
            if inst.lower() == "any other instrument":
                inst = f.get("TypeOfInstrumentOthers") or inst
            mode = f.get("ModeOfAcquisitionOrDisposal") or ""
            if inst and not re.fullmatch(r"equity( shares?)?", inst, re.I):   # e.g. ADRs, warrants: say so
                mode = (mode + " · " if mode else "") + inst
            rows.append({"date": day(f.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate")),
                         "owner": f.get("NameOfThePerson"), "title": f.get("CategoryOfPerson"),
                         "type": f.get("SecuritiesAcquiredOrDisposedTransactionType"), "mode": mode or None,
                         "shares": num(f.get("SecuritiesAcquiredOrDisposedNumberOfSecurity")),
                         "value": num(f.get("SecuritiesAcquiredOrDisposedValueOfSecurity"))})
        if len(PIT_XML_CACHE) > 5000:
            PIT_XML_CACHE.clear()
        PIT_XML_CACHE[url] = rows
        return rows

    from concurrent.futures import ThreadPoolExecutor
    out, complete, i = [], True, 0
    seen, revised = set(), []   # trades already kept from newer filings; the rows of newer revisions
    while i < len(filings) and len(out) < want and i < 60:
        batch = filings[i:i + 8]; i += 8
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(parse, f["xmlFileName"]) for f in batch]
        for f, fu in zip(batch, futs):
            try:
                rows = fu.result()
            except Exception as e:
                complete = False
                LOG.warning("nse_pit_filings %s: %s failed: %s", sym, f.get("xmlFileName"), _err(e))
                continue
            # identical rows inside ONE filing are separate lines of that filing, so compare across filings only
            keys = [_pit_key(r) for r in rows]
            out.extend(r for r, k in zip(rows, keys)
                       if k not in seen and not any(_pit_same_trade(r, n) for n in revised))
            seen.update(keys)
            if str(f.get("typeOfSubmission") or "").lower() == "revision":
                revised.extend(rows)
    return out, complete


INDEALS_CACHE = {}   # "card" | "all" -> (fetched_ts, data): NSE bulk & block deals


def _nse_deals(kind, frm, to):
    """NSE bulk or block deals between two dates (DD-MM-YYYY), each with the NAMED client. The JSON form
    of this endpoint stops at 70 rows; the csv=true form returns them all, so that is what we read."""
    import csv, io
    b = nse_get("historicalOR/bulk-block-short-deals?optionType=%s_deals&from=%s&to=%s&csv=true" % (kind, frm, to),
                raw=True)
    out = []
    num = lambda s: float((s or "").replace(",", "").strip() or "nan")
    for r in csv.reader(io.StringIO(b.decode("utf-8-sig", "replace"))):
        if len(r) < 7 or r[0].strip().lower().startswith("date"):
            continue
        try:
            d = datetime.datetime.strptime(r[0].strip().title(), "%d-%b-%Y").date().isoformat()
            qty, px = num(r[5]), num(r[6])
        except Exception:
            continue
        side = r[4].strip().lower()
        out.append({"date": d, "symbol": r[1].strip(), "name": r[2].strip(), "client": r[3].strip(),
                    "side": side if side in ("buy", "sell") else side or None, "kind": kind,
                    "qty": int(qty) if qty == qty else None, "price": px if px == px else None,
                    "value": qty * px if qty == qty and px == px else None,     # ₹, from the reported qty × price
                    "remarks": (r[7].strip() if len(r) > 7 else "") or None})
    return out


def yahoo_reco(sym):
    """'Recommended' (related) symbols for a ticker — fallback peer source. No crumb needed."""
    try:
        url = "https://query1.finance.yahoo.com/v6/finance/recommendationsbysymbol/" + urllib.parse.quote(sym)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
        rec = ((d.get("finance", {}).get("result") or [{}])[0].get("recommendedSymbols") or [])
        return [r.get("symbol") for r in rec if r.get("symbol")]
    except Exception as e:
        LOG.warning("yahoo_reco %s failed: %s", sym, _err(e))
        return []


def yahoo_batch_quotes(symbols):
    """Fetch many symbols' quotes in ONE Yahoo v7/quote request (crumb+cookie). Returns the result list."""
    global Y_OPENER, Y_CRUMB
    def session():
        global Y_OPENER, Y_CRUMB
        if Y_OPENER is not None and Y_CRUMB:
            return
        with Y_LOCK:                                   # only one thread rebuilds the crumb
            if Y_OPENER is not None and Y_CRUMB:
                return
            cj = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
            try:
                op.open("https://fc.yahoo.com", timeout=15)
            except Exception:
                pass
            crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
            Y_CRUMB = crumb; Y_OPENER = op
    def fetch():
        session()
        url = ("https://query1.finance.yahoo.com/v7/finance/quote?symbols="
               + urllib.parse.quote(symbols) + "&crumb=" + urllib.parse.quote(Y_CRUMB))
        return json.loads(Y_OPENER.open(url, timeout=15).read())
    try:
        d = fetch()
    except Exception as e:
        LOG.warning("yahoo_batch_quotes (%d symbols): first attempt failed (%s); refreshing crumb and retrying", len(symbols), _err(e))
        Y_OPENER = None; Y_CRUMB = None   # crumb may have expired -> refresh once and retry
        d = fetch()
    return (d.get("quoteResponse", {}) or {}).get("result") or []


def yahoo_options(sym, date=None):
    """Fetch a Yahoo option chain (v7/finance/options) using the shared crumb+cookie session.
    date=None returns the nearest expiry plus the full list of expirationDates; date=<epoch secs>
    returns that specific expiry's calls & puts. Returns optionChain.result[0] (or {})."""
    global Y_OPENER, Y_CRUMB
    def session():
        global Y_OPENER, Y_CRUMB
        if Y_OPENER is not None and Y_CRUMB:
            return
        with Y_LOCK:
            if Y_OPENER is not None and Y_CRUMB:
                return
            cj = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
            try:
                op.open("https://fc.yahoo.com", timeout=15)
            except Exception:
                pass
            crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
            Y_CRUMB = crumb; Y_OPENER = op
    def fetch():
        session()
        url = ("https://query1.finance.yahoo.com/v7/finance/options/"
               + urllib.parse.quote(sym) + "?crumb=" + urllib.parse.quote(Y_CRUMB))
        if date:
            url += "&date=" + urllib.parse.quote(str(date))
        return json.loads(Y_OPENER.open(url, timeout=15).read())
    try:
        d = fetch()
    except Exception as e:
        LOG.warning("yahoo_options %s: first attempt failed (%s); refreshing crumb and retrying", sym, _err(e))
        Y_OPENER = None; Y_CRUMB = None   # crumb may have expired -> refresh once and retry
        d = fetch()
    return ((d.get("optionChain", {}) or {}).get("result") or [{}])[0]


_UNIVERSE_LOCK = threading.Lock()


def nse_universe():
    # Full NSE equity universe (symbol/name/isin) from Upstox's instrument file.
    global NSE_CACHE
    with _UNIVERSE_LOCK:
        if NSE_CACHE is None or time.time() - NSE_CACHE[0] > UNIVERSE_TTL:
            req = urllib.request.Request(NSE_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
            data = json.loads(gzip.decompress(raw))
            out = []
            for x in data:
                if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ":
                    s = x.get("trading_symbol")
                    if s:
                        out.append({"s": s, "n": x.get("name") or s, "isin": x.get("isin")})
            out.sort(key=lambda z: z["s"])
            NSE_CACHE = (time.time(), out)
        return NSE_CACHE[1]


def us_universe():
    # Full US-listed universe (symbol + company name) from the SEC company directory — keyless and
    # reliable (~10k filers incl. ADRs like TX/Ternium), so any name is searchable.
    global US_CACHE
    with _UNIVERSE_LOCK:
        if US_CACHE is None or time.time() - US_CACHE[0] > UNIVERSE_TTL:
            raw = urllib.request.urlopen(
                urllib.request.Request("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA),
                timeout=40).read()
            data = json.loads(raw)
            out, seen = [], set()
            for v in data.values():
                s = str(v.get("ticker", "")).strip().upper()
                n = str(v.get("title", "")).strip()
                if s and s not in seen:
                    seen.add(s); out.append({"s": s, "n": n})
            out.sort(key=lambda z: z["s"])
            US_CACHE = (time.time(), out)
        return US_CACHE[1]


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Never let the browser serve a stale cached copy of the page — always fetch the latest HTML/JS
        # so code changes take effect on a normal reload (no hard-refresh needed).
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _sf(self, fn):
        # serialize identical in-flight requests (cached endpoints only) — see _path_lock
        with _path_lock(self.path):
            return fn()

    def do_GET(self):
        if self.path.startswith('/api/avg-volume'):
            return self._sf(self._avg_volume)
        if self.path.startswith('/api/order-announcements'):
            return self._sf(self._rpo)
        if self.path.startswith("/api/ping"):
            return self._json(200, {"ok": True})
        if self.path.startswith("/api/pdfview"):   # must precede "/api/pdf" (prefix would shadow it)
            return self._pdfview()
        if self.path.startswith("/api/pdf"):
            return self._pdf()
        if self.path.startswith("/api/yahoo"):
            return self._proxy_yahoo()
        if self.path.startswith("/api/gnews"):
            return self._proxy_gnews()
        if self.path.startswith("/api/upstox"):
            return self._proxy_upstox()
        if self.path.startswith("/api/nse-list"):
            return self._sf(self._nse_list)
        if self.path.startswith("/api/us-list"):
            return self._sf(self._us_list)
        if self.path.startswith("/api/mcx"):
            return self._mcx()
        if self.path.startswith("/api/screener"):   # must precede "/api/screen" (prefix would shadow it)
            return self._screener()
        if self.path.startswith("/api/screen"):
            return self._screen()
        if self.path.startswith("/api/premovers"):   # must precede "/api/movers"? no shared prefix, but keep grouped
            return self._sf(self._premovers)
        if self.path.startswith("/api/movers"):
            return self._sf(self._movers)
        if self.path.startswith("/api/options"):
            return self._sf(self._options)
        if self.path.startswith("/api/inearn"):
            return self._sf(self._inearn)
        if self.path.startswith("/api/indfin"):
            return self._sf(self._indfin)
        if self.path.startswith("/api/dividends"):
            return self._sf(self._dividends)
        if self.path.startswith("/api/splits"):
            return self._splits()
        if self.path.startswith("/api/peers"):
            return self._sf(self._peers)
        if self.path.startswith("/api/ipos"):
            return self._sf(self._ipos)
        if self.path.startswith("/api/earncal"):
            return self._sf(self._earncal)
        if self.path.startswith("/api/econ"):
            return self._sf(self._econ)
        if self.path.startswith("/api/corrpair"):
            return self._sf(self._corrpair)
        if self.path.startswith("/api/corr"):
            return self._sf(self._corr)
        if self.path.startswith("/api/earnings"):
            return self._sf(self._earnings)
        if self.path.startswith("/api/yquotes"):
            return self._yquotes()
        if self.path.startswith("/api/current-ratio"):
            return self._sf(self._current_ratio)
        if self.path.startswith("/api/yfund"):
            return self._sf(self._yfund)
        if self.path.startswith("/api/analyst"):
            return self._sf(self._analyst)
        if self.path.startswith("/api/spchanges"):
            return self._sf(self._spchanges)
        if self.path.startswith("/api/index-members"):
            return self._sf(self._index_members)
        if self.path.startswith("/api/holidays"):
            return self._sf(self._holidays)
        if self.path.startswith("/api/yholders"):
            return self._sf(self._yholders)
        if self.path.startswith("/api/shareholding"):
            return self._sf(self._shareholding)
        if self.path.startswith("/api/indeals"):
            return self._sf(self._indeals)
        if self.path.startswith("/api/exec"):
            return self._sf(self._exec)
        if self.path.startswith("/api/congress"):
            return self._sf(self._congress)
        if self.path.startswith("/api/govcon"):
            return self._sf(self._govcon)
        if self.path.startswith("/api/insttotals"):
            return self._sf(self._insttotals)
        if self.path.startswith("/api/institutions"):
            return self._json(200, INSTITUTIONS)
        if self.path.startswith("/api/cusip"):
            return self._sf(self._cusip)
        if self.path.startswith("/api/financials"):
            return self._sf(self._financials)
        if self.path.startswith("/api/rels"):
            return self._sf(self._rels)
        if self.path.startswith("/api/madeals"):
            return self._sf(self._madeals)
        if self.path.startswith("/api/inmadeals"):
            return self._sf(self._inmadeals)
        if self.path.startswith("/api/inma"):
            return self._sf(self._inma)
        if self.path.startswith("/api/ma"):
            return self._sf(self._ma)
        if self.path.startswith("/api/inst"):
            return self._sf(self._inst)
        return super().do_GET()

    def _avg_volume(self):
        sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('symbol', [''])[0].strip().upper()
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-/&]{0,30}', sym):
            return self._json(400, {'error': 'Invalid symbol'})
        today = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=330 if sym.endswith('.NS') else -240)).date()
        cache = getattr(self.__class__, '_avg_volume_cache', {})
        hit = cache.get(sym)
        if hit and hit[0] == today and time.time()-hit[1] < (21600 if hit[2]['value'] is not None else 600):
            return self._json(200, hit[2])
        def average(points, source):
            sessions = sorted(points.items())[-20:]
            if len(sessions) != 20:
                return None
            return dict(value=sum(v for _, v in sessions)/20, sessions=20, asOf=sessions[-1][0], source=source)
        result = None
        if sym.endswith('.NS'):
            try:
                query = urllib.parse.urlencode(dict(symbol=sym[:-3], series='["EQ"]', from_date=(today-datetime.timedelta(days=90)).strftime('%d-%m-%Y'), to_date=today.strftime('%d-%m-%Y')))
                data = nse_get('historical/securityArchives?'+query)
                points = {}
                for row in data.get('data', []):
                    day = datetime.datetime.strptime(row['CH_TIMESTAMP'][:10], '%Y-%m-%d').date()
                    volume = float(row['CH_TOT_TRADED_QTY'])
                    if day < today and volume >= 0 and math.isfinite(volume):
                        points[day.isoformat()] = volume
                result = average(points, 'NSE')
            except Exception:
                pass
        if result is None:
            try:
                request = urllib.request.Request(YBASE+urllib.parse.quote(sym)+'?interval=1d&range=3mo', headers={'User-Agent': 'Mozilla/5.0'})
                chart = json.loads(urllib.request.urlopen(request, timeout=25).read())['chart']['result'][0]
                offset = chart['meta'].get('gmtoffset', 0)
                market_today = (datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=offset)).date()
                points = {}
                for stamp, volume in zip(chart.get('timestamp', []), chart['indicators']['quote'][0].get('volume', [])):
                    day = (datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)+datetime.timedelta(seconds=offset)).date()
                    if day < market_today and isinstance(volume, (int, float)) and volume >= 0 and math.isfinite(volume):
                        points[day.isoformat()] = volume
                result = average(points, 'Yahoo Finance')
            except Exception:
                pass
        result = result or dict(value=None, sessions=0, source=None, asOf=None)
        if len(cache) >= 256:
            cache.pop(next(iter(cache)))
        cache[sym] = (today, time.time(), result)
        self.__class__._avg_volume_cache = cache
        return self._json(200, result)

    def _rpo(self):
        symbol = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('symbol', [''])[0].strip().upper()
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-/&]{0,30}', symbol):
            return self._json(400, {'error': 'Invalid symbol'})
        now = time.time()
        cache = getattr(self.__class__, '_rpo_cache', {})
        hit = cache.get(symbol)
        if hit and now-hit[0] < (21600 if hit[1]['status'] in ('ok', 'none') else 600):
            return self._json(200, hit[1])
        result = {'value': None, 'status': 'unsupported', 'rows': []}
        if symbol.endswith('.NS'):
            today = datetime.datetime.now().date()
            query = urllib.parse.urlencode(dict(index='equities', symbol=symbol[:-3], from_date=(today-datetime.timedelta(days=365)).strftime('%d-%m-%Y'), to_date=today.strftime('%d-%m-%Y')))
            try:
                announcements = nse_get('corporate-announcements?'+query)
                rows = [{'date': a.get('an_dt', ''), 'title': a.get('attchmntText') or a.get('desc'), 'url': a.get('attchmntFile')} for a in announcements if a.get('desc') == 'Bagging/Receiving of orders/contracts' and str(a.get('attchmntFile','')).startswith(('https://nsearchives.nseindia.com/','https://archives.nseindia.com/'))]
                # Announcement metadata has no exact monetary field. Counts and value bands are not amounts.
                result = dict(value=None, currency='INR', status='not_disclosed', source='NSE', metric='order_announcement_total', count=len(rows), rows=rows, window='Last 365 days', reason='No verified complete monetary total in announcement metadata')
            except Exception:
                result = dict(value=None, status='unavailable', rows=[])
        elif re.fullmatch(r'[A-Z]{1,6}(-[A-Z]{1,2})?', symbol):
            try:
                self._cik_for(symbol)   # fills SEC_TITLES
                result = _us_contracts(symbol, SEC_TITLES.get(symbol))
            except Exception as e:
                LOG.warning('us contracts %s failed: %s', symbol, _err(e))
                result = dict(value=None, status='unavailable', rows=[], note='USAspending is unavailable right now.')
        else:
            result['note'] = 'Government contracts are available for US-listed companies.'
        result['symbol'] = symbol
        if len(cache) >= 256:
            cache.pop(next(iter(cache)))
        cache[symbol] = (now, result)
        self.__class__._rpo_cache = cache
        return self._json(200, result)

    def _cik_for(self, sym):
        # Resolve a ticker to its SEC CIK via the official company_tickers directory (cached daily).
        global CIK_MAP
        now = time.time()
        if CIK_MAP is None or now - CIK_MAP[0] > 20 * 3600:
            raw = urllib.request.urlopen(
                urllib.request.Request("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA),
                timeout=25).read()
            d = json.loads(raw)
            m = {}
            for v in d.values():
                t = str(v.get("ticker", "")).upper()
                if t:
                    m[t] = str(v.get("cik_str", "")).zfill(10)
                    SEC_TITLES[t] = v.get("title")
            CIK_MAP = (now, m)
        t = (sym or "").upper().replace("/", "-").replace(".", "-")
        return CIK_MAP[1].get(t)

    def _pdf(self):
        # Proxy an NSE announcement PDF so the browser VIEWS it inline instead of downloading it
        # (nsearchives serves PDFs with Content-Disposition: attachment). Restricted to NSE hosts (no open SSRF).
        try:
            url = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("url", [""])[0]
            if not url:
                self._json(400, {"error": "missing url"}); return
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if not (host == "nseindia.com" or host.endswith(".nseindia.com")):
                self._json(403, {"error": "host not allowed"}); return
            hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
                    "Accept": "application/pdf,*/*", "Referer": "https://www.nseindia.com/"}
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=30)
                body = r.read(); ctype = r.headers.get("Content-Type") or "application/pdf"
            except Exception as e:
                LOG.warning("pdf %s: direct fetch failed (%s); retrying via cookie session", url[:160], _err(e))
                # some archive paths gate on the NSE bot cookie -> retry through a warmed cookie session
                cj = http.cookiejar.CookieJar()
                op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
                op.addheaders = list(hdrs.items())
                try:
                    op.open("https://www.nseindia.com/", timeout=20).read()
                except Exception:
                    pass
                r = op.open(url, timeout=30)
                body = r.read(); ctype = r.headers.get("Content-Type") or "application/pdf"
            self.send_response(200)
            self.send_header("Content-Type", ctype if "pdf" in (ctype or "").lower() else "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", "inline")   # <- view in browser, don't download
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            LOG.error("pdf %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _pdfview(self):
        # HTML viewer that renders the proxied NSE PDF with PDF.js (canvas), so opening the link lands on a
        # normal page and the PDF displays inline in ANY browser — never a file download.
        try:
            url = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("url", [""])[0]
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if not url or not (host == "nseindia.com" or host.endswith(".nseindia.com")):
                self._json(400 if not url else 403, {"error": "bad url"}); return
            src = json.dumps("/api/pdf?url=" + urllib.parse.quote(url, safe=""))
            orig = json.dumps(url)
            name = (url.rsplit("/", 1)[-1] or "Document").replace("<", "").replace(">", "")
            tpl = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__</title>
<style>html,body{margin:0;background:#3a3d42;font:13px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
#bar{position:sticky;top:0;display:flex;align-items:center;gap:12px;padding:8px 14px;background:#25272b;color:#e8eaed;border-bottom:1px solid #000}
#bar a{color:#6cb0f2;text-decoration:none;margin-left:auto}#bar a:hover{text-decoration:underline}
#doc{display:flex;flex-direction:column;align-items:center;gap:12px;padding:16px 8px}
#doc canvas{max-width:100%;height:auto;box-shadow:0 2px 10px rgba(0,0,0,.55);background:#fff}
#msg{color:#c7cad0;padding:40px 16px;text-align:center}</style></head>
<body><div id="bar"><span>__NAME__</span><a id="dl" href=__ORIG__ target="_blank" rel="noopener">Download original ↗</a></div>
<div id="msg">Loading PDF…</div><div id="doc"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script>
var SRC=__SRC__;
if(!window.pdfjsLib){document.getElementById('msg').textContent='PDF viewer failed to load. Use Download original.';}
else{
pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
pdfjsLib.getDocument(SRC).promise.then(function(pdf){
  var msg=document.getElementById('msg'); if(msg) msg.remove();
  var doc=document.getElementById('doc');
  var wrap=Math.min(1000,(doc.clientWidth||900));
  var chain=Promise.resolve();
  for(var i=1;i<=pdf.numPages;i++){(function(n){
    chain=chain.then(function(){return pdf.getPage(n);}).then(function(page){
      var base=page.getViewport({scale:1}); var scale=Math.min(2.2,(wrap/base.width))*1.4;
      var vp=page.getViewport({scale:scale});
      var c=document.createElement('canvas'); c.width=vp.width; c.height=vp.height; doc.appendChild(c);
      return page.render({canvasContext:c.getContext('2d'),viewport:vp}).promise;
    });
  })(i);}
  return chain;
}).catch(function(e){document.getElementById('msg').textContent='Could not load PDF: '+(e&&e.message||e);});
}
</script></body></html>"""
            html = (tpl.replace("__NAME__", name).replace("__ORIG__", orig).replace("__SRC__", src)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        except Exception as e:
            LOG.error("pdfview %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _cusip(self):
        # Resolve a CUSIP to its (US) ticker via OpenFIGI — used to open a 13F holding as a stock.
        try:
            c = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("c", [""])[0].strip().upper()
            if not c:
                self._json(400, {"error": "missing cusip"}); return
            if c in CUSIP_CACHE:
                self._json(200, {"ticker": CUSIP_CACHE[c]}); return
            body = json.dumps([{"idType": "ID_CUSIP", "idValue": c}]).encode()
            req = urllib.request.Request("https://api.openfigi.com/v3/mapping", data=body,
                                         headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            r = json.loads(urllib.request.urlopen(req, timeout=15).read())
            d = (r[0].get("data") if r and isinstance(r, list) else None) or []
            us = [e.get("ticker") for e in d if e.get("exchCode") in ("US", "UN", "UW", "UQ", "UA", "UP")]
            ticker = us[0] if us else (d[0].get("ticker") if d else None)
            CUSIP_CACHE[c] = ticker
            self._json(200, {"ticker": ticker})
        except Exception as e:
            LOG.error("cusip %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    # Line items (label, candidate tags [US-GAAP + IFRS], is_duration, format). First tag with data wins.
    FIN_INCOME = [
        ("Revenue", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "Revenue", "RevenueFromContractsWithCustomers"], True, "usd"),
        ("Cost of revenue", ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "CostOfSales"], True, "usd"),
        ("Gross profit", ["GrossProfit"], True, "usd"),
        ("R&D expense", ["ResearchAndDevelopmentExpense"], True, "usd"),
        ("SG&A expense", ["SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"], True, "usd"),
        ("Operating income", ["OperatingIncomeLoss", "ProfitLossFromOperatingActivities"], True, "usd"),
        ("Net income", ["NetIncomeLoss", "ProfitLoss"], True, "usd"),
        ("Diluted EPS", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "DilutedEarningsLossPerShare"], True, "eps"),
    ]
    FIN_BALANCE = [
        ("Cash & equivalents", ["CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"], False, "usd"),
        ("Current assets", ["AssetsCurrent", "CurrentAssets"], False, "usd"),
        ("Total assets", ["Assets"], False, "usd"),
        ("Current liabilities", ["LiabilitiesCurrent", "CurrentLiabilities"], False, "usd"),
        ("Total liabilities", ["Liabilities"], False, "usd"),
        ("Long-term debt", ["LongTermDebtNoncurrent", "LongTermDebt", "NoncurrentBorrowings", "LongtermBorrowings"], False, "usd"),
        ("Shareholders' equity", ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "Equity", "EquityAttributableToOwnersOfParent"], False, "usd"),
    ]
    FIN_CASHFLOW = [
        ("Operating cash flow", ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", "CashFlowsFromUsedInOperatingActivities"], True, "usd"),
        ("Investing cash flow", ["NetCashProvidedByUsedInInvestingActivities", "CashFlowsFromUsedInInvestingActivities"], True, "usd"),
        ("Financing cash flow", ["NetCashProvidedByUsedInFinancingActivities", "CashFlowsFromUsedInFinancingActivities"], True, "usd"),
        ("Capital expenditure", ["PaymentsToAcquirePropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"], True, "usd"),
    ]

    def _financials(self):
        # Income statement / balance sheet / cash flow (last ~6 fiscal years) from SEC XBRL company facts.
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].strip().upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = FIN_CACHE.get(sym)
            if hit and now - hit[0] < 12 * 3600:
                self._json(200, hit[1]); return
            cik = self._cik_for(sym)
            if not cik:
                data = {"symbol": sym, "cik": None, "years": [], "income": [], "balance": [], "cashflow": [], "note": "no-cik"}
                FIN_CACHE[sym] = (now, data); self._json(200, data); return
            raw = urllib.request.urlopen(
                urllib.request.Request("https://data.sec.gov/api/xbrl/companyfacts/CIK" + cik + ".json", headers=SEC_UA),
                timeout=45).read()
            cf = json.loads(raw)
            facts = cf.get("facts", {})
            taxos = [facts.get("us-gaap", {}), facts.get("ifrs-full", {})]   # US GAAP (10-K) or IFRS (20-F/40-F)
            cur = {"code": None}   # reporting currency, detected from the first monetary line item

            def annual(concepts, dur, is_eps=False):
                # -> {fiscal_year(int): value} from annual filings only
                for c in concepts:
                    for tax in taxos:
                        node = tax.get(c)
                        if not node:
                            continue
                        units = node.get("units", {})
                        if is_eps:
                            uk = next((k for k in units if k.endswith("/shares")), None)
                        else:
                            # a monetary unit (USD, EUR, GBP…) — prefer USD, else the first 3-letter currency
                            uk = "USD" if "USD" in units else next((k for k in units if len(k) == 3), None)
                            if uk and not cur["code"]:
                                cur["code"] = uk
                        if not uk:
                            continue
                        out = {}
                        for e in units[uk]:
                            f = str(e.get("form", ""))
                            if not (f.startswith("10-K") or f.startswith("20-F") or f.startswith("40-F")):
                                continue
                            end = e.get("end")
                            if not end:
                                continue
                            if dur:
                                st = e.get("start")
                                if not st:
                                    continue
                                try:
                                    days = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(st)).days
                                except Exception:
                                    continue
                                if not (340 <= days <= 380):   # annual (full-year) durations only
                                    continue
                            out[int(end[:4])] = e.get("val")
                        if out:
                            return out
                return {}

            # collect the year set across all line items, keep the most recent 6
            extracted = {}   # label -> {year: val, fmt}
            allyears = set()
            for group in (self.FIN_INCOME, self.FIN_BALANCE, self.FIN_CASHFLOW):
                for label, concepts, dur, fmt in group:
                    vals = annual(concepts, dur, fmt == "eps")
                    extracted[label] = (vals, fmt)
                    allyears.update(vals.keys())
            years = sorted(allyears)[-6:]

            def row(label, vals, fmt):
                return {"label": label, "fmt": fmt, "values": [vals.get(y) for y in years]}

            def pct_row(label, num_label, den_label):
                num = extracted.get(num_label, ({}, ""))[0]
                den = extracted.get(den_label, ({}, ""))[0]
                vv = []
                for y in years:
                    n, d = num.get(y), den.get(y)
                    vv.append(round(n / d * 100, 1) if (isinstance(n, (int, float)) and isinstance(d, (int, float)) and d) else None)
                return {"label": label, "fmt": "pct", "values": vv}

            income = [row(l, extracted[l][0], f) for l, _, _, f in self.FIN_INCOME]
            income += [pct_row("Gross margin", "Gross profit", "Revenue"),
                       pct_row("Operating margin", "Operating income", "Revenue"),
                       pct_row("Net margin", "Net income", "Revenue")]
            balance = [row(l, extracted[l][0], f) for l, _, _, f in self.FIN_BALANCE]
            cashflow = [row(l, extracted[l][0], f) for l, _, _, f in self.FIN_CASHFLOW]
            # Free cash flow = operating cash flow - capex
            ocf = extracted.get("Operating cash flow", ({}, ""))[0]
            capex = extracted.get("Capital expenditure", ({}, ""))[0]
            fcf = []
            for y in years:
                o, c = ocf.get(y), capex.get(y)
                fcf.append((o - c) if (isinstance(o, (int, float)) and isinstance(c, (int, float))) else None)
            cashflow.append({"label": "Free cash flow", "fmt": "usd", "values": fcf})

            data = {"symbol": sym, "cik": cik, "name": cf.get("entityName"), "unit": cur["code"] or "USD",
                    "years": years, "income": income, "balance": balance, "cashflow": cashflow}
            FIN_CACHE[sym] = (now, data)
            self._json(200, data)
        except Exception as e:
            LOG.error("financials %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    def _rels(self):
        # Parent/subsidiary relationships from the company's latest 10-K Exhibit 21
        # ("Subsidiaries of the Registrant"). Real data; US SEC filers only.
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].strip().upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = RELS_CACHE.get(sym)
            if hit and now - hit[0] < 24 * 3600:
                self._json(200, hit[1]); return
            cik = self._cik_for(sym)
            if not cik:
                data = {"symbol": sym, "cik": None, "subsidiaries": [], "note": "no-cik"}
                RELS_CACHE[sym] = (now, data); self._json(200, data); return

            def sec(url):
                return urllib.request.urlopen(urllib.request.Request(url, headers=SEC_UA), timeout=25).read()

            sub = json.loads(sec("https://data.sec.gov/submissions/CIK" + cik + ".json"))
            name = sub.get("name")
            rec = sub.get("filings", {}).get("recent", {})
            forms = rec.get("form", []); accs = rec.get("accessionNumber", [])
            idx = next((i for i, f in enumerate(forms) if f in ("10-K", "20-F", "10-K/A")), None)
            subs = []
            if idx is not None:
                acc = accs[idx].replace("-", "")
                base = "https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/" + acc + "/"
                files = [f["name"] for f in json.loads(sec(base + "index.json"))["directory"]["item"]]
                cands = [f for f in files if re.search(r"(ex(hibit)?[\W_]{0,3}21|subsid)", f.lower())
                         and f.lower().endswith((".htm", ".html", ".txt"))]
                for f in cands:
                    body = sec(base + f).decode("utf-8", "ignore")
                    if "subsidiar" not in body.lower():
                        continue
                    subs = self._parse_ex21(body)
                    if subs:
                        break
            data = {"symbol": sym, "cik": cik, "name": name, "subsidiaries": subs[:400], "count": len(subs)}
            RELS_CACHE[sym] = (now, data)
            self._json(200, data)
        except Exception as e:
            LOG.error("rels %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    @staticmethod
    def _parse_ex21(html):
        # Ex-21 is a two-column table (Entity | Jurisdiction). Parse rows; skip the header.
        out = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).replace("&amp;", "&").replace("&#160;", " ").strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
            cells = [c for c in cells if c]
            if not cells:
                continue
            nm = cells[0]
            jur = cells[1] if len(cells) > 1 else ""
            low = nm.lower()
            if low in ("entity", "name", "company", "subsidiary", "subsidiaries", "domicile", "jurisdiction"):
                continue
            if any(w in low for w in ("jurisdiction", "incorporation", "domicile", "entity name",
                                      "company name", "subsidiaries of", "subsidiaries (", "registrant",
                                      "exhibit 21", "state of")):
                continue
            if len(nm) < 2 or len(nm) > 120:
                continue
            out.append({"name": nm, "jurisdiction": jur[:60]})
        return out

    def _ma(self):
        # A company's own M&A / capital-markets activity: merger & tender filings from SEC EDGAR.
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].strip().upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = MA_CACHE.get(sym)
            if hit and now - hit[0] < 6 * 3600:
                self._json(200, hit[1]); return
            cik = self._cik_for(sym)
            if not cik:
                data = {"symbol": sym, "cik": None, "filings": []}
                MA_CACHE[sym] = (now, data); self._json(200, data); return
            sub = json.loads(urllib.request.urlopen(
                urllib.request.Request("https://data.sec.gov/submissions/CIK" + cik + ".json", headers=SEC_UA),
                timeout=25).read())
            name = sub.get("name")
            rec = sub.get("filings", {}).get("recent", {})
            forms = rec.get("form", []); accs = rec.get("accessionNumber", [])
            fdates = rec.get("filingDate", []); docs = rec.get("primaryDocument", [])
            descs = rec.get("primaryDocDescription", [])
            DEAL = {"S-4": "Merger registration", "S-4/A": "Merger registration (amended)",
                    "425": "Business-combination communication", "DEFM14A": "Merger proxy (definitive)",
                    "PREM14A": "Merger proxy (preliminary)", "SC 14D9": "Tender-offer response",
                    "SC TO-T": "Third-party tender offer", "SC TO-I": "Issuer tender offer",
                    "SC 13E3": "Going-private transaction", "SC TO-T/A": "Third-party tender offer (amended)"}
            out = []
            for i, f in enumerate(forms):
                if f not in DEAL:
                    continue
                acc = accs[i].replace("-", "")
                doc = docs[i] if i < len(docs) else ""
                url = "https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/" + acc + "/" + (doc or "")
                out.append({"date": fdates[i] if i < len(fdates) else "", "form": f, "kind": DEAL[f],
                            "desc": (descs[i] if i < len(descs) else "") or "", "url": url})
                if len(out) >= 40:
                    break
            out.sort(key=lambda x: x.get("date", ""), reverse=True)   # newest first

            # ALL recent filings (any form), newest first as EDGAR returns them
            base = "https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/"
            allf = []
            for i in range(min(len(forms), len(fdates))):
                acc = accs[i].replace("-", "")
                doc = docs[i] if i < len(docs) else ""
                allf.append({"date": fdates[i], "form": forms[i],
                             "desc": (descs[i] if i < len(descs) else "") or "",
                             "url": base + acc + "/" + (doc or "")})
                if len(allf) >= 30:
                    break

            # INSIDER trading — parse the most recent Form 4 ownership XMLs (real transactions).
            # Batch 2.4: the XMLs are fetched in waves of up to 6 in parallel (well under SEC's 10 req/s
            # fair-use ceiling) but consumed strictly in filing order with the same stop rule as the old
            # one-at-a-time loop (12 parsed trades or 18 Form 4s), so the output is identical — only the
            # wall time changes. At most 5 XMLs past the stop point are fetched and discarded.
            cands = []                                            # the first 18 Form 4s, in filing order
            for i, f in enumerate(forms):
                if f != "4":
                    continue
                view = docs[i] if i < len(docs) else ""              # rendered HTML (readable) for the link
                raw = re.sub(r"^xsl[^/]*/", "", view)                # raw ownership XML for parsing
                cands.append((i, view, raw, accs[i].replace("-", "") if raw.endswith(".xml") else None))
                if len(cands) >= 18:
                    break
            def _get_xml(acc, raw):
                return urllib.request.urlopen(urllib.request.Request(
                    base + acc + "/" + raw, headers=SEC_UA), timeout=20).read().decode("utf-8", "ignore")
            insider, f4 = [], 0
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as ex:
                for w0 in range(0, len(cands), 6):
                    wave = cands[w0:w0 + 6]
                    futs = {c[3]: ex.submit(_get_xml, c[3], c[2]) for c in wave if c[3]}
                    for i, view, raw, acc in wave:
                        f4 += 1
                        if acc:
                            try:
                                t = self._parse_form4(futs[acc].result(), fdates[i] if i < len(fdates) else "",
                                                      base + acc + "/" + view)
                                if t:
                                    insider.append(t)
                            except Exception as e:
                                LOG.warning("ma %s: form4 %s skipped: %s", sym, acc, _err(e))
                        if len(insider) >= 12 or f4 >= 18:
                            break
                    else:
                        continue
                    break

            data = {"symbol": sym, "cik": cik, "name": name, "filings": out,
                    "allFilings": allf, "insider": insider}
            MA_CACHE[sym] = (now, data)
            self._json(200, data)
        except Exception as e:
            LOG.error("ma %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    @staticmethod
    def _parse_form4(xml, date, url):
        # Parse a Form 4 ownership XML into its principal transaction (the real insider trade).
        # Open-market share trades (non-derivative) are preferred; a filing that reports ONLY
        # derivative securities — an RSU award, an option grant/conversion — falls back to those,
        # otherwise such filings would be dropped and the insider would never appear at all.
        def one(tag):
            m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), xml, re.S)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else None
        def flag(tag):
            v = (one(tag) or "").lower()
            return v in ("1", "true")
        owner = one("rptOwnerName")
        title = one("officerTitle")
        roles = []
        if title:
            roles.append(title)
        elif flag("isOfficer"):
            roles.append("Officer")
        if flag("isDirector"):
            roles.append("Director")
        if flag("isTenPercentOwner"):
            roles.append("10% owner")
        def parse_blocks(tag, deriv):
            out = []
            for blk in re.findall(r"<%s>(.*?)</%s>" % (tag, tag), xml, re.S):
                def sub(pat):
                    m = re.search(pat, blk, re.S)
                    return m.group(1).strip() if m else None
                def num(s):
                    try:
                        return float(s) if s not in (None, "") else None
                    except Exception:
                        return None
                out.append({"code": sub(r"<transactionCode>(.*?)</transactionCode>") or "",
                            "ad": sub(r"<transactionAcquiredDisposedCode>\s*<value>(.*?)</value>") or "",
                            # the date the insider actually traded — a Form 4 is filed up to 2 business
                            # days later, so the filing date is NOT when the trade happened
                            "tdate": sub(r"<transactionDate>\s*<value>(.*?)</value>") or "",
                            "shares": num(sub(r"<transactionShares>\s*<value>(.*?)</value>")),
                            "price": num(sub(r"<transactionPricePerShare>\s*<value>(.*?)</value>")),
                            "sec": re.sub(r"\s+", " ", sub(r"<securityTitle>\s*<value>(.*?)</value>") or "").strip(),
                            "deriv": deriv})
            return out
        txns = parse_blocks("nonDerivativeTransaction", False)
        if not txns:
            txns = parse_blocks("derivativeTransaction", True)   # RSU/option-only filing
        if not txns:
            return None
        def val(t):
            return (t["shares"] or 0) * (t["price"] or 0)
        openmkt = [t for t in txns if t["code"] in ("P", "S") and (t["price"] or 0) > 0]
        prim = max(openmkt, key=val) if openmkt else max(txns, key=lambda t: (t["shares"] or 0))
        LABEL = {"P": "Buy", "S": "Sell", "M": "Option exercise", "F": "Tax withheld",
                 "A": "Grant", "G": "Gift", "C": "Conversion", "X": "Option exercise",
                 "J": "Other", "D": "Sale", "W": "Inherited"}
        ttype = LABEL.get(prim["code"], prim["code"] or "—")
        if prim["deriv"] and prim["sec"]:
            ttype += " · " + prim["sec"]        # e.g. "Grant · Restricted Stock Units"
        return {"date": prim["tdate"] or date, "filed": date, "owner": owner, "title": ", ".join(roles),
                "type": ttype, "deriv": prim["deriv"],
                "code": prim["code"], "direction": prim["ad"],
                "shares": prim["shares"], "price": prim["price"],
                "value": (val(prim) or None), "url": url}

    def _inmadeals(self):
        # Market-wide Indian M&A wire: NSE corporate announcements over the last ~20 days filtered to
        # genuine deal categories (Acquisition, Merger, Scheme, Open Offer, Disposal, Delisting…). Keyless.
        global IN_MADEALS_CACHE
        try:
            now = time.time()
            if IN_MADEALS_CACHE and now - IN_MADEALS_CACHE[0] < 3 * 3600:
                self._json(200, IN_MADEALS_CACHE[1]); return
            today = datetime.datetime.now(IST).date()
            frm = today - datetime.timedelta(days=20)
            _MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
                    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
            def dkey(s):
                m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", str(s or ""))
                return (int(m.group(3)), _MON.get(m.group(2).lower(), 0), int(m.group(1))) if m else (0, 0, 0)
            MA_RE = re.compile(r"acquisi|amalgamat|\bmerger\b|scheme of arrangement|demerger|open offer|"
                               r"slump sale|sale or disposal|buyback|delisting|joint venture|divest", re.I)
            out, seen = [], set()
            try:
                url = ("corporate-announcements?index=equities&from_date=%s&to_date=%s"
                       % (frm.strftime("%d-%m-%Y"), today.strftime("%d-%m-%Y")))
                for a in (nse_get(url) or []):
                    desc = a.get("desc") or ""
                    if not MA_RE.search(desc):
                        continue
                    sym = a.get("symbol"); dt = (a.get("an_dt") or "").split(" ")[0]
                    k = (sym, dt, desc)
                    if not sym or k in seen:
                        continue
                    seen.add(k)
                    out.append({"date": dt, "ticker": sym, "name": a.get("sm_name") or sym,
                                "form": desc, "url": a.get("attchmntFile") or ""})
            except Exception as e:
                LOG.warning("inmadeals: NSE wire block failed: %s", _err(e))
            out.sort(key=lambda x: dkey(x["date"]), reverse=True)
            out = out[:60]
            IN_MADEALS_CACHE = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("inmadeals failed: %s", _err(e))
            self._json(502, {"error": str(e)})

    def _inma(self):
        # Indian company M&A / capital-markets activity from NSE: deal announcements (acquisitions,
        # mergers, schemes, open offers), SEBI PIT insider trades, and recent announcements. Keyless.
        # Insider trades: NSE's XBRL filings (from 3 May 2026, nse_pit_filings) plus the older corporates-pit
        # feed, which has nothing after 2 May 2026; merged newest first.
        global IN_MA_CACHE
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].strip().upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = IN_MA_CACHE.get(sym)
            if hit and now - hit[0] < 6 * 3600:
                self._json(200, hit[1]); return

            def numf(s):
                try:
                    s = re.sub(r"[^0-9.]", "", str(s))
                    return float(s) if s else None
                except Exception:
                    return None

            # Match the announcement CATEGORY (desc) only — matching the full PDF text pulls in noise
            # like generic "Updates". Keep genuine deal categories (Acquisition, Sale or disposal, Scheme
            # of Arrangement, Amalgamation/Merger, Open Offer, Demerger, Buyback…) and drop the routine
            # "Disclosure under SEBI Takeover Regulations" shareholding disclosures, which aren't deals.
            MA_RE = re.compile(r"acquisi|amalgamat|\bmerger\b|scheme of arrangement|demerger|divest|"
                               r"open offer|slump sale|joint venture|sale or disposal|buyback|"
                               r"disposal of|de-?listing|delisting", re.I)
            deals, allf, name = [], [], None
            try:
                ann = nse_get("corporate-announcements?index=equities&symbol=" + urllib.parse.quote(sym))
                for a in (ann if isinstance(ann, list) else [])[:80]:
                    dt = (a.get("an_dt") or "").split(" ")[0]
                    desc = a.get("desc") or ""
                    row = {"date": dt, "desc": desc, "url": a.get("attchmntFile") or ""}
                    allf.append(row)
                    if MA_RE.search(desc):
                        deals.append(row)
                    if not name and a.get("sm_name"):
                        name = a.get("sm_name")
            except Exception as e:
                LOG.warning("inma %s: deals block failed: %s", sym, _err(e))
            _MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
                    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
            def dkey(s):   # "13-Feb-2026" -> sortable (year, month, day)
                m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", str(s or ""))
                return (int(m.group(3)), _MON.get(m.group(2).lower(), 0), int(m.group(1))) if m else (0, 0, 0)
            insider, pit_ok = [], True
            try:
                new, pit_ok = nse_pit_filings(sym)
                insider.extend(new)
            except Exception as e:
                pit_ok = False
                LOG.warning("inma %s: NSE insider filings (XBRL) failed: %s", sym, _err(e))
            try:
                pit = nse_get("corporates-pit?index=equities&symbol=" + urllib.parse.quote(sym))
                for r in ((pit.get("data") if isinstance(pit, dict) else []) or []):
                    if not name and r.get("company"):
                        name = r.get("company")
                    insider.append({"date": r.get("acqfromDt") or r.get("date"),
                                    "owner": r.get("acqName"), "title": r.get("personCategory"),
                                    "type": r.get("tdpTransactionType"), "mode": r.get("acqMode"),
                                    "shares": numf(r.get("secAcq")), "value": numf(r.get("secVal"))})
            except Exception as e:
                pit_ok = False
                LOG.warning("inma %s: SEBI PIT insider block failed: %s", sym, _err(e))
            insider.sort(key=lambda x: dkey(x["date"]), reverse=True)   # newest first (stable: filing order within a day)
            insider = insider[:20]
            out = {"symbol": sym, "name": name, "market": "IN",
                   "deals": deals[:20], "insider": insider, "announcements": allf[:40]}
            if pit_ok:   # a failed insider source is retried on the next request instead of cached for 6 h
                IN_MA_CACHE[sym] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("inma %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    def _madeals(self):
        # Market-wide recent M&A wire: EDGAR full-text search on Form 425 (business-combination
        # communications) over the last ~30 days -> deduped (company, ticker, date, filing link).
        global MADEALS_CACHE
        try:
            now = time.time()
            if MADEALS_CACHE and now - MADEALS_CACHE[0] < 3 * 3600:
                self._json(200, MADEALS_CACHE[1]); return
            frm = time.strftime("%Y-%m-%d", time.gmtime(now - 30 * 86400))
            to = time.strftime("%Y-%m-%d", time.gmtime(now))
            q = urllib.parse.quote('"merger agreement"')
            # Multiple deal forms so the wire is sortable by form (EDGAR FTS 500s are transient -> retry).
            url = ("https://efts.sec.gov/LATEST/search-index?q=" + q +
                   "&forms=" + urllib.parse.quote("425,S-4,DEFM14A,SC 14D9,SC TO-T") +
                   "&startdt=" + frm + "&enddt=" + to)
            r = None
            for attempt in range(4):
                try:
                    r = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=SEC_UA), timeout=25).read())
                    break
                except Exception as e:
                    LOG.warning("madeals: attempt %s failed: %s", attempt, _err(e))
                    if attempt == 3:
                        raise
                    time.sleep(4)
            rows = []
            for h in r.get("hits", {}).get("hits", []):
                s = h.get("_source", {})
                disp = (s.get("display_names") or [""])[0]
                m = re.match(r"(.*?)\s*\(([A-Z0-9.\-]+)[^)]*\)\s*\(CIK", disp)
                nm = (m.group(1) if m else disp.split("(")[0]).strip()
                tk = m.group(2) if m else ""
                if not nm:
                    continue
                form = (s.get("root_forms") or [s.get("file_type", "")])[0] or ""
                aid = h.get("_id", "")
                cik = str((s.get("ciks") or [s.get("cik", "")])[0] or "")
                acc = aid.split(":")[0].replace("-", "") if aid else ""
                doc = aid.split(":")[1] if ":" in aid else ""
                link = ("https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/" + acc + "/" + doc) if cik and acc else ""
                rows.append({"date": s.get("file_date", ""), "name": nm, "ticker": tk, "form": form, "url": link})
            rows.sort(key=lambda x: x.get("date", ""), reverse=True)   # newest first
            out, seen = [], set()                                      # one row per company (its most recent deal filing)
            for x in rows:
                k = x["name"].lower()
                if k in seen:
                    continue
                seen.add(k); out.append(x)
                if len(out) >= 40:
                    break
            MADEALS_CACHE = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("madeals failed: %s", _err(e))
            self._json(502, {"error": str(e)})

    def _inst(self):
        # An institution's 13F portfolio from SEC EDGAR — latest holdings, positions added in the last
        # 3 years (with the quarter they first appear), and the 1-year change in each holding.
        global INST_CACHE
        try:
            cik = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("cik", [""])[0].strip()
            if not cik:
                self._json(400, {"error": "missing cik"}); return
            now = time.time()
            hit = INST_CACHE.get(cik)
            if hit and now - hit[0] < 6 * 3600:
                self._json(200, hit[1]); return

            def sec(url):
                return urllib.request.urlopen(urllib.request.Request(url, headers=SEC_UA), timeout=20).read()

            sub = json.loads(sec("https://data.sec.gov/submissions/CIK" + cik.zfill(10) + ".json"))
            rec = sub.get("filings", {}).get("recent", {})
            forms = rec.get("form", []); accs = rec.get("accessionNumber", [])
            rdates = rec.get("reportDate", []); fdates = rec.get("filingDate", [])
            filings = []   # (accession, reportDate, filingDate) newest-first — go back far enough to find add dates
            for i, form in enumerate(forms):
                if form == "13F-HR":
                    q = rdates[i] if i < len(rdates) and rdates[i] else (fdates[i] if i < len(fdates) else "")
                    fd = fdates[i] if i < len(fdates) else ""
                    filings.append((accs[i], q, fd))
                    if len(filings) >= 24:     # ~6 years of quarterly filings (balance of history vs speed)
                        break

            def gt(b, tag):
                m = re.search(r"<(?:\w+:)?" + tag + r">(.*?)</(?:\w+:)?" + tag + ">", b, re.S)
                return m.group(1).strip() if m else None

            def parse_filing(acc, fdate):
                # -> ({issuer: {value, shares}}, bytes_downloaded)
                # SEC 13F "value" is in thousands for filings before 2023-01-03, in dollars on/after.
                mult = 1000 if (fdate and fdate < "2023-01-03") else 1
                accn = acc.replace("-", "")
                base = "https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/" + accn + "/"
                raw = sec(base + "index.json"); nbytes = len(raw)
                files = [f["name"] for f in json.loads(raw)["directory"]["item"]]
                cands = [f for f in files if f.lower().endswith(".xml")
                         and "primary" not in f.lower() and "xsl" not in f.lower()]
                rows = []
                for f in cands:
                    body = sec(base + f); nbytes += len(body)
                    rows = re.findall(r"<(?:\w+:)?infoTable>(.*?)</(?:\w+:)?infoTable>", body.decode("utf-8", "ignore"), re.S)
                    if rows:
                        break
                agg = {}   # keyed by CUSIP (stable across filings; issuer NAME varies)
                for b in rows:
                    name = gt(b, "nameOfIssuer")
                    if not name:
                        continue
                    name = name.replace("&amp;", "&").replace("&#38;", "&")
                    name = re.sub(r"\s+", " ", name).strip().title()
                    cusip = (gt(b, "cusip") or "").strip().upper() or name
                    try:
                        val = int(gt(b, "value") or 0)
                    except Exception:
                        val = 0
                    try:
                        sh = int(gt(b, "sshPrnamt") or 0)
                    except Exception:
                        sh = 0
                    a = agg.setdefault(cusip, {"issuer": name, "value": 0, "shares": 0})
                    a["value"] += val * mult; a["shares"] += sh
                return agg, nbytes

            per_quarter = []      # [(reportDate, agg)] newest-first
            budget = 45_000_000   # cap so huge index-fund filings don't blow up (small filers fit ~10y)
            spent = 0
            for acc, rdate, fdate in filings:
                if per_quarter and spent > budget:
                    break         # always keep the latest; stop pulling history once over budget
                try:
                    agg, nb = parse_filing(acc, fdate); spent += nb
                    per_quarter.append((rdate, agg))
                except Exception as e:
                    LOG.warning("inst %s: 13F %s skipped: %s", cik, acc, _err(e))

            data = {"name": sub.get("name"), "cik": cik, "reportDate": None,
                    "total": 0, "count": 0, "holdings": [], "additions": [],
                    "quarters": [q for _, q, _ in filings][:12]}   # show the last ~3 years of quarters
            if per_quarter:
                report, cur = per_quarter[0]
                data["reportDate"] = report
                oldest_q = per_quarter[-1][0]
                # group current positions by issuer for display (combine share classes), using the
                # per-CUSIP share timeline so name variations don't create fakes. For each issuer we
                # derive: added (first held), lastUpdate (last quarter the size changed), 1-year change.
                issuer_cusips = {}
                for cusip, v in cur.items():
                    issuer_cusips.setdefault(v["issuer"], []).append(cusip)
                holdings = []
                for issuer, cusips in issuer_cusips.items():
                    value = sum(cur[c]["value"] for c in cusips)
                    shares = sum(cur[c]["shares"] for c in cusips)
                    prim_cusip = max(cusips, key=lambda c: cur[c]["value"])   # largest class -> for CUSIP->ticker
                    series = [(rd, sum(agg[c]["shares"] for c in cusips if c in agg)) for rd, agg in per_quarter]  # newest-first
                    present = [q for q, s in series if s > 0]
                    firstq = present[-1] if present else None       # earliest quarter it appears in our history
                    since_before = bool(firstq and firstq == oldest_q)   # may predate our fetched window
                    last_upd = None
                    for i in range(len(series) - 1):
                        if series[i][1] != series[i + 1][1]:
                            last_upd = series[i][0]; break
                    prev_sh = series[4][1] if len(series) > 4 else 0
                    yr = round((shares - prev_sh) / prev_sh * 100, 1) if prev_sh > 0 else None
                    holdings.append({"issuer": issuer, "cusip": prim_cusip, "value": value, "shares": shares,
                                     "added": firstq, "sinceBefore": since_before, "lastUpdate": last_upd, "yrChg": yr})
                holdings.sort(key=lambda x: -x["value"])
                total = sum(h["value"] for h in holdings) or 1
                for h in holdings:
                    h["pct"] = round(h["value"] / total * 100, 2)
                data["total"] = sum(h["value"] for h in holdings)
                data["count"] = len(holdings)
                data["holdings"] = holdings[:50]
                # previous-filing portfolio value, for a QoQ portfolio-value change %
                if len(per_quarter) > 1:
                    data["prevTotal"] = sum(v["value"] for v in per_quarter[1][1].values())
                    data["prevQuarter"] = per_quarter[1][0]
                # "New positions" = first held within the last ~3 years (and not present before our window)
                recent_cut = per_quarter[min(len(per_quarter) - 1, 11)][0]
                adds = [{"issuer": h["issuer"], "cusip": h["cusip"], "added": h["added"], "value": h["value"],
                         "shares": h["shares"], "pct": h["pct"]}
                        for h in holdings if h.get("added") and not h["sinceBefore"] and h["added"] >= recent_cut]
                adds.sort(key=lambda x: x["added"], reverse=True)
                data["additions"] = adds[:25]
            INST_CACHE[cik] = (now, data)
            self._json(200, data)
        except Exception as e:
            LOG.error("inst %s failed: %s", cik, _err(e))
            self._json(502, {"error": str(e)})

    def _shareholding(self):
        # Indian shareholding pattern (Promoters / FIIs / DIIs / Government / Public) with quarterly
        # trend, scraped from Screener.in.
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = SHP_CACHE.get(sym)
            if hit and now - hit[0] < 6 * 3600:
                self._json(200, hit[1]); return
            req = urllib.request.Request("https://www.screener.in/company/" + urllib.parse.quote(sym) + "/",
                                         headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            data = {"quarters": [], "categories": [], "shareholders": None, "asOf": None}
            m = re.search(r'id="shareholding".*?(<table.*?</table>)', html, re.S)
            if m:
                tbl = m.group(1)
                thead = re.search(r"<thead>(.*?)</thead>", tbl, re.S)
                if thead:
                    cols = [re.sub(r"<[^>]*>", "", c, flags=re.S).strip()
                            for c in re.findall(r"<th[^>]*>(.*?)</th>", thead.group(1), re.S)]
                    data["quarters"] = [c for c in cols if c]
                body = re.search(r"<tbody>(.*?)</tbody>", tbl, re.S)
                for r in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1) if body else tbl, re.S):
                    cells = [re.sub(r"<[^>]*>", "", c, flags=re.S).replace("&nbsp;", " ").strip()
                             for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
                    if not cells or not cells[0]:
                        continue
                    name = re.sub(r"[\s+]+$", "", re.sub(r"\s+", " ", cells[0]).strip())   # drop trailing "+" toggle
                    vals = cells[1:]
                    if "shareholder" in name.lower():
                        data["shareholders"] = vals[-1] if vals else None
                        continue
                    nums = []
                    for v in vals:
                        vv = v.replace("%", "").replace(",", "").strip()
                        try:
                            nums.append(round(float(vv), 2))
                        except Exception:
                            nums.append(None)
                    data["categories"].append({
                        "name": name,
                        "latest": nums[-1] if nums else None,
                        "prev": nums[-2] if len(nums) >= 2 else None,
                        "series": nums,
                    })
                if data["quarters"]:
                    data["asOf"] = data["quarters"][-1]
            SHP_CACHE[sym] = (now, data)
            self._json(200, data)
        except Exception as e:
            LOG.error("shareholding %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _yquotes(self):
        # Batch quotes for the whole board in ONE Yahoo request -> the client refreshes fast without
        # dozens of round-trips. Returns {symbol: {price, prev, open, high, low, vol, ccy, name, hi52, lo52}}.
        try:
            syms = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbols", [""])[0].strip()
            if not syms:
                self._json(400, {"error": "missing symbols"}); return
            res = yahoo_batch_quotes(syms)
            out = {}
            for r in res:
                s = r.get("symbol")
                if not s:
                    continue
                out[s] = {
                    "price": r.get("regularMarketPrice"),
                    "prev": r.get("regularMarketPreviousClose") or r.get("chartPreviousClose"),
                    "pct": r.get("regularMarketChangePercent"),   # Yahoo's official day change % (authoritative)
                    "open": r.get("regularMarketOpen"),
                    "high": r.get("regularMarketDayHigh"),
                    "low": r.get("regularMarketDayLow"),
                    "vol": r.get("regularMarketVolume"),
                    "ccy": r.get("currency"),
                    "exchange": r.get("fullExchangeName") or r.get("exchange"),
                    "name": r.get("longName") or r.get("shortName"),
                    "hi52": r.get("fiftyTwoWeekHigh"),
                    "lo52": r.get("fiftyTwoWeekLow"),
                    "mcap": r.get("marketCap"),
                    # extended-hours (pre-market / after-hours) — present on US equities, best-effort elsewhere
                    "mstate": r.get("marketState"),
                    "preP": r.get("preMarketPrice"), "preC": r.get("preMarketChange"), "prePct": r.get("preMarketChangePercent"),
                    "postP": r.get("postMarketPrice"), "postC": r.get("postMarketChange"), "postPct": r.get("postMarketChangePercent"),
                }
            self._json(200, out)
        except Exception as e:
            LOG.error("yquotes failed: %s", _err(e))
            self._json(502, {"error": str(e)})

    def _yholders(self):
        # Institutional ownership: breakdown %, top institutions and top funds (US has full lists;
        # Indian names return the breakdown only). From Yahoo quoteSummary.
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0]
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = YHOLD_CACHE.get(sym)
            if hit and now - hit[0] < 6 * 3600:      # ownership updates quarterly
                self._json(200, hit[1]); return
            r = yahoo_qs(sym, "institutionOwnership,fundOwnership,majorHoldersBreakdown")

            def g(o, k):
                v = o.get(k)
                return v.get("raw") if isinstance(v, dict) else v

            def holders(mod):
                lst = (r.get(mod, {}) or {}).get("ownershipList") or []
                out = []
                for h in lst:
                    out.append({"org": g(h, "organization"), "pos": g(h, "position"),
                                "val": g(h, "value"), "pct": g(h, "pctHeld"),
                                "chg": g(h, "pctChange"), "date": g(h, "reportDate")})
                return out

            mh = r.get("majorHoldersBreakdown", {}) or {}
            data = {
                "insidersPct": g(mh, "insidersPercentHeld"),
                "institutionsPct": g(mh, "institutionsPercentHeld"),
                "institutionsFloatPct": g(mh, "institutionsFloatPercentHeld"),
                "institutionsCount": g(mh, "institutionsCount"),
                "institutions": holders("institutionOwnership"),
                "funds": holders("fundOwnership"),
            }
            YHOLD_CACHE[sym] = (now, data)
            self._json(200, data)
        except Exception as e:
            LOG.error("yholders %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    def _insttotals(self):
        # Portfolio total per 13F filer, for the institutions LIST. Deliberately NOT _inst: that walks up
        # to 24 quarterly filings and downloads every information table (megabytes for a BlackRock), which
        # for 20 filers is hundreds of heavy SEC requests and reliably earns a 503. The filer states the
        # same number on the cover page of its own filing (summaryPage/tableValueTotal, ~5 KB), so this
        # reads that: 3 small requests per filer, 4 at a time, matching _inst to the dollar.
        global INSTTOT_CACHE
        try:
            now = time.time()
            if INSTTOT_CACHE:
                # A complete answer is good for 6h (13Fs are quarterly). A partial one - SEC throttled
                # some filers with a 503 - is kept only briefly so the gaps get another try soon,
                # instead of showing a dash for the rest of the day.
                complete = all(r.get("total") for r in INSTTOT_CACHE[1])
                if now - INSTTOT_CACHE[0] < (6 * 3600 if complete else 600):
                    self._json(200, INSTTOT_CACHE[1]); return

            def sec(url):
                return urllib.request.urlopen(urllib.request.Request(url, headers=SEC_UA), timeout=20).read()

            def one(it):
                cik = str(it.get("cik", ""))
                row = {"cik": cik, "total": None, "prevTotal": None, "reportDate": None}
                try:
                    sub = json.loads(sec("https://data.sec.gov/submissions/CIK" + cik.zfill(10) + ".json"))
                    rec = sub.get("filings", {}).get("recent", {})
                    forms = rec.get("form", []); accs = rec.get("accessionNumber", [])
                    rds = rec.get("reportDate", []); fds = rec.get("filingDate", [])
                    picks = [(accs[i].replace("-", ""),
                              rds[i] if i < len(rds) else "",
                              fds[i] if i < len(fds) else "")
                             for i, f in enumerate(forms) if f == "13F-HR"][:2]
                    vals = []
                    for acc, rd, fd in picks:
                        body = sec("https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/" + acc
                                   + "/primary_doc.xml").decode("utf-8", "ignore")
                        m = re.search(r"<(?:\w+:)?tableValueTotal>(.*?)</(?:\w+:)?tableValueTotal>", body)
                        if not m:
                            vals.append(None); continue
                        # SEC 13F values are in THOUSANDS for filings before 2023-01-03, dollars on/after
                        vals.append(int(re.sub(r"[^0-9]", "", m.group(1)) or 0) * (1000 if (fd and fd < "2023-01-03") else 1))
                    if picks:
                        row["reportDate"] = picks[0][1]
                    # UNITS. A 13F is supposed to state dollars since 2023-01-03, but some filers still
                    # report thousands (Baupost's 2026 cover page says 5,415,853 for a ~$5.4bn book, and
                    # its information table agrees - so it is the unit, not a parse error). Where the
                    # stated total is small enough for that to matter, corroborate it against the table:
                    # value/shares has to look like a share price. Such a filer's table is small, so this
                    # only costs two extra requests exactly where it is needed.
                    if vals and vals[0] and vals[0] < 1e9:
                        try:
                            acc0 = picks[0][0]
                            b0 = "https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/" + acc0 + "/"
                            names = [f["name"] for f in json.loads(sec(b0 + "index.json"))["directory"]["item"]]
                            tbl = next((n for n in names if n.endswith(".xml") and "primary_doc" not in n), None)
                            if tbl:
                                body = sec(b0 + tbl).decode("utf-8", "ignore")
                                vs = [int(re.sub(r"[^0-9]", "", x) or 0)
                                      for x in re.findall(r"<(?:\w+:)?value>(.*?)</(?:\w+:)?value>", body)]
                                sh = [int(re.sub(r"[^0-9]", "", x) or 0)
                                      for x in re.findall(r"<(?:\w+:)?sshPrnamt>(.*?)</(?:\w+:)?sshPrnamt>", body)]
                                px = sorted(v / s for v, s in zip(vs, sh) if v > 0 and s > 0)
                                if px and px[len(px) // 2] < 1.0:      # median "price" under $1 -> thousands
                                    vals = [(v * 1000 if v else v) for v in vals]
                                    row["unit"] = "thousands-as-filed"
                        except Exception as e:
                            LOG.warning("insttotals %s unit check failed: %s", cik, _err(e))
                    if vals:
                        row["total"] = vals[0]
                    if len(vals) > 1:
                        row["prevTotal"] = vals[1]
                except Exception as e:
                    LOG.warning("insttotals %s failed: %s", cik, _err(e))
                return row

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as ex:      # SEC fair access: keep concurrency low
                out = list(ex.map(one, INSTITUTIONS))
            INSTTOT_CACHE = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("insttotals %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _congress(self):
        # Congressional STOCK Act transactions (Senate eFD + House Clerk), newest filing first.
        # Default: the newest 40, for the card. ?all=1: everything filed in the last 12 months.
        global CONGRESS_CACHE
        try:
            full = "all=1" in (urllib.parse.urlparse(self.path).query or "")
            now = time.time()
            if full:
                self._json(200, _congress_all_cached()); return
            hit = CONGRESS_CACHE
            if not (hit and now - hit[0] < hit[2]):     # dumps publish daily
                out = _with_fresh_house([_cg_row(r) for r in _mt_snapshot("/congress/trades/snapshot.json.gz")])
                out.sort(key=lambda x: ((x.get("filed") or ""), (x.get("traded") or "")), reverse=True)
                CONGRESS_CACHE = hit = (now, out[:40], 1800)
            # ALL TRADES reads ~6 archive shards from GitHub releases: fetch them now, off the request
            full_hit = CONGRESS_ALL_CACHE
            if not (full_hit and now - full_hit[0] < full_hit[2]):
                _warm(_CG_ALL_LOCK, _congress_all_cached, "congress")
            self._json(200, _house_party(hit[1]))   # the 12-month list knows more members' parties than the snapshot
        except Exception as e:
            LOG.error("congress %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _indeals(self):
        # India: NSE bulk and block deals - the named institution / client behind each large trade.
        # Default: the newest 40 (last 30 days), for the card. ?all=1: the last 12 months.
        try:
            full = "all=1" in (urllib.parse.urlparse(self.path).query or "")
            key = "all" if full else "card"
            hit = INDEALS_CACHE.get(key)
            if hit and time.time() - hit[0] < 1800:          # NSE publishes these once a day, after the close
                self._json(200, hit[1]); return
            today = datetime.date.today()
            frm = (today - datetime.timedelta(days=365 if full else 30)).strftime("%d-%m-%Y")
            to = today.strftime("%d-%m-%Y")
            # A block deal that is also more than 0.5% of the company appears in BOTH of NSE's lists (385 trades,
            # ~12% of the value, over a year): keep it once, as the block deal, flagged alsoBulk.
            rows = _nse_deals("block", frm, to)
            seen = {(r["date"], r["symbol"], r["client"], r["side"], r["qty"]): r for r in rows}
            for r in _nse_deals("bulk", frm, to):
                twin = seen.get((r["date"], r["symbol"], r["client"], r["side"], r["qty"]))
                if twin is not None:
                    twin["alsoBulk"] = True
                else:
                    rows.append(r)
            rows.sort(key=lambda r: (r["date"], r["value"] or 0), reverse=True)
            data = ({"rows": rows, "complete": True, "from": rows[-1]["date"] if rows else None,
                     "to": rows[0]["date"] if rows else None} if full else rows[:40])
            INDEALS_CACHE[key] = (time.time(), data)
            self._json(200, data)
        except Exception as e:
            LOG.error("indeals %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _exec(self):
        # Executive-branch trades (Open Cabinet, from OGE Form 278-T / 278e). Default: the newest 40, for the
        # card. ?all=1: every trade dated in the last 12 months.
        try:
            rows = _exec_all()
            if "all=1" in (urllib.parse.urlparse(self.path).query or ""):
                self._json(200, {"rows": rows, "complete": False, "from": _one_year_ago(),
                                 "latest": rows[0]["traded"] if rows else None})
            else:
                self._json(200, rows[:40])
        except Exception as e:
            LOG.error("exec %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _govcon(self):
        # Federal contract awards to listed companies (USAspending, via the Market Trackers dumps).
        # Default: the 40 most recent, for the card. ?all=1: every award signed in the last 12 months.
        try:
            full = "all=1" in (urllib.parse.urlparse(self.path).query or "")
            self._json(200, _govcon_all() if full else _govcon_card())
        except Exception as e:
            LOG.error("govcon %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _spchanges(self):
        # Recent S&P 500 constituent ADDITIONS (date, ticker, name) from Wikipedia's constituents table.
        global SP_CACHE
        try:
            now = time.time()
            if SP_CACHE and now - SP_CACHE[0] < 12 * 3600:
                self._json(200, SP_CACHE[1]); return
            # Wikipedia deleted the "Selected changes to the list" section (the old source: a table with
            # Added/Removed/Reason columns). The constituents table is still there and still carries a
            # "Date added" column, so recent ADDITIONS come from it - newest first. Removals and the
            # reason text are no longer published on this page, so they are not reported.
            req = urllib.request.Request(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies?action=raw",
                headers={"User-Agent": "Mozilla/5.0"})
            wt = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            out = []
            i = wt.find('id="constituents"')
            if i >= 0:
                tbl = wt[i:]
                end = tbl.find("\n|}")
                if end > 0:
                    tbl = tbl[:end]
                def _cell(x):
                    x = re.sub(r"\{\{[A-Za-z]*Symbol\|([^}|]+)[^}]*\}\}", r"\1", x)   # {{NyseSymbol|MMM}} -> MMM
                    x = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", x)           # [[A. O. Smith]] -> A. O. Smith
                    x = re.sub(r"<[^>]*>", "", x).replace("'''", "")
                    return x.strip().lstrip("|").strip()
                rows = []
                for blk in tbl.split("\n|-")[1:]:
                    cells = [_cell(c) for c in re.split(r"\n\|\|", blk) if c.strip()]
                    if len(cells) >= 6 and re.match(r"\d{4}-\d{2}-\d{2}$", cells[5]) and cells[0]:
                        rows.append({"date": cells[5], "ticker": cells[0], "name": cells[1]})
                rows.sort(key=lambda r: r["date"], reverse=True)
                out = rows[:8]
            SP_CACHE = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("spchanges %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _holidays(self):
        # Exchange holidays for the open/closed logic: NSE equities (CM segment) from NSE's holiday-master JSON,
        # NYSE from the rule-based calendar above, for this year and next. 24h cache.
        global HOL_CACHE
        try:
            now = time.time()
            if HOL_CACHE and now - HOL_CACHE[0] < 24 * 3600:
                self._json(200, HOL_CACHE[1]); return
            yr = datetime.date.today().year
            out = {"us": _us_holidays(yr) + _us_holidays(yr + 1), "in": [], "inSource": "unavailable"}
            try:
                j = nse_get("holiday-master?type=trading")
                days = []
                for h in (j.get("CM") or []):
                    try:
                        days.append(datetime.datetime.strptime(h["tradingDate"], "%d-%b-%Y").date().isoformat())
                    except Exception:
                        pass
                if days:
                    out["in"] = sorted(set(days)); out["inSource"] = "NSE holiday-master"
            except Exception as e:
                LOG.warning("holidays: NSE calendar failed: %s", _err(e))
            HOL_CACHE = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("holidays failed: %s", _err(e))
            self._json(502, {"error": str(e)})

    def _index_members(self):
        # Constituents for SPX / DJI (State Street daily holdings, with weights), NDX (Nasdaq's index list) and
        # NIFTY (NSE's official CSV). DJI / NDX fall back to Wikipedia's list pages if the official source fails.
        # Response: {"asof","src","rows":[{"s","n","w"?}]}; 24h cache per index.
        try:
            idx = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("idx", [""])[0].upper()
            if idx not in ("SPX", "DJI", "NDX", "NIFTY", "SENSEX"):
                self._json(400, {"error": "unknown index"}); return
            now = time.time()
            hit = IDXM_CACHE.get(idx)
            if hit and now - hit[0] < 24 * 3600:
                self._json(200, hit[1]); return
            data, err = None, None
            try:
                if idx in IDXM_SSGA: data = _idxm_ssga(IDXM_SSGA[idx])
                elif idx == "NDX": data = _idxm_nasdaq()
                elif idx == "NIFTY": data = _idxm_nse()
                else: data = _idxm_wiki(IDXM_PAGES[idx])          # SENSEX: Wikipedia is the primary source
            except Exception as e:
                err = e
                LOG.warning("index-members %s official source failed (%s); trying fallback", idx, _err(e))
                if idx in IDXM_PAGES:
                    data = _idxm_wiki(IDXM_PAGES[idx])
            if not data:
                raise err or RuntimeError("no data")
            IDXM_CACHE[idx] = (now, data)
            self._json(200, data)
        except Exception as e:
            LOG.error("index-members %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _yfund(self):
        # Full fundamentals (PE/PB/PS/ROE/ROA/margins/D-E/current ratio/EPS...) for ANY symbol —
        # US or Indian (.NS) — from Yahoo quoteSummary. Needs a crumb+cookie, handled here server-side.
        global Y_OPENER, Y_CRUMB
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0]
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = YFUND_CACHE.get(sym)
            if hit and now - hit[0] < 3600:        # fundamentals change slowly; 1h cache
                self._json(200, hit[1]); return

            def session():
                global Y_OPENER, Y_CRUMB
                if Y_OPENER is not None and Y_CRUMB:
                    return
                with Y_LOCK:                           # only one thread rebuilds the crumb
                    if Y_OPENER is not None and Y_CRUMB:
                        return
                    cj = http.cookiejar.CookieJar()
                    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
                    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
                    try:
                        op.open("https://fc.yahoo.com", timeout=15)
                    except Exception as e:
                        LOG.warning("yahoo cookie warm-up failed: %s", _err(e))
                    crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
                    Y_CRUMB = crumb; Y_OPENER = op

            def fetch():
                session()
                url = ("https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
                       + urllib.parse.quote(sym)
                       + "?modules=defaultKeyStatistics,financialData,summaryDetail,summaryProfile,earningsHistory&crumb="
                       + urllib.parse.quote(Y_CRUMB))
                return json.loads(Y_OPENER.open(url, timeout=15).read())

            try:
                d = fetch()
            except Exception as e:
                LOG.warning("yfund: first attempt failed, refreshing crumb: %s", _err(e))
                Y_OPENER = None; Y_CRUMB = None   # crumb may have expired -> refresh once and retry
                d = fetch()

            r = (d.get("quoteSummary", {}).get("result") or [{}])[0]
            fd = r.get("financialData", {}) or {}
            ks = r.get("defaultKeyStatistics", {}) or {}
            sd = r.get("summaryDetail", {}) or {}
            sp = r.get("summaryProfile", {}) or {}

            def g(o, k):
                v = o.get(k)
                return v.get("raw") if isinstance(v, dict) else v

            out = {
                "pe": g(sd, "trailingPE"), "pb": g(ks, "priceToBook"),
                "ps": g(sd, "priceToSalesTrailing12Months"),
                "eps": g(ks, "trailingEps"), "rps": g(fd, "revenuePerShare"),
                "roe": g(fd, "returnOnEquity"), "roa": g(fd, "returnOnAssets"),
                "pm": g(fd, "profitMargins"), "gm": g(fd, "grossMargins"),
                "om": g(fd, "operatingMargins"),
                "de": g(fd, "debtToEquity"), "cr": g(fd, "currentRatio"),
                "qr": g(fd, "quickRatio"), "beta": g(ks, "beta"),
                "bookValue": g(ks, "bookValue"), "divYield": g(sd, "dividendYield"),
                "mcap": g(sd, "marketCap"),
                "sector": sp.get("sector"), "industry": sp.get("industry"),   # replaces the hard-coded seed labels
                # extra fields used by the BRIEF (overall summary) synthesis — all from the same modules
                "forwardPe": g(ks, "forwardPE"), "peg": g(ks, "pegRatio"),
                "evEbitda": g(ks, "enterpriseToEbitda"),
                "revGrowth": g(fd, "revenueGrowth"), "earnGrowth": g(fd, "earningsGrowth"),
                "fcf": g(fd, "freeCashflow"), "fcfCurrency": fd.get("financialCurrency"),
                "ebitda": g(fd, "ebitda"),
                "totalDebt": g(fd, "totalDebt"), "totalCash": g(fd, "totalCash"),
                "targetMean": g(fd, "targetMeanPrice"), "targetHigh": g(fd, "targetHighPrice"),
                "targetLow": g(fd, "targetLowPrice"), "recKey": g(fd, "recommendationKey"),
                "recMean": g(fd, "recommendationMean"), "numAnalysts": g(fd, "numberOfAnalystOpinions"),
                "curPrice": g(fd, "currentPrice"),
                "heldInst": g(ks, "heldPercentInstitutions"), "heldInsiders": g(ks, "heldPercentInsiders"),
                "shortPct": g(ks, "shortPercentOfFloat"), "chg52w": g(ks, "52WeekChange"),
            }
            # quarterly EPS estimate vs actual (works for US and Indian .NS names)
            eh = r.get("earningsHistory", {}) or {}
            earnings = []
            for q in (eh.get("history") or []):
                qq = q.get("quarter", {})
                earnings.append({
                    "date": qq.get("fmt") if isinstance(qq, dict) else None,
                    "estimate": g(q, "epsEstimate"),
                    "actual": g(q, "epsActual"),
                    "surprisePercent": g(q, "surprisePercent"),
                })
            out["earnings"] = earnings
            YFUND_CACHE[sym] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("yfund %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    def _analyst(self):
        # Analyst recommendations + price targets (ANR) from Yahoo quoteSummary. Works for US and
        # Indian (.NS) names, though coverage is thinner for non-US listings. Keyless (crumb+cookie).
        global ANALYST_CACHE
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0]
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = ANALYST_CACHE.get(sym)
            if hit and now - hit[0] < 6 * 3600:   # ratings move slowly; 6h cache
                self._json(200, hit[1]); return

            r = yahoo_qs(sym, "financialData,recommendationTrend,price")
            fd = r.get("financialData", {}) or {}
            pr = r.get("price", {}) or {}
            rt = r.get("recommendationTrend", {}) or {}

            def g(o, k):
                v = o.get(k)
                return v.get("raw") if isinstance(v, dict) else v

            # recommendationTrend.trend is a list of periods (0m=current, -1m, -2m, -3m); take the
            # most recent that actually has vote counts.
            trend = rt.get("trend") or []
            dist = None
            for p in trend:
                sb, b, h, s, ss = (g(p, "strongBuy"), g(p, "buy"), g(p, "hold"),
                                   g(p, "sell"), g(p, "strongSell"))
                if any(x for x in (sb, b, h, s, ss)):
                    dist = {"strongBuy": sb or 0, "buy": b or 0, "hold": h or 0,
                            "sell": s or 0, "strongSell": ss or 0, "period": p.get("period")}
                    break

            out = {
                "symbol": sym,
                "name": g(pr, "longName") or g(pr, "shortName") or sym,
                "currency": pr.get("currency") or fd.get("financialCurrency"),
                "price": g(fd, "currentPrice") or g(pr, "regularMarketPrice"),
                "targetMean": g(fd, "targetMeanPrice"),
                "targetMedian": g(fd, "targetMedianPrice"),
                "targetHigh": g(fd, "targetHighPrice"),
                "targetLow": g(fd, "targetLowPrice"),
                "numAnalysts": g(fd, "numberOfAnalystOpinions"),
                "recMean": g(fd, "recommendationMean"),   # 1=Strong Buy ... 5=Strong Sell
                "recKey": fd.get("recommendationKey"),     # e.g. "buy", "hold"
                "dist": dist,
            }
            ANALYST_CACHE[sym] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("analyst %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    def _earnings(self):
        # Quarterly EPS estimate vs actual (many quarters) from StockAnalysis.com — keyless, US only.
        global EARN_CACHE
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = EARN_CACHE.get(sym)
            if hit and now - hit[0] < 6 * 3600:   # earnings change quarterly; 6h cache is plenty
                self._json(200, hit[1]); return
            url = "https://stockanalysis.com/api/symbol/s/" + urllib.parse.quote(sym) + "/earnings"
            req = urllib.request.Request(url, headers={   # full browser headers — the bare UA now gets a 403
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://stockanalysis.com/stocks/" + urllib.parse.quote(sym) + "/"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
            rows = body.get("data") if isinstance(body, dict) else []
            rows = rows or []
            EARN_CACHE[sym] = (now, rows)
            self._json(200, rows)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._json(200, [])   # not covered (e.g. Indian) -> empty, panel shows N/A
            else:
                self._json(502, {"error": str(e)})
        except Exception as e:
            LOG.error("earnings failed: %s", _err(e))
            self._json(502, {"error": str(e)})

    def _current_ratio(self):
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0]
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            url = ("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
                   + urllib.parse.quote(sym) + "?type=annualCurrentAssets,annualCurrentLiabilities&period1="
                   + str(int(time.time()) - 5 * 366 * 86400) + "&period2=" + str(int(time.time())))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            series = {}
            for result in data.get("timeseries", {}).get("result") or []:
                for key in ("annualCurrentAssets", "annualCurrentLiabilities"):
                    for item in result.get(key, []):
                        value = item.get("reportedValue", {}).get("raw")
                        if isinstance(value, (int, float)) and math.isfinite(value):
                            series.setdefault(key, {})[(item.get("asOfDate"), item.get("currencyCode"))] = value
            assets = series.get("annualCurrentAssets", {})
            liabilities = series.get("annualCurrentLiabilities", {})
            dates = sorted(k for k in assets if k[0] and k[1] and k in liabilities and liabilities[k] > 0)
            out = {"value": None, "source": "Yahoo Finance annual statements"}
            if dates:
                key = dates[-1]
                out.update(value=assets[key] / liabilities[key], asOf=key[0], currency=key[1])
            self._json(200, out)
        except Exception as e:
            LOG.warning("current ratio %s", _err(e))
            self._json(502, {"error": "Current ratio unavailable"})

    def _screener(self):
        # Indian fundamentals from Screener.in company page (top ratios).
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            url = "https://www.screener.in/company/" + urllib.parse.quote(sym) + "/"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read().decode("utf-8", "ignore")
            m = re.search(r'<ul[^>]*id="top-ratios"[^>]*>(.*?)</ul>', html, re.S)
            out = {}
            if m:
                for it in re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), re.S):
                    nm = re.search(r'class="name">(.*?)</span>', it, re.S)
                    nums = re.findall(r'class="number">(.*?)</span>', it, re.S)
                    if nm:
                        name = re.sub("<.*?>", "", nm.group(1)).strip()
                        vals = [re.sub("<.*?>", "", x).strip() for x in nums]
                        out[name] = " / ".join(vals)
            self._json(200, out)
        except Exception as e:
            LOG.error("screener failed: %s", _err(e))
            self._json(502, {"error": str(e)})

    def _screen(self):
        # Custom equity screener (EQS) — find stocks meeting numeric conditions via Yahoo's screener, then
        # re-verify every condition against the shown quote values so results always match. Keyless.
        # ?region=us|in&f=field:op:value~...&sort=field:dir&size=40   (op: gt|lt)
        global Y_OPENER, Y_CRUMB
        # field -> (yahoo screener id, quote field shown, scale to convert quote value to the user's units)
        FIELDS = {
            "mcap":   ("intradaymarketcap", "marketCap", 1),
            "price":  ("intradayprice", "regularMarketPrice", 1),
            "pe":     ("trailingpe", "trailingPE", 1),
            "fwdpe":  ("forward_pe", "forwardPE", 1),
            "pb":     ("pricebookratio", "priceToBook", 1),
            "divy":   ("dividendyield", "trailingAnnualDividendYield", 100),   # quote is a fraction -> *100 = %
            "chg":    ("percentchange", "regularMarketChangePercent", 1),
            "vol":    ("dayvolume", "regularMarketVolume", 1),
            "beta":   ("beta", "beta", 1),
        }
        # fundamentals — filtered by Yahoo but not in the quote, so applied server-side without re-verify.
        # All in PERCENT (verified), so the user's % value passes straight through.
        FILTER_ONLY = {
            "roe":         "returnonequity.lasttwelvemonths",
            "netmargin":   "netincomemargin.lasttwelvemonths",
            "grossmargin": "grossprofitmargin.lasttwelvemonths",
            "revgrowth":   "quarterlyrevenuegrowth.quarterly",
            "epsgrowth":   "epsgrowth.lasttwelvemonths",
        }
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            region = qs.get("region", ["us"])[0]
            fstr = qs.get("f", [""])[0]
            sort = qs.get("sort", ["mcap:desc"])[0]
            try: size = max(5, min(250 if qs.get("fundamentals", [""])[0] == "1" else 50, int(qs.get("size", ["40"])[0])))
            except Exception: size = 40
            ops = [{"operator": "EQ", "operands": ["region", region]},
                   {"operator": "GT", "operands": ["intradayprice", 1]},       # drop penny / OTC junk
                   {"operator": "GT", "operands": ["dayvolume", 100000]}]      # require real liquidity
            sector = qs.get("sector", [""])[0]
            if sector:
                ops.append({"operator": "EQ", "operands": ["sector", sector]})
            conds = []   # (quoteField, scale, op, value) for post-verification against shown values
            tech = []    # (key, op, value): computed from each candidate's daily history (_technicals)
            for cond in fstr.split("~"):
                if not cond:
                    continue
                p = cond.split(":")
                if len(p) == 3 and p[0] in ("macd", "ema", "sig") and p[1] == "is":
                    tech.append((p[0], "is", p[2])); continue
                if len(p) == 3 and p[0] in ("rsi", "hi52", "lo52") and p[1] in ("gt", "lt"):
                    try: tech.append((p[0], p[1], float(p[2])))
                    except Exception: pass
                    continue
                if len(p) != 3 or (p[0] not in FIELDS and p[0] not in FILTER_ONLY):
                    continue
                try: v = float(p[2])
                except Exception: continue
                op = p[1].lower()
                if op not in ("gt", "lt"):
                    continue
                yop = "GT" if op == "gt" else "LT"
                if p[0] in FIELDS:
                    yid, qfield, scale = FIELDS[p[0]]
                    ops.append({"operator": yop, "operands": [yid, v]})
                    conds.append((qfield, scale, op, v))   # re-verified against the shown value
                else:
                    ops.append({"operator": yop, "operands": [FILTER_ONLY[p[0]], v]})   # Yahoo-filtered only
            sk = sort.split(":")
            sfk = sk[0] if sk and sk[0] in FIELDS else "mcap"
            sd = (sk[1].upper() if len(sk) > 1 else "DESC")
            if sd not in ("ASC", "DESC"):
                sd = "DESC"
            yid = FIELDS[sfk][0]; qf = FIELDS[sfk][1]
            def run():
                if Y_OPENER is None or not Y_CRUMB:
                    yahoo_qs("AAPL", "price")
                # technical filters discard most candidates, so start from Yahoo's largest page
                body = {"size": 250 if tech or qs.get("fundamentals", [""])[0] == "1" else 100, "offset": 0, "sortField": yid, "sortType": sd,
                        "quoteType": "EQUITY", "userId": "", "userIdType": "guid",
                        "query": {"operator": "AND", "operands": ops}}
                url = "https://query1.finance.yahoo.com/v1/finance/screener?crumb=" + urllib.parse.quote(Y_CRUMB)
                req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
                return json.loads(Y_OPENER.open(req, timeout=25).read())
            try:
                d = run()
            except Exception as e:
                LOG.warning("screen: first attempt failed, refreshing crumb: %s", _err(e))
                Y_OPENER = None; Y_CRUMB = None
                d = run()
            res = (d.get("finance", {}).get("result") or [{}])[0].get("quotes") or []
            out = []
            for r in res:
                s = r.get("symbol")
                if not s or (region == "in" and not s.endswith(".NS")):
                    continue
                dy = r.get("trailingAnnualDividendYield")   # Yahoo's yield is broken for many ADRs (currency
                if dy is not None and dy * 100 > 30:        # mismatch) -> implausible >30% means bad data
                    r["trailingAnnualDividendYield"] = None
                # re-verify every condition against the actual quote value (with unit scaling)
                ok = True
                for qfield, scale, op, v in conds:
                    qv = r.get(qfield)
                    if qv is None:
                        ok = False; break
                    qv = qv * scale
                    if (op == "gt" and not qv > v) or (op == "lt" and not qv < v):
                        ok = False; break
                if not ok:
                    continue
                out.append({"symbol": s, "name": r.get("shortName") or r.get("longName") or s,
                            "price": r.get("regularMarketPrice"), "pct": r.get("regularMarketChangePercent"),
                            "mcap": r.get("marketCap"), "pe": r.get("trailingPE"),
                            "pb": r.get("priceToBook"), "divY": r.get("trailingAnnualDividendYield"),
                            "_s": r.get(qf)})
            have = [x for x in out if x["_s"] is not None]
            none_ = [x for x in out if x["_s"] is None]
            have.sort(key=lambda x: x["_s"], reverse=(sd == "DESC"))
            out = have + none_
            if tech:
                # walk the candidates in display order, a batch at a time, computing technicals until
                # `size` stocks pass - so a common condition costs a few chart fetches, not 250
                from concurrent.futures import ThreadPoolExecutor
                kept = []
                with ThreadPoolExecutor(max_workers=8) as ex:
                    for i in range(0, len(out), 16):
                        batch = out[i:i + 16]
                        for x, t in zip(batch, ex.map(lambda x: _technicals(x["symbol"]), batch)):
                            if _tech_ok(t, tech):
                                # shown with the PRED tab's own formulas, so the columns match that tab
                                x.update({"rsi": t.get("p_rsi"), "macdH": t.get("p_hist"),
                                          "ret3m": t.get("p_r3m"), "pos52": t.get("p_pos"),
                                          "hi52pct": t.get("hi52pct"), "lo52pct": t.get("lo52pct")})
                                kept.append(x)
                        if len(kept) >= size:
                            break
                out = kept
            out = out[:size]
            for x in out:
                x.pop("_s", None)
            self._json(200, out)
        except Exception as e:
            LOG.error("screen failed: %s", _err(e))
            self._json(502, {"error": str(e)})

    def _movers(self):
        # Market-wide top gainers / losers / most-active (Yahoo screener), optional market-cap band. Keyless.
        global MOVERS_CACHE
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            mode = qs.get("mode", ["gainers"])[0]
            cap = qs.get("cap", [""])[0]
            region = qs.get("region", ["us"])[0]
            now = time.time()
            key = (mode, cap, region)
            hit = MOVERS_CACHE.get(key)
            if hit and now - hit[0] < 2.5:     # short cache: near real-time, but coalesces the 3 mode calls per poll
                self._json(200, hit[1]); return
            # pull a wide pool (Yahoo's own sort lags the live session), then rank by the real quote fields
            res = yahoo_movers(mode, cap, region, 100)
            out = []
            for r in res:
                s = r.get("symbol")
                if not s:
                    continue
                if region == "in" and not s.endswith(".NS"):   # NSE only — drop BSE (.BO) duplicates
                    continue
                out.append({"symbol": s,
                            "name": r.get("shortName") or r.get("longName") or r.get("symbol"),
                            "price": r.get("regularMarketPrice"),
                            "pct": r.get("regularMarketChangePercent"),
                            "vol": r.get("regularMarketVolume"),
                            "mcap": r.get("marketCap")})
            if mode == "active":
                out = [x for x in out if x["vol"] is not None]; out.sort(key=lambda x: -x["vol"])
            elif mode == "losers":
                out = [x for x in out if x["pct"] is not None and x["pct"] < 0]; out.sort(key=lambda x: x["pct"])
            else:
                out = [x for x in out if x["pct"] is not None and x["pct"] > 0]; out.sort(key=lambda x: -x["pct"])
            out = out[:12]
            MOVERS_CACHE[key] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("movers %s failed: %s", key, _err(e))
            self._json(502, {"error": str(e)})

    def _premovers(self):
        # Extended-hours (pre-market / after-hours) movers: rank a wide liquid pool by the current
        # extended-session % change. Real data from Yahoo (screener quotes carry marketState +
        # preMarket*/postMarket*). Returns {session:'pre'|'post'|None, gainers:[], losers:[]}.
        global PREMOVERS_CACHE
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            region = qs.get("region", ["us"])[0]
            now = time.time()
            hit = PREMOVERS_CACHE.get(region)
            if hit and now - hit[0] < 20:      # extended hours move slower; a short cache is plenty
                self._json(200, hit[1]); return
            pool = yahoo_movers("active", "", region, 100)   # liquid universe most likely to trade after hours
            rows = []
            for r in pool:
                s = r.get("symbol")
                if not s:
                    continue
                if region == "in" and not s.endswith(".NS"):
                    continue
                ms = (r.get("marketState") or "").upper()
                if ms.startswith("PRE") and r.get("preMarketPrice") is not None and r.get("preMarketChangePercent") is not None:
                    rows.append({"symbol": s, "name": r.get("shortName") or r.get("longName") or s,
                                 "price": r.get("preMarketPrice"), "pct": r.get("preMarketChangePercent"),
                                 "vol": r.get("regularMarketVolume"), "prev": r.get("regularMarketPrice"), "session": "pre"})
                elif ms.startswith("POST") and r.get("postMarketPrice") is not None and r.get("postMarketChangePercent") is not None:
                    rows.append({"symbol": s, "name": r.get("shortName") or r.get("longName") or s,
                                 "price": r.get("postMarketPrice"), "pct": r.get("postMarketChangePercent"),
                                 "vol": r.get("regularMarketVolume"), "prev": r.get("regularMarketPrice"), "session": "post"})
            session = None
            if rows:
                pre = sum(1 for x in rows if x["session"] == "pre")
                session = "pre" if pre >= (len(rows) - pre) else "post"
                rows = [x for x in rows if x["session"] == session]
            gainers = sorted([x for x in rows if x["pct"] is not None and x["pct"] > 0], key=lambda x: -x["pct"])[:12]
            losers = sorted([x for x in rows if x["pct"] is not None and x["pct"] < 0], key=lambda x: x["pct"])[:12]
            active = sorted([x for x in rows if x.get("vol") is not None], key=lambda x: -x["vol"])[:12]
            data = {"session": session, "gainers": gainers, "losers": losers, "active": active}
            PREMOVERS_CACHE[region] = (now, data)
            self._json(200, data)
        except Exception as e:
            LOG.error("premovers %s failed: %s", region, _err(e))
            self._json(502, {"error": str(e)})

    def _options(self):
        # Option chain (strikes, bid/ask, IV, OI, volume) for a symbol from Yahoo v7/finance/options.
        # ?symbol=AAPL for the nearest expiry (+ the full expiration list); add &date=<epoch secs> for a
        # specific expiry. US-listed underlyings only (Yahoo has no NSE option chains). Keyless.
        global OPT_CACHE
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sym = (qs.get("symbol", [""])[0]).upper().strip()
            date = (qs.get("date", [""])[0]).strip()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            key = (sym, date)
            hit = OPT_CACHE.get(key)
            if hit and now - hit[0] < 45:      # options move intraday; a short cache smooths polling
                self._json(200, hit[1]); return

            if sym.endswith('.NS'):
                symbol = sym[:-3]
                info = nse_get('option-chain-contract-info?symbol=' + urllib.parse.quote(symbol))
                def epoch(value):
                    return int(datetime.datetime.strptime(value, '%d-%b-%Y').replace(hour=10, tzinfo=datetime.timezone.utc).timestamp())
                expiry_map = {epoch(v): v for v in info.get('expiryDates', [])}
                selected = int(date) if date else (min(expiry_map) if expiry_map else None)
                out = {'symbol': sym, 'source': 'NSE', 'currency': 'INR', 'expirations': sorted(expiry_map),
                       'expiry': selected, 'underlyingPrice': None, 'change': None, 'changePct': None, 'calls': [], 'puts': []}
                if selected not in expiry_map:
                    out['note'] = 'no-options'
                else:
                    data = nse_get('option-chain-v3?' + urllib.parse.urlencode({'type': 'Equity', 'symbol': symbol, 'expiry': expiry_map[selected]}))
                    records = data.get('records', {})
                    out['asOf'] = records.get('timestamp')
                    for row in records.get('data', []):
                        for side, target in (('CE', 'calls'), ('PE', 'puts')):
                            leg = row.get(side)
                            if not leg:
                                continue
                            spot = leg.get('underlyingValue')
                            if isinstance(spot, (int, float)) and spot > 0:
                                out['underlyingPrice'] = spot
                            strike = leg.get('strikePrice', row.get('strikePrice'))
                            iv = leg.get('impliedVolatility')
                            out[target].append({'strike': strike, 'last': leg.get('lastPrice'), 'bid': leg.get('buyPrice1'),
                                                'ask': leg.get('sellPrice1'), 'change': leg.get('change'), 'pct': leg.get('pChange'),
                                                'vol': leg.get('totalTradedVolume'), 'oi': leg.get('openInterest'),
                                                'iv': iv / 100 if isinstance(iv, (int, float)) and iv > 0 else None,
                                                'itm': strike < spot if side == 'CE' and spot and strike else strike > spot if spot and strike else None})
                OPT_CACHE[key] = (now, out)
                self._json(200, out); return

            res = yahoo_options(sym, date or None)
            if not res or not res.get("options"):
                out = {"symbol": sym, "note": "no-options", "expirations": [],
                       "underlyingPrice": None, "calls": [], "puts": []}
                OPT_CACHE[key] = (now, out); self._json(200, out); return

            q = res.get("quote", {}) or {}
            opt = (res.get("options") or [{}])[0]

            def leg(rows):
                out = []
                for r in (rows or []):
                    out.append({
                        "strike": r.get("strike"),
                        "last": r.get("lastPrice"),
                        "bid": r.get("bid"),
                        "ask": r.get("ask"),
                        "change": r.get("change"),
                        "pct": r.get("percentChange"),
                        "vol": r.get("volume"),
                        "oi": r.get("openInterest"),
                        "iv": r.get("impliedVolatility"),
                        "itm": r.get("inTheMoney"),
                    })
                return out

            out = {
                "symbol": res.get("underlyingSymbol") or sym,
                "underlyingPrice": q.get("regularMarketPrice"),
                "change": q.get("regularMarketChange"),
                "changePct": q.get("regularMarketChangePercent"),
                "currency": q.get("currency") or "USD",
                "expirations": res.get("expirationDates") or [],
                "expiry": opt.get("expirationDate"),
                "calls": leg(opt.get("calls")),
                "puts": leg(opt.get("puts")),
            }
            OPT_CACHE[key] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("options %s failed: %s", key, _err(e))
            self._json(502, {"error": str(e)})

    def _inearn(self):
        # Indian quarterly EPS history from Screener.in "Quarterly Results" table. Yahoo's earningsHistory
        # needs analyst estimates, which most NSE mid/small caps lack — Screener has reported EPS for nearly
        # every listed name. Returns actual EPS per quarter, newest-first. ₹ per share, keyless.
        global IND_EARN_CACHE
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = IND_EARN_CACHE.get(sym)
            if hit and now - hit[0] < 12 * 3600:   # results change quarterly; 12h cache
                self._json(200, hit[1]); return

            def fetch(path):
                url = "https://www.screener.in/company/" + urllib.parse.quote(sym) + path
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read().decode("utf-8", "ignore")

            html = None
            for path in ("/consolidated/", "/"):
                try:
                    h = fetch(path)
                    if 'id="quarters"' in h:
                        html = h; break
                except Exception as e:
                    LOG.warning("inearn %s: fetch %s failed: %s", sym, path, _err(e))
                    continue
            if not html:
                out = {"symbol": sym, "note": "not-found", "earnings": []}
                IND_EARN_CACHE[sym] = (now, out); self._json(200, out); return

            nm = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
            name = re.sub("<.*?>", "", nm.group(1)).strip() if nm else sym

            def clean(s):
                s = re.sub(r"<[^>]*>", " ", s)
                s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("+", " ")
                return re.sub(r"\s+", " ", s).strip()

            def numval(s):
                s = (s or "").strip().rstrip("%").replace(",", "").replace("₹", "").strip()
                if s in ("", "-"):
                    return None
                try:
                    return float(s)
                except Exception:
                    return None

            MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
                   "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
            m = re.search(r'id="quarters".*?(<table.*?</table>)', html, re.S)
            earnings = []
            if m:
                t = m.group(1)
                heads = [clean(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", t, re.S)]
                # value columns are the "Mon YYYY" quarter-ending headers (skip the blank first cell)
                cols = []   # (list_index_into_values, month, year)
                for i, c in enumerate(heads[1:] if heads else []):
                    mm = re.match(r"([A-Za-z]{3})\s+(\d{4})$", c)
                    if mm:
                        cols.append((i, MON.get(mm.group(1).lower(), 0), int(mm.group(2))))
                body = re.search(r"<tbody>(.*?)</tbody>", t, re.S)
                src = body.group(1) if body else t
                eps_vals = None
                for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", src, re.S):
                    cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
                    if not cells:
                        continue
                    label = clean(cells[0]).lower()
                    if label.startswith("eps in rs") or label == "eps":
                        eps_vals = [clean(c) for c in cells[1:]]
                        break
                if eps_vals is not None:
                    for idx, mo, yr in cols:
                        if mo == 0 or idx >= len(eps_vals):
                            continue
                        v = numval(eps_vals[idx])
                        if v is None:
                            continue
                        try:
                            last = (datetime.date(yr + 1, 1, 1) - datetime.timedelta(days=1)) if mo == 12 \
                                   else (datetime.date(yr, mo + 1, 1) - datetime.timedelta(days=1))
                            day = last.day
                        except Exception:
                            day = 28
                        earnings.append({"date": "%04d-%02d-%02d" % (yr, mo, day),
                                         "month": mo, "year": yr, "actual": v})
                    earnings.reverse()   # Screener is oldest-first left->right -> newest-first
            out = {"symbol": sym, "name": name, "earnings": earnings}
            IND_EARN_CACHE[sym] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("inearn %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _indfin(self):
        # Indian financial statements (P&L / Balance Sheet / Cash Flow) scraped from Screener.in.
        # Figures are ₹ Crore, as the company reports them. Prefers consolidated, falls back to standalone.
        global IND_FIN_CACHE
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0].upper()
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = IND_FIN_CACHE.get(sym)
            if hit and now - hit[0] < 12 * 3600:   # statements change quarterly; 12h cache
                self._json(200, hit[1]); return

            def fetch(path):
                url = "https://www.screener.in/company/" + urllib.parse.quote(sym) + path
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read().decode("utf-8", "ignore")

            html = None
            for path in ("/consolidated/", "/"):
                try:
                    h = fetch(path)
                    if 'id="profit-loss"' in h:
                        html = h; break
                except Exception as e:
                    LOG.warning("indfin %s: fetch %s failed: %s", sym, path, _err(e))
                    continue
            if not html:
                out = {"symbol": sym, "note": "not-found", "years": [],
                       "income": [], "balance": [], "cashflow": []}
                IND_FIN_CACHE[sym] = (now, out); self._json(200, out); return

            nm = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
            name = re.sub("<.*?>", "", nm.group(1)).strip() if nm else sym

            def clean(s):
                s = re.sub(r"<[^>]*>", " ", s)   # [^>] spans newlines -> handles multi-line <button> tags
                s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("+", " ")
                return re.sub(r"\s+", " ", s).strip()

            def parse_table(sec):
                m = re.search(r'id="%s".*?(<table.*?</table>)' % sec, html, re.S)
                if not m:
                    return [], []
                t = m.group(1)
                heads = [clean(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", t, re.S)]
                body = re.search(r"<tbody>(.*?)</tbody>", t, re.S)
                src = body.group(1) if body else t
                rows = []
                for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", src, re.S):
                    cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
                    if not cells:
                        continue
                    rows.append((clean(cells[0]), [clean(c) for c in cells[1:]]))
                return heads, rows

            def yearcols(heads):
                # header value columns (skip the blank first cell); keep only "Mon YYYY" fiscal-year cols
                out = []
                for i, c in enumerate(heads[1:] if heads else []):
                    mm = re.match(r"[A-Za-z]{3}\s+(\d{4})$", c)
                    if mm:
                        out.append((int(mm.group(1)), i))
                return out

            def numval(s):
                s = (s or "").strip().rstrip("%").replace(",", "").replace("₹", "").strip()
                if s in ("", "-"):
                    return None
                try:
                    return float(s)
                except Exception:
                    return None

            ph, prows = parse_table("profit-loss")
            pcols = yearcols(ph)
            years_int = sorted({y for y, _ in pcols})[-7:]      # last 7 fiscal years
            years = ["FY" + str(y)[2:] for y in years_int]

            def section(heads, rows):
                ymap = {y: i for y, i in yearcols(heads)}
                out = []
                for label, vals in rows:
                    sel, raw = [], []
                    for y in years_int:
                        idx = ymap.get(y)
                        cellstr = vals[idx] if (idx is not None and idx < len(vals)) else ""
                        raw.append(cellstr)
                        sel.append(numval(cellstr))
                    if all(x is None for x in sel):
                        continue
                    ne = [r for r in raw if r.strip() not in ("", "-")]
                    if label.lower() == "eps in rs":
                        fmt = "eps"
                    elif ne and all(r.strip().endswith("%") for r in ne):
                        fmt = "pct"
                    else:
                        fmt = "cr"
                    out.append({"label": label, "fmt": fmt, "values": sel})
                return out

            bh, brows = parse_table("balance-sheet")
            ch, crows = parse_table("cash-flow")
            out = {"symbol": sym, "name": name, "unit": "INR", "scale": "cr", "years": years,
                   "income": section(ph, prows), "balance": section(bh, brows),
                   "cashflow": section(ch, crows)}
            IND_FIN_CACHE[sym] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("indfin %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _mcx(self):
        # MCX commodity (GOLD/SILVER/CRUDEOIL...) front-month candles, ₹ — read-only, no auth.
        global MCX_CACHE
        try:
            p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            base = p.get("base", [""])[0].upper()
            frm = p.get("from", [""])[0]; to = p.get("to", [""])[0]
            interval = p.get("interval", ["day"])[0]
            intraday = p.get("intraday", [""])[0]   # e.g. 1minute/30minute -> today's live candles
            if not base or (not intraday and (not frm or not to)):
                self._json(400, {"error": "missing base/from/to"}); return
            if MCX_CACHE is None or time.time() - MCX_CACHE[0] > UNIVERSE_TTL:
                req = urllib.request.Request(MCX_URL, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=40) as r:
                    data = json.loads(gzip.decompress(r.read()))
                MCX_CACHE = (time.time(), [{"base": (x.get("trading_symbol") or "").split(" ")[0],
                              "key": x.get("instrument_key"),
                              "name": x.get("trading_symbol"),
                              "expiry": x.get("expiry") or 0}
                             for x in data if x.get("instrument_type") == "FUT"])
            now = time.time() * 1000
            cands = sorted([x for x in MCX_CACHE[1] if x["base"] == base and x["expiry"] > now],
                           key=lambda z: z["expiry"])
            if not cands:
                self._json(404, {"error": "no live contract for " + base}); return
            inst = cands[0]
            if intraday:   # today's in-progress session (historical-candle only has completed days)
                url = ("https://api.upstox.com/v2/historical-candle/intraday/"
                       + urllib.parse.quote(inst["key"], safe='') + "/"
                       + urllib.parse.quote(intraday))
            else:
                url = ("https://api.upstox.com/v2/historical-candle/"
                       + urllib.parse.quote(inst["key"], safe='') + "/"
                       + urllib.parse.quote(interval) + "/"
                       + urllib.parse.quote(to) + "/" + urllib.parse.quote(frm))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
            self._json(200, {"name": inst["name"], "candles": body.get("data", {}).get("candles", [])})
        except Exception as e:
            LOG.error("mcx %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _nse_list(self):
        try:
            self._json(200, nse_universe())
        except Exception as e:
            LOG.error("nse_list %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _us_list(self):
        try:
            self._json(200, us_universe())
        except Exception as e:
            LOG.error("us_list %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _proxy_upstox(self):
        # Upstox historical candles are READ-ONLY (no OAuth/token needed).
        try:
            p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = p.get("key", [""])[0]          # e.g. NSE_EQ|INE002A01018
            frm = p.get("from", [""])[0]
            to = p.get("to", [""])[0]
            interval = p.get("interval", ["day"])[0]
            if not key or not frm or not to:
                self._json(400, {"error": "missing key/from/to"}); return
            url = ("https://api.upstox.com/v2/historical-candle/"
                   + urllib.parse.quote(key, safe='') + "/"
                   + urllib.parse.quote(interval) + "/"
                   + urllib.parse.quote(to) + "/" + urllib.parse.quote(frm))
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                body = r.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers(); self.wfile.write(body)
        except Exception as e:
            LOG.error("proxy_upstox %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _proxy_gnews(self):
        try:
            qs = urllib.parse.urlparse(self.path).query
            q = urllib.parse.parse_qs(qs).get("q", [""])[0]
            if not q:
                self._json(400, {"error": "missing q"}); return
            url = GNEWS + "?" + urllib.parse.urlencode(
                {"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
            root = ET.fromstring(raw)
            out = []
            for it in root.findall(".//item")[:80]:
                title = (it.findtext("title") or "").strip()
                link = it.findtext("link") or ""
                src_el = it.find("source")
                src = src_el.text if src_el is not None else "News"
                # Google formats titles as "Headline - Source"; trim the source tail
                if src and title.endswith(" - " + src):
                    title = title[:-(len(src) + 3)]
                tm = ""; ts = 0
                try:
                    dt = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    ts = int(dt.timestamp() * 1000)   # absolute epoch ms (tz-independent) for client-side age filtering
                    tm = dt.astimezone(IST).strftime("%d %b %H:%M")   # display string in IST
                except Exception:
                    pass
                out.append({"title": title, "source": src, "link": link, "time": tm, "ts": ts})
            self._json(200, out)
        except Exception as e:
            LOG.error("gnews %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _splits(self):
        # Share-count events per symbol for the portfolio tracker, which applies them automatically to trades dated
        # before each ex-date: {symbol: [{date, kind: "split"|"bonus", num, den, src, confirmed}] | null on failure}.
        # num/den = shares after / before (a 1:1 bonus and a 2-for-1 split are both 2/1). Dates come from Yahoo
        # chart "split" events (ex-date in the exchange's time zone). Yahoo also files Indian bonus issues as
        # splits, and the two are taxed differently, so Indian symbols are matched against NSE's corporate actions
        # ("Bonus 1:1" / "Face Value Split ... From Rs 10 To Rs 2"), which also adds events Yahoo lacks.
        syms = [s for s in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                .get("symbols", [""])[0].split(",") if s.strip()][:60]

        def yahoo(sym):
            url = YBASE + urllib.parse.quote(sym) + "?interval=1mo&range=max&events=split"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                res = (json.loads(r.read()).get("chart", {}).get("result") or [{}])[0]
            off = (res.get("meta") or {}).get("gmtoffset") or 0
            out = []
            for ev in (((res.get("events") or {}).get("splits")) or {}).values():
                ts, num, den = ev.get("date"), ev.get("numerator"), ev.get("denominator")
                if ts is None or not num or not den or num == den:
                    continue
                day = datetime.datetime.fromtimestamp(ts + off, datetime.timezone.utc).strftime("%Y-%m-%d")
                out.append({"date": day, "kind": "split", "num": num, "den": den, "src": "Yahoo", "confirmed": True})
            return out

        def nse(code):
            today = datetime.datetime.now(IST).strftime("%d-%m-%Y")
            rows = nse_get("corporates-corporateActions?index=equities&symbol=%s&from_date=01-01-1990&to_date=%s"
                           % (urllib.parse.quote(code), today)) or []
            out = []
            for r in rows:
                low = re.sub(r"\s+", " ", (r.get("subject") or "").lower())
                try:
                    day = datetime.datetime.strptime((r.get("exDate") or "").strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
                except ValueError:
                    continue
                m = re.search(r"bonus\s*(\d+)\s*:\s*(\d+)", low)
                if m and int(m[1]) > 0 and int(m[2]) > 0:          # "Bonus a:b" = a new shares for every b held
                    a, b = int(m[1]), int(m[2])
                    out.append({"date": day, "kind": "bonus", "num": a + b, "den": b, "src": "NSE", "confirmed": True})
                    continue
                m = re.search(r"from (?:rs|re)\.?\s*([\d.]+).*?to (?:rs|re)\.?\s*([\d.]+)", low)
                if m and any(k in low for k in ("split", "sub-division", "subdivision", "consolidat")):
                    fv0, fv1 = float(m[1]), float(m[2])            # face value Rs 10 -> Rs 2 = 5 shares for 1
                    if fv0 > 0 and fv1 > 0 and fv0 != fv1:
                        out.append({"date": day, "kind": "split", "num": fv0, "den": fv1, "src": "NSE", "confirmed": True})
            return out

        def one(sym):
            hit = SPLIT_CACHE.get(sym)
            if hit and time.time() - hit[0] < 12 * 3600:
                return hit[1]
            try:
                ev = yahoo(sym)
            except Exception as e:
                LOG.error("splits %s failed: %s", sym, _err(e))
                return None
            indian, ne = sym.upper().endswith((".NS", ".BO")), None
            if indian:
                try:
                    ne = nse(re.sub(r"\.(NS|BO)$", "", sym, flags=re.I))
                except Exception as e:
                    LOG.warning("splits nse %s failed: %s", sym, _err(e))
                    ne = None
                if ne is None:      # can't tell bonus from split: apply as a split (share count is right) but say so
                    for e in ev:
                        e["confirmed"] = False
                else:
                    near = lambda a, b: abs((datetime.date.fromisoformat(a) - datetime.date.fromisoformat(b)).days) <= 3
                    merged = []
                    for e in ev:    # NSE's own record wins on kind and ratio for the same ex-date
                        m = next((x for x in ne if near(x["date"], e["date"])), None)
                        merged.append(dict(m, date=e["date"]) if m else dict(e, confirmed=False))   # older than NSE's records
                    merged += [x for x in ne if not any(near(x["date"], e["date"]) for e in ev)]
                    ev = merged
            ev.sort(key=lambda e: e["date"])
            if not indian or ne is not None:      # don't keep an unconfirmed answer for 12 h
                SPLIT_CACHE[sym] = (time.time(), ev)
            return ev
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            self._json(200, dict(zip(syms, ex.map(one, syms))))

    def _dividends(self):
        # Dividend history from Yahoo chart events (works for US and Indian .NS). Returns the raw
        # cash dividends plus derived TTM total, yield, frequency, per-year totals and CAGR.
        global DIV_CACHE
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0]
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = DIV_CACHE.get(sym)
            if hit and now - hit[0] < 12 * 3600:
                self._json(200, hit[1]); return

            url = (YBASE + urllib.parse.quote(sym)
                   + "?interval=1mo&range=10y&events=div")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
            res = (body.get("chart", {}).get("result") or [{}])[0]
            meta = res.get("meta", {}) or {}
            price = meta.get("regularMarketPrice")
            ccy = meta.get("currency") or ("INR" if sym.upper().endswith(".NS") else "USD")

            divs = ((res.get("events", {}) or {}).get("dividends", {}) or {})
            events = []
            for _, ev in divs.items():
                ts = ev.get("date"); amt = ev.get("amount")
                if ts is None or amt is None:
                    continue
                d = datetime.datetime.fromtimestamp(ts, IST)
                events.append({"ts": ts * 1000, "date": d.strftime("%d %b %Y"),
                               "year": d.year, "amount": amt})
            events.sort(key=lambda e: e["ts"])

            # TTM total = dividends in the trailing 365 days
            cutoff = (now - 365 * 86400) * 1000
            ttm = round(sum(e["amount"] for e in events if e["ts"] >= cutoff), 4)

            # frequency from the median gap between the last several ex-dates
            freq, per_year = None, None
            if len(events) >= 3:
                gaps = sorted(events[i]["ts"] - events[i - 1]["ts"] for i in range(1, len(events)))
                med = gaps[len(gaps) // 2] / 86400000.0
                if med <= 45:   freq, per_year = "Monthly", 12
                elif med <= 135: freq, per_year = "Quarterly", 4
                elif med <= 225: freq, per_year = "Semi-annual", 2
                else:            freq, per_year = "Annual", 1

            # per-calendar-year totals
            yr = {}
            for e in events:
                yr[e["year"]] = round(yr.get(e["year"], 0) + e["amount"], 4)
            years = [{"year": y, "total": yr[y]} for y in sorted(yr)]

            # CAGR across complete years only: the current year is still running, and the oldest year is cut
            # short by the 10-year window (or is the year payments began), so both are left out. Counting
            # the cut-off first year made growth look far higher than it was (AAPL: 24.6 % vs 6.7 %).
            cagr = cagr_from = cagr_to = None
            comp = [y for y in years if y["year"] < datetime.datetime.now(IST).year and y["total"] > 0][1:]
            if len(comp) >= 2:
                first, last = comp[0], comp[-1]
                span = last["year"] - first["year"]
                if span > 0 and first["total"] > 0:
                    cagr = round(((last["total"] / first["total"]) ** (1.0 / span) - 1) * 100, 1)
                    cagr_from, cagr_to = first["year"], last["year"]

            out = {"symbol": sym, "currency": ccy, "price": price,
                   "ttm": ttm, "yield": round(ttm / price * 100, 2) if (price and ttm) else None,
                   "frequency": freq, "perYear": per_year, "cagr": cagr,
                   "cagrFrom": cagr_from, "cagrTo": cagr_to,
                   "years": years[-12:], "events": events[-40:]}
            DIV_CACHE[sym] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("dividends %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    def _peers(self):
        # Relative-value comps: the target company first, then the top companies (by market cap) in the
        # SAME Yahoo industry, each with valuation + profitability + growth metrics. US and Indian names.
        global PEERS_CACHE
        try:
            sym = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol", [""])[0]
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            now = time.time()
            hit = PEERS_CACHE.get(sym)
            if hit and now - hit[0] < 6 * 3600:
                self._json(200, hit[1]); return

            up = sym.upper()
            region = "in" if (up.endswith(".NS") or up.endswith(".BO")) else "us"
            MODS = "price,summaryDetail,defaultKeyStatistics,financialData,assetProfile"

            # Group by the target's exchange BOARD. NYSE + NASDAQ + AMEX are one "US" group (so a US
            # name compares across both big boards); NSE + BSE are one "India" group. The domicile-country
            # filter below still excludes foreign ADRs that merely list on a US board.
            BOARDS = {"NMS": "US", "NGM": "US", "NCM": "US", "NAS": "US",
                      "NYQ": "US", "NYS": "US", "NYE": "US",
                      "ASE": "US", "AMX": "US", "PCX": "US",
                      "NSI": "India", "BSE": "India"}
            tq = yahoo_batch_quotes(up)
            texch = (tq[0].get("exchange") if tq else None)
            tboard = BOARDS.get(texch)
            def same_board(ex):
                return bool(tboard) and BOARDS.get(ex) == tboard

            def g(o, k):
                v = (o or {}).get(k)
                return v.get("raw") if isinstance(v, dict) else v

            # multiples below ~0.02 or above ~1e5 are almost always currency artifacts of foreign
            # ADR/OTC listings (USD price vs local-currency per-share figures) -> drop as unreliable.
            def mult(v, lo=0.02, hi=1e5):
                return round(v, 2) if isinstance(v, (int, float)) and lo < v < hi else None

            def metrics(sy):
                try:
                    r = yahoo_qs(sy, MODS)
                except Exception as e:
                    LOG.warning("peers: peer %s skipped: %s", sy, _err(e))
                    return None                          # one bad/delisted/rate-limited peer must not 502 the comps
                if not r:
                    return None
                pr = r.get("price", {}) or {}; sd = r.get("summaryDetail", {}) or {}
                ks = r.get("defaultKeyStatistics", {}) or {}; fd = r.get("financialData", {}) or {}
                ap = r.get("assetProfile", {}) or {}
                mc = g(pr, "marketCap") or g(sd, "marketCap")
                pct = lambda v: round(v * 100, 1) if isinstance(v, (int, float)) else None
                ev = g(ks, "enterpriseToEbitda")
                return {"sym": sy, "name": g(pr, "longName") or g(pr, "shortName") or sy,
                        "currency": pr.get("currency"), "country": ap.get("country"),
                        "sector": ap.get("sector"), "industry": ap.get("industry"),
                        "price": g(fd, "currentPrice") or g(pr, "regularMarketPrice"), "mcap": mc,
                        "pe": mult(g(sd, "trailingPE")), "fpe": mult(g(sd, "forwardPE")),
                        "ps": mult(g(sd, "priceToSalesTrailing12Months")), "pb": mult(g(ks, "priceToBook")),
                        "evEbitda": round(ev, 2) if isinstance(ev, (int, float)) and -1000 < ev < 1000 else None,
                        "divYield": pct(g(sd, "dividendYield")),
                        "grossMargin": pct(g(fd, "grossMargins")), "operMargin": pct(g(fd, "operatingMargins")),
                        "netMargin": pct(g(fd, "profitMargins")), "roe": pct(g(fd, "returnOnEquity")),
                        "revGrowth": pct(g(fd, "revenueGrowth"))}

            tgt = metrics(sym)
            if not tgt:
                self._json(200, {"symbol": sym, "note": "no-data", "rows": []}); return
            industry = tgt.get("industry"); sector = tgt.get("sector")

            def norm(name):
                n = re.sub(r"[^A-Za-z0-9 ]", " ", (name or "").upper())
                n = re.sub(r"\b(CL|CLASS|SERIES)\s+[A-C]\b", " ", n)   # BRK CL A / CL B share classes
                for w in (" LIMITED", " LTD", " INCORPORATED", " INC", " CORPORATION", " CORP",
                          " COMPANY", " CO", " GROUP", " HOLDINGS", " PLC", " SA", " NV", " AG",
                          " NEW", " ADR", " DRN", " CEDEAR"):
                    n = n.replace(w, " ")
                n = re.sub(r"\s+[A-C]$", "", re.sub(r"\s+", " ", n).strip())   # trailing lone class letter
                return n.strip()

            # Dedup key: India dual-lists the SAME root code on NSE & BSE (BPCL.NS / BPCL.BO), so key by
            # root there; US cross-listings share a name but not a ticker, so key by normalised name.
            def dkey(s, nm):
                if region == "in":
                    return re.sub(r"\.(NS|BO)$", "", s.upper())
                return norm(nm)
            def rank(s):   # which listing of a company to keep
                su = s.upper()
                if su == up: return 3                                   # the target's own ticker
                if region == "in": return 2 if su.endswith(".NS") else (1 if su.endswith(".BO") else 0)
                return 2 if ("." not in su and "-" not in su) else 0    # US: prefer clean primary tickers
            tkey = dkey(sym, tgt["name"])

            # Same-board filter: keep only listings on the target's OWN exchange (NYSE with NYSE, NASDAQ
            # with NASDAQ, NSE with NSE), which drops foreign ADR / OTC cross-listings on other boards.
            def peers_for(field, value):
                if not value:
                    return []
                best = {}
                for q in yahoo_screener(field, value, region, 25):
                    s = q.get("symbol")
                    if not s or not same_board(q.get("exchange")):
                        continue
                    nm = q.get("shortName") or q.get("longName") or s
                    key = dkey(s, nm); mc = q.get("marketCap") or 0
                    if not key:
                        continue
                    if key not in best or rank(s) > rank(best[key][0]) or (rank(s) == rank(best[key][0]) and mc > best[key][1]):
                        best[key] = (s, mc, nm)
                out = [v for k, v in best.items() if k != tkey]
                out.sort(key=lambda x: x[1] or 0, reverse=True)
                return out

            # Market-recognised competitors (Yahoo's "compare to" list), priced for ranking + filtered
            # to the target's own market. This is what surfaces the mega-cap tech cohort for names whose
            # Yahoo *industry* is too narrow to hold them (e.g. Apple, alone in "Consumer Electronics").
            def reco_peers_market():
                syms = yahoo_reco(sym)
                if not syms:
                    return []
                best = {}
                for q in yahoo_batch_quotes(",".join(syms[:12])):
                    s = q.get("symbol")
                    if not s or not same_board(q.get("exchange")):
                        continue
                    nm = q.get("shortName") or q.get("longName") or s
                    mc = q.get("marketCap") or 0
                    key = dkey(s, nm)
                    if not key or key == tkey:
                        continue
                    if key not in best or mc > best[key][1]:
                        best[key] = (s, mc, nm)
                out = list(best.values()); out.sort(key=lambda x: x[1] or 0, reverse=True)
                return out

            def merge(*lists):
                pool = {}
                for lst in lists:
                    for s, mc, nm in lst:
                        key = dkey(s, nm)
                        if not key or key == tkey:
                            continue
                        if key not in pool or (mc or 0) > (pool[key][1] or 0):
                            pool[key] = (s, mc, nm)
                out = list(pool.values()); out.sort(key=lambda x: x[1] or 0, reverse=True)
                return out

            basis = None
            ind_peers = peers_for("industry", industry)
            tmc = tgt.get("mcap") or 0
            # "dominates" = the company dwarfs the biggest name in its own industry (>12x). Its true
            # comparables are then the market's competitor set, not the leftovers of a narrow industry.
            dominates = bool(ind_peers) and tmc > 0 and (ind_peers[0][1] or 0) * 12 < tmc

            if dominates:
                pool = merge(reco_peers_market(), ind_peers)
                if len(pool) >= 3:
                    basis, peers = "competitors", pool
                else:
                    basis, peers = ("industry", ind_peers) if len(ind_peers) >= 2 else ("sector", peers_for("sector", sector))
            elif len(ind_peers) >= 4:
                basis, peers = "industry", ind_peers
            else:
                sec_peers = peers_for("sector", sector)
                if len(sec_peers) > len(ind_peers):
                    basis, peers = "sector", sec_peers
                elif ind_peers:
                    basis, peers = "industry", ind_peers
                else:
                    reco = reco_peers_market()
                    basis, peers = ("competitors", reco) if reco else (("sector", sec_peers) if sec_peers else (None, []))

            # Keep same-country peers of a COMPARABLE VALUATION (market cap). Two names aren't really
            # comparable if one is 50x the other, so we walk candidates CLOSEST in size to the target,
            # drop foreign domiciles (a US-exchange ADR like Sony/Baidu isn't a US company), and keep
            # those within ~8x of the target's market cap.
            tcountry = tgt.get("country"); tmc = tgt.get("mcap") or 0
            def prox(mc):
                if not mc or not tmc:
                    return 1e18
                return max(mc, tmc) / min(mc, tmc)
            BAND = 8.0
            in_band, near, seen = [], [], {tkey}
            def scan(cands, budget):
                fetched = 0
                for s, mc, nm in sorted(cands, key=lambda x: prox(x[1])):
                    if fetched >= budget or len(in_band) >= 7:
                        break
                    k = dkey(s, nm)
                    if s.upper() == up or not k or k in seen:
                        continue
                    m = metrics(s); fetched += 1; seen.add(k)
                    if not m or not m.get("mcap"):
                        continue
                    if tcountry and m.get("country") and m["country"] != tcountry:
                        continue                          # different domicile -> not a domestic peer
                    r = prox(m["mcap"])
                    (in_band if r <= BAND else near).append((r, m))

            scan(peers, 14)
            # If the company has no similarly-valued names in its own industry (e.g. Reliance dwarfs every
            # Indian refiner), broaden to same-country competitors/sector to find comparable-size peers.
            escalated = False
            if len(in_band) < 3:
                escalated = True
                scan(merge(reco_peers_market(), peers_for("sector", sector)), 12)
            if escalated and basis != "competitors":
                basis = "size"
            in_band.sort(key=lambda x: x[0]); near.sort(key=lambda x: x[0])
            picked = [m for _, m in (in_band if len(in_band) >= 3 else in_band + near)][:7]
            rows = [dict(tgt, isTarget=True)] + picked

            out = {"symbol": sym, "name": tgt["name"], "sector": sector,
                   "industry": industry, "basis": basis, "country": tcountry, "board": tboard,
                   "currency": tgt.get("currency"), "region": region, "rows": rows}
            PEERS_CACHE[sym] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("peers %s failed: %s", sym, _err(e))
            self._json(502, {"error": str(e)})

    @staticmethod
    def _dirof(x):
        return "up" if x > 0 else ("down" if x < 0 else "flat")

    def _wb_card(self, country, indicator, label):
        # World Bank indicator card (latest annual value + change vs prior year). Keyless. World Bank
        # rate-limits rapid sequential calls, so retry a couple of times before giving up.
        try:
            u = ("https://api.worldbank.org/v2/country/%s/indicator/%s?format=json&per_page=4&mrv=4"
                 % (country, indicator))
            rows = []
            for attempt in range(3):
                try:
                    w = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read())
                    rows = [r for r in (w[1] if isinstance(w, list) and len(w) > 1 else []) if r.get("value") is not None]
                    if rows:
                        break
                except Exception as e:
                    LOG.warning("wb_card %s/%s attempt %d failed: %s", country, indicator, attempt, _err(e))
                    pass
                time.sleep(0.4)
            if not rows:
                return None
            cur = rows[0]["value"]
            c = {"label": label, "value": "%.1f%%" % cur, "sub": "FY " + rows[0]["date"]}
            if len(rows) > 1:
                ch = cur - rows[1]["value"]
                c["change"] = "%+.1f%%" % ch; c["dir"] = self._dirof(ch)
            return c
        except Exception as e:
            LOG.warning("wb_card %s/%s failed: %s", country, indicator, _err(e))
            return None

    def _econ(self):
        # Economic indicators. US: inflation/labour from the BLS public API (keyless batch) + GDP from the
        # World Bank; India: from the World Bank (annual). Each card carries the change vs the prior period.
        global ECON_CACHE
        try:
            now = time.time()
            if ECON_CACHE and now - ECON_CACHE[0] < 6 * 3600:
                self._json(200, ECON_CACHE[1]); return
            yr = datetime.datetime.now(IST).year
            us, ind = [], []
            # ---- US: BLS (current monthly) ----
            try:
                body = json.dumps({"seriesid": ["CUUR0000SA0", "CUUR0000SA0L1E", "LNS14000000",
                                                "CES0000000001", "CES0500000003"],
                                   "startyear": str(yr - 2), "endyear": str(yr)}).encode()
                req = urllib.request.Request("https://api.bls.gov/publicAPI/v1/timeseries/data/", data=body,
                                             headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
                d = json.loads(urllib.request.urlopen(req, timeout=25).read())
                S = {s["seriesID"]: (s.get("data") or []) for s in d.get("Results", {}).get("series", [])}
                def val(a, i=0):
                    try: return float(a[i]["value"])
                    except Exception: return None
                def when(a):
                    return (a[0]["periodName"][:3] + " " + a[0]["year"]) if a else ""
                def yoy_at(a, i):
                    return (val(a, i) / val(a, i + 12) - 1) * 100 if (len(a) > i + 12 and val(a, i) and val(a, i + 12)) else None
                for sid, label in (("CUUR0000SA0", "CPI Inflation · YoY"), ("CUUR0000SA0L1E", "Core CPI · YoY"),
                                   ("CES0500000003", "Wage growth · YoY")):
                    a = S.get(sid, []); cur = yoy_at(a, 0)
                    if cur is None:
                        continue
                    c = {"label": label, "value": "%.1f%%" % cur, "sub": when(a)}
                    prev = yoy_at(a, 1)
                    if prev is not None:
                        c["change"] = "%+.1f%%" % (cur - prev); c["dir"] = self._dirof(cur - prev)
                    us.append(c)
                a = S.get("LNS14000000", [])
                if a and val(a) is not None:
                    c = {"label": "Unemployment rate", "value": "%.1f%%" % val(a), "sub": when(a)}
                    if val(a, 1) is not None:
                        ch = val(a, 0) - val(a, 1); c["change"] = "%+.1f%%" % ch; c["dir"] = self._dirof(ch)
                    us.append(c)
                a = S.get("CES0000000001", [])
                if len(a) > 2 and val(a, 0) is not None and val(a, 1) is not None:
                    cur = round(val(a, 0) - val(a, 1))   # jobs added this month (thousands)
                    c = {"label": "Nonfarm payrolls · MoM", "value": "%+dK" % cur, "sub": when(a)}
                    if val(a, 2) is not None:
                        ch = cur - round(val(a, 1) - val(a, 2)); c["change"] = "%+dK vs prior" % ch; c["dir"] = self._dirof(ch)
                    us.append(c)
            except Exception:
                pass
            gdp = self._wb_card("US", "NY.GDP.MKTP.KD.ZG", "Real GDP growth · annual")
            if gdp:
                us.append(gdp)
            # ---- India: World Bank (annual) ----
            for indc, label in (("NY.GDP.MKTP.KD.ZG", "Real GDP growth · annual"),
                                ("FP.CPI.TOTL.ZG", "CPI Inflation · annual"),
                                ("SL.UEM.TOTL.ZS", "Unemployment rate"),
                                ("BN.CAB.XOKA.GD.ZS", "Current account · % GDP")):
                c = self._wb_card("IN", indc, label)
                if c:
                    ind.append(c)
            # next scheduled US release date per indicator (real, via the keyless Nasdaq economic calendar)
            try:
                nxt = self._econ_next()
                NEXT_MAP = {"CPI Inflation · YoY": "cpi", "Core CPI · YoY": "cpi", "Wage growth · YoY": "emp",
                            "Unemployment rate": "emp", "Nonfarm payrolls · MoM": "emp", "Real GDP growth · annual": "gdp"}
                for c in us:
                    k = NEXT_MAP.get(c["label"])
                    if k and nxt.get(k):
                        c["next"] = nxt[k]
            except Exception:
                pass
            out = {"us": us, "in": ind}
            ECON_CACHE = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("econ %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _econ_next(self):
        # Earliest upcoming US release date for CPI / Employment Situation / GDP, from the Nasdaq economic
        # calendar (keyless). Fetches the next ~5 weeks of dates in parallel, then picks the earliest match
        # for each. Called only from the 6h-cached _econ, so the cost is paid rarely.
        from concurrent.futures import ThreadPoolExecutor
        today = datetime.datetime.now(IST).date()
        days = [today + datetime.timedelta(days=i) for i in range(35)]
        def fetch_day(d):
            try:
                url = "https://api.nasdaq.com/api/calendar/economicevents?date=" + d.isoformat()
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"})
                return (d, (json.loads(urllib.request.urlopen(req, timeout=8).read()).get("data") or {}).get("rows") or [])
            except Exception as e:
                LOG.warning("econ_next %s: fetch failed: %s", d.isoformat(), _err(e))
                return (d, [])
        rows_by_day = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            for d, rows in ex.map(fetch_day, days):
                rows_by_day[d] = rows
        out = {}
        for d in days:   # ascending date order -> the first match is the earliest release
            if len(out) >= 3:
                break
            ds = "%d %s" % (d.day, d.strftime("%b"))
            for r in rows_by_day.get(d, []):
                if r.get("country") != "United States":
                    continue
                nm = (r.get("eventName") or "").strip(); low = nm.lower()
                if "cpi" in low and "cpi" not in out:
                    out["cpi"] = ds
                if ("nonfarm payrolls" in low or "unemployment rate" in low or "average hourly earnings" in low) and "emp" not in out:
                    out["emp"] = ds
                if nm.upper() == "GDP" and "gdp" not in out:
                    out["gdp"] = ds
        return out

    def _corr(self):
        # Correlation matrix from daily returns. Fetches each symbol's daily closes from Yahoo, aligns on
        # common trading days, and computes pairwise Pearson correlation. All markets via Yahoo (US, .NS, crypto).
        global CORR_CACHE
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            syms = [s for s in (q.get("symbols", [""])[0].split(",")) if s][:24]
            rng = q.get("range", ["1y"])[0]
            if rng not in ("1mo", "3mo", "6mo", "1y", "2y"):
                rng = "1y"
            if len(syms) < 2:
                self._json(400, {"error": "need >=2 symbols"}); return
            now = time.time()
            ck = (",".join(syms), rng)
            hit = CORR_CACHE.get(ck)
            if hit and now - hit[0] < 6 * 3600:
                self._json(200, hit[1]); return

            series = {}   # sym -> {date: close}
            for s in syms:
                try:
                    url = YBASE + urllib.parse.quote(s) + "?interval=1d&range=" + rng
                    d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read())
                    res = (d.get("chart", {}).get("result") or [{}])[0]
                    ts = res.get("timestamp") or []
                    ind = res.get("indicators", {}) or {}
                    px = None
                    adj = ind.get("adjclose")
                    if adj and adj[0].get("adjclose"):
                        px = adj[0]["adjclose"]
                    elif ind.get("quote"):
                        px = ind["quote"][0].get("close")
                    if not ts or not px:
                        continue
                    m = {}
                    for t, p in zip(ts, px):
                        if p is None:
                            continue
                        m[datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")] = p
                    if len(m) > 5:
                        series[s] = m
                except Exception as e:
                    LOG.warning("corr: series %s skipped: %s", s, _err(e))
                    continue

            valid = [s for s in syms if s in series]
            if len(valid) < 2:
                self._json(200, {"symbols": [], "matrix": [], "days": 0, "range": rng, "note": "insufficient-data"}); return
            common = sorted(set.intersection(*[set(series[s]) for s in valid]))
            if len(common) < 10:
                self._json(200, {"symbols": valid, "matrix": [], "days": len(common), "range": rng, "note": "few-common-days"}); return

            rets = {}
            for s in valid:
                a = [series[s][d] for d in common]
                rets[s] = [(a[i] / a[i - 1] - 1) for i in range(1, len(a)) if a[i - 1]]

            def pearson(x, y):
                n = min(len(x), len(y))
                if n < 5:
                    return None
                x, y = x[-n:], y[-n:]
                mx = sum(x) / n; my = sum(y) / n
                sx = sum((a - mx) ** 2 for a in x); sy = sum((b - my) ** 2 for b in y)
                if sx <= 0 or sy <= 0:
                    return None
                cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
                return cov / ((sx * sy) ** 0.5)

            matrix = []
            for a in valid:
                row = []
                for b in valid:
                    if a == b:
                        row.append(1.0)
                    else:
                        c = pearson(rets[a], rets[b])
                        row.append(round(c, 2) if c is not None else None)
                matrix.append(row)
            out = {"symbols": valid, "matrix": matrix, "days": len(common) - 1, "range": rng}
            CORR_CACHE[ck] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("corr %s failed: %s", ck, _err(e))
            self._json(502, {"error": str(e)})

    def _corrpair(self):
        # Two-company comparison: aligned daily returns, correlation, beta (B on A), R², plus the
        # normalized price paths for an overlay chart. All markets via Yahoo daily closes.
        global CORR_CACHE
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            a = q.get("a", [""])[0]; b = q.get("b", [""])[0]
            rng = q.get("range", ["1y"])[0]
            if rng not in ("1mo", "3mo", "6mo", "1y", "2y", "5y"):
                rng = "1y"
            if not a or not b:
                self._json(400, {"error": "need a and b"}); return
            now = time.time()
            ck = ("pair", a, b, rng)
            hit = CORR_CACHE.get(ck)
            if hit and now - hit[0] < 3 * 3600:
                self._json(200, hit[1]); return

            def fetch(s):
                url = YBASE + urllib.parse.quote(s) + "?interval=1d&range=" + rng
                d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read())
                res = (d.get("chart", {}).get("result") or [{}])[0]
                ts = res.get("timestamp") or []
                ind = res.get("indicators", {}) or {}
                px = None
                if ind.get("adjclose") and ind["adjclose"][0].get("adjclose"):
                    px = ind["adjclose"][0]["adjclose"]
                elif ind.get("quote"):
                    px = ind["quote"][0].get("close")
                m = {}
                for t, p in zip(ts, px or []):
                    if p is not None:
                        m[datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")] = p
                return m

            ma, mb = fetch(a), fetch(b)
            common = sorted(set(ma) & set(mb))
            if len(common) < 10:
                self._json(200, {"a": a, "b": b, "range": rng, "days": len(common), "note": "few-common-days"}); return
            pa = [ma[d] for d in common]; pb = [mb[d] for d in common]
            rA = [pa[i] / pa[i - 1] - 1 for i in range(1, len(pa))]
            rB = [pb[i] / pb[i - 1] - 1 for i in range(1, len(pb))]
            n = len(rA)
            mA = sum(rA) / n; mB = sum(rB) / n
            sA = sum((x - mA) ** 2 for x in rA); sB = sum((x - mB) ** 2 for x in rB)
            cov = sum((rA[i] - mA) * (rB[i] - mB) for i in range(n))
            corr = cov / ((sA * sB) ** 0.5) if (sA > 0 and sB > 0) else None
            beta = cov / sA if sA > 0 else None
            out = {"a": a, "b": b, "range": rng, "days": n,
                   "corr": round(corr, 3) if corr is not None else None,
                   "beta": round(beta, 3) if beta is not None else None,
                   "r2": round(corr * corr, 3) if corr is not None else None,
                   "rA": [round(x, 5) for x in rA], "rB": [round(x, 5) for x in rB],
                   "idxA": [round(p / pa[0] * 100, 2) for p in pa],
                   "idxB": [round(p / pb[0] * 100, 2) for p in pb],
                   "dates": common}
            CORR_CACHE[ck] = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("corrpair %s failed: %s", ck, _err(e))
            self._json(502, {"error": str(e)})

    def _nasdaq(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9"})
        return json.loads(urllib.request.urlopen(req, timeout=20).read())

    def _ipos(self):
        # Recent (priced) + upcoming IPOs from the Nasdaq IPO calendar (keyless). Scans prev/current/next month.
        # Nasdaq's "upcoming" table only holds deals with an expected pricing date (usually this week's), so the
        # "filed" table — registrations with no open date yet — follows them in the upcoming list, undated.
        global IPO_CACHE
        try:
            now = time.time()
            if IPO_CACHE and now - IPO_CACHE[0] < 2 * 3600:     # open / upcoming issues change daily
                self._json(200, IPO_CACHE[1]); return
            today = datetime.datetime.now(IST).date()
            today_t = (today.year, today.month, today.day)
            cur = today.replace(day=1)
            prev = (cur - datetime.timedelta(days=1)).replace(day=1)
            nxt = (cur.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)
            def mdY(s):   # "7/30/2026" -> (year, month, day) sortable tuple
                m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(s or ""))
                return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)
            priced, upcoming, filed, complete = [], [], [], True
            for mdt in (prev, cur, nxt):
                try:
                    d = self._nasdaq("https://api.nasdaq.com/api/ipo/calendar?date=%04d-%02d" % (mdt.year, mdt.month)).get("data", {}) or {}
                except Exception:
                    complete = False
                    continue
                for r in ((d.get("priced") or {}).get("rows") or []):
                    priced.append({"sym": r.get("proposedTickerSymbol"), "name": r.get("companyName"),
                                   "exchange": r.get("proposedExchange"), "price": r.get("proposedSharePrice"),
                                   "date": r.get("pricedDate"), "value": r.get("dollarValueOfSharesOffered")})
                for r in (((d.get("upcoming") or {}).get("upcomingTable") or {}).get("rows") or []):
                    upcoming.append({"sym": r.get("proposedTickerSymbol"), "name": r.get("companyName"),
                                     "exchange": r.get("proposedExchange"), "price": r.get("proposedSharePrice"),
                                     "shares": r.get("sharesOffered"), "date": r.get("expectedPriceDate"),
                                     "value": r.get("dollarValueOfSharesOffered")})
                for r in ((d.get("filed") or {}).get("rows") or []):
                    filed.append({"sym": r.get("proposedTickerSymbol"), "name": r.get("companyName"),
                                  "filed": r.get("filedDate"), "value": r.get("dollarValueOfSharesOffered")})
            seen = set(); pr = []
            for x in sorted(priced, key=lambda x: mdY(x["date"]), reverse=True):   # newest first
                k = (x["sym"], x["date"])
                if x["sym"] and k not in seen:
                    seen.add(k); pr.append(x)
            # soonest first; a deal without a ticker or a date yet is still upcoming (undated ones go last)
            seen = set(); up = []
            for x in sorted(upcoming, key=lambda x: mdY(x["date"]) if mdY(x["date"])[0] else (9999, 0, 0)):
                k = (x["sym"] or x["name"], x["date"])
                d = mdY(x["date"])
                if (x["sym"] or x["name"]) and k not in seen and (not d[0] or d >= today_t):
                    seen.add(k); up.append(x)
            # then filed registrations, newest filing first, skipping any already scheduled above
            seen = {(x["sym"] or "").upper() for x in up if x["sym"]} | {(x["name"] or "").lower() for x in up}
            n_filed = 0
            for x in sorted(filed, key=lambda x: mdY(x["filed"]), reverse=True):
                ks = {(x["sym"] or "").upper(), (x["name"] or "").lower()} - {""}
                if ks and not (ks & seen) and n_filed < 20:
                    seen |= ks; up.append(x); n_filed += 1
            # ---- Indian IPOs (NSE) ----
            def dmy(s):   # "30-Jul-2026" / "30-JUL-2026" -> (year, month, day)
                m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", str(s or ""))
                if not m:
                    return (0, 0, 0)
                mon = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
                       "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}.get(m.group(2).lower(), 0)
                return (int(m.group(3)), mon, int(m.group(1)))
            # Issues open now ("ipo-current-issue", mainboard and SME) plus forthcoming ones ("all-upcoming-issues",
            # which lists mainboard only under category=ipo); the same issue can appear in both.
            in_recent, in_closed, in_up, seen_in = [], [], [], set()
            for path in ("ipo-current-issue", "all-upcoming-issues?category=ipo", "all-upcoming-issues?category=sme"):
                try:
                    rows = nse_get(path) or []
                except Exception as e:
                    LOG.warning("ipos nse %s failed: %s", path, _err(e))
                    complete = False
                    continue
                for r in (rows if isinstance(rows, list) else []):
                    key = r.get("symbol") or r.get("companyName")
                    if not key or key in seen_in:
                        continue
                    seen_in.add(key)
                    in_up.append({"sym": r.get("symbol"), "name": r.get("companyName") or r.get("company"),
                                  "exchange": ("NSE SME" if r.get("series") == "SME" else "NSE"),
                                  "price": r.get("issuePrice"), "date": r.get("issueStartDate"),
                                  "end": r.get("issueEndDate"), "status": r.get("status")})
            try:
                past = nse_get("public-past-issues") or []
                recent_cut = today - datetime.timedelta(days=14)
                for r in past:
                    if r.get("securityType") not in ("EQ", "SME", "BE", "IV", "RR"):
                        continue                          # equity / SME / REIT / InvIT only — skip DEBT etc.
                    ld = r.get("listingDate")
                    if not ld or ld == "-":
                        # bidding has closed but the shares have not listed yet: heads the recent list (recent closes
                        # only, so a withdrawn or stale issue without a listing date does not linger)
                        e = dmy(r.get("ipoEndDate"))
                        if e[0] and e >= (recent_cut.year, recent_cut.month, recent_cut.day) and r.get("symbol") not in seen_in:
                            seen_in.add(r.get("symbol"))
                            in_closed.append({"sym": r.get("symbol"), "name": r.get("company") or r.get("companyName"),
                                          "exchange": ("NSE SME" if r.get("securityType") == "SME" else "NSE"),
                                          "price": r.get("priceRange"), "date": r.get("ipoStartDate"),
                                          "end": r.get("ipoEndDate"), "status": "Closed"})
                        continue
                    in_recent.append({"sym": r.get("symbol"), "name": r.get("company") or r.get("companyName"),
                                      "exchange": ("NSE SME" if r.get("securityType") == "SME" else "NSE"),
                                      "price": r.get("priceRange"), "date": ld})
                in_recent.sort(key=lambda x: dmy(x["date"]), reverse=True)   # newest listing first
                in_closed.sort(key=lambda x: dmy(x["end"]), reverse=True)     # latest close first
            except Exception:
                complete = False
            # open now first (closing soonest first), then the rest by opening date
            in_up.sort(key=lambda x: (0, dmy(x["end"])) if x["status"] == "Active" else (1, dmy(x["date"])))

            out = {"priced": pr[:15], "upcoming": up[:40],
                   "inPriced": in_closed + in_recent[:15], "inUpcoming": in_up[:40]}
            if complete:            # a source that failed is retried on the next request, not cached for hours
                IPO_CACHE = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("ipos %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _earncal(self):
        # Upcoming earnings calendar (next 5 trading days) from Nasdaq, kept to the most notable names by
        # market cap so the dashboard list stays readable. Keyless.
        global EARNCAL_CACHE
        try:
            now = time.time()
            if EARNCAL_CACHE and now - EARNCAL_CACHE[0] < 6 * 3600:
                self._json(200, EARNCAL_CACHE[1]); return
            dt = datetime.datetime.now(IST).date()
            rows, scanned = [], 0
            while scanned < 5:
                if dt.weekday() < 5:   # Mon-Fri
                    try:
                        d = self._nasdaq("https://api.nasdaq.com/api/calendar/earnings?date=" + dt.isoformat()).get("data", {}) or {}
                        for r in (d.get("rows") or []):
                            try:
                                mcn = float(re.sub(r"[^0-9.]", "", r.get("marketCap") or "")) if r.get("marketCap") else 0
                            except Exception:
                                mcn = 0
                            rows.append({"date": dt.isoformat(), "sym": r.get("symbol"), "name": r.get("name"),
                                         "mcap": mcn, "eps": r.get("epsForecast"), "time": r.get("time")})
                    except Exception:
                        pass
                    scanned += 1
                dt += datetime.timedelta(days=1)
            seen, top = set(), []
            for r in sorted(rows, key=lambda x: x["mcap"], reverse=True):   # notable names only
                if r["sym"] and r["sym"] not in seen:
                    seen.add(r["sym"]); top.append(r)
                if len(top) >= 25:
                    break
            top.sort(key=lambda x: (x["date"], -x["mcap"]))                 # present in date order

            # ---- Indian earnings (NSE upcoming board meetings for financial results) ----
            def dmy(s):
                m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", str(s or ""))
                if not m:
                    return (0, 0, 0)
                mon = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
                       "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}.get(m.group(2).lower(), 0)
                return (int(m.group(3)), mon, int(m.group(1)))
            in_earn, inseen = [], set()
            try:
                for r in (nse_get("corporate-board-meetings?index=equities") or []):
                    txt = (r.get("bm_purpose") or "") + " " + (r.get("bm_desc") or "")
                    if not re.search(r"result|financial|dividend|earning", txt, re.I):
                        continue
                    sym = r.get("bm_symbol")
                    if not sym or sym in inseen:
                        continue
                    inseen.add(sym)
                    nm = re.match(r"(.*?)\s+has informed", r.get("bm_desc") or "")
                    in_earn.append({"date": r.get("bm_date"), "sym": sym,
                                    "name": (nm.group(1).strip() if nm else sym)})
                in_earn.sort(key=lambda x: dmy(x["date"]))
            except Exception:
                pass

            out = {"us": top, "in": in_earn[:25]}
            EARNCAL_CACHE = (now, out)
            self._json(200, out)
        except Exception as e:
            LOG.error("earncal %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _proxy_yahoo(self):
        try:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            sym = params.get("symbol", [""])[0]
            rng = params.get("range", ["1d"])[0]
            interval = params.get("interval", ["1d"])[0]
            if not sym:
                self._json(400, {"error": "missing symbol"}); return
            url = (YBASE + urllib.parse.quote(sym)
                   + "?interval=" + urllib.parse.quote(interval)
                   + "&range=" + urllib.parse.quote(rng))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            LOG.error("yahoo %s failed: %s", self.path[:160], _err(e))
            self._json(502, {"error": str(e)})

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, *args):
        pass  # quiet

# Dual-stack (IPv4 + IPv6) so browsers hitting "localhost" (which resolves to ::1 first on Windows)
# connect instantly instead of waiting ~2s for an IPv4-only fallback on every request.
class DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True
    # Own the port exclusively (Windows SO_EXCLUSIVEADDRUSE) instead of socketserver's default
    # SO_REUSEADDR: with REUSEADDR a second `python serve.py 8777` silently co-binds and requests get
    # split between two processes; now the second bind fails loudly (and the exe's _pick_port already
    # steps to a free port). Rebinding right after a restart still works on Windows (verified).
    allow_reuse_address = False
    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception as e:
            LOG.warning("server_bind: IPV6_V6ONLY=0 failed (%s); IPv4 clients may be refused", _err(e))
            pass
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

def _port_free(p):
    # 1) Probe with a socket shaped exactly like the real server (IPv6 dual-stack wildcard). On
    #    Windows a specific-address IPv4 bind does NOT conflict with the server's SO_REUSEADDR
    #    wildcard, so the old probe alone reported 8777 free while an instance held it and a second
    #    instance bound alongside it (two servers, one port). A same-shaped bind does conflict.
    #    If IPv6 dual-stack is unavailable the server itself falls back to IPv4, so skip this step.
    s6 = None
    try:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    except OSError:
        if s6:
            s6.close()
        s6 = None
    if s6 is not None:
        try:
            s6.bind(("::", p))
        except OSError:
            return False                    # conflicts with a live dual-stack listener -> busy
        finally:
            s6.close()
    # 2) IPv4 probes. Measured on Windows: a specific-address bind conflicts only with a specific
    #    listener, and a wildcard bind only with a wildcard one — so probe both shapes. (The
    #    127.0.0.1 probe is the original one; 0.0.0.0 closes a gap the original never covered.)
    for addr in ("127.0.0.1", "0.0.0.0"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((addr, p))
        except OSError:
            return False
        finally:
            s.close()
    return True

def _pick_port(preferred):
    if _port_free(preferred):
        return preferred
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

def loopback_for(srv):
    """The loopback address that reaches `srv` whatever it bound: ::1 for the IPv6 server (dual-stack or not -
    if IPV6_V6ONLY could not be cleared, 127.0.0.1 would be refused), 127.0.0.1 for the IPv4-only fallback."""
    return "::1" if srv.address_family == socket.AF_INET6 else "127.0.0.1"


def warm_start(port, host="127.0.0.1", delay=8.0):
    """Shortly after launch, fetch the Institutions tab's slow data in the background so the tab opens
    filled: the 13F portfolio totals (20 filers' cover pages, ~3.5 s cold) and the congress / executive / contracts
    cards (which in turn start their 12-month lists). It goes through the server's own endpoints, so the
    page shares the same single-flight locks and caches. The delay lets the launch's quote/chart requests
    go first; the calls run one after another, so SEC sees no more than the page itself would send."""
    def go():
        time.sleep(delay)
        for p in ("/api/insttotals", "/api/congress", "/api/exec", "/api/govcon"):
            try:
                urllib.request.urlopen("http://%s:%d%s" % ("[%s]" % host if ":" in host else host, port, p),
                                       timeout=300).read()
            except Exception as e:
                LOG.warning("startup warm-up %s failed: %s", p, _err(e))
    threading.Thread(target=go, daemon=True, name="startup-warm").start()

if __name__ == "__main__":
    explicit = len(sys.argv) > 1                      # `serve.py 8777` -> use exactly that port
    port = PORT if explicit else _pick_port(PORT)     # double-clicked exe -> auto-pick if 8777 is busy
    handler = functools.partial(Handler, directory=BASE_DIR)   # serve the bundled static files
    url = f"http://localhost:{port}/{APP_PAGE}"
    print(f"EQUIDEX — serving on {url}")
    if FROZEN:
        print("Leave this window open while you use EQUIDEX. Close it to stop the server.")
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()   # open the app once the server is up
    try:
        srv = DualStackServer(("::", port), handler)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", port), handler)     # IPv4-only fallback
    warm_start(port, loopback_for(srv))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

