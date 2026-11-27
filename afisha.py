#!/usr/bin/env python3
"""Free scheduled Saratov digest. No AI/API keys except Telegram token."""
import argparse, concurrent.futures, datetime as dt, hashlib, html, json, os, re, time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup
TZ = ZoneInfo('Europe/Saratov')
ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'preview'
STATE = ROOT / 'state' / 'publication.json'
MONTHS = 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
CITIES = {'saratov':'Саратов', 'engels':'Энгельс', 'balakovo':'Балаково', 'balashov':'Балашов', 'volsk':'Вольск', 'marks':'Маркс'}
KASSIR = 'https://saratov.kassir.ru'
CATEGORIES = ['', '/bilety-na-koncert', '/bilety-v-teatr', '/bilety-na-shou', '/bilety-na-sportivnye-meropriyatiya', '/bilety-v-muzei', '/bilety-na-festival']

def get(url):
    r = requests.get(url, timeout=(10, 35), headers={'User-Agent':'SaratovAfisha/1.0 (+https://t.me/afisha_saratov_oblast)'})
    r.raise_for_status()
    return r.text

def clean(v):
    return re.sub(r'\s+', ' ', str(v or '')).strip()

def safe_url(v):
    return isinstance(v,str) and urlsplit(v).scheme in ('http','https')

def image_url(value):
    if isinstance(value, list):
        return next((u for item in value if (u := image_url(item))), '')
    if isinstance(value, dict):
        return image_url(value.get('contentUrl') or value.get('url'))
    return value if safe_url(value) else ''

def cover_for(events, mode, ref):
    start, end = window(mode, ref)
    choices = [e for e in events if start.isoformat() <= e['date'] <= end.isoformat() and image_url(e.get('image'))]
    # Prefer a not-sold-out event; deterministic selection for each digest.
    choices.sort(key=lambda e: (e.get('sold', False), e['date'], e['time'], e['title']))
    if not choices: return None
    e = choices[0]
    label = {'today':'Сегодня', 'three':'Ближайшие три дня', 'week':'Следующая неделя'}[mode]
    caption = (f'<b>📅 Афиша Саратова и области · {label}</b>\n'
               f'{start:%d.%m.%Y} — {end:%d.%m.%Y}\n\n'
               f'На постере: <b>{html.escape(e["title"][:180])}</b>\n'
               f'{e["date"][8:10]}.{e["date"][5:7]} · {e["time"]} · {html.escape(e["city"])}\n'
               f'<a href="{html.escape(e["url"], quote=True)}">Страница события / источник постера</a>\n\n'
               '<a href="https://t.me/afisha_saratov_oblast">Афиша Саратова и области</a>')
    return {'photo': e['image'], 'caption': caption}

def preview_html(chunks, cover):
    media = ('<article><img style="max-width:100%;max-height:800px;object-fit:contain" src="'
             + html.escape(cover['photo'], quote=True) + '"><br>' + cover['caption'] + '</article>') if cover else ''
    return ('<!doctype html><meta charset="utf-8"><style>body{max-width:760px;margin:32px auto;font:18px/1.5 Arial;background:#f4f5f7}article{white-space:pre-wrap;background:white;padding:24px;margin:20px;border-radius:16px}a{color:#1262ac}</style>'
            + media + ''.join('<article>'+c+'</article>' for c in chunks))

def walk(v):
    if isinstance(v,dict):
        yield v
        for x in v.values(): yield from walk(x)
    elif isinstance(v,list):
        for x in v: yield from walk(x)

