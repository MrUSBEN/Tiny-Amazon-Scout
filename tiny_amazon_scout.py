"""Tiny Amazon Scout - local app. Python 3.8+, no pip installs needed.
  python pricetracker.py          -> opens the UI in your browser
  python pricetracker.py --check  -> runs one price check (what Task Scheduler runs)
Everything (database, settings, reports, logs) lives in this folder."""
import os, sys, re, json, time, sqlite3, datetime as dt, urllib.request as ur, urllib.parse as up
import urllib.error, subprocess, webbrowser, html, random, string
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP = os.path.dirname(os.path.abspath(__file__))
DATA, REP = os.path.join(APP, 'data'), os.path.join(APP, 'reports')
DB, LOG = os.path.join(DATA, 'tracker.db'), os.path.join(DATA, 'run.log')
os.makedirs(DATA, exist_ok=True); os.makedirs(REP, exist_ok=True)
TASK, PORT, UA = 'TinyAmazonScout', 8765, {'User-Agent': 'Mozilla/5.0'}
# Order here = order shown in the UI and the automatic fallback order.
# ScraperAPI: 1,000 free credits/month, an Amazon request costs 5 credits -> about 200 requests.
PROV = {'openwebninja': {'name': 'OpenWeb Ninja', 'limit': 100}, 'scraperapi': {'name': 'ScraperAPI', 'limit': 200},
        'serpapi': {'name': 'SerpApi', 'limit': 250}}
DEF = {'provider': 'openwebninja', 'ntfy_on': '0', 'ntfy_topic': '', 'toast_on': '1', 'keep_days': '30',
       'key_openwebninja': '', 'key_serpapi': '', 'key_scraperapi': '', 'time': '08:00',
       'days': 'Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday', 'retries': '2', 'retry_min': '10',
       'wake': '0', 'notify_mode': 'always', 'pin': '', 'fallback': '1'}
CC = {'in': 'IN', 'com': 'US', 'co.uk': 'GB', 'de': 'DE', 'fr': 'FR', 'it': 'IT', 'es': 'ES', 'ca': 'CA',
      'com.au': 'AU', 'co.jp': 'JP', 'ae': 'AE'}
NOWIN = 0x08000000  # hide console windows


class Skip(Exception):
    """Problem before any API request was made (does not use quota)."""


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.executescript("""create table if not exists products(asin text primary key,domain text,title text,target real,added text);
    create table if not exists prices(id integer primary key,asin text,ts text,price real,cur text,ok int,err text,prov text);
    create table if not exists settings(k text primary key,v text);""")
    try: c.execute('alter table prices add column deliv text')
    except sqlite3.OperationalError: pass
    return c


def cfg():
    c = dict(DEF)
    for r in db().execute('select * from settings'): c[r['k']] = r['v']
    return c


def used(p):
    m = dt.date.today().strftime('%Y-%m')
    return db().execute("select count(*) from prices where prov=? and ts like ?", (p, m + '%')).fetchone()[0]


def parse(line):
    line = line.strip()
    if not line: return None
    if re.match(r'https?://(amzn\.[a-z]+|a\.co)/', line):
        try: line = ur.urlopen(ur.Request(line, headers=UA), timeout=15).geturl()
        except urllib.error.HTTPError as e: line = e.url
        except Exception: return None
    m = re.search(r'(?:/dp/|/gp/product/|/d/)([A-Z0-9]{10})', line)
    asin = m.group(1) if m else (line if re.fullmatch(r'[A-Z0-9]{10}', line) else None)
    if not asin: return None
    d = re.search(r'amazon\.([a-z.]+?)/', line)
    return asin, 'amazon.' + (d.group(1) if d else 'in')


def get_json(url, headers=None):
    try:
        with ur.urlopen(ur.Request(url, headers={**UA, **(headers or {})}), timeout=60) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        raise Exception('API error %s: %s' % (e.code, e.read()[:150].decode('utf8', 'ignore')))


def num(s):
    try: return float(re.sub(r'[^\d.]', '', str(s).replace(',', '')))
    except Exception: return None


def delivery_text(v):
    if isinstance(v, list): v = ' '.join(str(x) for x in v[:2])
    return re.sub(r'\s+', ' ', str(v or '')).strip()[:90]


def find_deliv(d):
    for k, v in d.items():
        if isinstance(k, str) and re.search('deliver|shipping', k, re.I) and v:
            return delivery_text(list(v.values()) if isinstance(v, dict) else v)
    return ''


