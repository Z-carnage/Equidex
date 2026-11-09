"""Official OGE electronic PTR ingestion; scanned reports require separate review."""
import concurrent.futures
import datetime
import hashlib
import io
import json
import logging
import pathlib
import re
import threading
import time
import urllib.request

API = 'https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest'
HEADERS = {'User-Agent': 'EQUIDEX public disclosure research client'}
_lock = threading.Lock()
_cache = None
_index = None
_index_at = 0
_index_refresh = threading.Lock()


def _read(url, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url.replace(' ', '%20'), headers=HEADERS), timeout=timeout).read()


def _pdf_report(r):
    """Transaction reports with a public PDF are the only ones fresh_rows can read; request-only filings are not."""
    return 'Transaction' in r.get('type', '') and re.search(r"href=['\"][^'\"]+\.pdf['\"]", r['type'], re.I)


def _saved_index_path():
    import os
    return pathlib.Path(os.environ.get('LOCALAPPDATA') or pathlib.Path.home(), 'EQUIDEX', 'executive-oge-index.json')


def _load_index():
    """The newer of the bundled discovery seed and the last refresh saved on this machine."""
    best = None
    for path in (pathlib.Path(__file__).with_name('executive-oge-index.json'), _saved_index_path()):
        try:
            doc = json.loads(path.read_text())
            if best is None or doc.get('checkedAt', '') > best.get('checkedAt', ''):
                best = doc
        except Exception:
            pass
    return [r for r in (best or {}).get('reports', []) if _pdf_report(r)]


def _refresh_index():
    # OGE's index is ~170 pages in name order (it ignores sort parameters), so a new filing can be on any
    # page. A page that keeps timing out no longer discards the whole refresh: the pages that loaded are
    # merged into the last index, and the refresh is retried on the next cycle.
    global _index, _index_at
    if not _index_refresh.acquire(blocking=False):
        return
    log = logging.getLogger('equidex')
    try:
        def page(start):
            for attempt in range(3):
                try:
                    return json.loads(_read(API + '?start=%d&length=100' % start, timeout=45))
                except Exception as exc:
                    if attempt == 2:
                        log.warning('OGE index page %d failed: %s', start, exc)
                        return None
                    time.sleep(3 * (attempt + 1))
        first = page(0)
        if first is None:
            raise ValueError('OGE index unavailable')
        total = int(first['recordsTotal'])
        if not 0 < total <= 50000:
            raise ValueError('Unexpected OGE index size')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            parts = [first] + list(ex.map(page, range(100, total, 100)))
        failed = sum(p is None for p in parts)
        found = [r for p in parts if p for r in p['data'] if _pdf_report(r)]
        if failed:
            known = {(r['type'], r.get('docDate')) for r in found}
            _index = found + [r for r in (_index or []) if (r['type'], r.get('docDate')) not in known]
            log.warning('OGE index refresh incomplete: %d of %d pages failed; merged with last index, retrying next cycle',
                        failed, len(parts))
            return
        _index = found
        _index_at = time.time()
        try:   # next launch starts from this instead of the bundled seed
            path = _saved_index_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'checkedAt': datetime.datetime.now().isoformat(timespec='seconds'),
                                        'source': API, 'reports': found}))
        except Exception as exc:
            log.warning('OGE index not saved: %s', exc)
    except Exception:
        log.exception('Official OGE index refresh failed; retaining last index')
    finally:
        _index_refresh.release()


