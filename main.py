import csv, json, logging, os, re, sys, time, hashlib
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
CORE=['Opportunity ID','Date Found','State','Agency','Opportunity Title','Solicitation / Reference No.','Opportunity Type','Service Category','Official Source URL','Published Date','Deadline Date','Deadline Time / Time Zone','Estimated Value (if published)','Scope Summary','Eligibility / Mandatory Requirements','Required Certifications / Experience','Contract Duration / Options','Incumbent / Prior Award (verified only)','Healthcare IT Fit (0-25)','Eligibility & Compliance (0-20)','Customer / Contract Fit (0-15)','Delivery Capability (0-15)','Deadline Feasibility (0-10)','Commercial Attractiveness (0-10)','Information Completeness (0-5)','Total Score (0-100)','Mandatory Gate (Pass / Fail / Unknown)','Recommendation (Go / Investigate / No-Go)','Fit Rationale','Risks / Gaps','Next Action','Last Verified Date','Owner','Review Status','Reviewer Notes']
EXTRA=['Portal Advertisement ID','Advertisement Status','Matched Keywords','Keyword Match Count','First Seen Date','Last Seen Date','Last Source Update','Record Fingerprint','Scrape / Extraction Status','Detail Verification Status','Source Document URLs']
REVIEW=['Date Detected','Keyword','Title','Solicitation ID','Official Source URL','Failure Stage','Failure Reason','Retry Status','Last Attempt']

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
    text=' '.join(clean(x.get(k,'')) for k in ('title','description','agency','commodity','category','text')).lower()
    return [k for k in KEYWORDS if k.lower() in text]
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
def records_from_api_payload(payload):
    """Walk MFMP JSON responses and convert advertisement-like objects into records."""
    found=[]; seen=set()
    title_keys=('title','advertisementTitle','adTitle','bidTitle','solicitationTitle','name','description')
    id_keys=('advertisementId','uniqueName','advertisementNumber','advertisementNo','agencyAdNumber','adNumber','number','solicitationNumber','solicitationId','bidNumber','id')
    agency_keys=('agency','agencyName','organization','organizationName','department','buyerName','organizationShortName')
    date_keys=('startDate','postedDate','publishDate','publishDateLocal','publishedDate','openDate','closeDate','closeDateLocal','endDate','dueDate','responseDueDate','deadline')
    url_keys=('url','href','detailUrl','advertisementUrl','source_url','link','viewUrl')
    def get(d, keys):
        for k in keys:
            v=d.get(k)
            if isinstance(v,(str,int,float)) and clean(v): return clean(v)
        return ''
    def walk(obj):
        if isinstance(obj,dict):
            title=get(obj,title_keys); sid=get(obj,id_keys); agency=get(obj,agency_keys)
            # Only treat objects with a title-like value plus an identifier or agency as a listing.
            if title and (sid or agency) and title.lower() not in {'title','show more','search results'}:
                text=' | '.join(f'{k}: {clean(v)}' for k,v in obj.items() if isinstance(v,(str,int,float)) and clean(v))
                url=get(obj,url_keys)
                if not url:
                    detail_id=get(obj,('advertisementId','id'))
                    if detail_id and detail_id.isdigit():
                        url=urljoin(SOURCE, f'search/bids/detail/{detail_id}')
                    else:
                        url=SEARCH
                key=(sid.lower(),title.lower(),url.lower())
                if key not in seen:
                    seen.add(key)
                    rec=listing(title,url,text)
                    rec.update({'solicitation_id':sid or rec.get('solicitation_id',''),'agency':agency or rec.get('agency',''),
                                'published_date':get(obj,('publishDate','publishDateLocal','postedDate','startDate','publishedDate')),
                                'deadline_date':get(obj,('closeDate','closeDateLocal','endDate','dueDate','responseDueDate','deadline')),
                                'opportunity_type':get(obj,('type','adType','advertisementType','solicitationType')),
                                'status':get(obj,('status','adStatus','advertisementStatus')),
                                'advertisement_number':get(obj,('advertisementNumber','advertisementNo','adNumber')),
                                'detail_verified':'Extracted from MFMP JSON response','api_record':obj})
                    found.append(rec)
            for v in obj.values(): walk(v)
        elif isinstance(obj,list):
            for v in obj: walk(v)
    walk(payload)
    return found