def kassir_parse(text, ref):
    soup=BeautifulSoup(text,'html.parser'); result=[]
    for tag in soup.select('script[type="application/ld+json"]'):
        try: data=json.loads(tag.string or tag.get_text())
        except (ValueError,TypeError): continue
        for x in walk(data):
            typ=x.get('@type',[])
            if 'Event' not in ([typ] if isinstance(typ,str) else typ): continue
            if any(s in str(x.get('eventStatus','')) for s in ('Cancelled','Postponed','Rescheduled')): continue
            try:
                # KASSIR labels local wall times as +00:00; verified against visible cards.
                start=dt.datetime.fromisoformat(x['startDate'].replace('Z','+00:00')).replace(tzinfo=TZ)
            except (KeyError,ValueError,TypeError): continue
            loc=x.get('location') or {}; address=loc.get('address','')
            if isinstance(address,dict): address=', '.join(clean(address.get(k)) for k in ('addressLocality','streetAddress') if address.get(k))
            city=next((c for c in CITIES.values() if re.search(r'\b'+re.escape(c)+r'\b', str(address), re.I)),None)
            if not city: continue # no guesses about geography
            offers=x.get('offers') or {}; offers=offers if isinstance(offers,list) else [offers]
            prices=[]; sold=False
            for offer in offers:
                if not isinstance(offer,dict): continue
                sold |= 'SoldOut' in str(offer.get('availability',''))
                try: prices.append(float(offer.get('lowPrice',offer.get('price'))))
                except (ValueError,TypeError): pass
            price=('от '+f'{min(prices):g}'+' ₽') if prices and min(prices)>0 else 'цену уточняйте'
            if prices and min(prices)==0: price='0 ₽ в карточке — условия уточняйте'
            url=x.get('url',''); name=clean(x.get('name'))
            if name and safe_url(url):
                result.append(dict(date=start.date().isoformat(),time=start.strftime('%H:%M'),city=city,title=name,venue=clean(loc.get('name')),address=clean(address),price=price,url=url,sold=sold,image=image_url(x.get('image')),source='KASSIR.RU'))
    return result

def quick_parse(text, city, ref):
    soup=BeautifulSoup(text,'html.parser'); result=[]
    for card in soup.select('.quick-tickets-event-card'):
        title=card.select_one('.event-card__title .underline')
        if not title: continue
        venue=card.select_one('.quick-tickets-event-location__organiser dd')
        address=card.select_one('.quick-tickets-event-location__address dd')
        age=card.select_one('.ageRestriction')
        poster = ''
        for button in card.select('[data-image-preview-src]'):
            if button.find_parent(class_='quick-tickets-event-location'): continue
            poster = image_url(button.get('data-image-preview-src'))
            if poster: break
        for img in card.select('img'):
            if poster: break
            if img.find_parent(class_='quick-tickets-event-location'): continue
            candidate = urljoin('https://quicktickets.ru', img.get('data-src') or img.get('src') or '')
            if safe_url(candidate) and '/files/' in candidate:
                poster = candidate; break
        for session in card.select('.sessions__session'):
            label=session.select_one('.quick-tickets-session-option__label'); a=session.select_one('a[href]')
            if not label or not a: continue
            m=re.search(r'(\d{1,2})\s+([а-яё]+)(?:\s+(20\d{2}))?.*?(\d{2}:\d{2})',label.get_text(' ',strip=True),re.I)
            if not m or m[2] not in MONTHS: continue
            year=int(m[3]) if m[3] else ref.year
            month=MONTHS.index(m[2])+1
            if not m[3] and ref.month==12 and month==1: year+=1
            try: date=dt.date(year,month,int(m[1]))
            except ValueError: continue
            result.append(dict(date=date.isoformat(),time=m[4],city=city,title=clean(title.text)+((' '+clean(age.text)) if age else ''),venue=clean(venue.text if venue else ''),address=clean(address.text if address else ''),price='цену уточняйте',url=urljoin('https://quicktickets.ru',a['href']),sold=bool(session.select_one('.session-sold-label')),image=poster,source='Quick Tickets'))
    return result

