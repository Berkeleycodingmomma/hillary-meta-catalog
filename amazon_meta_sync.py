#!/usr/bin/env python3
from __future__ import annotations
import csv,getpass,json,re,subprocess,time
from collections import defaultdict
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT=Path(__file__).resolve().parent; CONFIG=ROOT/'config'; PUBLIC=ROOT/'public'; OUTPUT=ROOT/'output'; REPORTS=ROOT/'reports'; LOGS=ROOT/'logs'
TOKEN_URL='https://api.amazon.com/auth/o2/token'; API_URL='https://creatorsapi.amazon/catalog/v1/getItems'; MARKETPLACE='www.amazon.com'
LIST_RE=re.compile(r'https?://(?:www\.)?amazon\.com/shop/[^/]+/list/([A-Z0-9]+)',re.I)
RESOURCES=['images.primary.large','images.primary.medium','images.variants.large','itemInfo.title','itemInfo.byLineInfo','itemInfo.features','itemInfo.contentInfo','itemInfo.productInfo','itemInfo.externalIds','itemInfo.classifications','offersV2.listings.price','offersV2.listings.availability','offersV2.listings.condition','parentASIN']
FIELDS=['id','title','description','availability','condition','price','link','image_link','additional_image_link','brand','gtin','mpn','google_product_category','fb_product_category','custom_label_0','custom_label_1','custom_label_2','custom_label_3','custom_label_4']

def load_settings(): return json.loads((CONFIG/'settings.json').read_text(encoding='utf-8'))
def clean_url(url):
    m=LIST_RE.search(url or '')
    return f'https://www.amazon.com/shop/thehillarystyle/list/{m.group(1).upper()}' if m else (url or '').strip()