def get_candidates(page):
    """Extract advertisement records from MFMP result tables and expandable result panels."""
    raw = page.evaluate(r"""() => {
      const out = [], seen = new Set();
      const norm = v => (v || '').replace(/\s+/g, ' ').trim();
      const add = (title, href, text, cells, source) => {
        title = norm(title); text = norm(text); href = href || '';
        cells = (cells || []).map(norm);
        const key = [href, title, text].join('|');
        if (text.length > 12 && !seen.has(key)) {
          seen.add(key); out.push({title, href, text, cells, source});
        }
      };
      // Standard HTML tables: use cell labels and the whole row, not only link text.
      for (const table of document.querySelectorAll('table')) {
        const headers = [...table.querySelectorAll('thead th')].map(x => norm(x.innerText || x.textContent));
        for (const tr of table.querySelectorAll('tr')) {
          const cells = [...tr.querySelectorAll(':scope > td, :scope > th')].map(x => norm(x.innerText || x.textContent));
          if (!cells.length) continue;
          const text = norm(tr.innerText || tr.textContent);
          if (!text || /^(title number agency advertisement number version organization ad type start date end date status)$/i.test(text)) continue;
          const links = [...tr.querySelectorAll('a[href]')];
          const titleLink = links.find(a => norm(a.innerText || a.textContent).length > 2);
          const titleIndex = headers.findIndex(h => /title/i.test(h));
          let title = titleLink ? norm(titleLink.innerText || titleLink.textContent) : (titleIndex >= 0 ? cells[titleIndex] : cells[0]);
          let href = titleLink ? titleLink.href : '';
          if (!href) {
            const clickable = tr.querySelector('[onclick]');
            const js = clickable ? clickable.getAttribute('onclick') || '' : tr.getAttribute('onclick') || '';
            const m = js.match(/(?:location(?:\.href)?\s*=|open\s*\()\s*['\"]([^'\"]+)/i);
            if (m) href = m[1];
          }
          if (!href) href = location.href;
          add(title, href, text, cells, 'table');
        }
      }
      // Angular/React/ARIA grid rows and common MFMP result panels.
      for (const row of document.querySelectorAll('[role="row"], [role="listitem"], .search-result, .result-row, .advertisement-row')) {
        const text = norm(row.innerText || row.textContent);
        const cells = [...row.querySelectorAll('[role="cell"], [role="gridcell"], td')].map(x => norm(x.innerText || x.textContent));
        const a = [...row.querySelectorAll('a[href]')].find(x => norm(x.innerText || x.textContent).length > 2);
        if (text && !/^(title number agency advertisement number version organization ad type start date end date status)$/i.test(text)) add(a ? (a.innerText || a.textContent) : (cells[0] || text.slice(0,250)), a ? a.href : location.href, text, cells, 'aria');
      }
      // Links whose parent container contains the actual result metadata.
      for (const a of document.querySelectorAll('a[href]')) {
        const title = norm(a.innerText || a.textContent);
        if (title.length < 3) continue;
        const box = a.closest('tr,[role="row"],[role="listitem"],article,li') || a.parentElement;
        const text = norm(box && (box.innerText || box.textContent) || title);
        if (/advertisement|solicitation|bid|request|proposal|contract|purchase|rfp|rfq|itb|itn|rfi|\b\d{4,}\b/i.test(title + ' ' + text)) add(title, a.href, text, [], 'link');
      }
      return out;
    }""")
    out = []
    for item in raw:
        title, body = clean(item.get('title')), clean(item.get('text'))
        # Exclude navigation and header rows, but retain result rows even if the title has no keyword.
        if not title or re.search(r'^(title|number|agency|advertisement number|version|organization|ad type|start date|end date|status)$', title, re.I):
            continue
        if len(body) < 12 or re.search(r'copyright ©|privacy statement|help desk:', body, re.I):
            continue
        cells = item.get('cells') or []
        # MFMP search results columns: Title, Number, Agency, Advertisement Number, Version,
        # Organization, Ad Type, Start Date, End Date, Status.
        sid = clean(cells[1]) if len(cells) > 1 else ''
        agency = clean(cells[2]) if len(cells) > 2 else ''
        adno = clean(cells[3]) if len(cells) > 3 else ''
        org = clean(cells[5]) if len(cells) > 5 else ''
        adtype = clean(cells[6]) if len(cells) > 6 else ''
        start_date = clean(cells[7]) if len(cells) > 7 else ''
        end_date = clean(cells[8]) if len(cells) > 8 else ''
        status = clean(cells[9]) if len(cells) > 9 else ''
        u = urlnorm(item.get('href') or SEARCH)
        # A result row must contain identifying fields beyond just the column headings.
        has_listing_shape = bool((sid or adno) and title and title.lower() not in {'show more search results','export to excel'})
        if item.get('source') in {'table','aria'} and has_listing_shape:
            rec = listing(title, u, body)
            rec.update({'solicitation_id': sid or adno or rec.get('solicitation_id',''),
                        'agency': agency or org or rec.get('agency',''),
                        'published_date': start_date or rec.get('published_date',''),
                        'deadline_date': end_date or rec.get('deadline_date',''),
                        'opportunity_type': adtype or rec.get('opportunity_type',''),
                        'status': status, 'advertisement_number': adno,
                        'detail_verified': 'Extracted from MFMP result row'})
            out.append(rec)
        elif item.get('source') == 'link' and re.search(r'advert|solicit|bid|request|proposal|contract|purchase|rfp|rfq|itb|itn|rfi', title + ' ' + body, re.I):
            rec = listing(title, u, body)
            rec['detail_verified'] = 'Extracted from portal result link; row fields not fully available'
            out.append(rec)
    unique = {}
    for item in out:
        key = (clean(item.get('solicitation_id') or item.get('advertisement_number')).lower(), clean(item.get('title')).lower(), item.get('url','').lower())
        unique.setdefault(key, item)
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
def reviewrow(x,kw,stage,reason): return {'Date Detected':TODAY,'Keyword':kw or '(no keyword matched)','Title':clean(x.get('title') or x.get('advertisement_title') or x.get('name')),'Solicitation ID':x.get('solicitation_id',''),'Official Source URL':x.get('url',''),'Failure Stage':stage,'Failure Reason':str(reason)[:1000],'Retry Status':'Pending','Last Attempt':IST.strftime('%Y-%m-%d %H:%M:%S %Z')}
def sheetrow(x,old=None):
    old=old or {}; ms=x.get('matched_keywords',[]); u=urlnorm(x.get('url')); sid=clean(x.get('solicitation_id')); ident=sid or u or fp(x)
    r={h:'' for h in CORE+EXTRA}; r.update(old)
    vals={'Opportunity ID':ident,'Date Found':old.get('Date Found') or TODAY,'State':STATE,'Agency':clean(x.get('agency')),'Opportunity Title':clean(x.get('title')),'Solicitation / Reference No.':sid,'Opportunity Type':clean(x.get('opportunity_type')),'Service Category':', '.join(ms[:5]),'Official Source URL':u,'Published Date':clean(x.get('published_date')),'Deadline Date':clean(x.get('deadline_date')),'Scope Summary':clean(x.get('description'))[:1500],'Last Verified Date':TODAY,'Portal Advertisement ID':sid,'Matched Keywords':', '.join(ms),'Keyword Match Count':len(ms),'First Seen Date':old.get('First Seen Date') or x.get('first_seen_date') or TODAY,'Last Seen Date':TODAY,'Last Source Update':TODAY,'Record Fingerprint':fp(x),'Scrape / Extraction Status':('Listing extracted; keyword matched' if ms else 'Listing extracted; manual keyword review required'),'Detail Verification Status':x.get('detail_verified','Not visited'),'Source Document URLs':u}
    r.update({k:v for k,v in vals.items() if v!=''})
    r.setdefault('Mandatory Gate','Unknown'); r.setdefault('Recommendation','Investigate')
    r.setdefault('Review Status','Pending Review' if ms else 'Manual Review')
    if not ms and not r.get('Reviewer Notes'):
        r['Reviewer Notes']='No configured keyword matched listing text; manually assess relevance before pursuing.'
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
    sid=clean(r.get('Solicitation / Reference No.')) or clean(r.get('Portal Advertisement ID')); u=urlnorm(r.get('Official Source URL'))
    return 'id:'+sid.lower() if sid else ('url:'+u.lower() if u else '')
