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
REVIEW=['Date Detected','Keyword','Solicitation ID','Official Source URL','Failure Stage','Failure Reason','Retry Status','Last Attempt']

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
    raw=page.evaluate("""() => {const out=[],seen=new Set(); for(const a of document.querySelectorAll('a[href]')) {const title=(a.innerText||a.textContent||'').replace(/\\s+/g,' ').trim(), href=a.href||''; if(title.length<4||title.length>500||!/^https?:/i.test(href)) continue; if(!/bid|advert|solicit|rfp|rfq|itn|contract|opportun|\\d{4}[-/]/i.test(href+' '+title)) continue; const box=a.closest('tr')||a.closest('[role=row]')||a.closest('article')||a.closest('li')||a.parentElement; const text=((box&&(box.innerText||box.textContent))||title).replace(/\\s+/g,' ').trim(); const key=href+'|'+title; if(!seen.has(key)){seen.add(key);out.push({title,href,text});}} return out;}""")
    out=[]
    for x in raw:
        u=urlnorm(x['href']); host=urlparse(u).netloc.lower()
        if u and (host.endswith('myfloridamarketplace.com') or host.endswith('myflorida.com')) and clean(x['title']).lower() not in {'home','login','register','search advertisements','search vendors'}: out.append(listing(x['title'],u,x['text']))
    return out
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
def reviewrow(x,kw,stage,reason): return {'Date Detected':TODAY,'Keyword':kw or '(no keyword matched)','Solicitation ID':x.get('solicitation_id',''),'Official Source URL':x.get('url',''),'Failure Stage':stage,'Failure Reason':str(reason)[:1000],'Retry Status':'Pending','Last Attempt':IST.strftime('%Y-%m-%d %H:%M:%S %Z')}
def sheetrow(x,old=None):
    old=old or {}; ms=x.get('matched_keywords',[]); u=urlnorm(x.get('url')); sid=clean(x.get('solicitation_id')); ident=sid or u or fp(x)
    r={h:'' for h in CORE+EXTRA}; r.update(old)
    vals={'Opportunity ID':ident,'Date Found':old.get('Date Found') or TODAY,'State':STATE,'Agency':clean(x.get('agency')),'Opportunity Title':clean(x.get('title')),'Solicitation / Reference No.':sid,'Opportunity Type':clean(x.get('opportunity_type')),'Service Category':', '.join(ms[:5]),'Official Source URL':u,'Published Date':clean(x.get('published_date')),'Deadline Date':clean(x.get('deadline_date')),'Scope Summary':clean(x.get('description'))[:1500],'Last Verified Date':TODAY,'Portal Advertisement ID':sid,'Matched Keywords':', '.join(ms),'Keyword Match Count':len(ms),'First Seen Date':old.get('First Seen Date') or x.get('first_seen_date') or TODAY,'Last Seen Date':TODAY,'Last Source Update':TODAY,'Record Fingerprint':fp(x),'Scrape / Extraction Status':'Listing extracted; keyword matched','Detail Verification Status':x.get('detail_verified','Not visited'),'Source Document URLs':u}
    r.update({k:v for k,v in vals.items() if v!=''})
    r.setdefault('Mandatory Gate','Unknown'); r.setdefault('Recommendation','Investigate'); r.setdefault('Review Status','Pending Review')
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
def ensure_headers(ws,needed):
    vals=ws.get_all_values()
    if not vals or not any(clean(v) for v in vals[0]): ws.update('A1',[needed]); return needed
    cur=vals[0]; missing=[h for h in needed if h not in cur]
    if missing:
        from gspread.utils import rowcol_to_a1
        ws.update(rowcol_to_a1(1,len(cur)+1),[missing],value_input_option='USER_ENTERED'); cur+=missing
    return cur
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
        try:
            resp=page.goto(SEARCH,wait_until='domcontentloaded',timeout=45000); page.wait_for_timeout(2500); diag['http_status']=resp.status if resp else None; diag['final_url']=page.url; diag['title']=clean(page.title())
            # If search page is not already open, click the portal's search navigation.
            try:
                link=page.get_by_role('link',name=re.compile('Search Advertisements',re.I))
                if link.count() and link.first.is_visible(): link.first.click(timeout=2500); page.wait_for_load_state('domcontentloaded',timeout=10000); page.wait_for_timeout(800)
            except Exception: pass
            # Submit blank search; official Florida vendor guidance says blank search returns open ads.
            clicked=False
            for pat in ['Advertisement Search','Search']:
                try:
                    btn=page.get_by_role('button',name=re.compile(pat,re.I))
                    if btn.count() and btn.first.is_visible(): btn.first.click(timeout=2500); page.wait_for_load_state('domcontentloaded',timeout=10000); page.wait_for_timeout(1500); clicked=True; break
                except Exception: pass
            if not clicked:
                try:
                    btn=page.locator('input[type=submit],button[type=submit]')
                    if btn.count(): btn.first.click(timeout=2500); page.wait_for_load_state('domcontentloaded',timeout=10000); page.wait_for_timeout(1500); clicked=True
                except Exception: pass
            diag['search_submitted']=clicked; diag['body_excerpt']=clean(page.locator('body').inner_text())[:6000]
            discovered.extend(get_candidates(page)); diag['pages'].append({'url':page.url,'link_count':page.locator('a[href]').count()})
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
    if not selected and candidates: diag['errors'].append('No listings matched keywords. Inspect body_excerpt and portal DOM/selectors in portal_diagnostics.json.')
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
    output=[]; new_count=0
    for x in selected:
        sid=clean(x.get('solicitation_id')); k='id:'+sid.lower() if sid else 'url:'+urlnorm(x.get('url')).lower(); prior=histmap.get(k,{})
        x['first_seen_date']=prior.get('first_seen_date') or prior.get('Date Found') or TODAY; x['last_seen_date']=TODAY
        if not prior: new_count+=1
        # Store raw data history with first/last seen.
        saved=dict(prior); saved.update({a:b for a,b in x.items() if b not in ('',None,[])})
        histmap[k]=saved; output.append(sheetrow(x,prior))
    all_history=list(histmap.values()); writej(leads_path,all_history); writecsv(DATA/'leads.csv',[sheetrow(x) for x in all_history],CORE+EXTRA)
    writej(reviews_path,reviews); writecsv(DATA/'manual_review.csv',reviews,REVIEW); writej(diag_path,diag)
    log.info('Saved data: %d historical, %d matched this run, %d new',len(all_history),len(output),new_count)
    try:
        added,updated=sync(output,reviews); diag['sheets_sync']={'status':'success','appended':added,'updated_cells':updated}
        log.info('Sheets sync success: %d appended; %d cells updated',added,updated)
    except Exception as e:
        diag['sheets_sync']={'status':'failed','error':str(e)}; log.exception('Google Sheets sync failed; local files retained')
    writej(diag_path,diag)
    if not candidates: log.warning('No candidates detected. Check data/portal_diagnostics.json and data/portal-error.png if present.')
    log.info('Run complete.')
if __name__=='__main__': main()
