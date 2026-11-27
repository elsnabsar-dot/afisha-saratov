#!/usr/bin/env python3
"""One notable event daily; public Yandex pages; real downloaded event poster."""
import argparse, datetime as dt, html, io, json, os, re, time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'concert-preview'; STATE=ROOT/'state'/'concerts.json'
TZ=ZoneInfo('Europe/Saratov'); BASE='https://afisha.yandex.ru'
PAGES=['/saratov/events','/saratov/concert','/saratov/standup']
# Configurable seed list, not a complete ranking of artists. Top badges and hot tours also qualify.
ARTISTS='руки вверх|сергей лазарев|лазарев|баста|macan|макан|дмитрий маликов|маликов|мельница|княzz|князь|би 2|би-2|сплин|ддт|кипелов|ария|алиса|пикник|звери|мумий тролль|ленинград|леонид агутин|агутин|валерий меладзе|меладзе|полина гагарина|гагарина|елка|ёлка|zivert|з чеботина|люся чеботина|анна асти|anna asti|jony|hammali|navai|мот|егор крид|клава кока|дима билан|билан|лепс|стас михайлов|любэ|любэ|хор турецкого|султан лагучев|слава|валерия|земфира|ночные снайперы|диана арбенина|чайф|чичерина|lumen|animal джаz|25/17|три дня дождя|тринадцать карат|iowa|dabro|моя мишель|комната культуры|градусы|дискотека авария|отпетые мошенники|иванушки|на-на|serebro|винтаж|хлеб|the hatters|гудтаймс|shortparis|pyrokinesis|noize mc|oxxxymiron|скриптонит|jah khalib|мари краймбрери|мэйби бэйби|pepel nahudi|виктор комаров|нурлан сабуров|иван абрамов|сергей орлов|алексей щербаков|руслан белый|дмитрий романов|стас старовойтов|денис дорохов|карина мейханаджян|евгений чебатков'.split('|')
EXCLUDE=re.compile(r'трибьют|tribute|кавер|cover|отч[её]тн|песни\s+(?:группы|в\s+исполнении)|симфонические\s+хиты|хиты\s+(?:группы|в\s+исполнении)',re.I)

def norm(s):return re.sub(r'[^\w]+',' ',str(s).casefold().replace('ё','е')).strip()
def known(title):
    title=' '+norm(title)+' '
    return any(' '+norm(a)+' ' in title for a in ARTISTS)
def get(url):
    r=requests.get(url,timeout=(10,30),headers={'User-Agent':'Mozilla/5.0 (compatible; SaratovAfisha/1.1)'})
    r.raise_for_status()
    if 'showcaptcha' in r.url.lower() or 'подтвердите, что вы не робот' in r.text.lower():raise RuntimeError('Yandex captcha; no bypass attempted')
    return r.text

def apollo(text):
    soup=BeautifulSoup(text,'html.parser')
    for tag in soup.find_all('script'):
        t=tag.string or tag.get_text()
        if re.match(r"\s*window\[['\"]__APOLLO_STATE__['\"]\]\s*=",t):
            obj=json.JSONDecoder().raw_decode(t.split('=',1)[1].lstrip())[0]
            if isinstance(obj,dict):return soup,obj
    raise RuntimeError('Yandex page structure changed or data unavailable')

def images(obj):
    if not isinstance(obj,dict):return ''
    origin=obj.get('image({"size":"origin"})') or {}
    if origin.get('url'):return origin['url']
    sizes=[x for x in obj.values() if isinstance(x,dict) and x.get('url') and x.get('width')]
    return max(sizes,key=lambda x:x['width']*x.get('height',1))['url'] if sizes else ''

def list_candidates(text):
    soup,data=apollo(text); tops=set()
    for card in soup.select('.eventCardInner'):
        a=card.select_one('[data-test-id="eventCard.link"]')
        if a and card.select_one('[data-test-id="topEventBadge.inner"]'):tops.add(a.get('href'))
    out=[]
    for v in data.values():
        if not isinstance(v,dict) or v.get('__typename')!='EventPreview':continue
        url=v.get('url','');kind=(v.get('type') or {}).get('code')
        if not url.startswith('/saratov/') or kind not in ('concert','standup','festival'):continue
        title=v.get('title','');reason=[]
        if EXCLUDE.search(title):continue
        if url in tops:reason.append('Отметка «Топ» в карточке Яндекса')
        if known(title):reason.append('Совпадение со списком известных исполнителей')
        codes=set()
        for k,tags in v.items():
            if k.startswith('tags(') and isinstance(tags,list):codes.update(t.get('code') for t in tags if isinstance(t,dict))
        if {'artist-tour','hot'}<=codes:reason.append('Тур артиста с отметкой hot в данных Яндекса')
        if not reason:continue
        out.append(dict(id=v['id'],title=title,url=urljoin(BASE,url),reason=reason,score=(100 if url in tops else 0)+(60 if known(title) else 0)+(20 if {'artist-tour','hot'}<=codes else 0),image=images(v.get('image')),age=v.get('contentRating') or '',kind=kind))
    return out

