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
CORE=['Opportunity ID','Date Found','State','Agency','Opportunity Title','Solicitation / Reference No.','Opportunity Type','Service Category','Official Source URL','Published Date','Deadline Date','Deadline Time / Time Zone','Estimated Value (if published)','Scope Summary','Eligibility / Mandatory Requirements','Required Certifications / Experience','Contract Duration / Options','Incumbent / Prior Award (verified only)','Healthcare IT Fit (0-25)','Eligibility & Compliance (0-20)','Customer / Contract Fit (0-15)','Delivery Capability (0-15)','Deadline Feasibility (0-10)','Commercial Attractiveness (0-10)','Information Completeness (0-5)','Total Score (0-100)','Mandatory Gate (Pass / Fail / Unknown)','Recommendation (Go / Investigate / No-Go)','Fit Rationale','Risks / Gaps','Next Action','Last Verified Date','Owner','Review Status','Reviewer Notes']
EXTRA=['Portal Advertisement ID','Advertisement Status','Matched Keywords','Keyword Match Count','First Seen Date','Last Seen Date','Last Source Update','Record Fingerprint','Scrape / Extraction Status','Detail Verification Status','Source Document URLs']
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
def reviewrow(x,kw,stage,reason): return {'Date Detected':TODAY,'Keyword':kw or '(no keyword matched)','Solicitation ID':x.get('solicitation_id',''),'Opportunity Title':x.get('title',''),'Official Source URL':x.get('url',''),'Failure Stage':stage,'Failure Reason':str(reason)[:1000],'Retry Status':'Pending','Last Attempt':IST.strftime('%Y-%m-%d %H:%M:%S %Z')}
def sheetrow(x,old=None):
    old=old or {}; ms=x.get('matched_keywords',[]); u=urlnorm(x.get('url')); sid=clean(x.get('solicitation_id')); ident=sid or u or fp(x)
    r={h:'' for h in CORE+EXTRA}; r.update(old)
    vals={'Opportunity ID':ident,'Date Found':old.get('Date Found') or TODAY,'State':STATE,'Agency':clean(x.get('agency')),'Opportunity Title':clean(x.get('title')),'Solicitation / Reference No.':sid,'Opportunity Type':clean(x.get('opportunity_type')),'Service Category':', '.join(ms[:5]),'Official Source URL':u,'Published Date':clean(x.get('published_date')),'Deadline Date':clean(x.get('deadline_date')),'Scope Summary':clean(x.get('description'))[:1500],'Last Verified Date':TODAY,'Portal Advertisement ID':sid,'Matched Keywords':', '.join(ms),'Keyword Match Count':len(ms),'First Seen Date':old.get('First Seen Date') or x.get('first_seen_date') or TODAY,'Last Seen Date':TODAY,'Last Source Update':TODAY,'Record Fingerprint':fp(x),'Scrape / Extraction Status':('Listing extracted; keyword matched' if ms else 'Listing extracted; manual relevance review'),'Detail Verification Status':x.get('detail_verified','Not visited'),'Source Document URLs':u}
    r.update({k:v for k,v in vals.items() if v!=''})
    r.setdefault('Mandatory Gate','Unknown'); r.setdefault('Recommendation','Investigate'); r.setdefault('Review Status','Pending Review' if ms else 'Manual Review')
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
        # Ignore legacy malformed rows (blank title or generic search URL) so they are not reused.
        if not clean(r.get('Opportunity Title')): continue
        if urlnorm(r.get('Official Source URL')) == urlnorm(SEARCH) and not clean(r.get('Solicitation / Reference No.') or r.get('Portal Advertisement ID')): continue
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
        rh=ensure_headers(reviewws,REVIEW); old=reviewws.get_all_values(); known=set()
        for v in old[1:]:
            row={h:(v[j] if j<len(v) else '') for j,h in enumerate(rh)}
            sid=clean(row.get('Solicitation ID')); u=urlnorm(row.get('Official Source URL'))
            if sid: known.add('id:'+sid.lower())
            elif u: known.add('url:'+u.lower())
        new=[]
        for x in reviews:
            sid=clean(x.get('Solicitation ID')); u=urlnorm(x.get('Official Source URL'))
            k='id:'+sid.lower() if sid else ('url:'+u.lower() if u else '')
            if k and k not in known: new.append([x.get(h,'') for h in rh]); known.add(k)
        if new: reviewws.append_rows(new,value_input_option='USER_ENTERED',table_range='A1')
    return len(append),len(updates)
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
            sid=clean(getv(raw,'advertisementNumber','adNumber','solicitationNumber','solicitationId','advertisementId','id','referenceNumber'))
            agency=clean(getv(raw,'agencyName','agency','organizationName','departmentName','buyerName'))
            status=clean(getv(raw,'status','advertisementStatus','bidStatus'))
            pub=clean(getv(raw,'publishedDate','publicationDate','postedDate','datePosted','createdDate'))
            due=clean(getv(raw,'endDate','closeDate','dueDate','responseDueDate','openingDate'))
            desc=clean(getv(raw,'description','summary','scope','advertisementDescription','details','shortDescription')) or clean(raw)
            url=clean(getv(raw,'url','detailUrl','advertisementUrl','bidUrl','href','link'))
            url=urlnorm(url) if url else (SEARCH if sid else '')
            if len(title)<4 or not (sid or url):continue
            rec={'title':title,'solicitation_id':sid,'agency':agency,'status':status,
                'published_date':pub,'deadline_date':due,'description':desc,'text':' '.join([title,agency,desc]),
                'url':url,'detail_verified':'Extracted from official MyFloridaMarketPlace JSON API'}
            key=('id:'+sid.lower()) if sid else ('url:'+url.lower())
            if key not in seen:seen.add(key);allrows.append(rec)
        if len(batch)<page_size:break
        time.sleep(max(0,float(CFG.get('request_delay_seconds',0.25))))
    diag['pages_requested']=len(diag['pages']);diag['records_extracted']=len(allrows)
    return allrows,diag

