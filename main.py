import csv, json, logging, os, re, sys, time, hashlib
import requests
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse, urldefrag
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'; LOGS=ROOT/'run_logs'
DATA.mkdir(exist_ok=True); LOGS.mkdir(exist_ok=True)
NOW=datetime.now(timezone.utc); IST=NOW.astimezone(ZoneInfo('Asia/Kolkata')); TODAY=IST.strftime('%Y-%m-%d')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOGS/f'florida-{TODAY}.log', encoding='utf-8')])
log=logging.getLogger('florida')
with open(ROOT/'config.json',encoding='utf-8') as f: CFG=json.load(f)
STATE=CFG.get('state','Florida'); SOURCE=CFG.get('source_url','https://vendor.myfloridamarketplace.com/')
SEARCH=CFG.get('search_url','https://vendor.myfloridamarketplace.com/search/bids')
KEYWORDS=[str(x).strip() for x in CFG.get('keywords',[]) if str(x).strip()]
DELAY=max(.5,float(CFG.get('request_delay_seconds',2))); CAP=min(100,max(1,int(CFG.get('max_details_per_run',100))))
SHEET_ID=CFG.get('spreadsheet_id',''); TRACKER_GID=int(CFG.get('worksheet_id',1466561086)); REVIEW_GID=int(CFG.get('manual_review_worksheet_id',53817621))
CORE=['Opportunity ID','Date Found','State','Agency','Opportunity Title','Solicitation / Reference No.','Opportunity Type','Service Category','Official Source URL','Published Date','Opportunity Open Date & Time','Deadline Date','Deadline Time / Time Zone','Closing Date & Time (Source Time Zone)','Estimated Value (if published)','Scope Summary','Eligibility / Mandatory Requirements','Required Certifications / Experience','Contract Duration / Options','Incumbent / Prior Award (verified only)','Healthcare IT Fit (0-25)','Eligibility & Compliance (0-20)','Customer / Contract Fit (0-15)','Delivery Capability (0-15)','Deadline Feasibility (0-10)','Commercial Attractiveness (0-10)','Information Completeness (0-5)','Total Score (0-100)','Mandatory Gate (Pass / Fail / Unknown)','Recommendation (Go / Investigate / No-Go)','Fit Rationale','Risks / Gaps','Next Action','Last Verified Date','Owner','Review Status','Reviewer Notes','Opportunity Contact Name','Opportunity Contact Phone','Opportunity Contact Email','Opportunity Contact Address','Contact Source','Contact Verification Status']
EXTRA=['Portal Advertisement ID','Advertisement Status','Matched Keywords','Keyword Match Count','First Seen Date','Last Seen Date','Last Source Update','Record Fingerprint','Scrape / Extraction Status','Detail Verification Status','Source Document URLs','Contact Extraction Status']
REVIEW=['Date Detected','Keyword','Solicitation ID','Opportunity Title','Official Source URL','Failure Stage','Failure Reason','Retry Status','Last Attempt']

def readj(path, default):
    try: return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception: return default