def details(candidate,text,ref):
    soup,data=apollo(text);event=data.get('Event:'+candidate['id']) or {}
    for tag in soup.select('script[type="application/ld+json"]'):
        try: info=json.loads(tag.string or tag.get_text())
        except (ValueError,TypeError): continue
        if isinstance(info,dict) and any(t in str(info.get('eventStatus','')) for t in ('Cancelled','Postponed','Rescheduled')): return []
    # Do not use JSON-LD startDate/endDate: these may describe the entire tour.
    root=data.get('ROOT_QUERY',{});out=[]
    for key,schedule in root.items():
        if not key.startswith('eventScheduleOther(') or not isinstance(schedule,dict):continue
        if candidate['id'] not in key:continue
        for group in schedule.get('byDate',[]):
            for item in group.get('sessions',[]):
                session=item.get('session') or {};stamp=session.get('datetime')
                try:when=dt.datetime.fromisoformat(stamp).replace(tzinfo=TZ)
                except (ValueError,TypeError):continue
                if not ref<=when.date()<=ref+dt.timedelta(days=180):continue
                place=data.get((item.get('place') or {}).get('__ref'),{})
                city=data.get((place.get('city') or {}).get('__ref'),{})
                if city.get('id')!='saratov':continue
                ticket=session.get('ticket') or {}
                if ticket.get('saleStatus')!='available':continue
                price=ticket.get('price') or {}; value=price.get('min')
                price_text=f'от {value/100:,.0f} ₽'.replace(',',' ') if isinstance(value,(int,float)) and price.get('currency')=='rub' else 'цену уточняйте на странице'
                e=dict(candidate);e.update(date=when.date().isoformat(),time=when.strftime('%H:%M'),venue=place.get('title',''),address=place.get('address',''),price=price_text,image=images(event.get('image')) or candidate.get('image',''))
                e['key']=e['id']+':'+e['date']+':'+str(place.get('id',''))
                if e['image']:out.append(e)
    return out

def download_image(url):
    if urlsplit(url).hostname!='avatars.mds.yandex.net':raise RuntimeError('Unrecognized poster host')
    with requests.get(url,stream=True,timeout=(10,30)) as r:
        r.raise_for_status();blob=bytearray()
        for piece in r.iter_content(65536):
            blob.extend(piece)
            if len(blob)>12_000_000:raise RuntimeError('Poster too large')
    with Image.open(io.BytesIO(blob)) as im:
        if im.width*im.height>25_000_000:raise RuntimeError('Poster pixel limit')
        im=ImageOps.exif_transpose(im).convert('RGB');im.thumbnail((1800,1800))
        if min(im.size)<100:raise RuntimeError('Poster too small')
        out=io.BytesIO();im.save(out,format='JPEG',quality=90);return out.getvalue()

def caption(e):
    d=dt.date.fromisoformat(e['date'])
    # No invented artist biography or "sold out soon" claims.
    return (f'<b>🎤 {html.escape(e["title"][:160])}</b> {html.escape(e["age"])}\n\n'
            f'📅 {d:%d.%m.%Y} · {e["time"]}\n'
            f'📍 {html.escape(e["venue"][:130])}\n{html.escape(e["address"][:140])}\n'
            f'🎟 {html.escape(e["price"])}\n\n'
            f'<a href="{html.escape(e["url"],quote=True)}">Подробнее и билеты · Яндекс Афиша</a>\n'
            'Время саратовское. Цена и наличие билетов могут измениться.\n\n'
            '<a href="https://t.me/afisha_saratov_oblast">Афиша Саратова и области</a>')

def save(s):
    STATE.parent.mkdir(exist_ok=True);tmp=STATE.with_suffix('.tmp');tmp.write_text(json.dumps(s,ensure_ascii=False,indent=2));tmp.replace(STATE)