def collect(ref):
    jobs=[(KASSIR+p,'k',None) for p in CATEGORIES]
    jobs += [(f'https://quicktickets.ru/{slug}/afisha','q',city) for slug,city in CITIES.items()]
    result=[]; report=[]
    def run(job):
        url,kind,city=job
        try:
            text=get(url); events=kassir_parse(text,ref) if kind=='k' else quick_parse(text,city,ref)
            return events,dict(url=url,count=len(events),status='ok' if events else 'empty_or_changed')
        except Exception as ex:
            return [],dict(url=url,status='error',error=type(ex).__name__)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for events,status in pool.map(run,jobs): result.extend(events); report.append(status)
    unique={}
    for e in result:
        if not ref <= dt.date.fromisoformat(e['date']) <= ref+dt.timedelta(days=14): continue
        # Source-specific entries stay distinct unless title, venue, date and time agree.
        key=tuple(re.sub(r'[^\w]','',e[k].casefold()) for k in ('date','time','city','title','venue'))
        if key not in unique: unique[key]=e
    return sorted(unique.values(),key=lambda e:(e['date'],e['city']!='Саратов',e['city'],e['time'],e['title'])),report

def window(mode,ref):
    if mode=='today': return ref,ref
    if mode=='three': return ref+dt.timedelta(days=1),ref+dt.timedelta(days=3)
    start=ref+dt.timedelta(days=7-ref.weekday())
    return start,start+dt.timedelta(days=6)

def weather(start,end):
    try:
        url='https://api.open-meteo.com/v1/forecast?latitude=51.53&longitude=46.03&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Europe%2FSaratov&forecast_days=14'
        daily=json.loads(get(url))['daily']; rows=[]
        for i,date in enumerate(daily['time']):
            if start.isoformat()<=date<=end.isoformat():
                lo=daily['temperature_2m_min'][i]; hi=daily['temperature_2m_max'][i]; p=daily['precipitation_probability_max'][i]
                if None not in (lo,hi,p): rows.append(f'{date[8:10]}.{date[5:7]}: {lo:+.0f}…{hi:+.0f} °C, вероятность осадков {p}%')
        return '<b>🌦 Прогноз для Саратова</b>\n'+'\n'.join(rows)+'\nДанные: Open-Meteo. В области погода может отличаться.' if rows else ''
    except Exception: return ''

def render(events,mode,ref,forecast=''):
    start,end=window(mode,ref)
    label={'today':'Сегодня','three':'Ближайшие три дня','week':'Следующая неделя'}[mode]
    title=f'📅 Афиша Саратова и области · {label}\n{start:%d.%m.%Y}'+(f' — {end:%d.%m.%Y}' if start!=end else '')
    blocks=[]; selected=[e for e in events if start.isoformat()<=e['date']<=end.isoformat()]
    prev=None
    for e in selected:
        group=(e['date'],e['city']); heading=''
        if group!=prev:
            d=dt.date.fromisoformat(e['date']); heading=f'<b>{d:%d.%m} · {html.escape(e["city"])}</b>\n';prev=group
        sold=' · ⚠️ мест нет по данным площадки' if e['sold'] else ''
        blocks.append(heading+f'<b>{e["time"]} — {html.escape(e["title"][:220])}</b>\n📍 {html.escape(e["venue"][:200])}\n{html.escape(e["address"][:200])}\n🎟 {html.escape(e["price"])}{sold}\n<a href="{html.escape(e["url"],quote=True)}">Подробнее и билеты</a>')
    if not selected: blocks=['Подтверждённых событий на эти даты в подключённых источниках не найдено. Это не означает, что в городе ничего не проходит.']
    if forecast: blocks.append(forecast)
    blocks.append('Подборка по подключённым афишам, охват неполный. Время саратовское. Цены и наличие билетов могут измениться — проверьте страницу события.\n\n<a href="https://t.me/afisha_saratov_oblast">Афиша Саратова и области</a>')
    # Raw HTML length capped too: this is stricter than Telegram's parsed-text limit.
    chunks=[]; chunk=html.escape(title)+'\n\n'
    for block in blocks:
        if len(chunk)+len(block)>3400:
            chunks.append(chunk);chunk=html.escape(title)+' · продолжение\n\n'
        chunk+=block+'\n\n'
    if chunk.strip():chunks.append(chunk)
    return chunks