def call(p, asin, domain, c):
    key, tld = c['key_' + p].strip(), domain[7:]
    if p == 'openwebninja':
        j = get_json('https://api.openwebninja.com/realtime-amazon-data/product-details?' +
                     up.urlencode({'asin': asin, 'country': CC.get(tld, 'US')}), {'x-api-key': key})
        d = j.get('data') or {}
        return d.get('product_title'), num(d.get('product_price')), d.get('currency'), delivery_text(d.get('delivery') or d.get('delivery_message') or d.get('primary_delivery_time'))
    if p == 'scraperapi':
        j = get_json('https://api.scraperapi.com/structured/amazon/product?' + up.urlencode(
            {'api_key': key, 'asin': asin, 'tld': tld, 'country_code': CC.get(tld, 'US').lower()}))
        ps = str(j.get('pricing') or '')
        return j.get('name'), num(ps), re.sub(r'[\d.,\s]', '', ps), find_deliv(j)
    q = {'engine': 'amazon_product', 'asin': asin, 'amazon_domain': domain, 'api_key': key}
    if c['pin'].strip(): q['delivery_zip'] = c['pin'].strip()
    j = get_json('https://serpapi.com/search.json?' + up.urlencode(q))
    if j.get('error'): raise Exception(j['error'])
    r = j.get('product_results') or {}
    return r.get('title'), r.get('extracted_price'), re.sub(r'[\d.,\s]', '', str(r.get('price') or '')), delivery_text(r.get('delivery'))


def fetch(asin, domain, c):
    """Selected provider first; if it has no key or its free quota is used up, fall back to the other (if enabled)."""
    order = [c['provider']] + ([p for p in PROV if p != c['provider']] if c['fallback'] == '1' else [])
    for p in order:
        if c['key_' + p].strip() and used(p) < PROV[p]['limit']:
            return call(p, asin, domain, c) + (p,)
    raise Skip('No provider available: add an API key, or wait for the free monthly limit to reset')


def test_key(p):
    """One real request with your first product (or a sample one) to prove the key works. Uses 1 request of quota."""
    c, con = cfg(), db()
    if not c['key_' + p].strip(): return '%s: no key saved' % PROV[p]['name']
    row = con.execute('select asin,domain from products limit 1').fetchone()
    asin, dom = (row['asin'], row['domain']) if row else ('B08N5WRWNW', 'amazon.com')
    try:
        t, price, cur, dl = call(p, asin, dom, c)
        con.execute('insert into prices(asin,ts,ok,prov) values(?,?,1,?)', ('_test', dt.datetime.now().isoformat(' ', 'seconds'), p)); con.commit()
        return '%s: OK (%s, %s)' % (PROV[p]['name'], (t or 'no title')[:28], money(price, cur))
    except Exception as e:
        return '%s: FAILED - %s' % (PROV[p]['name'], str(e)[:110])


def summary():
    con, out = db(), []
    for p in con.execute('select * from products order by added'):
        rows = con.execute('select * from prices where asin=? order by id', (p['asin'],)).fetchall()
        good = [r for r in rows if r['ok']]
        last = rows[-1] if rows else None
        out.append(dict(asin=p['asin'], domain=p['domain'], title=p['title'] or p['asin'], target=p['target'],
                        url='https://www.%s/dp/%s' % (p['domain'], p['asin']),
                        price=good[-1]['price'] if good else None, cur=good[-1]['cur'] if good else '',
                        prev=good[-2]['price'] if len(good) > 1 else None,
                        lo=min((r['price'] for r in good), default=None), hi=max((r['price'] for r in good), default=None),
                        hist=[r['price'] for r in good][-30:], ts=good[-1]['ts'] if good else None, deliv=good[-1]['deliv'] if good else None,
                        err=last['err'] if last and not last['ok'] else None))
    return out


def money(v, cur=''): return '-' if v is None else ('%s %s' % (cur, format(v, ',.2f')) if cur else format(v, ',.2f'))


def spark(h):
    if len(h) < 2: return ''
    lo, hi = min(h), max(h); span = (hi - lo) or 1
    pts = ' '.join('%.1f,%.1f' % (i * 100 / (len(h) - 1), 24 - (v - lo) / span * 22) for i, v in enumerate(h))
    return '<svg width="100" height="26" viewBox="0 0 100 26"><polyline fill="none" stroke="#0b6e8a" stroke-width="1.8" points="%s"/></svg>' % pts


def delivbadge(d):
    if not d: return '<span style="color:#888">unknown</span>'
    free = 'free' in d.lower()
    return '<b style="color:%s">%s</b><div style="font-size:12px;color:#5b6b78">%s</div>' % ('#1a7f4b' if free else '#b3372d', 'Free' if free else 'Paid', html.escape(d))