def sync(rows,reviews):
    tracker,reviewws=open_sheets(); headers=ensure_headers(tracker,CORE+EXTRA); vals=tracker.get_all_values(); oldmap={}; rownum={}
    for i,v in enumerate(vals[1:],start=2):
        r={h:(v[j] if j<len(v) else '') for j,h in enumerate(headers)}; k=keyrow(r)
        if k: oldmap[k]=r; rownum[k]=i
    protected={'Risks / Gaps','Next Action','Owner','Review Status','Reviewer Notes','Healthcare IT Fit (0-25)','Eligibility & Compliance (0-20)','Customer / Contract Fit (0-15)','Delivery Capability (0-15)','Deadline Feasibility (0-10)','Commercial Attractiveness (0-10)','Information Completeness (0-5)','Total Score (0-100)','Mandatory Gate (Pass / Fail / Unknown)','Recommendation (Go / Investigate / No-Go)'}
    append=[]; updates=[]
    for x in rows:
        k=keyrow(x); old=oldmap.get(k); merged=sheetrow(x,old)
        if old and k in rownum:
            for j,h in enumerate(headers,1):
                if h not in protected and merged.get(h,'') not in ('',old.get(h,'')): updates.append((rownum[k],j,merged[h]))
        else: append.append([merged.get(h,'') for h in headers])
    # Use batch update ranges grouped by cell to avoid slow per-cell requests.
    for rn,cn,val in updates: tracker.update_cell(rn,cn,val); time.sleep(.03)
    if append: tracker.append_rows(append,value_input_option='USER_ENTERED',table_range='A1')
    if reviewws is not None and reviews:
        rh=ensure_headers(reviewws,REVIEW); old=reviewws.get_all_values(); known={tuple(v[:4]) for v in old[1:]}; new=[]
        for x in reviews:
            k=tuple(x.get(h,'') for h in REVIEW[:4])
            if k not in known: new.append([x.get(h,'') for h in rh])
        if new: reviewws.append_rows(new,value_input_option='USER_ENTERED',table_range='A1')
    return len(append),len(updates)