def main():
    leads_path=DATA/'leads.json'; reviews_path=DATA/'manual_review.json'; diag_path=DATA/'portal_diagnostics.json'
    history=readj(leads_path,[]); reviews=readj(reviews_path,[]); histmap={}
    for x in history:
        k=('id:'+clean(x.get('solicitation_id')).lower()) if clean(x.get('solicitation_id')) else ('url:'+urlnorm(x.get('url')).lower())
        if k!='url:': histmap[k]=x
    diag={'run_utc':NOW.isoformat(),'run_ist':IST.isoformat(),'search_url':SEARCH,'keywords':len(KEYWORDS),'errors':[],'pages':[]}
    try:
        candidates, api_diag = fetch_api_candidates()
        diag.update(api_diag)
    except Exception as e:
        diag['errors'].append('Official API extraction failed: '+str(e))
        writej(diag_path,diag)
        log.exception('Official API extraction failed')
        raise
    log.info('Extracted %d real portal records from API', len(candidates))
    if not candidates:
        diag['status']='failed_no_records'; writej(diag_path,diag)
        raise RuntimeError('No valid API records extracted; no placeholder rows were written')
    selected=[]
    reviews=[]
    previous_keys=set(histmap)
    for x in candidates:
        x['matched_keywords']=matches(x)
        x['record_fingerprint']=fp(x)
        sid=clean(x.get('solicitation_id')); k='id:'+sid.lower() if sid else 'url:'+urlnorm(x.get('url')).lower()
        prior=histmap.get(k,{})
        x['first_seen_date']=prior.get('first_seen_date') or prior.get('Date Found') or TODAY
        x['last_seen_date']=TODAY
        if not x['matched_keywords']:
            reviews.append(reviewrow(x,'','Keyword matching','Portal listing extracted but no configured keyword matched available listing text; manually assess relevance'))
        saved=dict(prior); saved.update({a:b for a,b in x.items() if b not in ('',None,[])})
        histmap[k]=saved
        selected.append(x)
    output=[sheetrow(x,histmap.get(('id:'+clean(x.get('solicitation_id')).lower()) if clean(x.get('solicitation_id')) else 'url:'+urlnorm(x.get('url')).lower(),{})) for x in selected]
    new_count=sum(1 for x in selected if (('id:'+clean(x.get('solicitation_id')).lower()) if clean(x.get('solicitation_id')) else ('url:'+urlnorm(x.get('url')).lower())) not in previous_keys)
    all_history=list(histmap.values()); writej(leads_path,all_history); writecsv(DATA/'leads.csv',[sheetrow(x) for x in all_history],CORE+EXTRA)
    writej(reviews_path,reviews); writecsv(DATA/'manual_review.csv',reviews,REVIEW); writej(diag_path,diag)
    diag['candidate_count']=len(candidates); diag['matched_candidates_count']=sum(bool(x.get('matched_keywords')) for x in selected); diag['unmatched_candidates_count']=len(reviews); diag['historical_record_count']=len(all_history); diag['new_records_this_run']=new_count
    log.info('Saved data: %d historical, %d matched this run, %d new',len(all_history),diag['matched_candidates_count'],new_count)
    try:
        added,updated=sync(output,reviews); diag['sheets_sync']={'status':'success','appended':added,'updated_cells':updated}
        log.info('Sheets sync success: %d appended; %d cells updated',added,updated)
    except Exception as e:
        diag['sheets_sync']={'status':'failed','error':str(e)}; log.exception('Google Sheets sync failed; local files retained')
    writej(diag_path,diag)
    if not candidates:
        diag['status']='failed'
        diag.setdefault('errors', []).append('No advertisement candidates extracted from the official API.')
        writej(diag_path,diag)
        log.error('ZERO CANDIDATES: portal result extraction did not find any advertisement rows.')
        raise RuntimeError('No Florida advertisement records extracted; see data/portal_diagnostics.json')
    if diag.get('sheets_sync', {}).get('status') != 'success':
        diag['status']='partial_failure'; writej(diag_path,diag)
        raise RuntimeError('Records extracted but Google Sheets sync failed; see data/portal_diagnostics.json')
    diag['status']='success'; writej(diag_path,diag)
    log.info('Run complete: candidates=%d matched=%d history=%d', len(candidates), diag['matched_candidates_count'], len(all_history))
if __name__=='__main__': main()
