// Enrich the existing screener candidate page; never imply a full-market rpo index.
const screenFundCache = new Map();
async function screenFundamentals(rows, bounds, region, paint) {
  const currency = region === 'in' ? 'INR' : 'USD';
  const request = async url => {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 110000);
    try { const r = await fetch(url, {signal: controller.signal}); if (!r.ok) return {}; return await r.json(); }
    catch (_) { return {}; } finally { clearTimeout(timer); }
  };
  const matches = row => ['fcf','rpo'].every(key => {
    const value=key==='fcf'?row.fcf:row.rpo?.value;
    const ccy=key==='fcf'?row.fcfCurrency:row.rpo?.currency;
    return ['min','max'].every(side=>bounds[key+'_'+side]==null ||
      (typeof value==='number' && Number.isFinite(value) && ccy===currency &&
       (side==='min'?value>bounds[key+'_'+side]:value<bounds[key+'_'+side])));
  });
  let next = 0, completed = 0;
  await Promise.all(Array.from({length: Math.min(3, rows.length)}, async () => {
    while (next < rows.length) {
      const row = rows[next++], key = row.symbol;
      let entry = screenFundCache.get(key);
      if (!entry || Date.now() - entry.at > 600000) {
        const [f,b] = await Promise.all([request('/api/yfund?symbol=' + encodeURIComponent(key) + '&fields=fcf'), region === 'in' ? request('/api/order-announcements?symbol=' + encodeURIComponent(key)) : {}]);
        entry = {at: Date.now(), fcf: f.fcf, fcfCurrency: f.fcfCurrency, rpo: b};
        screenFundCache.set(key, entry);
      }
      Object.assign(row, entry);
      completed++;
      if (paint) paint(rows.filter(matches),completed,rows.length);
    }
  }));
  return rows.filter(matches);
}
function screenFundMoney(value, currency) {
  if (typeof value !== 'number' || !Number.isFinite(value) || !currency) return '—';
  const unit = currency === 'INR' ? 1e7 : 1e9;
  return (currency === 'INR' ? '₹' : currency === 'USD' ? '$' : currency + ' ') +
    (value / unit).toLocaleString('en-US', {maximumFractionDigits: 2}) + (currency === 'INR' ? ' Cr' : 'B');
}