def main():
    leads_path=DATA/'leads.json'; reviews_path=DATA/'manual_review.json'; diag_path=DATA/'portal_diagnostics.json'
    history=readj(leads_path,[]); reviews=readj(reviews_path,[]); histmap={}
    for x in history:
        k=('id:'+clean(x.get('solicitation_id')).lower()) if clean(x.get('solicitation_id')) else ('url:'+urlnorm(x.get('url')).lower())
        if k!='url:': histmap[k]=x
    diag={'run_utc':NOW.isoformat(),'run_ist':IST.isoformat(),'search_url':SEARCH,'keywords':len(KEYWORDS),'errors':[],'pages':[]}
    discovered=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True); page=browser.new_page(viewport={'width':1440,'height':1000}); page.set_default_timeout(10000)
        api_responses=[]; api_records=[]; api_payload_previews=[]
        def capture_response(response):
            try:
                ct=response.headers.get('content-type','')
                if ('json' in ct.lower() or '/mfmp/pub/search/bids' in response.url.lower()) and len(api_responses)<100:
                    entry={'url':response.url[:500],'status':response.status,'content_type':ct[:120]}
                    try:
                        req=response.request
                        entry['method']=req.method
                        if req.post_data: entry['request_body']=req.post_data[:3000]
                    except Exception: pass
                    api_responses.append(entry)
                    try:
                        payload=response.json()
                        if len(api_payload_previews)<12: api_payload_previews.append({'url':response.url[:300],'payload_type':type(payload).__name__,'preview':str(payload)[:1200]})
                        if re.search(r'/mfmp/pub/search/bids(?:\?.*)?$',response.url,re.I):
                            api_records.extend(records_from_api_payload(payload))
                    except Exception: pass
            except Exception: pass
        page.on('response', capture_response)
        try:
            resp=page.goto(SEARCH,wait_until='domcontentloaded',timeout=45000); page.wait_for_timeout(2500); diag['http_status']=resp.status if resp else None; diag['final_url']=page.url; diag['title']=clean(page.title())
            # If search page is not already open, click the portal's search navigation.
            try:
                link=page.get_by_role('link',name=re.compile('Search Advertisements',re.I))
                if link.count() and link.first.is_visible(): link.first.click(timeout=2500); page.wait_for_load_state('domcontentloaded',timeout=10000); page.wait_for_timeout(800)
            except Exception: pass
            # Inspect controls first. MFMP may render the result grid shell before rows load.
            try:
                diag['controls'] = page.evaluate(r"""() => ({
                  buttons:[...document.querySelectorAll('button,input[type=submit],input[type=button],[role=button]')].map(e=>({tag:e.tagName,text:(e.innerText||e.value||e.getAttribute('aria-label')||'').trim(),disabled:!!e.disabled})).slice(0,60),
                  selects:[...document.querySelectorAll('select')].map(e=>({name:e.name,id:e.id,label:(e.labels&&[...e.labels].map(x=>x.innerText).join(' '))||'',options:[...e.options].map(o=>({text:o.text,value:o.value})).slice(0,40)})),
                  inputs:[...document.querySelectorAll('input')].map(e=>({type:e.type,name:e.name,id:e.id,placeholder:e.placeholder,aria:e.getAttribute('aria-label')})).slice(0,60)
                })""")
            except Exception: pass
            # If the status selector offers Open/Active, choose it so blank search returns open ads.
            try:
                for sel in page.locator('select').all():
                    info = sel.evaluate("e => ({name:e.name,id:e.id,label:((e.labels&&[...e.labels].map(x=>x.innerText).join(' '))||'')+' '+(e.getAttribute('aria-label')||'')})")
                    if re.search(r'ad\s*status|status', clean(info.get('label','')+' '+info.get('name','')+' '+info.get('id','')), re.I):
                        opts = sel.locator('option').all_text_contents()
                        choice = next((v for v in opts if re.search(r'open|active|published',v,re.I)), None)
                        if choice: sel.select_option(label=choice, timeout=2500); page.wait_for_timeout(300)
            except Exception as e: diag.setdefault('errors',[]).append('Status filter: '+str(e))
            # Submit the actual form containing the search criteria, preferring exact button names.
            clicked=False
            for selector in [
                'button:has-text("Search Advertisements")','input[value*="Search Advertisements" i]',
                'button:has-text("Advertisement Search")','input[value*="Search" i]',
                'button[type="submit"]','input[type="submit"]','[role="button"]:has-text("Search")'
            ]:
                try:
                    loc=page.locator(selector)
                    target=next((loc.nth(i) for i in range(min(loc.count(),8)) if loc.nth(i).is_visible() and loc.nth(i).is_enabled()),None)
                    if target:
                        target.click(timeout=3000); page.wait_for_timeout(1800); clicked=True; break
                except Exception: pass
            # Some versions bind search to a form submit event rather than a native button.
            if not clicked:
                try:
                    forms=page.locator('form')
                    if forms.count():
                        forms.first.evaluate('(f) => f.requestSubmit()'); page.wait_for_timeout(1800); clicked=True
                except Exception as e: diag.setdefault('errors',[]).append('Form submit: '+str(e))
            # MFMP's Angular UI renders the result header first; the control itself is
            # labelled "Show More" (not "Show More Search Results"). Click that real
            # button and wait for data rows to appear. Repeat only while the row count grows.
            for _ in range(8):
                try:
                    before_rows = page.locator('table tbody tr, table tr, [role="row"], .mat-row, .mat-mdc-row').count()
                    candidates_btn = page.locator('button, [role="button"], a, input[type="button"]')
                    target = None
                    for i in range(min(candidates_btn.count(), 100)):
                        el = candidates_btn.nth(i)
                        if not el.is_visible() or not el.is_enabled():
                            continue
                        label = clean(el.inner_text() if el.evaluate("e => e.tagName !== 'INPUT'") else el.get_attribute('value'))
                        if re.fullmatch(r'show more(?:\s+search results)?', label, re.I):
                            target = el; break
                    if not target:
                        break
                    target.click(timeout=3000)
                    page.wait_for_timeout(1800)
                    try:
                        page.wait_for_load_state('networkidle', timeout=2500)
                    except Exception:
                        pass
                    after_rows = page.locator('table tbody tr, table tr, [role="row"], .mat-row, .mat-mdc-row').count()
                    log.info('Clicked MFMP Show More: rows before=%s after=%s', before_rows, after_rows)
                    if after_rows <= before_rows:
                        break
                except Exception as e:
                    diag.setdefault('errors', []).append('Show More expansion: ' + str(e))
                    break
            # Wait for client-rendered rows after submission.
            try: page.wait_for_function("() => [...document.querySelectorAll('tbody tr, [role=row], .mat-row, .ag-row')].some(r => (r.innerText||'').trim().length > 25)", timeout=6000)
            except Exception: pass
            # MFMP's current API returns zero results when status is left as an empty array.
            # Explicitly request OPEN advertisements through the same-origin endpoint, using
            # the portal's observed request schema and browser session cookies.
            try:
                forced = page.evaluate(r"""async () => {
                  const base = {pageSize:25,type:[],status:['OPEN'],agency:[],adNumber:'',agencyAdvertisementNumber:'',title:'',publishedDate:'',openDate:'',endDate:'',commodityCodes:[],intendsToParticipate:'',assignee:''};
                  const r = await fetch('/mfmp/pub/search/bids', {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify({...base,page:1})});
                  const body = await r.text();
                  let payload; try { payload=JSON.parse(body); } catch(e) { payload={_raw:body.slice(0,1500)}; }
                  let count=null;
                  try { const c=await fetch('/mfmp/pub/search/bids/count',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify(base)}); const t=await c.text(); try { count=JSON.parse(t); } catch(e) { count=t.slice(0,500); } } catch(e) { count='count request failed: '+String(e); }
                  return {status:r.status,payload,count,request:base};
                }""")
                diag['forced_open_search'] = {'http_status':forced.get('status'),'count_response':forced.get('count'),'payload_type':type(forced.get('payload')).__name__,'payload_preview':str(forced.get('payload'))[:2500],'request':forced.get('request')}
                api_records.extend(records_from_api_payload(forced.get('payload')))
            except Exception as e:
                diag.setdefault('errors',[]).append('Explicit OPEN API search failed: '+str(e))
            diag['search_submitted']=clicked; diag['api_responses']=api_responses; diag['api_payload_previews']=api_payload_previews; diag['api_records_count']=len(api_records); diag['body_excerpt']=clean(page.locator('body').inner_text())[:10000]; diag['table_count']=page.locator('table').count(); diag['aria_row_count']=page.locator('[role=row]').count(); diag['anchor_count']=page.locator('a[href]').count(); diag['dom_rows']=page.evaluate("() => [...document.querySelectorAll('table tr, [role=row], tbody tr, .mat-row, .ag-row')].map(r=>(r.innerText||r.textContent||'').replace(/\\s+/g,' ').trim()).filter(Boolean).slice(0,40)")
            discovered.extend(get_candidates(page)); discovered.extend(api_records); diag['row_preview']=[{'title':x.get('title',''),'solicitation_id':x.get('solicitation_id',''),'agency':x.get('agency',''),'url':x.get('url','')} for x in discovered[:20]]; diag['pages'].append({'url':page.url,'link_count':page.locator('a[href]').count()})
            try: (DATA/'portal-rendered.html').write_text(page.content(),encoding='utf-8')
            except Exception: pass
            for _ in range(9):
                if len(discovered)>=CAP*3: break
                nxt=None
                for sel in ['a[aria-label*=Next i]','button[aria-label*=Next i]','a:has-text("Next")','button:has-text("Next")']:
                    try:
                        q=page.locator(sel)
                        for i in range(min(q.count(),3)):
                            if q.nth(i).is_visible() and q.nth(i).is_enabled(): nxt=q.nth(i); break
                        if nxt: break
                    except Exception: pass
                if not nxt: break
                before=len(discovered)
                try: nxt.click(timeout=2500); page.wait_for_timeout(1000); discovered.extend(get_candidates(page)); diag['pages'].append({'url':page.url,'link_count':page.locator('a[href]').count()})
                except Exception as e: diag['errors'].append('Pagination: '+str(e)); break
                if len(discovered)==before: break
        except Exception as e:
            diag['errors'].append('Portal navigation/extraction: '+str(e))
            try: page.screenshot(path=str(DATA/'portal-error.png'),full_page=True)
            except Exception: pass
        finally: browser.close()
    unique={}
    for x in discovered:
        k=('id:'+clean(x.get('solicitation_id')).lower()) if x.get('solicitation_id') else 'url:'+urlnorm(x.get('url')).lower()
        if k not in unique: unique[k]=x
    candidates=list(unique.values()); log.info('Found %d candidate links',len(candidates))
    selected=[]
    for x in candidates:
        x['matched_keywords']=matches(x)
        if x['matched_keywords']: selected.append(x)
    if not selected and candidates: diag['errors'].append('Candidates extracted but none matched configured keywords; review row_preview and broaden keywords only if appropriate.')
    if not selected and candidates:
        reviews.extend(reviewrow(x, '', 'Keyword matching', 'Portal row extracted but no configured keyword matched the listing text; manual review required') for x in candidates)
    # Visit matching detail pages (up to configured cap).
    if selected:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True); page=browser.new_page(); page.set_default_timeout(10000)
            refined=[]
            for x in selected[:CAP]:
                x=open_detail(page,x); x['matched_keywords']=matches(x)
                if x['matched_keywords']:
                    x['record_fingerprint']=fp(x); refined.append(x)
                else: reviews.append(reviewrow(x,'','Detail extraction','Listing no longer matched configured keywords after opening detail page'))
                time.sleep(DELAY)
            browser.close(); selected=refined
    # Keep ALL extracted advertisements in local leads history. Keyword-matched items
    # are pursued normally; non-matches remain visible for human review instead of vanishing.
    output=[]; new_count=0
    selected_by_key={}
    for x in selected:
        sid=clean(x.get('solicitation_id')); k='id:'+sid.lower() if sid else 'url:'+urlnorm(x.get('url')).lower()
        selected_by_key[k]=x
    for x in candidates:
        sid=clean(x.get('solicitation_id')); k='id:'+sid.lower() if sid else 'url:'+urlnorm(x.get('url')).lower()
        # Prefer the detail-verified version if this candidate matched a configured keyword.
        x=selected_by_key.get(k, x)
        prior=histmap.get(k,{})
        x['first_seen_date']=prior.get('first_seen_date') or prior.get('Date Found') or TODAY
        x['last_seen_date']=TODAY
        if not prior: new_count+=1
        saved=dict(prior)
        saved.update({a:b for a,b in x.items() if b not in ('',None,[])})
        histmap[k]=saved
        output.append(sheetrow(x,prior))
    all_history=list(histmap.values())
    writej(leads_path,all_history)
    writecsv(DATA/'leads.csv',[sheetrow(x) for x in all_history],CORE+EXTRA)
    writej(reviews_path,reviews); writecsv(DATA/'manual_review.csv',reviews,REVIEW); writej(diag_path,diag)
    log.info('Saved data: %d historical, %d advertisements tracked this run, %d new',len(all_history),len(output),new_count)
    try:
        added,updated=sync(output,reviews); diag['sheets_sync']={'status':'success','appended':added,'updated_cells':updated}
        log.info('Sheets sync success: %d appended; %d cells updated',added,updated)
    except Exception as e:
        diag['sheets_sync']={'status':'failed','error':str(e)}; log.exception('Google Sheets sync failed; local files retained')
    writej(diag_path,diag)
    if not candidates:
        # Persist diagnostics and empty/previous history files even when extraction fails.
        writej(leads_path, all_history if 'all_history' in locals() else history)
        writecsv(DATA/'leads.csv',[sheetrow(x) for x in (all_history if 'all_history' in locals() else history)],CORE+EXTRA)
        writej(reviews_path,reviews); writecsv(DATA/'manual_review.csv',reviews,REVIEW)
        diag['status']='failed'
        diag.setdefault('errors', []).append('No advertisement candidates extracted; inspect body_excerpt and portal DOM.')
        writej(diag_path,diag)
        log.error('ZERO CANDIDATES: portal result extraction did not find any advertisement rows.')
        raise RuntimeError('No Florida advertisement records extracted; see data/portal_diagnostics.json')
    if diag.get('sheets_sync', {}).get('status') != 'success':
        raise RuntimeError('Records extracted but Google Sheets sync failed; see data/portal_diagnostics.json')
    diag['status'] = 'success' if selected else ('no_keyword_matches' if candidates else 'no_candidates')
    diag['matched_candidates_count'] = len(selected)
    diag['tracked_candidates_count'] = len(candidates)
    writej(diag_path,diag)
    log.info('Run complete: candidates=%d matched=%d tracked=%d history=%d status=%s', len(candidates), len(selected), len(output), len(all_history), diag['status'])
if __name__=='__main__': main()