def build_report(rows):
    tr = ''
    for r in rows:
        ch = ''
        if r['price'] is not None and r['prev'] is not None and r['price'] != r['prev']:
            d = r['price'] - r['prev']
            ch = '<b style="color:%s">%s %s</b>' % ('#1a7f4b' if d < 0 else '#b3372d', '&#9660;' if d < 0 else '&#9650;', money(abs(d), r['cur']))
        hit = ' <span style="background:#1a7f4b;color:#fff;padding:1px 6px;border-radius:9px;font-size:12px">target hit</span>' if (r['target'] and r['price'] and r['price'] <= r['target']) else ''
        err = '<div style="color:#b3372d;font-size:12px">Last check failed: %s</div>' % html.escape(r['err']) if r['err'] else ''
        tr += '<tr><td><a href="%s">%s</a>%s%s</td><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            r['url'], html.escape(r['title'][:90]), hit, err, money(r['price'], r['cur']), ch or '<span style="color:#888">no change</span>',
            money(r['lo'], r['cur']), money(r['hi'], r['cur']), delivbadge(r['deliv']), spark(r['hist']))
    return ('<!doctype html><meta charset="utf-8"><title>Price report</title><style>body{font:15px Segoe UI,sans-serif;background:#f3f5f7;color:#14202b;margin:0;padding:28px}'
            'table{border-collapse:collapse;width:100%%;background:#fff}td,th{padding:10px 12px;border-bottom:1px solid #e3e8ec;text-align:left;vertical-align:top}th{background:#14202b;color:#fff;font-weight:600}a{color:#0b6e8a}</style>'
            '<h2>Price report &mdash; %s</h2><table><tr><th>Product</th><th>Price now</th><th>Change</th><th>Lowest seen</th><th>Highest seen</th><th>Delivery</th><th>Trend</th></tr>%s</table>'
            % (dt.datetime.now().strftime('%d %b %Y, %H:%M'), tr or '<tr><td colspan=7>No products yet.</td></tr>'))


def prune(days):
    for f in os.listdir(REP):
        m = re.fullmatch(r'(\d{4}-\d\d-\d\d)\.html', f)
        if m and (days <= 0 or (dt.date.today() - dt.date.fromisoformat(m.group(1))).days > days):
            os.remove(os.path.join(REP, f))