def parse_electronic(text, report, url, page):
    """Accept exact electronic table columns, dates and ranges; never repair OCR."""
    columns = None
    pending = None
    rows = []
    def finish():
        if not pending:
            return
        asset, side, date, late, amount, row_id = pending
        bounds = re.fullmatch(r'\$([\d,]+)\s*-\s*\$([\d,]+)', amount.strip())
        if not bounds or not asset:
            return
        low, high = (int(bounds[i].replace(',', '')) for i in (1, 2))
        if low > high:
            return
        names = report['name'].split(',', 1)
        name = (names[1].strip() + ' ' + names[0].strip()) if len(names) == 2 else names[0]
        ticker = re.findall(r'\(([A-Z][A-Z0-9.]{0,5})\)', asset)
        rows.append({'official': name, 'title': report.get('title'), 'agency': report.get('agency'),
                     'traded': date, 'posted': report['docDate'][:10], 'asset': asset,
                     'ticker': ticker[-1] if ticker else None, 'side': side, 'amount': amount.strip(),
                     'amtMin': low, 'amtMax': high, 'late': late, 'former': None,
                     'kind': '278-T', 'verified': False, 'source': 'OGE electronic PTR',
                     'sourceRow': row_id, 'url': url + '#page=' + str(page)})
    for line in text.splitlines():
        if all(k in line for k in ('DESCRIPTION', 'TYPE', 'DATE', 'NOTIFICATION', 'AMOUNT')):
            finish(); pending = None
            columns = [line.index(k) for k in ('DESCRIPTION', 'TYPE', 'DATE', 'NOTIFICATION', 'AMOUNT')]
            continue
        if not columns:
            continue
        if line.strip().startswith(('Endnotes', 'Summary of Contents', 'Privacy Act', 'Public Burden')) or ' - Page ' in line:
            finish(); pending = None; columns = None
            continue
        a, t, d, n, m = columns
        asset, typ, date, late, amount = line[a:t].strip(), line[t:d].strip(), line[d:n].strip(), line[n:m].strip(), line[m:].strip()
        number = line[:a].strip()
        if number.isdigit():
            finish(); pending = None
            if typ not in ('Purchase', 'Sale', 'Exchange') or late not in ('Yes', 'No'):
                continue
            try:
                trade_date = datetime.datetime.strptime(date, '%m/%d/%Y').date()
            except ValueError:
                continue
            if trade_date > datetime.date.today():
                continue
            pending = [asset, {'Purchase': 'buy', 'Sale': 'sell', 'Exchange': 'exchange'}[typ], trade_date.isoformat(), late == 'Yes', amount, int(number)]
        elif pending and not typ and not date and not late and not number:
            if asset:
                pending[0] += ' ' + asset
            if amount.startswith('$'):
                pending[4] += ' ' + amount
    finish()
    return rows


def fresh_rows():
    global _cache, _index
    with _lock:
        if _cache and time.time() - _cache[0] < 1800:
            return _cache[1]
        if _index is None:
            _index = _load_index()
        # Discovery is slow; update in the background while using the last known public PDF URLs.
        if time.time() - _index_at >= 21600:
            threading.Thread(target=_refresh_index, daemon=True).start()
        cutoff = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
        reports = sorted((r for r in _index if r.get('docDate', '')[:10] >= cutoff and 'Transaction' in r.get('type', '') and not r.get('amended')), key=lambda r: r['docDate'], reverse=True)
        jobs = []
        seen = set()
        for r in reports:
            match = re.search(r"href=['\"]([^'\"]+\.pdf)['\"]", r['type'], re.I)
            if not match or 'AMEND' in match[1].upper() or match[1] in seen:
                continue
            url = match[1]
            if not url.startswith('https://extapps2.oge.gov/201/Presiden.nsf/'):
                continue
            seen.add(url); jobs.append((r, url))
        def fetch(job):
            from pypdf import PdfReader
            r, url = job
            try:
                data = _read(url)
                pdf = PdfReader(io.BytesIO(data))
                cover = pdf.pages[0].extract_text() or ''
                # Native Integrity.gov reports only. Scanned OCR can silently corrupt figures.
                if 'electronically signed' not in cover or 'Integrity.gov' not in cover:
                    return hashlib.sha256(data).hexdigest(), []
                rows = [row for i, p in enumerate(pdf.pages, 1) for row in parse_electronic(p.extract_text(extraction_mode='layout') or '', r, url, i)]
                return hashlib.sha256(data).hexdigest(), rows
            except Exception:
                logging.getLogger('equidex').exception('Official executive PTR failed: %s', url)
                return None, []
        rows = []; tables = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            for digest, part in ex.map(fetch, jobs[:20]):
                if not part:
                    continue
                # Reposted copies can have different bytes and URLs but the same full table.
                # Deduplicate whole tables, retaining repeated transactions within each table.
                key = tuple((r['official'], r['sourceRow'], r['traded'], r['asset'], r['side'], r['amtMin'], r['amtMax'], r['late']) for r in part)
                if key in tables:
                    alias = part[0]['url'].split('#', 1)[0]
                    for r in tables[key]:
                        r.setdefault('sourceAliases', []).append(alias)
                else:
                    tables[key] = part
                    rows.extend(part)
        if not rows and _cache:
            return _cache[1]
        _cache = (time.time(), rows)
        return rows