def read_approved():
    out=[]
    with (CONFIG/'approved_lists.csv').open(newline='',encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if str(r.get('enabled','')).strip().lower() not in {'yes','true','1','on'}: continue
            lid=str(r.get('list_id','')).strip().upper(); url=clean_url(r.get('idea_list_url',''))
            if lid and url: out.append({'fallback_name':str(r.get('fallback_name','')).strip() or lid,'list_id':lid,'idea_list_url':url})
    return out

def get_title(page,fallback):
    for sel in ['h1','[data-testid*="list-title"]','[class*="listTitle"]']:
        try:
            t=(page.locator(sel).first.inner_text(timeout=1200) or '').strip()
            if t and len(t)<180 and 'Amazon' not in t: return re.sub(r'\s+',' ',t)
        except Exception: pass
    return fallback

def expected_count(page):
    try:
        text=page.locator('body').inner_text(timeout=5000); m=re.findall(r'(?im)^\s*(\d{1,4})\s+Items\s*$',text)
        return int(m[0]) if m else None
    except Exception: return None

def trim(html):
    cuts=[]
    for p in [r'More\s+from\s+THE\s+HILLARY\s+STYLE',r'More\s+from\s+']:
        m=re.search(p,html or '',re.I)
        if m: cuts.append(m.start())
    return (html or '')[:min(cuts)] if cuts else (html or '')
def extract_asins(html):
    html=trim(html); found=set()
    pats=[r'/dp/([A-Z0-9]{10})(?:[/?&#\"\']|$)',r'/gp/product/([A-Z0-9]{10})(?:[/?&#\"\']|$)',r'/product/([A-Z0-9]{10})(?:[/?&#\"\']|$)',r'data-asin=[\"\']([A-Z0-9]{10})[\"\']',r'[\"\'](?:asin|ASIN|productAsin|productASIN|productId)[\"\']\s*[:=]\s*[\"\']([A-Z0-9]{10})[\"\']']
    for p in pats:
        for m in re.finditer(p,html,re.I): found.add(m.group(1).upper())
    return found

def scrape_list(context,entry):
    page=context.new_page()
    try:
        print(f"\nOpening {entry['fallback_name']}"); page.goto(entry['idea_list_url'],wait_until='domcontentloaded',timeout=120000); page.wait_for_timeout(2200)
        title=get_title(page,entry['fallback_name']); expected=expected_count(page); print(f'Amazon title: {title}');
        if expected: print(f'Amazon displays {expected} items.')
        found=set(); no_new=0
        for step in range(1,301):
            before=len(found)
            try: found.update(extract_asins(page.content()))
            except Exception: pass
            added=len(found)-before
            if step==1 or added or step%20==0: print(f'  Step {step}: {len(found)} ASINs (+{added})')
            if expected and len(found)>=expected: break
            try:
                body=page.locator('body').inner_text(timeout=1200)
                if re.search(r'More\s+from\s+THE\s+HILLARY\s+STYLE',body,re.I): break
            except Exception: pass
            no_new=no_new+1 if added==0 else 0
            try:
                y,maxy=page.evaluate("""() => {const h=window.innerHeight||900;window.scrollBy(0,Math.max(500,Math.floor(h*.75)));const m=Math.max(document.body.scrollHeight,document.documentElement.scrollHeight)-h;return [window.scrollY,m];}"""); page.wait_for_timeout(900); bottom=int(y)>=int(maxy)-50
            except Exception: bottom=False
            if (bottom and no_new>=8) or no_new>=20: break
        print(f'Finished {title}: {len(found)} unique ASINs'); return title,found,expected
    finally: page.close()

def discover(context,url):
    page=context.new_page(); found={}
    try:
        print('\nScanning storefront for new Idea Lists...'); page.goto(url,wait_until='domcontentloaded',timeout=120000); page.wait_for_timeout(2000); no_new=0
        for _ in range(200):
            before=len(found)
            try:
                a=page.locator('a[href*="/shop/"][href*="/list/"]')
                for i in range(a.count()):
                    node=a.nth(i); href=node.get_attribute('href') or ''; m=LIST_RE.search(href)
                    if not m: continue
                    u=clean_url(href)
                    try: t=(node.inner_text(timeout=500) or '').strip()
                    except Exception: t=''
                    found[u]=re.sub(r'\s+',' ',t) if t else m.group(1).upper()
            except Exception: pass
            no_new=no_new+1 if len(found)==before else 0; page.evaluate('window.scrollBy(0,Math.max(600,window.innerHeight*.8))'); page.wait_for_timeout(650)
            if no_new>=12: break
    finally: page.close()
    return [{'amazon_title':t,'idea_list_url':u} for u,t in sorted(found.items())]

def chunks(items,n):
    for i in range(0,len(items),n): yield items[i:i+n]
def nested(obj,*keys,default=None):
    cur=obj
    for k in keys:
        if not isinstance(cur,dict) or k not in cur: return default
        cur=cur[k]
    return cur
def dvalue(obj): return obj.get('displayValue','').strip() if isinstance(obj,dict) and isinstance(obj.get('displayValue'),str) else ''
def dvalues(obj): return [str(v).strip() for v in obj.get('displayValues',[]) if str(v).strip()] if isinstance(obj,dict) and isinstance(obj.get('displayValues'),list) else []
def token(cid,secret):
    r=requests.post(TOKEN_URL,headers={'Content-Type':'application/json'},json={'grant_type':'client_credentials','client_id':cid,'client_secret':secret,'scope':'creatorsapi::default'},timeout=60)
    if not r.ok: raise RuntimeError(f'Token request failed ({r.status_code}): {r.text[:1000]}')
    t=r.json().get('access_token')
    if not t: raise RuntimeError('Amazon returned no access token.')
    return t
def get_items(tok,tag,asins):
    r=requests.post(API_URL,headers={'Authorization':f'Bearer {tok}','Content-Type':'application/json','x-marketplace':MARKETPLACE},json={'itemIds':asins,'itemIdType':'ASIN','marketplace':MARKETPLACE,'partnerTag':tag,'resources':RESOURCES},timeout=90)
    if r.status_code==429: raise RuntimeError('Amazon rate limit reached')
    if not r.ok: raise RuntimeError(f'GetItems failed ({r.status_code}): {r.text[:1500]}')
    data=r.json(); return (data.get('itemsResult') or {}).get('items') or [],data.get('errors') or []
def parse_offer(item):
    listings = (item.get('offersV2') or {}).get('listings') or []

    if not listings:
        return '', 'out of stock'

    listing = listings[0] or {}
    price_data = listing.get('price') or {}
    money = (
        price_data.get('money')
        or price_data.get('currentPrice')
        or price_data
    )

    amount = money.get('amount') if isinstance(money, dict) else None
    currency = money.get('currency') if isinstance(money, dict) else None

    if amount is not None:
        try:
            price = f"{float(amount):.2f} {(currency or 'USD').upper()}"
        except (TypeError, ValueError):
            price = ''
    else:
        price = ''

    availability_text = json.dumps(
        listing.get('availability') or {}
    ).lower()

    availability = (
        'in stock'
        if any(
            value in availability_text
            for value in ['in_stock', 'instock', 'available', 'now']
        )
        else 'out of stock'
    )

    return price, availability

def brand(item):
    b=nested(item,'itemInfo','byLineInfo',default={}) or {}
    for k in ('brand','manufacturer','contributors'):
        v=dvalue(b.get(k))
        if v:return v
    return 'Amazon'
def description(item):
    vals=dvalues(nested(item,'itemInfo','features',default={}))
    if vals:return ' • '.join(vals)[:4999]
    for v in (nested(item,'itemInfo','contentInfo',default={}) or {}).values():
        vals=dvalues(v)
        if vals:return ' • '.join(vals)[:4999]
        val=dvalue(v)
        if val:return val[:4999]
    return dvalue(nested(item,'itemInfo','title',default={}))[:4999]
def image(item): return nested(item,'images','primary','large','url') or nested(item,'images','primary','medium','url') or ''
def extra_images(item):
    out=[]
    for v in nested(item,'images','variants',default=[]) or []:
        u=nested(v,'large','url') or nested(v,'medium','url') or nested(v,'small','url')
        if u and u not in out:out.append(u)
    return ','.join(out[:20])
def meta_row(item,labels):
    asin=str(item.get('asin') or '').upper(); title=dvalue(nested(item,'itemInfo','title',default={})) or asin; price,avail=parse_offer(item); ext=nested(item,'itemInfo','externalIds',default={}) or {}; gtin=''
    for k in ('upcs','eans','isbns'):
        vals=dvalues(ext.get(k))
        if vals:gtin=vals[0];break
    labels=[re.sub(r'\s+',' ',x.strip())[:100] for x in labels if x.strip()][:5]+['']*5
    return {'id':asin,'title':title[:200],'description':description(item),'availability':avail,'condition':'new','price':price,'link':item.get('detailPageURL') or f'https://www.amazon.com/dp/{asin}','image_link':image(item),'additional_image_link':extra_images(item),'brand':brand(item)[:100],'gtin':gtin,'mpn':asin,'google_product_category':'','fb_product_category':'','custom_label_0':labels[0],'custom_label_1':labels[1],'custom_label_2':labels[2],'custom_label_3':labels[3],'custom_label_4':labels[4]}
def write_csv(path,rows,fields):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8-sig') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def publish(settings):
    if not settings.get('auto_git_publish',False): return 'Git publishing is OFF. Test first, then turn it on.'
    try:
        subprocess.run(['git','-C',str(ROOT),'add','public','reports'],check=True); status=subprocess.run(['git','-C',str(ROOT),'status','--porcelain'],capture_output=True,text=True,check=True).stdout.strip()
        if not status:return 'No GitHub changes to publish.'
        subprocess.run(['git','-C',str(ROOT),'commit','-m',time.strftime('Update Meta catalog %Y-%m-%d %H:%M')],check=True); subprocess.run(['git','-C',str(ROOT),'push','origin',settings.get('github_branch','main')],check=True); return 'Published updated feed to GitHub.'
    except Exception as e:return f'Git publishing failed: {e}'

def main():
    for d in (PUBLIC,OUTPUT,REPORTS,LOGS): d.mkdir(parents=True,exist_ok=True)
    settings=load_settings(); approved=read_approved()
    tag=input(f"Amazon Store/Partner Tag [{settings.get('partner_tag','hillarypeil-20')}]: ").strip() or settings.get('partner_tag','hillarypeil-20'); cid=input('Creators API Credential ID: ').strip(); secret=getpass.getpass('Creators API Secret (hidden): ').strip()
    memberships=defaultdict(set); resolved=[]; discovered=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=False); context=browser.new_context(viewport={'width':1400,'height':1000},locale='en-US')
        if settings.get('discover_storefront_lists',True):
            discovered=discover(context,settings.get('storefront_url')); approved_urls={x['idea_list_url'] for x in approved}; new=[dict(x,status='new_not_approved') for x in discovered if x['idea_list_url'] not in approved_urls]; write_csv(REPORTS/'new_lists_found.csv',new,['amazon_title','idea_list_url','status'])
        for entry in approved:
            try:
                title,asins,expected=scrape_list(context,entry); resolved.append({'list_id':entry['list_id'],'configured_name':entry['fallback_name'],'amazon_title':title,'idea_list_url':entry['idea_list_url'],'displayed_count':expected or '','unique_asins':len(asins)})
                for a in asins:memberships[a].add(title)
            except PlaywrightTimeoutError: print(f"Timed out: {entry['fallback_name']}")
            except Exception as e: print(f"Skipped {entry['fallback_name']}: {e}")
        browser.close()
    if not memberships: print('No products extracted.'); return 1
    tok=token(cid,secret); print('\nAmazon Creators API authentication succeeded.'); asins=sorted(memberships); items_by={}; errors=[]
    for n,batch in enumerate(chunks(asins,10),1):
        print(f'Getting product data: batch {n}/{(len(asins)+9)//10}')
        try:items,errs=get_items(tok,tag,batch)
        except RuntimeError as e:
            if 'rate limit' in str(e).lower():time.sleep(35);items,errs=get_items(tok,tag,batch)
            else:raise
        for item in items:
            a=str(item.get('asin') or '').upper()
            if a:items_by[a]=item
        errors.extend(errs);time.sleep(1.1)
    omitted=[a for a in asins if a not in items_by]
    for i,a in enumerate(omitted,1):
        print(f'Retry {i}/{len(omitted)}: {a}')
        try:
            items,errs=get_items(tok,tag,[a]);errors.extend(errs)
            for item in items:
                r=str(item.get('asin') or '').upper()
                if r:items_by[r]=item
        except Exception as e:errors.append({'asin':a,'message':str(e)})
        time.sleep(1.1)
    rows=[meta_row(item,sorted(memberships.get(a,set()))) for a,item in items_by.items()];rows.sort(key=lambda r:r['id']);ready=[r for r in rows if r['id'] and r['title'] and r['link'] and r['image_link'] and r['price']]
    write_csv(OUTPUT/'meta_catalog_all_returned.csv',rows,FIELDS);write_csv(PUBLIC/'meta_catalog.csv',ready,FIELDS);write_csv(REPORTS/'approved_lists_resolved.csv',resolved,['list_id','configured_name','amazon_title','idea_list_url','displayed_count','unique_asins'])
    review=[]
    for a in asins:
        labels=' | '.join(sorted(memberships[a]))
        if a not in items_by:review.append({'asin':a,'amazon_url':f'https://www.amazon.com/dp/{a}?tag={tag}','idea_lists':labels,'issue':'Amazon Creators API returned no product record after retry'})
    for r in rows:
        labels=' | '.join(sorted(memberships[r['id']]))
        if not r['price']:review.append({'asin':r['id'],'amazon_url':f"https://www.amazon.com/dp/{r['id']}?tag={tag}",'idea_lists':labels,'issue':'Amazon Creators API returned the product but no price'})
        if not r['image_link']:review.append({'asin':r['id'],'amazon_url':f"https://www.amazon.com/dp/{r['id']}?tag={tag}",'idea_lists':labels,'issue':'Amazon Creators API returned the product but no main image'})
    write_csv(OUTPUT/'products_needing_review.csv',review,['asin','amazon_url','idea_lists','issue']);(OUTPUT/'api_errors.json').write_text(json.dumps(errors,indent=2),encoding='utf-8')
    report=['HILLARY STYLE META CATALOG RUN REPORT',time.strftime('%Y-%m-%d %H:%M:%S'),' ',f'Approved lists processed: {len(resolved)}',f'Storefront lists discovered: {len(discovered)}',f'Unique ASINs extracted: {len(asins)}',f'Products returned by Amazon API: {len(rows)}',f'Meta-ready products: {len(ready)}',f'Products needing review: {len(review)}',' ',f"PUBLIC FEED: {PUBLIC/'meta_catalog.csv'}"]
    (REPORTS/'latest_run_report.txt').write_text('\n'.join(report),encoding='utf-8');print('\nDONE');[print(x) for x in report[3:]];print(publish(settings));return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:print('\nCancelled.');raise SystemExit(130)
    except Exception as e:print(f'\nERROR: {e}');raise SystemExit(1)
