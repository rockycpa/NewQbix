"""QuickBooks Online connection + report sync for the admin Financial Statements page.

Uses only the standard library (urllib), per the project rule of no `requests`.

Environment variables (set in Railway, never committed):
    QBO_CLIENT_ID, QBO_CLIENT_SECRET   Intuit developer app keys
    QBO_ENVIRONMENT                    'production' (default) or 'sandbox'
    QBO_REDIRECT_URI                   defaults to APP_URL + '/admin/qbo/callback'
    FIN_YEAR_START_MONTH               first month of the company's fiscal year (default 1 = January)

Tokens are stored in the qbix_qbo_tokens table. Statement output uses the same JSON
shape as the manual upload, plus account ids so the page can drill to transactions.
"""
import base64
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

AUTH_URL = 'https://appcenter.intuit.com/connect/oauth2'
TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
REVOKE_URL = 'https://developer.api.intuit.com/v2/oauth2/tokens/revoke'
SCOPE = 'com.intuit.quickbooks.accounting'
MINOR_VERSION = '75'

_token_lock = threading.Lock()


class QBOError(Exception):
    pass


def _cfg():
    app_url = os.environ.get('APP_URL', 'http://localhost:5000').rstrip('/')
    env = os.environ.get('QBO_ENVIRONMENT', 'production').strip().lower()
    return {
        'client_id': os.environ.get('QBO_CLIENT_ID', '').strip(),
        'client_secret': os.environ.get('QBO_CLIENT_SECRET', '').strip(),
        'redirect_uri': os.environ.get('QBO_REDIRECT_URI', app_url + '/admin/qbo/callback').strip(),
        'api_base': 'https://sandbox-quickbooks.api.intuit.com' if env == 'sandbox'
                    else 'https://quickbooks.api.intuit.com',
        'year_start': max(1, min(12, int(os.environ.get('FIN_YEAR_START_MONTH', '1') or 1))),
    }


def configured():
    c = _cfg()
    return bool(c['client_id'] and c['client_secret'])


# ── Token storage ─────────────────────────────────────────────────────────────