def send(e,poster,state,ref):
    token=os.environ.get('TELEGRAM_BOT_TOKEN','');chat=os.environ.get('TELEGRAM_CHAT_ID','')
    if not token or not chat:raise RuntimeError('Missing Telegram secrets')
    c=caption(e)
    if len(c)>1024:raise RuntimeError('Caption too long')
    # Reserve day and event before network call; an uncertain result must not be retried blindly.
    key=e['key'];state['events'][key]={'status':'sending','day':str(ref),'title':e['title'],'url':e['url']};save(state)
    try:
        r=requests.post(f'https://api.telegram.org/bot{token}/sendPhoto',data={'chat_id':chat,'caption':c,'parse_mode':'HTML','reply_markup':json.dumps({'inline_keyboard':[[{'text':'🎟 Подробнее и билеты','url':e['url']}]]})},files={'photo':('concert.jpg',poster,'image/jpeg')},timeout=(10,50));result=r.json()
    except Exception:raise RuntimeError('Delivery uncertain; check channel and state/concerts.json before retrying') from None
    if not result.get('ok'):
        if r.status_code>=500:raise RuntimeError('Telegram server error; delivery uncertain')
        state['events'][key]['status']='failed';save(state)
        raise RuntimeError('Telegram rejected post, code '+str(result.get('error_code')))
    state['events'][key]['status']='sent';save(state);print('Published concert:',e['title'])

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--publish',action='store_true');args=ap.parse_args()
    ref=dt.datetime.now(TZ).date();OUT.mkdir(exist_ok=True)
    state=json.loads(STATE.read_text()) if STATE.exists() else {'events':{}}
    if args.publish and any(x.get('day')==str(ref) and x.get('status') in ('sent','sending') for x in state['events'].values()):
        print('Day already published or delivery uncertain; no new post');return
    found={};report=[]
    for path in PAGES:
        try:
            items=list_candidates(get(BASE+path));report.append({'url':BASE+path,'candidates':len(items)})
            for x in items:
                if x['id'] not in found or x['score']>found[x['id']]['score']:found[x['id']]=x
        except Exception as ex:report.append({'url':BASE+path,'error':type(ex).__name__})
    (OUT/'sources.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if not found:raise RuntimeError('No verified notable candidates; inspect sources.json. No post sent.')
    verified=[]
    for x in sorted(found.values(),key=lambda e:(-e['score'],e['title']))[:14]:
        try:verified.extend(details(x,get(x['url']),ref))
        except Exception as ex:report.append({'url':x['url'],'detail_error':type(ex).__name__})
        # Bounded detail requests: enough variety for one post; full catalogue is not claimed.
        if len(verified)>=8:break
        time.sleep(.3)
    verified.sort(key=lambda e:(-e['score'],e['date'],e['time'],e['title']))
    (OUT/'sources.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (OUT/'candidates.json').write_text(json.dumps(verified,ensure_ascii=False,indent=2))
    cards=[];chosen=None;poster=None
    for e in verified:
        status=state['events'].get(e['key'],{}).get('status')
        if status in ('sent','sending'):continue
        try:raw=download_image(e['image'])
        except Exception as ex:
            report.append({'url':e['url'],'poster_error':type(ex).__name__});continue
        n=len(cards)+1;(OUT/f'poster-{n}.jpg').write_bytes(raw)
        cards.append('<article><img src="poster-'+str(n)+'.jpg"><p>'+caption(e).replace('\n','<br>')+'</p><small>Причина отбора: '+html.escape('; '.join(e['reason']))+'</small></article>')
        if chosen is None:chosen=e;poster=raw
        if args.publish or len(cards)==3:break
    (OUT/'sources.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (OUT/'concerts.html').write_text('<!doctype html><meta charset="utf-8"><style>body{max-width:700px;margin:30px auto;font:18px/1.5 Arial;background:#eef1f5}article{background:white;padding:24px;margin:20px;border-radius:16px}img{max-width:100%}small{color:#666}</style><h2>Большие концерты — предпросмотр</h2><p>Отправляется один новый анонс за запуск, максимум один в день.</p>'+''.join(cards or ['Нет новых событий с доступными постерами.']))
    if args.publish:
        if chosen:send(chosen,poster,state,ref)
        else:print('No new eligible event with poster; no post sent')
    print('Candidate artists:',len(found),'Verified sessions:',len(verified),'Previews:',len(cards))

if __name__=='__main__':main()