def writej(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(path)
def writecsv(path,rows,headers):
    cols=list(headers)
    for r in rows:
        for k in r:
            if k not in cols: cols.append(k)
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore'); w.writeheader(); w.writerows(rows)
def clean(x): return re.sub(r'\s+',' ',str(x or '')).strip()
def urlnorm(u):
    if not u: return ''
    u,_=urldefrag(urljoin(SOURCE,str(u).strip())); p=urlparse(u)
    return u.rstrip('/') if p.scheme in ('http','https') and p.path not in ('','/') else (f'{p.scheme}://{p.netloc}/' if p.scheme in ('http','https') else '')
def fp(x): return hashlib.sha256('|'.join([clean(x.get('solicitation_id')).lower(),clean(x.get('title')).lower(),urlnorm(x.get('url')).lower()]).encode()).hexdigest()[:20]
def matches(x):
    """Conservative healthcare-IT matching; agency names alone never qualify.

    Search only the opportunity title, procurement scope/description, commodity,
    and category. Exclude agency metadata and the contact/footer boilerplate, which
    previously caused unrelated bids to match Florida health-agency keywords.
    """
    title = clean(x.get('title', ''))
    description = clean(x.get('description', '') or x.get('text', ''))
    # Remove known page boilerplate/contact blocks before testing scope.
    description = re.split(r'Please direct all questions to\s*:', description, flags=re.I)[0]
    description = re.split(r'Copyright\s*©|Copyright\s+20\d\d|All rights reserved', description, flags=re.I)[0]
    description = re.split(r'Downloadable Files for Advertisement|Click here to view the original advertisement', description, flags=re.I)[0]
    scope = ' '.join([
        description,
        clean(x.get('commodity', '')),
        clean(x.get('category', '')),
    ])
    # Only technical / system / data phrases qualify. Agency names are deliberately
    # not searched, and generic words such as "health", "technology", or "services"
    # are never sufficient on their own.
    found = []
    for keyword in KEYWORDS:
        kw = clean(keyword)
        low = kw.casefold()
        if len(low) < 6 or low in {
            'healthcare', 'health care', 'medical', 'technology', 'services',
            'florida agency for health care administration',
            'agency for health care administration', 'florida department of health',
            'florida medicaid', 'integrated eligibility'
        }:
            continue
        # Phrase boundaries prevent e.g. short terms matching inside unrelated words.
        pattern = r'(?<![a-z0-9])' + re.escape(low) + r'(?![a-z0-9])'
        # A specific technical phrase in the title is strong evidence. In scope text,
        # require an explicit health/clinical/Medicaid/public-health context as well.
        in_title = bool(re.search(pattern, title, re.I))
        in_scope = bool(re.search(pattern, scope, re.I))
        if in_title or (in_scope and re.search(
            r'\b(health|healthcare|health care|clinical|patient|hospital|medical|medicaid|public health|immunization|disease surveillance)\b',
            scope, re.I
        )):
            found.append(kw)
    return list(dict.fromkeys(found))

def label(text, names):
    text=clean(text)
    for n in names:
        m=re.search(rf'{re.escape(n)}\s*[:\-]\s*(.+?)(?=\s+[A-Z][A-Za-z /()&-]{{2,35}}\s*[:\-]|$)',text,re.I)
        if m: return clean(m.group(1))
    return ''
def listing(title, href, text):
    sid=''
    m=re.search(r'\b([A-Z0-9][A-Z0-9._/-]{3,})\b',title)
    if m and re.search(r'\d',m.group(1)): sid=m.group(1)
    return {'title':clean(title),'url':urlnorm(href),'text':clean(text),'description':clean(text),'solicitation_id':sid,'agency':label(text,['Agency','Organization','Department']),'published_date':label(text,['Posted Date','Publish Date','Date Posted','Published']),'deadline_date':label(text,['Responses Due','Due Date','Closing Date','Deadline','End Date']),'opportunity_type':label(text,['Advertisement Type','Solicitation Type','Type'])}
def get_candidates(page):
    """Read result rows (including rows whose links do not contain bid keywords), then useful links."""
    raw = page.evaluate("""() => {
      const out = [], seen = new Set();
      const add = (title, href, text, source) => {
        title = (title || '').replace(/\\s+/g, ' ').trim();
        text = (text || '').replace(/\\s+/g, ' ').trim(); href = href || '';
        const key = href + '|' + title + '|' + text;
        if (title.length >= 4 && title.length < 500 && text.length >= 4 && !seen.has(key)) {
          seen.add(key); out.push({title, href, text, source});
        }
      };
      for (const tr of document.querySelectorAll('table tr, [role="row"]')) {
        const text = (tr.innerText || tr.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!text) continue;
        const links = [...tr.querySelectorAll('a[href]')];
        const link = links.find(a => (a.innerText || a.textContent || '').trim());
        const cells = [...tr.querySelectorAll('td,th,[role="cell"],[role="gridcell"]')];
        const title = link ? (link.innerText || link.textContent || '').trim() :
          cells.map(c => (c.innerText || c.textContent || '').trim()).find(Boolean) || '';
        add(title, link ? link.href : location.href, text, 'row');
      }
      for (const a of document.querySelectorAll('a[href]')) {
        const title = a.innerText || a.textContent || '';
        const box = a.closest('tr,[role="row"],article,li') || a.parentElement;
        add(title, a.href, (box && (box.innerText || box.textContent)) || title, 'link');
      }
      return out;
    }""")
    out = []
    for item in raw:
        u = urlnorm(item.get('href', ''))
        host = urlparse(u).netloc.lower()
        title, body = clean(item.get('title')), clean(item.get('text'))
        if not u or not (host.endswith('myfloridamarketplace.com') or host.endswith('myflorida.com')):
            continue
        if title.lower() in {'home','login','register','search advertisements','search vendors','help','contact us'}:
            continue
        if item.get('source') == 'row' or re.search(r'advert|solicit|bid|request|proposal|contract|purchase|rfp|rfq|itb|itn|rfi|due date|open', title + ' ' + body, re.I):
            rec = listing(title, u, body)
            rec['detail_verified'] = 'Extracted from portal result row/link'
            out.append(rec)
    unique = {}
    for item in out:
        unique.setdefault((item.get('url','').lower(), item.get('title','').lower()), item)
    return list(unique.values())

def open_detail(page,x):
    try:
        page.goto(x['url'],wait_until='domcontentloaded',timeout=25000); page.wait_for_timeout(400); body=clean(page.locator('body').inner_text(timeout=8000))
        if len(body)>len(x.get('text','')): x['text']=x['description']=body[:30000]
        if not x.get('solicitation_id'): x['solicitation_id']=label(body,['Advertisement Number','Solicitation Number','Solicitation ID','Number'])
        x['agency']=x.get('agency') or label(body,['Agency','Organization','Department'])
        x['published_date']=x.get('published_date') or label(body,['Posted Date','Publish Date','Date Posted','Published'])
        x['deadline_date']=x.get('deadline_date') or label(body,['Responses Due','Due Date','Closing Date','Deadline','End Date'])
        x['opportunity_type']=x.get('opportunity_type') or label(body,['Advertisement Type','Solicitation Type','Type']); x['detail_verified']='Visited detail page'
    except Exception as e: x['detail_verified']=f'Failed: {e}'
    return x
def extract_contact_from_text(text):
    """Extract the opportunity's own contact block from its official detail page."""
    text = clean(text)
    # MFMP detail pages explicitly introduce the owner/contact block this way.
    m = re.search(r'Please direct all questions to\s*:\s*(.*?)(?=Click here to view the original advertisement|Downloadable Files for Advertisement|Copyright ©|$)', text, re.I)
    block = m.group(1) if m else ''
    if not block:
        return {}
    def field(names):
        for name in names:
            hit = re.search(rf'{name}\s*:\s*(.*?)(?=\s+(?:Name|Phone|Address|Email)\s*:|$)', block, re.I)
            if hit:
                value = clean(hit.group(1))
                if value:
                    return value
        return ''
    name = field(['Name'])
    phone = field(['Phone'])
    address = field(['Address'])
    email = field(['Email'])
    em = re.search(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', block, re.I)
    if em:
        email = em.group(0)
    ph = re.search(r'\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}', block)
    if ph:
        phone = ph.group(0)
    return {'contact_name':name, 'contact_phone':phone, 'contact_email':email, 'contact_address':address,
            'contact_source':'Official MyFloridaMarketPlace opportunity detail page' if any((name,phone,email,address)) else '',
            'contact_verification_status':'Extracted from official opportunity page' if any((name,phone,email,address)) else 'Not published / not found on official page'}

def enrich_opportunity_contacts(rows, limit):
    """Visit official opportunity pages and extract the contact specified on each listing."""
    targets = [x for x in rows if x.get('url')][:limit]
    if not targets:
        return {'attempted':0,'visited':0,'contacts_found':0,'errors':[]}
    stats={'attempted':len(targets),'visited':0,'contacts_found':0,'errors':[]}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            for i, x in enumerate(targets, 1):
                try:
                    page.goto(x['url'], wait_until='domcontentloaded', timeout=30000)
                    page.wait_for_timeout(350)
                    body = clean(page.locator('body').inner_text(timeout=10000))
                    stats['visited'] += 1
                    # Prefer the full detail-page text over the API's short metadata fallback.
                    if len(body) > len(clean(x.get('description'))):
                        x['description'] = body[:30000]
                        x['text'] = body[:30000]
                    contact = extract_contact_from_text(body)
                    x.update({k:v for k,v in contact.items() if v})
                    if any(x.get(k) for k in ('contact_name','contact_phone','contact_email','contact_address')):
                        stats['contacts_found'] += 1
                    # Pick up official downloadable document URLs, if linked from the page.
                    try:
                        links = page.locator('a[href]').evaluate_all("els => els.map(a => ({text:(a.innerText||a.textContent||'').trim(), href:a.href}))")
                        docs=[]
                        for a in links:
                            label=(a.get('text') or '').lower(); href=a.get('href') or ''
                            if href.startswith('http') and (any(ext in href.lower() for ext in ('.pdf','.doc','.docx','.xls','.xlsx','.zip')) or any(w in label for w in ('download','solicitation document','attachment','original advertisement'))):
                                docs.append(href)
                        if docs: x['document_urls'] = '; '.join(dict.fromkeys(docs))
                    except Exception:
                        pass
                except Exception as exc:
                    stats['errors'].append({'id':x.get('portal_id') or x.get('solicitation_id'),'error':str(exc)[:300]})
                    x.setdefault('contact_verification_status','Detail page visit failed; contact not verified')
                if i % 10 == 0:
                    log.info('Opportunity detail/contact enrichment: %d/%d pages visited', i, len(targets))
                time.sleep(DELAY)
            browser.close()
    except Exception as exc:
        stats['errors'].append({'error':'Browser initialization failed: '+str(exc)[:300]})
    return stats

def reviewrow(x,kw,stage,reason): return {'Date Detected':TODAY,'Keyword':kw or '(no keyword matched)','Solicitation ID':x.get('solicitation_id',''),'Opportunity Title':x.get('title',''),'Official Source URL':x.get('url',''),'Failure Stage':stage,'Failure Reason':str(reason)[:1000],'Retry Status':'Pending','Last Attempt':IST.strftime('%Y-%m-%d %H:%M:%S %Z')}
def sheetrow(x,old=None):
    old=old or {}; ms=x.get('matched_keywords',[]); u=urlnorm(x.get('url')); sid=clean(x.get('solicitation_id')); pid=clean(x.get('portal_id')) or (sid if sid.isdigit() else ''); ident=pid or sid or u or fp(x)
    r={h:'' for h in CORE+EXTRA}; r.update(old)
    vals={'Opportunity ID':ident,'Date Found':old.get('Date Found') or TODAY,'State':STATE,'Agency':clean(x.get('agency')),'Opportunity Title':clean(x.get('title')),'Solicitation / Reference No.':sid,'Opportunity Type':clean(x.get('opportunity_type')),'Service Category':', '.join(ms[:5]),'Official Source URL':u,'Published Date':clean(x.get('published_date')),'Opportunity Open Date & Time':clean(x.get('open_datetime') or x.get('opening_datetime')),'Deadline Date':clean(x.get('deadline_date')),'Deadline Time / Time Zone':clean(x.get('deadline_time_timezone')),'Closing Date & Time (Source Time Zone)':clean(x.get('closing_datetime') or x.get('deadline_date')),'Scope Summary':clean(x.get('description'))[:1500],'Last Verified Date':TODAY,'Portal Advertisement ID':pid,'Matched Keywords':', '.join(ms),'Keyword Match Count':len(ms),'First Seen Date':old.get('First Seen Date') or x.get('first_seen_date') or TODAY,'Last Seen Date':TODAY,'Last Source Update':TODAY,'Record Fingerprint':fp(x),'Scrape / Extraction Status':('Listing extracted; keyword matched' if ms else 'Valid listing; no configured keyword matched'),'Detail Verification Status':x.get('detail_verified','Not visited'),'Source Document URLs':clean(x.get('document_urls')),'Opportunity Contact Name':clean(x.get('contact_name')),'Opportunity Contact Phone':clean(x.get('contact_phone')),'Opportunity Contact Email':clean(x.get('contact_email')),'Opportunity Contact Address':clean(x.get('contact_address')),'Contact Source':clean(x.get('contact_source')),'Contact Verification Status':clean(x.get('contact_verification_status') or 'Not checked'),'Contact Extraction Status':('Contact found' if any(x.get(k) for k in ('contact_name','contact_phone','contact_email','contact_address')) else 'Not found on official detail page / page not checked')}
    r.update({k:v for k,v in vals.items() if v!=''})
    if not r.get('Mandatory Gate'): r['Mandatory Gate'] = 'Unknown'
    if not r.get('Recommendation'): r['Recommendation'] = 'Investigate'
    if not r.get('Review Status'): r['Review Status'] = 'Pending Review'
    if not r.get('Next Action'): r['Next Action']='Review official advertisement and download solicitation documents'
    if not r.get('Risks / Gaps'): r['Risks / Gaps']='Confirm eligibility, scope, deadline and mandatory requirements from official documents'
    return r
def open_sheets():
    raw=os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON','').strip()
    if not raw: raise RuntimeError('Missing GOOGLE_SERVICE_ACCOUNT_JSON GitHub secret')
    if not SHEET_ID: raise RuntimeError('Add spreadsheet_id to config.json')
    info=json.loads(raw); creds=Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive']); gc=gspread.authorize(creds); book=gc.open_by_key(SHEET_ID)
    tracker=next((w for w in book.worksheets() if w.id==TRACKER_GID),None); rev=next((w for w in book.worksheets() if w.id==REVIEW_GID),None)
    if tracker is None: raise RuntimeError(f'Tracker tab gid {TRACKER_GID} not found')
    return tracker,rev
def ensure_headers(ws, needed):
    """Keep existing headers, add missing headers, and expand the worksheet first."""
    from gspread.utils import rowcol_to_a1
    values = ws.get_all_values()
    current = list(values[0]) if values and values[0] else []
    while current and not clean(current[-1]):
        current.pop()
    headers = list(current)
    for header in needed:
        if header not in headers:
            headers.append(header)
    if ws.col_count < len(headers):
        ws.add_cols(len(headers) - ws.col_count)
    if not current or headers != current:
        ws.update(
            range_name=f"A1:{rowcol_to_a1(1, len(headers))}",
            values=[headers],
            value_input_option="USER_ENTERED",
        )
    return headers

def keyrow(r):
    # Portal advertisement ID is the most stable unique key; solicitation reference
    # can be reused or formatted differently by agencies.
    sid = clean(r.get('Portal Advertisement ID')) or clean(r.get('Solicitation / Reference No.'))
    u = urlnorm(r.get('Official Source URL'))
    return 'id:' + sid.casefold() if sid else ('url:' + u.casefold() if u else '')
def with_retry(fn, attempts=6):
    """Retry Google API calls on quota/rate-limit/transient errors."""
    delay = 2
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            msg = str(exc).lower()
            retryable = any(t in msg for t in ('429', 'quota', 'rate limit', '500', '502', '503', '504', 'timed out'))
            if not retryable or attempt == attempts - 1:
                raise
            log.warning('Google Sheets transient error (attempt %s/%s): %s; retrying in %ss', attempt + 1, attempts, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)

def sync(rows, reviews):
    """Upsert matching opportunities and append only genuine data exceptions to Manual Review.

    All cell changes are sent in a small number of batch requests to avoid per-cell write quota.
    """
    from gspread.utils import rowcol_to_a1
    tracker, reviewws = open_sheets()
    headers = with_retry(lambda: ensure_headers(tracker, CORE + EXTRA))
    vals = with_retry(lambda: tracker.get_all_values())
    # Never delete existing opportunities automatically. Relevance changes and partial
    # API runs must not erase previously collected rows or user annotations.
    seen_sheet_keys = set()
    oldmap, rownum = {}, {}
    for i, v in enumerate(vals[1:], start=2):
        r = {h: (v[j] if j < len(v) else '') for j, h in enumerate(headers)}
        k = keyrow(r)
        if not clean(r.get('Opportunity Title')):
            continue
        if k:
            oldmap[k] = r
            rownum[k] = i
    protected = {
        'Risks / Gaps','Next Action','Owner','Review Status','Reviewer Notes',
        'Healthcare IT Fit (0-25)','Eligibility & Compliance (0-20)',
        'Customer / Contract Fit (0-15)','Delivery Capability (0-15)',
        'Deadline Feasibility (0-10)','Commercial Attractiveness (0-10)',
        'Information Completeness (0-5)','Total Score (0-100)',
        'Mandatory Gate (Pass / Fail / Unknown)','Recommendation (Go / Investigate / No-Go)'
    }
    append_rows, cell_updates = [], []
    for x in rows:
        if 'solicitation_id' in x or 'title' in x:
            sid = clean(x.get('portal_id')) or clean(x.get('solicitation_id'))
            u = urlnorm(x.get('url'))
            k = 'id:' + sid.casefold() if sid else ('url:' + u.casefold() if u else '')
            merged = sheetrow(x, oldmap.get(k))
        else:
            k = keyrow(x)
            merged = dict(oldmap.get(k, {}))
            merged.update(x)
        if not k:
            continue
        old = oldmap.get(k)
        if old and k in rownum:
            rn = rownum[k]
            for j, h in enumerate(headers, start=1):
                if h in protected:
                    continue
                newval = merged.get(h, '')
                if newval not in ('', old.get(h, '')):
                    cell_updates.append({'range': rowcol_to_a1(rn, j), 'values': [[newval]]})
        else:
            append_rows.append([merged.get(h, '') for h in headers])

    # Batch cell updates; do not call update_cell in a loop.
    if cell_updates:
        for i in range(0, len(cell_updates), 400):
            chunk = cell_updates[i:i + 400]
            with_retry(lambda chunk=chunk: tracker.batch_update(chunk, value_input_option='USER_ENTERED'))
    if append_rows:
        with_retry(lambda: tracker.append_rows(append_rows, value_input_option='USER_ENTERED', table_range='A1'))

    if reviewws is not None and reviews:
        rh = with_retry(lambda: ensure_headers(reviewws, REVIEW))
        existing = with_retry(lambda: reviewws.get_all_values())
        known = set()
        for v in existing[1:]:
            row = {h: (v[j] if j < len(v) else '') for j, h in enumerate(rh)}
            sid = clean(row.get('Solicitation ID'))
            u = urlnorm(row.get('Official Source URL'))
            k = 'id:' + sid.lower() if sid else ('url:' + u.lower() if u else '')
            if k:
                known.add(k)
        new_review_rows = []
        for x in reviews:
            sid = clean(x.get('Solicitation ID'))
            u = urlnorm(x.get('Official Source URL'))
            k = 'id:' + sid.lower() if sid else ('url:' + u.lower() if u else '')
            # For malformed records without either key, don't insert duplicates blindly.
            if k and k not in known:
                new_review_rows.append([x.get(h, '') for h in rh])
                known.add(k)
        if new_review_rows:
            with_retry(lambda: reviewws.append_rows(new_review_rows, value_input_option='USER_ENTERED', table_range='A1'))
    return len(append_rows), len(cell_updates)

def fetch_api_candidates():
    """Fetch every page from the official MyFloridaMarketPlace JSON API."""
    endpoint = 'https://vendor.myfloridamarketplace.com/mfmp/pub/search/bids'
    session = requests.Session()
    session.headers.update({'Accept':'application/json, text/plain, */*',
        'Content-Type':'application/json','Origin':'https://vendor.myfloridamarketplace.com',
        'Referer':SEARCH,'User-Agent':'Mozilla/5.0'})
    base = {'pageSize':25,'type':[],'status':['OPEN'],'agency':[], 'adNumber':'',
        'agencyAdvertisementNumber':'','title':'','publishedDate':'','openDate':'',
        'endDate':'','commodityCodes':[],'intendsToParticipate':'','assignee':''}
    diag={'method':'official_json_api','endpoint':endpoint,'errors':[],'pages':[]}
    def post(url,payload):
        r=session.post(url,json=payload,timeout=40); r.raise_for_status(); return r.json()
    def countval(o):
        if isinstance(o,(int,float)): return int(o)
        if isinstance(o,dict):
            for k,v in o.items():
                if re.sub(r'[^a-z]','',str(k).lower()) in ('count','total','totalcount','recordcount','totalrecords','totalresults'):
                    try:return int(v)
                    except (TypeError,ValueError):pass
            for v in o.values():
                n=countval(v)
                if n is not None:return n
        return None
    count=post(endpoint+'/count',base); total=countval(count)
    diag['count_response']=count; diag['api_count']=total
    max_pages=min(500,max(1,int(CFG.get('max_pages',100))))
    page_size=int(CFG.get('page_size',25)); base['pageSize']=page_size
    pages=min(max_pages,max(1,(total+page_size-1)//page_size if total is not None else 1))
    allrows=[]; seen=set()
    def nrm(x):return re.sub(r'[^a-z0-9]','',str(x).lower())
    def getv(d,*names):
        if not isinstance(d,dict):return ''
        z={nrm(k):v for k,v in d.items()}
        for name in names:
            v=z.get(nrm(name))
            if v not in (None,''):return v
        return ''
    def extract(obj):
        if isinstance(obj,list):
            ds=[z for z in obj if isinstance(z,dict)]
            if ds and any(any(nrm(k) in {'title','advertisementtitle','advertisementnumber','adnumber','solicitationnumber','agencyname','advertisementid','bidtitle'} for k in d) for d in ds):return ds
            out=[]
            for z in obj:out.extend(extract(z))
            return out
        if isinstance(obj,dict):
            for k,v in obj.items():
                if isinstance(v,list) and nrm(k) in {'data','results','records','content','advertisements','bids','searchresults','items','result','rows'}:
                    q=extract(v)
                    if q:return q
            out=[]
            for v in obj.values():
                if isinstance(v,(dict,list)):out.extend(extract(v))
            return out
        return []
    for pn in range(1,pages+1):
        payload=dict(base);payload['page']=pn; response=post(endpoint,payload); batch=extract(response)
        diag['pages'].append({'page':pn,'records_found':len(batch)})
        if not batch:
            diag['errors'].append('No parseable rows on API page '+str(pn));diag['empty_page_preview']=str(response)[:1000];break
        for raw in batch:
            title=clean(getv(raw,'title','advertisementTitle','adTitle','bidTitle','solicitationTitle','name'))
            sid=clean(getv(raw,'agencyAdNumber','agencyAdvertisementNumber','advertisementNumber','adNumber','solicitationNumber','solicitationId','referenceNumber'))
            portal_id=clean(getv(raw,'advertisementId','advertisementID','adId','id','bidId'))
            if not portal_id and sid.isdigit(): portal_id=sid
            agency_value=getv(raw,'agencyName','agency','organizationName','departmentName','buyerName')
            if isinstance(agency_value, dict):
                agency_value=getv(agency_value,'name','organizationName','shortName','entity')
            if not agency_value and isinstance(raw.get('organization'), dict):
                agency_value=raw['organization'].get('name') or raw['organization'].get('shortName') or ''
            agency=clean(agency_value)
            status=clean(getv(raw,'status','advertisementStatus','bidStatus'))
            pub=clean(getv(raw,'publishedDate','publicationDate','postedDate','datePosted','createdDate'))
            open_dt=clean(getv(raw,'openDate','openingDate','startDate','openDateTime','openingDateTime','availableDate','publishedDate','publicationDate','postedDate','datePosted'))
            due=clean(getv(raw,'endDate','closeDate','dueDate','responseDueDate','closingDate','endDateTime','closeDateTime','responseDeadline','deadlineDate'))
            due_time=clean(getv(raw,'endTime','closeTime','dueTime','responseDueTime','closingTime','deadlineTime'))
            due_zone=clean(getv(raw,'timeZone','timezone','endDateTimeZone','closeDateTimeZone'))
            # Keep source timestamps verbatim (including any timezone offset); do not guess missing times.
            closing_dt = due if re.search(r'\d[T ]\d{2}:\d{2}', due) else ''
            if not closing_dt and due and due_time and re.search(r'\d{1,2}:\d{2}', due_time):
                closing_dt = due + ' ' + due_time
            if due_zone:
                due_time = (due_time + ' ' + due_zone).strip()
            desc=clean(getv(raw,'description','summary','scope','advertisementDescription','details','shortDescription'))
            if not desc:
                desc=' | '.join(part for part in [
                    'Advertisement type: '+clean(getv(raw,'type','advertisementType')) if getv(raw,'type','advertisementType') else '',
                    'Status: '+clean(getv(raw,'status','advertisementStatus','bidStatus')) if getv(raw,'status','advertisementStatus','bidStatus') else '',
                    'Reference: '+clean(getv(raw,'agencyAdNumber','agencyAdvertisementNumber')) if getv(raw,'agencyAdNumber','agencyAdvertisementNumber') else ''
                ] if part)
            url=clean(getv(raw,'url','detailUrl','detailPageUrl','advertisementDetailUrl','advertisementUrl','advertisementLink','bidUrl','href','link'))
            url=urlnorm(url) if url else ''
            # Official public detail pages use /search/bids/detail/{advertisement-id}.
            # Only construct it from a portal ID (not an arbitrary agency reference).
            if not url and portal_id and portal_id.isdigit():
                url = f'https://vendor.myfloridamarketplace.com/search/bids/detail/{portal_id}'
            # Keep identifiable records even when the portal API omits a detail URL;
            # records missing both title and ID are not useful and are skipped.
            if len(title)<4 and not sid: continue
            rec={'title':title,'solicitation_id':sid,'portal_id':portal_id,'agency':agency,'status':status,
                'published_date':pub,'open_datetime':open_dt,'opening_datetime':open_dt,'deadline_date':(re.match(r'^\d{4}-\d{2}-\d{2}', due).group(0) if re.match(r'^\d{4}-\d{2}-\d{2}', due) else due),'deadline_time_timezone':due_time,'closing_datetime':closing_dt or due,'description':desc,'text':' '.join([title,agency,desc,json.dumps(raw,ensure_ascii=False,default=str)]),
                'url':url,'detail_verified':'Extracted from official MyFloridaMarketPlace JSON API'}
            key=('id:'+sid.lower()) if sid else ('url:'+url.lower())
            if key not in seen:seen.add(key);allrows.append(rec)
        if len(batch)<page_size:break
        time.sleep(max(0,float(CFG.get('request_delay_seconds',0.25))))
    diag['pages_requested']=len(diag['pages']);diag['records_extracted']=len(allrows)
    return allrows,diag

def main():
    leads_path = DATA / 'leads.json'
    reviews_path = DATA / 'manual_review.json'
    diag_path = DATA / 'portal_diagnostics.json'
    if not KEYWORDS:
        raise RuntimeError('No keywords configured in config.json; refusing to run or modify the Tracker sheet.')
    history = readj(leads_path, [])
    histmap = {}
    for item in history:
        sid = clean(item.get('portal_id')) or clean(item.get('solicitation_id'))
        u = urlnorm(item.get('url'))
        k = 'id:' + sid.casefold() if sid else ('url:' + u.casefold() if u else '')
        if k:
            histmap[k] = item

    diag = {
        'run_utc': NOW.isoformat(),
        'run_ist': IST.isoformat(),
        'run_florida': NOW.astimezone(ZoneInfo('America/New_York')).isoformat(),
        'search_url': SEARCH,
        'configured_keyword_count': len(KEYWORDS),
        'errors': [], 'pages': [],
        'manual_review_policy': 'Only missing/invalid critical fields or extraction/data problems; keyword mismatch alone is filtered out, not sent to Manual Review.'
    }
    try:
        candidates, api_diag = fetch_api_candidates()
        diag.update(api_diag)
    except Exception as exc:
        diag['errors'].append('Official API extraction failed: ' + str(exc))
        diag['status'] = 'failed_extraction'
        writej(diag_path, diag)
        log.exception('Official API extraction failed')
        raise

    log.info('Extracted %d records from official portal API', len(candidates))
    if not candidates:
        diag['status'] = 'failed_no_records'
        writej(diag_path, diag)
        raise RuntimeError('No valid API records extracted; refusing to overwrite outputs with empty data.')

    detail_stats = enrich_opportunity_contacts(candidates, CAP)
    diag['detail_contact_enrichment'] = detail_stats
    diag['contact_policy'] = 'Contact fields are extracted from each official opportunity detail page visited. No user-provided contact is substituted. Records beyond max_details_per_run remain unverified until a later run.'
    log.info('Official detail pages visited=%d; opportunity contacts found=%d', detail_stats.get('visited',0), detail_stats.get('contacts_found',0))

    all_records = []
    matched_records = []
    exceptions = []
    seen_this_run = set()
    previous_keys = set(histmap)
    for x in candidates:
        x['matched_keywords'] = matches(x)
        x['record_fingerprint'] = fp(x)
        sid = clean(x.get('portal_id')) or clean(x.get('solicitation_id'))
        u = urlnorm(x.get('url'))
        k = 'id:' + sid.casefold() if sid else ('url:' + u.casefold() if u else '')
        if not k:
            exceptions.append(reviewrow(x, '', 'Identity validation', 'Record has neither a stable advertisement/solicitation ID nor an official detail URL.'))
            continue
        if k in seen_this_run:
            continue
        seen_this_run.add(k)
        prior = histmap.get(k, {})
        x['first_seen_date'] = prior.get('first_seen_date') or prior.get('Date Found') or TODAY
        x['last_seen_date'] = TODAY
        # Missing critical data is an exception. A keyword mismatch by itself is NOT.
        missing = []
        if len(clean(x.get('title'))) < 4:
            missing.append('title is missing or too short')
        if not (clean(x.get('portal_id')) or sid):
            missing.append('official advertisement/solicitation ID is missing')
        if missing:
            exceptions.append(reviewrow(x, ', '.join(x.get('matched_keywords', [])), 'Critical field validation', '; '.join(missing)))

        saved = dict(prior)
        saved.update({a: b for a, b in x.items() if b not in ('', None, [])})
        histmap[k] = saved
        all_records.append(saved)
        # Only records matching at least one configured keyword enter Florida Tracker.
        if len(clean(x.get('title'))) >= 4 and (clean(x.get('portal_id')) or sid) and not missing and x.get('matched_keywords'):
            matched_records.append(x)

    all_history = list(histmap.values())
    # Always write complete extracted history to local files, regardless of whether Google Sheets sync succeeds.
    writej(leads_path, all_history)
    writecsv(DATA / 'leads.csv', [sheetrow(x) for x in all_history], CORE + EXTRA)
    writej(reviews_path, exceptions)
    writecsv(DATA / 'manual_review.csv', exceptions, REVIEW)

    diag['candidate_count'] = len(candidates)
    diag['historical_record_count'] = len(all_history)
    diag['matched_candidates_count'] = sum(1 for x in all_records if x.get('matched_keywords'))
    diag['tracker_eligible_count'] = len(matched_records)
    diag['unmatched_valid_count'] = sum(1 for x in all_records if not x.get('matched_keywords'))
    diag['manual_review_exception_count'] = len(exceptions)
    diag['new_records_this_run'] = sum(
        1 for x in all_records
        if (('id:' + clean(x.get('solicitation_id')).lower()) if clean(x.get('solicitation_id')) else ('url:' + urlnorm(x.get('url')).lower())) not in previous_keys
    )
    diag['status'] = 'extracted_local_pending_sheets'
    diag['tracker_policy'] = 'Only valid listings matching at least one configured keyword are sent to Florida Tracker; existing rows are never automatically deleted.'
    diag['date_time_policy'] = 'Published/open and closing date-time values are copied from official API fields when available. Source timestamps are preserved; missing times/time zones are left blank rather than guessed.'
    writej(diag_path, diag)
    log.info('Saved %d portal records locally; %d keyword-matching Tracker records; %d critical exceptions for Manual Review', len(all_history), len(matched_records), diag['manual_review_exception_count'])

    try:
        added, updated = sync(matched_records, exceptions)
        diag['sheets_sync'] = {'status': 'success', 'appended_rows': added, 'updated_cells': updated, 'tracker_candidates_sent': len(matched_records), 'manual_review_exceptions_sent': len(exceptions)}
        diag['status'] = 'success'
        log.info('Google Sheets sync succeeded: %d appended rows; %d cell updates', added, updated)
    except Exception as exc:
        diag['sheets_sync'] = {'status': 'failed', 'error': str(exc)}
        diag['status'] = 'partial_failure'
        log.exception('Google Sheets sync failed; local files retained')
    writej(diag_path, diag)
    if diag['status'] != 'success':
        raise RuntimeError('Portal extraction saved locally, but Google Sheets sync failed. See data/portal_diagnostics.json and run_logs.')
    log.info('Run complete: extracted=%d tracker_rows=%d keyword_matches=%d exceptions=%d', len(candidates), len(matched_records), diag['matched_candidates_count'], len(exceptions))
if __name__=='__main__': main()
