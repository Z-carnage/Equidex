"""Reported remaining performance obligations from SEC company-concept facts."""
import json
import urllib.error
from order_book import fetch

def select(data):
    rows=[]
    for currency, facts in data.get('units', {}).items():
        if len(currency)!=3 or not currency.isalpha():
            continue
        for fact in facts:
            value=fact.get('val')
            if fact.get('form') not in ('10-K','10-Q','20-F','40-F') or fact.get('start') or not fact.get('end'):
                continue
            if not isinstance(value,(int,float)) or value<0:
                continue
            rows.append(dict(value=value,currency=currency,asOf=fact['end'],filed=fact.get('filed',''),accession=fact.get('accn','')))
    if not rows: return None
    latest=max((r['asOf'],r['filed']) for r in rows)
    rows=[r for r in rows if (r['asOf'],r['filed'])==latest]
    if len({(r['currency'],r['value']) for r in rows})!=1: return None
    return rows[0]

def lookup(symbol,cik_for,headers):
    result=dict(value=None,status='not_found',source='SEC',metric='rpo',symbol=symbol)
    if symbol.endswith(('.NS','.BO')) or symbol.isdigit():
        return dict(result,status='unsupported')
    try:
        cik=cik_for(symbol)
        if not cik: return result
        url='https://data.sec.gov/api/xbrl/companyconcept/CIK'+str(cik).zfill(10)+'/us-gaap/RevenueRemainingPerformanceObligation.json'
        data=json.loads(fetch(url,headers))
        row=select(data)
        if row: result.update(row,status='ok',url=url)
    except urllib.error.HTTPError as exc:
        if exc.code!=404: result['status']='unavailable'
    except Exception:
        result['status']='unavailable'
    return result