def init_tables(get_conn):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qbix_qbo_tokens (
                    realm_id VARCHAR(40) PRIMARY KEY,
                    company_name VARCHAR(200),
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    access_expires_at TIMESTAMP NOT NULL,
                    refresh_expires_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()


def _save_tokens(get_conn, realm_id, tok, company_name=None):
    now = datetime.utcnow()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO qbix_qbo_tokens (realm_id, company_name, access_token, refresh_token,
                                             access_expires_at, refresh_expires_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (realm_id) DO UPDATE SET
                    company_name = COALESCE(EXCLUDED.company_name, qbix_qbo_tokens.company_name),
                    access_token = EXCLUDED.access_token, refresh_token = EXCLUDED.refresh_token,
                    access_expires_at = EXCLUDED.access_expires_at,
                    refresh_expires_at = EXCLUDED.refresh_expires_at, updated_at = NOW()
            """, (realm_id, company_name, tok['access_token'], tok['refresh_token'],
                  now + timedelta(seconds=int(tok.get('expires_in', 3600))),
                  now + timedelta(seconds=int(tok.get('x_refresh_token_expires_in', 8640000)))))
        conn.commit()


def get_connection(get_conn):
    """Return the most recently updated connection row as a dict, or None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT realm_id, company_name, access_token, refresh_token,
                                  access_expires_at, refresh_expires_at, updated_at
                           FROM qbix_qbo_tokens ORDER BY updated_at DESC LIMIT 1""")
            r = cur.fetchone()
    if not r:
        return None
    keys = ['realm_id', 'company_name', 'access_token', 'refresh_token',
            'access_expires_at', 'refresh_expires_at', 'updated_at']
    return dict(zip(keys, r))


def delete_connection(get_conn, realm_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM qbix_qbo_tokens WHERE realm_id = %s", (realm_id,))
        conn.commit()


# ── OAuth ─────────────────────────────────────────────────────────────────────

def auth_url(state):
    c = _cfg()
    return AUTH_URL + '?' + urllib.parse.urlencode({
        'client_id': c['client_id'], 'response_type': 'code', 'scope': SCOPE,
        'redirect_uri': c['redirect_uri'], 'state': state})


def _token_request(form):
    c = _cfg()
    basic = base64.b64encode(f"{c['client_id']}:{c['client_secret']}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(), headers={
        'Authorization': 'Basic ' + basic, 'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')[:300]
        raise QBOError(f'QuickBooks token request failed ({e.code}): {detail}')


def exchange_code(get_conn, code, realm_id):
    tok = _token_request({'grant_type': 'authorization_code', 'code': code,
                          'redirect_uri': _cfg()['redirect_uri']})
    _save_tokens(get_conn, realm_id, tok)
    try:
        info = company_info(get_conn)
        _save_tokens(get_conn, realm_id, tok, info.get('CompanyName'))
    except QBOError:
        pass


def revoke(get_conn):
    conn = get_connection(get_conn)
    if not conn:
        return
    c = _cfg()
    basic = base64.b64encode(f"{c['client_id']}:{c['client_secret']}".encode()).decode()
    req = urllib.request.Request(REVOKE_URL, data=json.dumps({'token': conn['refresh_token']}).encode(),
                                 headers={'Authorization': 'Basic ' + basic, 'Accept': 'application/json',
                                          'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # revoke is best-effort; always drop our copy
        print(f'[QBO] revoke failed: {e}')
    delete_connection(get_conn, conn['realm_id'])


def _access_token(get_conn, force_refresh=False):
    with _token_lock:
        conn = get_connection(get_conn)
        if not conn:
            raise QBOError('QuickBooks is not connected.')
        if force_refresh or conn['access_expires_at'] <= datetime.utcnow() + timedelta(seconds=120):
            tok = _token_request({'grant_type': 'refresh_token', 'refresh_token': conn['refresh_token']})
            _save_tokens(get_conn, conn['realm_id'], tok)
            return conn['realm_id'], tok['access_token']
        return conn['realm_id'], conn['access_token']


def api_get(get_conn, path, params=None):
    params = dict(params or {}, minorversion=MINOR_VERSION)
    for attempt in (0, 1):
        realm, token = _access_token(get_conn, force_refresh=attempt == 1)
        url = f"{_cfg()['api_base']}/v3/company/{realm}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token,
                                                   'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            detail = e.read().decode(errors='replace')[:300]
            raise QBOError(f'QuickBooks API error ({e.code}) on {path}: {detail}')
    raise QBOError('QuickBooks authorization failed; reconnect QuickBooks.')


def company_info(get_conn):
    realm, _ = _access_token(get_conn)
    return api_get(get_conn, f'companyinfo/{realm}').get('CompanyInfo', {})


# ── Report parsing (pure functions; unit-testable) ────────────────────────────

def _num(s):
    if s in (None, ''):
        return 0.0
    try:
        return float(str(s).replace(',', ''))
    except ValueError:
        return 0.0


def money_columns(report):
    """Indexes + metadata of the per-period money columns (the trailing Total column is dropped)."""
    cols = report.get('Columns', {}).get('Column', [])
    out = []
    for i, c in enumerate(cols):
        if c.get('ColType') != 'Money':
            continue
        meta = {m.get('Name'): m.get('Value') for m in c.get('MetaData', [])}
        if meta.get('ColKey') == 'total' or (c.get('ColTitle', '').strip().lower() == 'total' and 'StartDate' not in meta):
            continue
        out.append({'index': i, 'title': c.get('ColTitle', ''), 'start': meta.get('StartDate'),
                    'end': meta.get('EndDate')})
    return out


def _total_index(report):
    for i, c in enumerate(report.get('Columns', {}).get('Column', [])):
        meta = {m.get('Name'): m.get('Value') for m in c.get('MetaData', [])}
        if meta.get('ColKey') == 'total' or c.get('ColTitle', '').strip().lower() == 'total':
            return i
    return None


def _vals(coldata, idx):
    return [round(_num(coldata[i].get('value')) if i < len(coldata) else 0.0, 2) for i in idx]


def report_tree(report, idx):
    """Turn QuickBooks report rows into nested nodes: {label, id?, group?, values, own?, children?}.

    A parent account with sub-accounts comes back as a Section whose Header carries the
    parent's own amount; its Summary carries the total including sub-accounts.
    """
    def walk(rows):
        nodes = []
        for r in rows or []:
            if r.get('type') == 'Section' or 'Rows' in r or 'Summary' in r:
                head = (r.get('Header') or {}).get('ColData') or []
                summ = (r.get('Summary') or {}).get('ColData') or []
                label = (head[0].get('value') if head else '') or (summ[0].get('value') if summ else '')
                node = {'label': label.strip(), 'group': r.get('group')}
                if head and head[0].get('id'):
                    node['id'] = head[0]['id']
                kids = walk((r.get('Rows') or {}).get('Row'))
                own = _vals(head, idx) if head else [0.0] * len(idx)
                node['values'] = _vals(summ, idx) if summ else own
                if kids:
                    node['children'] = kids
                    if any(abs(x) > 0.004 for x in own):
                        node['own'] = own
                elif not head:
                    node['summaryOnly'] = True
                nodes.append(node)
            else:
                cd = r.get('ColData') or []
                if not cd:
                    continue
                node = {'label': (cd[0].get('value') or '').strip(), 'values': _vals(cd, idx)}
                if cd[0].get('id'):
                    node['id'] = cd[0]['id']
                nodes.append(node)
        return nodes
    return walk((report.get('Rows') or {}).get('Row'))


def _title(s):
    return s.title() if s.isupper() else s


def build_balance_sheet(report):
    cols = money_columns(report)
    idx = [c['index'] for c in cols]
    tree = [n for n in report_tree(report, idx) if not n.get('summaryOnly')]

    def tidy(n):
        n['label'] = _title(n['label'])
        n.pop('group', None)
        for k in n.get('children', []):
            tidy(k)
        return n
    return {'periods': [c['end'] for c in cols], 'tree': [tidy(n) for n in tree]}


_PL_SECTIONS = {'Income': 'Income', 'COGS': 'Cost of Goods Sold', 'Expenses': 'Expenses',
                'OtherIncome': 'Other Income', 'OtherExpenses': 'Other Expenses'}


def build_income_statement(report, sid, label, start, end):
    cols = money_columns(report)
    idx = [c['index'] for c in cols]
    tix = _total_index(report)
    top = report_tree(report, idx)
    totals = report_tree(report, [tix]) if tix is not None else None

    def to_line(n, t):
        out = {'label': n['label'], 'amount': round(t['values'][0] if t else sum(n['values']), 2),
               'values': n['values']}
        if n.get('id'):
            out['id'] = n['id']
        if n.get('children'):
            out['children'] = [to_line(k, tk) for k, tk in zip(n['children'], (t or {}).get('children') or [None] * len(n['children']))]
            if n.get('own') is not None:
                out['own'] = round(sum(n['own']) if tix is None else
                                   out['amount'] - sum(c['amount'] for c in out['children']), 2)
        return out

    sections, by_group = [], {}
    for i, n in enumerate(top):
        g = n.get('group')
        t = totals[i] if totals else None
        by_group[g] = (n, t)
        if g in _PL_SECTIONS and not n.get('summaryOnly'):
            sec = to_line(n, t)
            sec['label'] = _PL_SECTIONS[g]
            sec.setdefault('children', [])
            sec.pop('id', None)
            sections.append(sec)

    def series(g):
        n = by_group.get(g, (None,))[0]
        return n['values'] if n else [0.0] * len(idx)

    inc, cogs, exp = series('Income'), series('COGS'), series('Expenses')
    oi, oe = series('OtherIncome'), series('OtherExpenses')
    net = series('NetIncome') if 'NetIncome' in by_group else \
        [round(inc[i] - cogs[i] - exp[i] + oi[i] - oe[i], 2) for i in range(len(idx))]
    net_total = by_group['NetIncome'][1]['values'][0] if ('NetIncome' in by_group and by_group['NetIncome'][1]) else round(sum(net), 2)
    return {
        'id': sid, 'label': label, 'start': start, 'end': end, 'netIncome': round(net_total, 2),
        'sections': sections,
        'monthly': {
            'months': [(c['start'] or c['end'] or '')[:7] for c in cols],
            'Income': inc,
            'Operating Expenses': [round(cogs[i] + exp[i], 2) for i in range(len(idx))],
            'Other Expenses': [round(oe[i] - oi[i], 2) for i in range(len(idx))],
            'Net Income': net,
        },
    }


def restate_equity_to_year(bs, monthly_net_by_month, fiscal_start_month, wanted_start_month):
    """If QuickBooks closes the year in a different month than the company does, move the
    difference between Net Income and Retained Earnings so Net Income is year-to-date for
    the company's real year. Total equity does not change. Returns True when restated."""
    if fiscal_start_month == wanted_start_month:
        return False
    eq = next((n for n in bs['tree'][-1].get('children', []) if n['label'].lower() == 'equity'), None)
    if not eq:
        return False
    ni = next((k for k in eq.get('children', []) if k['label'].lower() == 'net income'), None)
    re_ = next((k for k in eq.get('children', []) if k['label'].lower() == 'retained earnings'), None)
    if not ni or not re_:
        return False
    for i, end in enumerate(bs['periods']):
        y, m = int(end[:4]), int(end[5:7])
        start_y = y if m >= wanted_start_month else y - 1
        ytd = 0.0
        for ym, v in monthly_net_by_month.items():
            yy, mm = int(ym[:4]), int(ym[5:7])
            if (yy, mm) >= (start_y, wanted_start_month) and (yy, mm) <= (y, m):
                ytd += v
        diff = round(ytd - ni['values'][i], 2)
        ni['values'][i] = round(ytd, 2)
        re_['values'][i] = round(re_['values'][i] - diff, 2)
    return True


# ── Sync ──────────────────────────────────────────────────────────────────────

def _month_end(d):
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def sync_statements(get_conn, today=None):
    """Pull the balance sheet + income statements from QuickBooks and return the page JSON."""
    today = today or date.today()
    c = _cfg()
    info = company_info(get_conn)
    company = info.get('CompanyName') or 'QuickBooks Company'
    qbo_fy = _month_index(info.get('FiscalYearStartMonth'))
    want = c['year_start']

    ys = date(today.year if today.month >= want else today.year - 1, want, 1)  # current fiscal year start
    pys = date(ys.year - 1, want, 1)
    pye = ys - timedelta(days=1)

    bs_rep = api_get(get_conn, 'reports/BalanceSheet', {
        'start_date': ys.isoformat(), 'end_date': today.isoformat(),
        'summarize_column_by': 'Month', 'accounting_method': 'Accrual'})
    pl_cur = api_get(get_conn, 'reports/ProfitAndLoss', {
        'start_date': ys.isoformat(), 'end_date': today.isoformat(),
        'summarize_column_by': 'Month', 'accounting_method': 'Accrual'})
    pl_prev = api_get(get_conn, 'reports/ProfitAndLoss', {
        'start_date': pys.isoformat(), 'end_date': pye.isoformat(),
        'summarize_column_by': 'Month', 'accounting_method': 'Accrual'})

    bs = build_balance_sheet(bs_rep)
    ytd_label = f"Year to Date ({ys.strftime('%b %-d')} - {today.strftime('%b %-d, %Y')})"
    prev_label = f"Year Ended {pye.strftime('%B %-d, %Y')}"
    cur = build_income_statement(pl_cur, 'ytd', ytd_label, ys.isoformat(), today.isoformat())
    prev = build_income_statement(pl_prev, 'prior', prev_label, pys.isoformat(), pye.isoformat())

    note = None
    if qbo_fy and qbo_fy != want:
        by_month = dict(zip(prev['monthly']['months'], prev['monthly']['Net Income']))
        by_month.update(zip(cur['monthly']['months'], cur['monthly']['Net Income']))
        if restate_equity_to_year(bs, by_month, qbo_fy, want):
            note = (f"QuickBooks has this company's fiscal year starting in "
                    f"{date(2000, qbo_fy, 1).strftime('%B')}. These statements use a year starting in "
                    f"{date(2000, want, 1).strftime('%B')}, so Retained Earnings and Net Income are "
                    f"restated; total equity is unchanged. To fix it at the source, set the first month "
                    f"of the fiscal year in QuickBooks (Settings > Account and settings > Advanced).")

    data = {
        'company': company, 'basis': 'Accrual',
        'fiscalYearStart': date(2000, want, 1).strftime('%B'),
        'source': 'QuickBooks Online (live sync)', 'live': True,
        'syncedAt': datetime.utcnow().isoformat() + 'Z',
        'balanceSheet': bs, 'incomeStatements': [cur, prev],
    }
    if note:
        data['note'] = note
    return data


def _month_index(name):
    if not name:
        return None
    try:
        return datetime.strptime(str(name)[:3], '%b').month
    except ValueError:
        return None


def account_transactions(get_conn, account_id, start, end):
    """Transactions posted to one account between two dates, from the General Ledger report."""
    keys = ['tx_date', 'txn_type', 'doc_num', 'name', 'memo', 'split_acc', 'subt_nat_amount', 'rbal_nat_amount']
    names = {'tx_date': 'date', 'txn_type': 'type', 'doc_num': 'num', 'name': 'name', 'memo': 'memo',
             'split_acc': 'split', 'subt_nat_amount': 'amount', 'rbal_nat_amount': 'balance'}
    rep = api_get(get_conn, 'reports/GeneralLedger', {
        'account': account_id, 'start_date': start, 'end_date': end, 'accounting_method': 'Accrual',
        'columns': ','.join(keys)})
    # Map each returned column to our field name by its ColKey, falling back to the requested order.
    order = []
    for i, c in enumerate(rep.get('Columns', {}).get('Column', [])):
        key = next((m.get('Value') for m in c.get('MetaData') or [] if m.get('Name') == 'ColKey'), None)
        order.append(names.get(key) or (names[keys[i]] if i < len(keys) else 'col%d' % i))
    out, begin = [], {'value': 0.0, 'found': False}

    def walk(rows):
        for r in rows or []:
            if 'Rows' in r:
                walk(r['Rows'].get('Row'))
            elif r.get('type') == 'Data' or 'ColData' in r:
                cd = r.get('ColData') or []
                vals = [x.get('value', '') for x in cd]
                if not any(vals):
                    continue
                row = dict(zip(order, vals))
                if vals[0] == 'Beginning Balance':
                    begin['value'] += _num(row.get('balance') or row.get('amount'))
                    begin['found'] = True
                    continue
                row['amount'] = round(_num(row.get('amount')), 2)
                row.pop('balance', None)
                tid = cd[1].get('id') if len(cd) > 1 else None
                if tid:
                    row['txnId'] = tid
                out.append(row)
    walk((rep.get('Rows') or {}).get('Row'))
    return {'rows': out, 'beginning': round(begin['value'], 2) if begin['found'] else None}