def save_state(state):
    STATE.parent.mkdir(exist_ok=True); temp=STATE.with_suffix('.tmp');temp.write_text(json.dumps(state,ensure_ascii=False,indent=2));temp.replace(STATE)

def publish(chunks,key,cover=None):
    token=os.environ.get('TELEGRAM_BOT_TOKEN','');chat=os.environ.get('TELEGRAM_CHAT_ID','')
    if not token or not chat: raise RuntimeError('Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets')
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    record=state.setdefault(key,{'parts':{},'payload':([cover] if cover else [])+chunks})
    # Persist the original payload: a retry must not reshuffle already sent parts.
    chunks=record['payload']
    for i,chunk in enumerate(chunks):
        status=record['parts'].get(str(i))
        if status in ('sent','skipped'):continue
        if status=='sending': raise RuntimeError('Ambiguous previous delivery. Check channel and state/publication.json before retrying.')
        record['parts'][str(i)]='sending';save_state(state)
        try:
            is_photo = isinstance(chunk,dict) and 'photo' in chunk
            method = 'sendPhoto' if is_photo else 'sendMessage'
            payload = {'chat_id':chat,'parse_mode':'HTML'}
            if is_photo: payload.update(photo=chunk['photo'],caption=chunk['caption'])
            else: payload.update(text=chunk,link_preview_options={'is_disabled':True})
            r=requests.post(f'https://api.telegram.org/bot{token}/{method}',json=payload,timeout=(10,40))
            data=r.json()
        except Exception:
            raise RuntimeError('Telegram delivery uncertain; automatic retry disabled') from None
        if not data.get('ok') and is_photo and data.get('error_code')==400:
            description=str(data.get('description','')).lower()
            if any(term in description for term in ('photo','image','wrong file','failed to get http','webpage_curl_failed','media_empty','file must','file is too big','content type','url host')):
                record['parts'][str(i)]='skipped';save_state(state)
                print('Poster unavailable to Telegram; continuing with text digest')
                continue
        if not data.get('ok'):
            record['parts'][str(i)]='failed';save_state(state)
            raise RuntimeError('Telegram rejected message: '+str(data.get('error_code')))
        record['parts'][str(i)]='sent';save_state(state);time.sleep(1.1)
    print(f'Published / already published: {len(chunks)} parts')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--mode',choices=['today','three','week','all'],default='all');ap.add_argument('--publish',action='store_true');ap.add_argument('--date');args=ap.parse_args()
    ref=dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(TZ).date()
    if args.publish and (args.mode=='all' or args.date):ap.error('Publish requires one mode and the current date')
    OUT.mkdir(exist_ok=True); events,report=collect(ref)
    (OUT/'sources.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (OUT/'events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
    modes=['today','three','week'] if args.mode=='all' else [args.mode]
    for mode in modes:
        start,end=window(mode,ref);chunks=render(events,mode,ref,weather(start,end));cover=cover_for(events,mode,ref)
        (OUT/f'{mode}.html').write_text(preview_html(chunks,cover))
        (OUT/f'{mode}-cover.json').write_text(json.dumps(cover,ensure_ascii=False,indent=2))
        (OUT/f'{mode}.txt').write_text('\n\n--- СЛЕДУЮЩЕЕ СООБЩЕНИЕ ---\n\n'.join(chunks))
        print(mode, start,end, 'messages',len(chunks))
        if args.publish:
            if not any(r['status']=='ok' for r in report):raise RuntimeError('All sources failed; publication stopped')
            # Never publish a weather-only digest when source coverage is absent.
            if not any(start.isoformat()<=e['date']<=end.isoformat() for e in events):raise RuntimeError('No verified events in this window; publication stopped, inspect sources.json')
            publish(chunks,ref.isoformat()+':'+mode,cover)
    print('Events next 14 days:',len(events),'Sources OK:',sum(r['status']=='ok' for r in report),'/',len(report))

if __name__=='__main__':main()
