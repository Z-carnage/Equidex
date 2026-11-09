"""Conservative, keyless company-backlog extraction from exchange/SEC disclosures."""
import datetime
import html
import io
import json
import re
import urllib.parse
import urllib.request

def fetch(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as response:
        data = response.read(12_000_001)
    if len(data) > 12_000_000:
        raise ValueError('Disclosure exceeds extraction size limit')
    return data

def attrs(text):
    return {k.lower(): html.unescape(v) for k, _, v in
            re.findall(r'([\w:.-]+)\s*=\s*([\"\x27])(.*?)\2', text, re.S)}

def sec_backlog(text, url, filed):
    """Only instant, non-dimensional, monetary inline facts explicitly named backlog."""
    contexts = dict(re.findall(r'<(?:\w+:)?context\b[^>]*\bid=[\"\x27]([^\"\x27]+)[\"\x27][^>]*>(.*?)</(?:\w+:)?context>', text, re.I | re.S))
    units = dict(re.findall(r'<(?:\w+:)?unit\b[^>]*\bid=[\"\x27]([^\"\x27]+)[\"\x27][^>]*>(.*?)</(?:\w+:)?unit>', text, re.I | re.S))
    found = []
    for tag, body in re.findall(r'<ix:nonfraction\b([^>]*)>(.*?)</ix:nonfraction>', text, re.I | re.S):
        a = attrs(tag)
        name = a.get('name', '').split(':')[-1].lower()
        if name not in {'backlog', 'totalbacklog', 'orderbacklog', 'totalorderbacklog', 'unfilledorders', 'unfilledorder'}:
            continue
        if a.get('xsi:nil', '').lower() == 'true':
            continue
        context = contexts.get(a.get('contextref'), '')
        if re.search(r'explicitmember|typedmember|<[^>]*segment\b', context, re.I):
            continue
        date = re.search(r'<(?:\w+:)?instant[^>]*>(\d{4}-\d{2}-\d{2})</', context, re.I)
        unit = units.get(a.get('unitref'), '')
        currency = re.search(r'<(?:\w+:)?measure[^>]*>\s*iso4217:([A-Z]{3})\s*</', unit)
        if not date or not currency:
            continue
        number = html.unescape(re.sub('<[^>]+>', '', body)).strip().replace(',', '')
        if not re.fullmatch(r'\d+(?:\.\d+)?', number):
            continue
        # Only ordinary decimal formatting; other iXBRL transformations need their own decoder.
        if a.get('format') and not re.search(r'num-dot-decimal|numdotdecimal', a['format'], re.I):
            continue
        value = float(number) * 10 ** int(a.get('scale', '0'))
        if a.get('sign') == '-' or value < 0:
            continue
        found.append(dict(value=value, currency=currency[1], asOf=date[1], filed=filed,
                          source='SEC', url=url, evidence=html.unescape(re.sub('<[^>]+>', '', body)).strip(), metric='backlog'))
    return choose(found)

def choose(rows):
    if not rows:
        return None
    latest = max(x['asOf'] for x in rows)
    rows = [x for x in rows if x['asOf'] == latest]
    if len({(x['value'], x['currency']) for x in rows}) != 1:
        return None  # conflicting total/standalone/segment figures are not guessed
    return rows[0]

def sec_table_backlog(text, url, filed):
    """Exact total-backlog rows with explicit dollar units and dated table columns."""
    currencies = set(re.findall(r'<(?:\w+:)?measure[^>]*>\s*iso4217:([A-Z]{3})\s*</', text))
    if currencies != {'USD'}:
        return None  # "Dollars" alone cannot distinguish USD from other reporting dollars.
    found = []
    for table in re.findall(r'<table\b.*?</table>', text, re.I | re.S):
        plain = html.unescape(re.sub('<[^>]+>', ' ', table))
        plain = re.sub(r'\s+', ' ', plain)
        if not re.search(r'\(Dollars in (millions|billions)\)', plain, re.I):
            continue
        multiplier = 1e9 if re.search(r'\(Dollars in billions\)', plain, re.I) else 1e6
        header = re.split(r'Total\s+Backlog', plain, flags=re.I)[0]
        dates = re.findall(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b', header)
        if len(dates) != 2:
            continue  # tables with extra/ambiguous period columns require a dedicated parser
        dates = [datetime.datetime.strptime(' '.join(d), '%B %d %Y').date().isoformat() for d in dates]
        for row in re.findall(r'<tr\b.*?</tr>', table, re.I | re.S):
            cells = [re.sub(r'\s+', ' ', html.unescape(re.sub('<[^>]+>', '', c))).strip()
                     for c in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.I | re.S)]
            labels = [c for c in cells if re.fullmatch('Total Backlog', c, re.I)]
            numbers = [c.replace('$', '').replace(',', '').strip() for c in cells
                       if re.fullmatch(r'\$?\s*\d[\d,]*(?:\.\d+)?', c)]
            if len(labels) != 1 or len(numbers) != len(dates):
                continue
            for date, number in zip(dates, numbers):
                found.append(dict(value=float(number)*multiplier, currency='USD', asOf=date,
                                  filed=filed, source='SEC', url=url, evidence='Total Backlog · '+date, metric='backlog'))
    return choose(found)

def prose_backlog(text, url, source, filed):
    """Accept explicit total/consolidated prose, not ambiguous multi-column PDF tables."""
    text = re.sub(r'\s+', ' ', text)
    pattern = (r'\b(?:total|consolidated|our)\s+(?:order\s*book|backlog)\b'
               r'[^.;]{0,90}?(?:₹|INR|Rs\.?|USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)\s*'
               r'(crores?|cr\.?|lakhs?|lacs?|billion|million)\b')
    rows = []
    for match in re.finditer(pattern, text, re.I):
        excerpt = text[max(0, match.start()-110):match.end()+120]
        # Use the date attached to the backlog statement, never the publication date.
        date = re.search(r'(?:as\s+(?:of|at)|at|on)\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})', excerpt, re.I)
        if not date:
            continue
        parsed = None
        for fmt in ('%d %B %Y', '%d %b %Y', '%B %d %Y', '%b %d %Y'):
            try:
                parsed = datetime.datetime.strptime(date[1].replace(',', ''), fmt).date().isoformat()
                break
            except ValueError:
                pass
        if not parsed:
            continue
        number = float(match[1].replace(',', ''))
        unit = match[2].lower().rstrip('.')
        multiplier = 1e7 if unit.startswith('cr') else 1e5 if unit.startswith(('lac', 'lak')) else 1e9 if unit == 'billion' else 1e6
        currency = 'USD' if re.search(r'USD|US\$|\$', match[0]) else 'INR'
        rows.append(dict(value=number*multiplier, currency=currency, asOf=parsed, filed=filed,
                         source=source, url=url, evidence=match[0], metric='backlog'))
    return choose(rows)

def pdf_backlog(data, url, source, filed):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    rows = []
    for i, page in enumerate(reader.pages[:80]):
        row = prose_backlog(page.extract_text() or '', url, source, filed)
        if row:
            row['page'] = i + 1
            rows.append(row)
    return choose(rows)

def lookup(symbol, cik_for, nse_get, sec_headers):
    errors, rows = [], []
    inr = symbol.endswith(('.NS', '.BO')) or symbol.isdigit()
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bseindia.com/'}
    if not inr:
        try:
            cik = cik_for(symbol)
            if not cik:
                return {'value': None, 'status': 'not_found', 'sources': ['SEC'], 'errors': []}
            sub = json.loads(fetch('https://data.sec.gov/submissions/CIK'+cik+'.json', sec_headers))
            recent = sub.get('filings', {}).get('recent', {})
            checked = 0
            for i, form in enumerate(recent.get('form', [])):
                if form not in ('10-K', '10-Q', '20-F', '40-F'):
                    continue
                acc = recent['accessionNumber'][i].replace('-', '')
                url = 'https://www.sec.gov/Archives/edgar/data/'+str(int(cik))+'/'+acc+'/'+recent['primaryDocument'][i]
                text = fetch(url, sec_headers).decode('utf-8', 'replace')
                row = sec_backlog(text, url, recent['filingDate'][i]) or sec_table_backlog(text, url, recent['filingDate'][i])
                if row:
                    rows.append(row)
                    break
                checked += 1
                if checked >= 2:
                    break
        except Exception as exc:
            errors.append({'source': 'SEC', 'message': str(exc)[:160]})
        sources = ['SEC']
    else:
        bare = re.sub(r'\.(NS|BO)$', '', symbol)
        docs = []
        try:
            today = datetime.date.today()
            params = dict(index='equities', symbol=bare, from_date=(today-datetime.timedelta(days=365)).strftime('%d-%m-%Y'), to_date=today.strftime('%d-%m-%Y'))
            ann = nse_get('corporate-announcements?'+urllib.parse.urlencode(params))
            docs.extend((a.get('attchmntFile'), 'NSE', a.get('an_dt', '')) for a in ann
                        if re.search(r'investor presentation|financial results|annual report', a.get('desc', ''), re.I))
        except Exception as exc:
            errors.append({'source': 'NSE', 'message': str(exc)[:160]})
        try:
            # Resolve exchange identifier exactly; never match a company by a fuzzy name.
            if bare.isdigit():
                code = bare
            else:
                listing = json.loads(fetch('https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active', headers))
                code = next((str(x['SCRIP_CD']) for x in listing if str(x.get('scrip_id', x.get('SCRIP_ID', ''))).upper() == bare), None)
            if code:
                today = datetime.date.today()
                params = dict(pageno=1, strCat='-1', subcategory='-1', strPrevDate=(today-datetime.timedelta(days=200)).strftime('%Y%m%d'), strToDate=today.strftime('%Y%m%d'), strSearch='P', strscrip=code, strType='C')
                result = json.loads(fetch('https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?'+urllib.parse.urlencode(params), headers))
                for a in result.get('Table', []):
                    if re.search(r'investor presentation|financial results|annual report', str(a.get('SUBCATNAME', ''))+' '+str(a.get('NEWSSUB', '')), re.I) and a.get('ATTACHMENTNAME'):
                        docs.append(('https://www.bseindia.com/xml-data/corpfiling/AttachLive/'+a['ATTACHMENTNAME'], 'BSE', a.get('NEWS_DT', '')))
        except Exception as exc:
            errors.append({'source': 'BSE', 'message': str(exc)[:160]})
        # Bound work, preserve both exchanges rather than starving BSE behind NSE documents.
        for source in ('NSE', 'BSE'):
            for url, _, filed in [d for d in docs if d[1] == source][:3]:
                host = urllib.parse.urlparse(url or '').hostname or ''
                if host not in ('nsearchives.nseindia.com', 'archives.nseindia.com', 'www.bseindia.com'):
                    continue
                try:
                    row = pdf_backlog(fetch(url, headers), url, source, filed)
                    if row:
                        rows.append(row)
                        break
                except Exception as exc:
                    errors.append({'source': source, 'message': str(exc)[:160]})
        sources = ['NSE', 'BSE']
    result = choose(rows)
    return dict(result or {'value': None}, status='ok' if result else 'unavailable' if errors else 'not_found', sources=sources, errors=errors)