def toast(title, text):
    if os.name != 'nt': return
    q = lambda s: s.replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;$n=New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;$n.ShowBalloonTip(10000,'%s','%s',[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep 11;$n.Dispose()" % (q(title), q(text)))
    subprocess.Popen(['powershell', '-NoProfile', '-Command', ps], creationflags=NOWIN)


def notify(title, text, c=None):
    c = c or cfg(); msg = []
    if c['toast_on'] == '1':
        toast(title, text[:250]); msg.append('desktop')
    if c['ntfy_on'] == '1' and c['ntfy_topic'].strip():
        try:
            ur.urlopen(ur.Request('https://ntfy.sh/' + c['ntfy_topic'].strip(), data=text.encode('utf8'),
                                  headers={'Title': title, 'Tags': 'shopping_cart'}), timeout=20); msg.append('ntfy')
        except Exception as e: msg.append('ntfy failed: %s' % e)
    return msg


def check(retries=0):
    c, con, notes = cfg(), db(), []
    now = lambda: dt.datetime.now().isoformat(' ', 'seconds')
    todo = [r['asin'] for r in con.execute('select asin from products')]
    for attempt in range(retries + 1):
        failed = []
        for a in todo:
            p = con.execute('select * from products where asin=?', (a,)).fetchone()
            try:
                t, price, cur, dl, prov = fetch(a, p['domain'], c)
                if price is None: raise Exception('Price not found (out of stock?)')
                con.execute('insert into prices(asin,ts,price,cur,ok,prov,deliv) values(?,?,?,?,1,?,?)', (a, now(), price, cur or '', prov, dl))
                if t: con.execute('update products set title=? where asin=?', (t, a))
            except Skip as e:
                notes.append(str(e)); failed = []; break
            except Exception as e:
                con.execute('insert into prices(asin,ts,ok,err,prov) values(?,?,0,?,?)', (a, now(), str(e)[:200], c['provider']))
                failed.append(a)
            con.commit()
        todo = failed
        if not todo or attempt == retries: break
        time.sleep(int(c['retry_min'] or 10) * 60)
    rows = summary()
    page = build_report(rows)
    for name in ('latest.html', dt.date.today().isoformat() + '.html'):
        open(os.path.join(REP, name), 'w', encoding='utf8').write(page)
    prune(int(c['keep_days'] or 30))
    lines, changed = [], False
    for r in rows:
        s = '%s: %s' % (r['title'][:40], money(r['price'], r['cur']) if r['price'] is not None else (r['err'] or 'no data')[:40])
        if r['price'] is not None and r['prev'] is not None and r['price'] != r['prev']:
            s += ' (%s %s)' % ('down' if r['price'] < r['prev'] else 'up', money(abs(r['price'] - r['prev']), r['cur']))
        if r['target'] and r['price'] is not None and r['price'] <= r['target']: s += ' TARGET HIT'; changed = True
        if r['deliv']: s += ' [%s delivery]' % ('free' if 'free' in r['deliv'].lower() else 'paid')
        changed = changed or r['price'] is None or r['prev'] is None or r['price'] != r['prev']
        lines.append(s)
    text = '\n'.join(lines + notes) or 'No products to check.'
    sent = notify('Amazon price report', text, c) if (c['notify_mode'] == 'always' or changed or notes) else ['skipped (nothing changed)']
    open(LOG, 'a', encoding='utf8').write('%s  checked %d products; notified: %s; %s\n' % (now(), len(rows), sent, notes))
    return dict(text=text, sent=sent)


# ---- Windows Task Scheduler (no admin needed; runs as you) ----
def ps(cmd):
    return subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, text=True, creationflags=NOWIN if os.name == 'nt' else 0)


def sched_status():
    if os.name != 'nt': return {'exists': False, 'msg': 'Scheduling works on Windows only.'}
    r = subprocess.run(['schtasks', '/Query', '/TN', TASK, '/FO', 'LIST'], capture_output=True, text=True, creationflags=NOWIN)
    if r.returncode: return {'exists': False}
    n = re.search(r'Next Run Time:\s*(.+)', r.stdout)
    return {'exists': True, 'next': n.group(1).strip() if n else ''}


DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def sched_create(t, days, wake):
    days = [d for d in days.split(',') if d in DAYS]
    if os.name != 'nt' or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', t or '') or not days:
        return 'Enter a time like 08:00 and pick at least one day (Windows only).'
    pw = sys.executable.replace('python.exe', 'pythonw.exe')
    if not os.path.exists(pw): pw = sys.executable
    q = lambda s: "'" + s.replace("'", "''") + "'"
    cmd = ("Unregister-ScheduledTask -TaskName 'AmazonPriceTracker' -Confirm:$false -ErrorAction SilentlyContinue;"
           "$a=New-ScheduledTaskAction -Execute %s -Argument %s -WorkingDirectory %s;$t=New-ScheduledTaskTrigger -Weekly -DaysOfWeek %s -At %s;"
           "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries %s;"
           "Register-ScheduledTask -TaskName %s -Action $a -Trigger $t -Settings $s -Force | Out-Null") % (
        q(pw), q('"%s" --check' % os.path.abspath(__file__)), q(APP), ','.join(days), q(t), '-WakeToRun' if wake else '', q(TASK))
    r = ps(cmd)
    return r.stderr.strip()[:300] if r.returncode else ''


def sched_remove():
    ps("Unregister-ScheduledTask -TaskName 'AmazonPriceTracker' -Confirm:$false -ErrorAction SilentlyContinue")
    r = ps("Unregister-ScheduledTask -TaskName %s -Confirm:$false" % TASK)
    return r.stderr.strip()[:300] if r.returncode else ''


# ---- web server ----
def state():
    c = cfg()
    keys = {p: ('\u2022\u2022\u2022\u2022' + c['key_' + p][-4:]) if c['key_' + p] else '' for p in PROV}
    s = {k: c[k] for k in DEF if not k.startswith('key_')}
    return dict(products=summary(), settings=s, keys=keys, prov={p: dict(name=v['name'], limit=v['limit'], used=used(p)) for p, v in PROV.items()},
                sched=sched_status(), reports=sorted((f for f in os.listdir(REP) if f.endswith('.html')), reverse=True), folder=APP)


def act(path, b):
    con = db()
    if path == '/api/set_list' or path == '/api/add':
        found = [parse(l) for l in b.get('text', '').splitlines() if l.strip()]
        bad = sum(1 for f in found if not f); found = [f for f in found if f]
        if path == '/api/set_list':
            keep = {a for a, _ in found}
            for r in con.execute('select asin from products').fetchall():
                if r['asin'] not in keep: con.execute('delete from products where asin=?', (r['asin'],))
        for a, d in found:
            con.execute('insert or ignore into products(asin,domain,added) values(?,?,?)', (a, d, dt.datetime.now().isoformat()))
        con.commit(); return {'msg': 'Saved %d link(s)%s.' % (len(found), ', %d line(s) not recognised' % bad if bad else '')}
    if path == '/api/remove':
        con.execute('delete from products where asin=?', (b['asin'],)); con.commit(); return {'msg': 'Removed.'}
    if path == '/api/target':
        con.execute('update products set target=? where asin=?', (num(b['v']), b['asin'])); con.commit(); return {'msg': 'Target saved.'}
    if path == '/api/settings':
        for k, v in b.items():
            if k in DEF and not (k.startswith('key_') and not str(v).strip()):
                con.execute('insert or replace into settings values(?,?)', (k, str(v).strip()))
        con.commit(); return {'msg': 'Settings saved.'}
    if path == '/api/clear_key':
        con.execute('delete from settings where k=?', ('key_' + b['p'],)); con.commit(); return {'msg': 'Key removed.'}
    if path == '/api/check': r = check(); return {'msg': 'Check done. Notified: %s' % (', '.join(r['sent']) or 'nobody (all notifications off)')}
    if path == '/api/test_key':
        ps_ = [k for k in PROV if cfg()['key_' + k].strip()] if b['p'] == 'all' else [b['p']]
        return {'msg': ' | '.join(test_key(k) for k in ps_) or 'No keys saved yet.'}
    if path == '/api/test': return {'msg': 'Test sent via: %s' % (', '.join(notify('Test', 'Price tracker notifications work.')) or 'nothing enabled')}
    if path == '/api/sched_on':
        for k in ('time', 'days', 'wake', 'retries', 'retry_min', 'notify_mode'):
            if k in b: con.execute('insert or replace into settings values(?,?)', (k, str(b[k])))
        con.commit(); e = sched_create(b['time'], b['days'], str(b['wake']) == '1')
        return {'msg': e or 'Task set for %s on %d day(s).' % (b['time'], len(b['days'].split(',')))}
    if path == '/api/sched_off': e = sched_remove(); return {'msg': e or 'Scheduled task removed.'}
    if path == '/api/clear_reports': prune(0); return {'msg': 'Old reports deleted (latest.html kept).'}
    if path == '/api/topic': return {'topic': 'price-' + ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(14))}
    return {'msg': '?'}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, body, ctype='application/json', code=200):
        b = body if isinstance(body, bytes) else body.encode('utf8')
        self.send_response(code); self.send_header('Content-Type', ctype + '; charset=utf-8'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/': return self.send(PAGE, 'text/html')
        if p == '/api/state': return self.send(json.dumps(state()))
        if p.startswith('/r/'):
            f = os.path.join(REP, os.path.basename(p[3:]))
            if os.path.isfile(f): return self.send(open(f, 'rb').read(), 'text/html')
        self.send('Not found', 'text/plain', 404)

    def do_POST(self):
        try:
            b = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
            self.send(json.dumps(act(self.path, b)))
        except Exception as e: self.send(json.dumps({'msg': 'Error: %s' % e}))


PAGE = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tiny Amazon Scout</title>
<style>
:root{--bg:#f3f5f7;--ink:#14202b;--mut:#5b6b78;--line:#dde3e8;--acc:#0b6e8a;--dn:#1a7f4b;--up:#b3372d;--card:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Segoe UI",system-ui,sans-serif}
header{background:var(--ink);color:#fff;padding:14px 28px;display:flex;gap:28px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0;font-weight:600}nav{display:flex;gap:4px}
nav button{background:none;border:0;color:#b9c6d0;padding:8px 14px;font:inherit;cursor:pointer;border-radius:6px}
nav button.on{background:#fff;color:var(--ink);font-weight:600}nav button:focus-visible,button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
main{max-width:1000px;margin:24px auto;padding:0 20px}section{display:none}section.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:18px}
h2{font-size:16px;margin:0 0 10px}p.m,.m{color:var(--mut);font-size:13px}
textarea,input[type=text],input[type=password],input[type=time],input[type=number],select{width:100%;padding:8px 10px;border:1px solid #c4ced6;border-radius:6px;font:inherit;background:#fff}
textarea{min-height:90px}button.b{background:var(--acc);color:#fff;border:0;padding:8px 16px;border-radius:6px;font:inherit;cursor:pointer}button.b.g{background:#e6ecf0;color:var(--ink)}button.b.r{background:#fff;color:var(--up);border:1px solid var(--up)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}.row>.grow{flex:1;min-width:180px}
table{width:100%;border-collapse:collapse}td,th{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}th{font-size:13px;color:var(--mut);font-weight:600}
.dn{color:var(--dn);font-weight:600}.up{color:var(--up);font-weight:600}td input{width:90px;padding:5px 7px}
#toast{position:fixed;bottom:20px;left:20px;background:var(--ink);color:#fff;padding:12px 16px;border-radius:10px;display:none;align-items:center;gap:12px;max-width:min(380px,calc(100% - 40px));box-shadow:0 6px 20px rgba(0,0,0,.25)}#toast .sp{flex:none;width:16px;height:16px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){#toast .sp{animation-duration:3s}}
label.k{display:block;font-weight:600;margin-top:12px}.bar{height:6px;background:#e3e8ec;border-radius:3px;margin-top:4px}.bar i{display:block;height:6px;background:var(--acc);border-radius:3px}
.help h3{font-size:14px;margin:16px 0 4px}.help li{margin:3px 0}code{background:#e9eef2;padding:1px 5px;border-radius:4px}
@media(max-width:640px){td:nth-child(2),th:nth-child(2){display:none}}
</style>
<header><h1>Tiny Amazon Scout</h1><nav id="nav"></nav></header>
<main>
<section id="products"><div class="card"><h2>Add products</h2><p class="m">Paste one or many Amazon links (or ASINs), one per line. amazon.in links are the default.</p>
<textarea id="add" placeholder="https://www.amazon.in/dp/B0XXXXXXXX"></textarea>
<div class="row"><button class="b" onclick="addL()">Add to list</button><button class="b g" onclick="chk()" id="chkb">Check prices now</button><span class="grow"></span><a href="/r/latest.html" target="_blank">Open latest report</a></div></div>
<div class="card"><h2>Your products</h2><div id="plist"></div>
<div class="row"><button class="b g" onclick="toggleEdit()">Edit whole list</button></div>
<div id="edit" style="display:none"><p class="m">One link per line. Saving makes the list match this box: new lines are added, missing ones are removed.</p><textarea id="editta" style="min-height:160px"></textarea><div class="row"><button class="b" onclick="saveList()">Save list</button></div></div></div></section>

<section id="settings"><div class="card"><h2>Price provider</h2><p class="m">Only providers with a recurring free tier are listed. Only the selected provider is used.</p>
<select id="provider" onchange="save({provider:this.value})"></select><div id="keys"></div><div class="row"><button class="b g" onclick="post('/api/test_key',{p:'all'})">Test all saved keys</button><span class="m">Each test uses 1 request from that provider's free quota.</span></div>
<label style="display:block;margin-top:14px"><input type="checkbox" id="fallback" onchange="save({fallback:this.checked?1:0})"> If the selected provider runs out of free requests, switch to the other one automatically (needs both keys)</label>
<div class="row"><label for="pin">Delivery PIN code (used by SerpApi)</label><input type="text" id="pin" style="width:140px" onchange="save({pin:this.value})"></div></div>
<div class="card"><h2>Notifications</h2>
<label><input type="checkbox" id="toast_on" onchange="save({toast_on:this.checked?1:0})"> Windows desktop notification (fully local)</label><br>
<label><input type="checkbox" id="ntfy_on" onchange="save({ntfy_on:this.checked?1:0})"> Phone push via ntfy.sh (no account; goes through ntfy's server)</label>
<div class="row"><input type="text" id="ntfy_topic" class="grow" placeholder="your private topic name" onchange="save({ntfy_topic:this.value})"><button class="b g" onclick="topic()">Generate random topic</button></div>
<div class="row"><button class="b g" onclick="post('/api/test')">Send test notification</button></div></div>
<div class="card"><h2>Reports</h2><div class="row" style="margin-top:0">Keep dated reports for <input type="number" id="keep_days" min="0" style="width:80px" onchange="save({keep_days:this.value})"> days (0 = only latest.html)<span class="grow"></span><button class="b g" onclick="post('/api/clear_reports')">Delete old reports now</button></div></div></section>

<section id="schedule"><div class="card"><h2>Daily schedule</h2><p class="m">Creates a Windows Task Scheduler task that runs the check for you. It runs under your account and needs no admin rights. If your PC was off at that time, it runs as soon as the PC is back on.</p>
<div id="sstat" class="m"></div><div class="row"><label>Time <input type="time" id="time" style="width:130px"></label></div><div class="row" id="days"></div>
<div class="row"><label>If a check fails, retry <input type="number" id="retries" min="0" max="10" style="width:70px"> times, every <input type="number" id="retry_min" min="1" max="120" style="width:70px"> minutes</label></div>
<div class="row"><label>Send notification <select id="notify_mode" style="width:auto"><option value="always">every run</option><option value="changes">only if a price changed, a target is hit, or something failed</option></select></label></div>
<div class="row"><label><input type="checkbox" id="wake"> Wake the PC from sleep to run</label></div>
<div class="row"><button class="b" onclick="sOn()">Create / update task</button><button class="b r" onclick="post('/api/sched_off')">Remove task</button></div></div></section>

<section id="reports"><div class="card"><h2>Reports</h2><p class="m">Saved in <code id="fold"></code></p><div id="rlist"></div></div></section>

<section id="help" class="help"><div class="card"><h2>What this app does</h2><ul>
<li><b>Products</b>: add or remove Amazon links, edit the whole list at once, set an optional target price per item.</li>
<li><b>Target</b> (optional): a price you'd like to pay. When the price falls to or below it, the notification and report say TARGET HIT. <b>Delivery</b> shows free or paid delivery as reported by the provider (PIN code affects it; not every provider returns it).</li>
<li><b>Settings</b>: choose the price provider, enter its API key (use <b>Test</b> to confirm a key works; each test uses 1 request), set up notifications, choose report retention.</li>
<li><b>Schedule</b>: create or remove the daily Windows task from here.</li>
<li><b>Reports</b>: every check writes <code>reports\latest.html</code> plus one dated copy; old ones are auto-deleted.</li>
<li>Everything is stored in this app's folder: <code>data\tracker.db</code> (products, price history, settings incl. API keys), <code>reports\</code>, <code>data\run.log</code>.</li></ul>
<p class="m">API keys are saved as plain text in the local database. Don't share the <code>data</code> folder.</p></div>
<div class="card"><h2>Getting an API key</h2>
<h3>OpenWeb Ninja (Real-Time Amazon Data) - free plan, 100 requests/month</h3><ol><li>Sign up at <code>app.openwebninja.com</code> (no credit card).</li><li>Subscribe to the <b>Real-Time Amazon Data</b> API on the free plan.</li><li>Copy your API key from the dashboard and paste it in Settings.</li></ol>
<h3>ScraperAPI - free plan, 1,000 credits/month</h3><ol><li>Sign up at <code>scraperapi.com</code> and open your dashboard.</li><li>Copy the API key shown there and paste it in Settings.</li></ol><p class="m">An Amazon request costs 5 credits, so the free plan allows about 200 checks a month. The app counts it that way.</p>
<h3>SerpApi - free plan, about 250 searches/month (asks for phone verification at signup)</h3><ol><li>Register at <code>serpapi.com</code> and verify your account.</li><li>Open your dashboard / "Manage API key" page and copy the private key.</li><li>Paste it in Settings.</li></ol>
<p class="m">Free limits are set by the providers and can change; check their pricing pages. The app counts your requests per month and stops before the limit shown in Settings. At 1-2 products per day you use roughly 30-60 requests a month.</p>
<h3>ntfy phone notifications</h3><ol><li>Install the <b>ntfy</b> app on your phone.</li><li>In Settings click "Generate random topic", then subscribe to that exact topic name in the app.</li><li>Keep the topic name private; anyone who knows it can read your alerts.</li></ol></div></section>
</main><div id="toast" role="status" aria-live="polite"><i class="sp" id="sp"></i><span id="tm"></span></div>
<script>
const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let S={};const TABS=['Products','Settings','Schedule','Reports','Help'];
$('nav').innerHTML=TABS.map(t=>`<button data-t="${t.toLowerCase()}" onclick="tab('${t.toLowerCase()}')">${t}</button>`).join('');
function tab(t){document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id==t));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t==t));localStorage.t=t}
function toast(m,busy){const e=$('toast');$('sp').style.display=busy?'block':'none';
$('tm').textContent=busy?m:(/^Error|FAILED|failed/.test(m)?'\u2715 ':'\u2713 ')+m;e.style.display='flex';clearTimeout(window.tt);
if(!busy)window.tt=setTimeout(()=>e.style.display='none',Math.max(3800,m.length*55))}
const LBL={'/api/check':'Checking prices (can take a minute)...','/api/test_key':'Testing API key(s)...','/api/test':'Sending test notification...','/api/sched_on':'Updating scheduled task...','/api/sched_off':'Removing scheduled task...','/api/add':'Adding links...','/api/set_list':'Saving list...','/api/remove':'Removing...','/api/clear_reports':'Deleting old reports...','/api/settings':'Saving...','/api/target':'Saving...','/api/clear_key':'Removing key...'};
async function post(p,b={}){toast(LBL[p]||'Working...',true);try{const r=await(await fetch(p,{method:'POST',body:JSON.stringify(b)})).json();if(r.msg)toast(r.msg);else $('toast').style.display='none';await load();return r}catch(e){toast('Error: '+e.message);return{}}}
const m=(v,c)=>v==null?'-':(c?c+' ':'')+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
async function load(){S=await(await fetch('/api/state')).json();draw()}
function draw(){const P=S.products;
$('plist').innerHTML=P.length?`<table><tr><th>Product</th><th>Store</th><th>Price</th><th>Delivery</th><th>Change</th><th title="Optional. You get a 'TARGET HIT' alert when the price falls to or below this number.">Target</th><th></th></tr>${P.map(p=>{let d=p.price!=null&&p.prev!=null&&p.price!=p.prev?p.price-p.prev:0;
return `<tr><td><a href="${p.url}" target="_blank">${esc(p.title.slice(0,70))}</a>${p.err?`<div class="m up">${esc(p.err)}</div>`:''}</td><td class="m">${p.domain}</td><td><b>${m(p.price,p.cur)}</b></td><td>${dv(p.deliv)}</td><td class="${d<0?'dn':'up'}">${d?(d<0?'&#9660; ':'&#9650; ')+m(Math.abs(d),p.cur):''}</td>
<td><input type="number" value="${p.target??''}" placeholder="none" onchange="post('/api/target',{asin:'${p.asin}',v:this.value})"></td><td><button class="b g" onclick="post('/api/remove',{asin:'${p.asin}'})">Remove</button></td></tr>`}).join('')}</table>`:'<p class="m">No products yet. Paste a link above to start.</p>';
const pv=$('provider');pv.innerHTML=Object.entries(S.prov).map(([k,v])=>`<option value="${k}" ${S.settings.provider==k?'selected':''}>${v.name} (free: ${v.limit}/month)</option>`).join('');
if(!document.activeElement||!document.activeElement.classList.contains('key'))$('keys').innerHTML=Object.entries(S.prov).map(([k,v])=>`<label class="k">${v.name} API key ${S.keys[k]?'<span class="m">(saved '+S.keys[k]+')</span>':''}</label>
<div class="row" style="margin-top:4px"><input type="password" class="key grow" id="key_${k}" placeholder="${S.keys[k]?'Enter a new key to replace':'Paste key here'}"><button class="b g" onclick="save({key_${k}:$('key_${k}').value})">Save</button><button class="b g" onclick="post('/api/test_key',{p:'${k}'})">Test</button>${S.keys[k]?`<button class="b g" onclick="post('/api/clear_key',{p:'${k}'})">Remove</button>`:''}</div>
<div class="m">Used this month: ${v.used} / ${v.limit}</div><div class="bar"><i style="width:${Math.min(100,v.used/v.limit*100)}%"></i></div>`).join('');
$('toast_on').checked=S.settings.toast_on=='1';$('ntfy_on').checked=S.settings.ntfy_on=='1';
if(document.activeElement.id!='ntfy_topic')$('ntfy_topic').value=S.settings.ntfy_topic;$('keep_days').value=S.settings.keep_days;
const sv=(i,v)=>{if(document.activeElement.id!=i)$(i).value=v};['time','retries','retry_min','notify_mode','pin'].forEach(i=>sv(i,S.settings[i]));
$('fallback').checked=S.settings.fallback=='1';$('wake').checked=S.settings.wake=='1';
if(!$('days').children.length)$('days').innerHTML=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].map(d=>`<label><input type="checkbox" value="${d}" ${S.settings.days.includes(d)?'checked':''}> ${d.slice(0,3)}</label>`).join(' ');
const s=S.sched;$('sstat').innerHTML=s.msg?esc(s.msg):s.exists?`<b class="dn">Task is active.</b> Next run: ${esc(s.next||'unknown')}`:'No scheduled task exists.';
$('fold').textContent=S.folder+'\\reports';
$('rlist').innerHTML=S.reports.length?S.reports.map(f=>`<div><a href="/r/${f}" target="_blank">${f=='latest.html'?'Latest report':f}</a></div>`).join(''):'<p class="m">No reports yet. Run a check first.</p>'}
const dv=d=>!d?'<span class="m">unknown</span>':`<b class="${/free/i.test(d)?'dn':'up'}">${/free/i.test(d)?'Free':'Paid'}</b><div class="m">${esc(d)}</div>`;
const save=b=>post('/api/settings',b);
async function addL(){await post('/api/add',{text:$('add').value});$('add').value=''}
async function chk(){$('chkb').disabled=true;await post('/api/check');$('chkb').disabled=false}
function toggleEdit(){const e=$('edit');e.style.display=e.style.display=='none'?'block':'none';$('editta').value=S.products.map(p=>p.url).join('\n')}
async function saveList(){await post('/api/set_list',{text:$('editta').value});$('edit').style.display='none'}
async function topic(){const r=await post('/api/topic');await save({ntfy_topic:r.topic})}
const sOn=()=>post('/api/sched_on',{time:$('time').value,days:[...document.querySelectorAll('#days input:checked')].map(i=>i.value).join(','),wake:$('wake').checked?1:0,retries:$('retries').value,retry_min:$('retry_min').value,notify_mode:$('notify_mode').value});
tab(localStorage.t||'products');load();
</script></html>'''

if __name__ == '__main__':
    if '--check' in sys.argv:
        try: check(int(cfg()['retries'] or 0))
        except Exception as e: open(LOG, 'a').write('%s  ERROR %s\n' % (dt.datetime.now(), e))
        sys.exit()
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    except OSError:
        webbrowser.open('http://127.0.0.1:%d' % PORT); sys.exit()
    webbrowser.open('http://127.0.0.1:%d' % PORT)
    print('Tiny Amazon Scout running at http://127.0.0.1:%d  (close this window to stop)' % PORT)
    srv.serve_forever()
