"""ALPHAQUANT V6.2 — BTC-USDT-SWAP signal-only research engine.
No exchange orders. OHLCV flow/volume profile are PROXIES, not trade-level data.
STRICT rejects unavailable macro, aggressor, historical spread inputs.
Run once every 15 minutes (cron/scheduler). Uses confirmed OKX candles only.
"""
from __future__ import annotations
import json, logging, math, os, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

URL = 'https://www.okx.com'
SYMBOL = 'BTC-USDT-SWAP'
TF15_MS, TF1H_MS, TF4H_MS, TF5_MS = 900000, 3600000, 14400000, 300000
MODE = os.getenv('GATE_MODE', 'PROXY').upper()
EQUITY = float(os.getenv('ACCOUNT_EQUITY', '1000'))
RISK = float(os.getenv('BASE_RISK', '0.005'))
FEE = float(os.getenv('FEE_PER_SIDE', '0.0005'))
SLIP = float(os.getenv('SLIP_PER_SIDE', '0.0002'))
MIN_NET_RR = float(os.getenv('GEOM_MIN_NET_RR', '1.0'))
MAX_OPEN = int(os.getenv('MAX_OPEN', '1'))
MAX_LEVERAGE = float(os.getenv('MAX_NOTIONAL_LEVERAGE', '3'))
HISTORY = Path(os.getenv('SIGNAL_HISTORY_FILE', 'signals_history_v62.json'))
TOKEN = os.getenv('TELEGRAM_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('BOT_TOKEN')
CHAT = os.getenv('CHAT_ID') or os.getenv('TELEGRAM_CHAT_ID') or os.getenv('TELEGRAM_CHAT')
LOG = logging.getLogger('ALPHAQUANT_V62')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'ALPHAQUANT-V6.2-Research'})
LONDON, NEW_YORK = ZoneInfo('Europe/London'), ZoneInfo('America/New_York')


def request(path, params=None):
    for attempt in range(3):
        try:
            r = SESSION.get(URL + path, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get('code') == '0':
                return data.get('data', [])
            LOG.warning('OKX error: %s', data)
        except (requests.RequestException, ValueError) as ex:
            LOG.warning('API attempt %s: %s', attempt + 1, ex)
        time.sleep(1 + attempt)
    return []


def candles(bar, limit=300):
    # OKX market/candles has a maximum of 300; confirmed bars only.
    rows = request('/api/v5/market/candles', {'instId': SYMBOL, 'bar': bar, 'limit': min(limit, 300)})
    out = []
    for x in rows:
        try:
            if x[8] == '1':
                out.append([int(x[0]), *[float(x[i]) for i in (1, 2, 3, 4, 5)]])
        except (ValueError, TypeError, IndexError):
            pass
    return sorted(out, key=lambda x: x[0])


def tick_size():
    rows = request('/api/v5/public/instruments', {'instType': 'SWAP', 'instId': SYMBOL})
    try:
        return float(rows[0]['tickSz'])
    except (IndexError, KeyError, TypeError, ValueError):
        return 0.1


def notify(msg):
    if not TOKEN or not CHAT:
        LOG.info('Telegram disabled: %s', msg[:180].replace('\n', ' | '))
        return
    try:
        r = SESSION.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={
            'chat_id': CHAT, 'text': msg, 'disable_web_page_preview': True}, timeout=15)
        r.raise_for_status()
    except requests.RequestException as ex:
        LOG.warning('Telegram: %s', ex)


def load():
    if not HISTORY.exists():
        return {'open': [], 'closed': [], 'equity': EQUITY, 'peak_equity': EQUITY}
    with HISTORY.open(encoding='utf-8') as f:
        h = json.load(f)
    h.setdefault('open', []); h.setdefault('closed', [])
    h.setdefault('equity', EQUITY); h.setdefault('peak_equity', max(EQUITY, h['equity']))
    return h


def save(h):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_name(HISTORY.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(h, f, indent=2, ensure_ascii=False)
    os.replace(tmp, HISTORY)


def ema(v, n):
    o = [None] * len(v)
    if len(v) < n: return o
    x = sum(v[:n]) / n; o[n-1] = x
    for i in range(n, len(v)):
        x += (v[i] - x) * 2 / (n+1); o[i] = x
    return o


def wilder(v, n):
    o = [None] * len(v)
    valid = [i for i, x in enumerate(v) if x is not None]
    if len(valid) < n: return o
    first = valid[n-1]
    x = sum(v[i] for i in valid[:n]) / n
    o[first] = x
    for i in range(first+1, len(v)):
        if v[i] is not None:
            x = (x * (n-1) + v[i]) / n
            o[i] = x
    return o


def atr(cs, n=14):
    if not cs: return []
    tr = [cs[0][2]-cs[0][3]]
    for i in range(1, len(cs)):
        tr.append(max(cs[i][2]-cs[i][3], abs(cs[i][2]-cs[i-1][4]), abs(cs[i][3]-cs[i-1][4])))
    return wilder(tr, n)


def rsi(cs, n=14):
    if not cs: return []
    diff = [0.] + [cs[i][4]-cs[i-1][4] for i in range(1,len(cs))]
    g, l = wilder([max(x,0) for x in diff], n), wilder([max(-x,0) for x in diff], n)
    return [None if a is None or b is None else (50. if a == b == 0 else 100. if b == 0 else 100-100/(1+a/b)) for a,b in zip(g,l)]


def adx(cs, n=14):
    m=len(cs)
    if not m: return []
    plus=[0.]*m; minus=[0.]*m; tr=[0.]*m
    for i in range(1,m):
        up=cs[i][2]-cs[i-1][2]; down=cs[i-1][3]-cs[i][3]
        plus[i]=up if up>down and up>0 else 0.
        minus[i]=down if down>up and down>0 else 0.
        tr[i]=max(cs[i][2]-cs[i][3],abs(cs[i][2]-cs[i-1][4]),abs(cs[i][3]-cs[i-1][4]))
    a,p,q=wilder(tr,n),wilder(plus,n),wilder(minus,n)
    dx=[None]*m
    for i in range(m):
        if a[i] and p[i] is not None and q[i] is not None:
            dx[i]=100*abs(p[i]-q[i])/max(p[i]+q[i],1e-12)
    return wilder(dx,n)


def pivots(cs, high=True, left=2, right=2):
    col=2 if high else 3
    return [i for i in range(left,len(cs)-right) if all((cs[i][col]>cs[j][col] if high else cs[i][col]<cs[j][col]) for j in range(i-left,i+right+1) if j!=i)]


def structure(h4):
    hi=pivots(h4,True,3,3); lo=pivots(h4,False,3,3)
    if len(hi)<2 or len(lo)<2: return 'UNKNOWN'
    hh=h4[hi[-1]][2]>h4[hi[-2]][2]; hl=h4[lo[-1]][3]>h4[lo[-2]][3]
    lh=h4[hi[-1]][2]<h4[hi[-2]][2]; ll=h4[lo[-1]][3]<h4[lo[-2]][3]
    return 'HH_HL' if hh and hl else 'LH_LL' if lh and ll else 'MIXED'


def regime(h1,h4):
    if len(h1)<210 or len(h4)<20: return 'UNCLEAR', 'insufficient HTF candles'
    cl=[c[4] for c in h1]; a=atr(h1); ax=adx(h1)
    e20,e50,e200=ema(cl,20),ema(cl,50),ema(cl,200)
    if a[-1] is None or ax[-1] is None: return 'UNCLEAR','indicators unavailable'
    ratios=[a[i]/cl[i] for i in range(len(cl)) if a[i] is not None and cl[i]>0]
    rank=100*sum(v<=a[-1]/cl[-1] for v in ratios[-500:])/len(ratios[-500:])
    tr=max(h1[-1][2]-h1[-1][3],abs(h1[-1][2]-cl[-2]),abs(h1[-1][3]-cl[-2]))
    vma=sum(c[5] for c in h1[-20:])/20
    if rank>=90 or tr>=2.5*a[-1] or h1[-1][5]>=2*vma: return 'SPIKE',f'ATR percentile={rank:.0f}'
    if all(v is not None and v<20 for v in ax[-24:]): return 'STANDBY','ADX<20 for 24 hours'
    st=structure(h4); v=ax[-1]
    if v>=25 and e20[-1]>e50[-1]>e200[-1] and st=='HH_HL': return 'STRONG_UP',f'ADX={v:.1f}'
    if v>=25 and e20[-1]<e50[-1]<e200[-1] and st=='LH_LL': return 'STRONG_DN',f'ADX={v:.1f}'
    slope=e50[-1]-e50[-7]
    if 20<=v<25 and e20[-1]>e50[-1] and slope>0 and st!='LH_LL': return 'WEAK_UP',f'ADX={v:.1f}'
    if 20<=v<25 and e20[-1]<e50[-1] and slope<0 and st!='HH_HL': return 'WEAK_DN',f'ADX={v:.1f}'
    return ('RANGE' if v<20 else 'UNCLEAR'),f'ADX={v:.1f}, structure={st}'


def stoch_rsi(rs):
    st=[None]*len(rs)
    for i in range(13,len(rs)):
        w=rs[i-13:i+1]
        if any(v is None for v in w): continue
        lo,hi=min(w),max(w)
        st[i]=50. if hi==lo else 100*(rs[i]-lo)/(hi-lo)
    def sma3(v):
        return [sum(v[i-2:i+1])/3 if i>=2 and all(x is not None for x in v[i-2:i+1]) else None for i in range(len(v))]
    k=sma3(st)
    return k,sma3(k)


def session_vwap(cs):
    d=datetime.fromtimestamp(cs[-1][0]/1000,timezone.utc).date()
    bars=[c for c in cs if datetime.fromtimestamp(c[0]/1000,timezone.utc).date()==d]
    v=sum(c[5] for c in bars)
    return sum((c[2]+c[3]+c[4])/3*c[5] for c in bars)/v if v else None


def delta_proxy(cs,tick):
    return [c[5]*(c[4]-c[1])/max(c[2]-c[3],tick) for c in cs]


def session_ok(ts):
    # Decide at the close of the confirmed signal candle.
    t=datetime.fromtimestamp((ts+TF15_MS)/1000,timezone.utc)
    lt=t.astimezone(LONDON); ny=t.astimezone(NEW_YORK)
    return 8<=lt.hour<16 and 8<=ny.hour<17


def gate_pipeline(c15,c1h,c4h,tick,direction):
    """Each gate returns first failure; levels are frozen before sweep candle.
    Signals require sweep -> reclaim -> BOS within a 9-bar rolling window.
    Momentum confirmation can occur within 6 bars of signal.
    """
    g={}; side=1 if direction=='LONG' else -1
    if len(c15)<120 or len(c1h)<210 or len(c4h)<20:
        return None,'G0','insufficient 15m/1H/4H data',g
    if MODE=='STRICT': return None,'G0','macro, spread, true aggressor UNKNOWN',g
    if MODE!='PROXY': return None,'G0','invalid GATE_MODE',g
    g['G0']='PROXY (macro/spread skipped; flow approximated)'
    sig=c15[-1]; close_ms=sig[0]+TF15_MS
    if not session_ok(sig[0]): return None,'G1','outside London/NY overlap',g
    g['G1']='PASS'
    h1=[c for c in c1h if c[0]+TF1H_MS<=close_ms]
    h4=[c for c in c4h if c[0]+TF4H_MS<=close_ms]
    reg,detail=regime(h1,h4)
    allowed=('STRONG_UP','WEAK_UP') if side==1 else ('STRONG_DN','WEAK_DN')
    if reg not in allowed: return None,'Regime',f'{reg}: {detail}',g
    g['G2']=reg
    a=atr(c15)[-1]
    if a is None or a<=0: return None,'G0','ATR unavailable',g
    # Evaluate sweep candidates only in previous 9 bars, with HTF levels known BEFORE each sweep.
    found=None
    for i in range(max(25,len(c15)-9),len(c15)):
        bar=c15[i]
        known=[c for c in c1h if c[0]+TF1H_MS<=bar[0]]
        if len(known)<21: continue
        levels={'1H_20bar_low' if side==1 else '1H_20bar_high':
                min(c[3] for c in known[-20:]) if side==1 else max(c[2] for c in known[-20:])}
        day=datetime.fromtimestamp(bar[0]/1000,timezone.utc).date()
        asia=[c for c in c15[:i] if datetime.fromtimestamp(c[0]/1000,timezone.utc).date()==day and datetime.fromtimestamp(c[0]/1000,timezone.utc).hour<8]
        if asia: levels['ASIA_LOW' if side==1 else 'ASIA_HIGH']=min(c[3] for c in asia) if side==1 else max(c[2] for c in asia)
        for name,lv in levels.items():
            breached=(bar[3]<lv-0.10*a) if side==1 else (bar[2]>lv+0.10*a)
            if not breached: continue
            # Reclaim may occur in same or next two candles, no later than signal.
            reclaim=next((j for j in range(i,min(i+3,len(c15))) if (c15[j][4]>lv if side==1 else c15[j][4]<lv)),None)
            if reclaim is not None: found=(i,reclaim,name,lv)
    if found is None: return None,'G3','no confirmed sweep + reclaim in last 9 bars',g
    si,ri,name,level=found; g['G3']=f'{name} sweep bar={si}, reclaim bar={ri}'
    # G4: separate pressure at sweep from bullish/bearish reclaim.
    dp=delta_proxy(c15,tick)
    mean=sum(dp[-96:])/96
    sd=math.sqrt(sum((x-mean)**2 for x in dp[-96:])/96)
    prior=sorted(c[5] for c in c15[max(0,si-20):si])
    med=prior[len(prior)//2] if prior else 0
    pressure=side*dp[si]<-0.75*sd
    volume=c15[si][5]>=1.2*med if med>0 else False
    if not pressure or not volume: return None,'G4',f'proxy pressure={pressure}, sweep volume={volume}',g
    g['G4']='PROXY pressure + volume (NOT true absorption)'
    # G5: confirmed pivot at/before sweep; BOS must occur after reclaim, within 8 bars.
    piv=pivots(c15[:si+1],high=(side==1))
    if not piv: return None,'G5','no confirmed prior pivot',g
    pivot=c15[piv[-1]][2 if side==1 else 3]
    bos=None
    for j in range(ri,len(c15)):
        if j-si>8: break
        vma=sum(c[5] for c in c15[max(0,j-20):j])/max(1,min(20,j))
        breakout=(c15[j][4]>pivot+0.05*a) if side==1 else (c15[j][4]<pivot-0.05*a)
        if breakout and c15[j][5]>=1.2*vma: bos=j
    if bos is None: return None,'G5','no BOS with volume within 8 bars',g
    # Avoid re-emitting the same signal on every subsequent candle.
    if bos!=len(c15)-1: return None,'G5','BOS occurred earlier; wait for new BOS',g
    g['G5']=f'BOS @ {pivot:.2f}'
    # G6: VWAP plus recent stochastic crossover; hidden divergence is optional confirmation.
    rs=rsi(c15); k,d=stoch_rsi(rs); vw=session_vwap(c15)
    if vw is None or side*(sig[4]-vw)<=0: return None,'G6','VWAP not reclaimed',g
    crosses=[]
    for j in range(max(1,len(c15)-6),len(c15)):
        if None in (k[j],d[j],k[j-1],d[j-1]): continue
        if side==1 and k[j-1]<=d[j-1] and k[j]>d[j] and k[j]<60: crosses.append(j)
        if side==-1 and k[j-1]>=d[j-1] and k[j]<d[j] and k[j]>40: crosses.append(j)
    if not crosses: return None,'G6','no StochRSI cross in last 6 bars',g
    lowp=pivots(c15[:-1],high=(side==-1))
    hidden=False
    if len(lowp)>=2:
        p1,p2=lowp[-2:]
        if rs[p1] is not None and rs[p2] is not None:
            hidden=(c15[p2][3]>c15[p1][3] and rs[p2]<rs[p1]) if side==1 else (c15[p2][2]<c15[p1][2] and rs[p2]>rs[p1])
    g['G6']=f'VWAP + StochRSI; hidden divergence={hidden} (optional)'
    entry=sig[4]
    extreme=min(c[3] for c in c15[si:ri+1]) if side==1 else max(c[2] for c in c15[si:ri+1])
    structural=extreme-side*max(tick,0.1*a)
    distance=side*(entry-structural)
    if distance<=0 or distance>1.5*a:
        return None,'G7',f'structural SL distance {distance:.2f} outside (0,1.5ATR]',g
    stop=structural; tp1=entry+side*distance; tp2=entry+side*2*distance
    cost_r=2*(FEE+SLIP)*entry/distance
    # With 50% at 1R, 30% at 2R, 20% runner at BE, gross is 1.1R, NOT 2R.
    conservative_net=1.1-cost_r
    if conservative_net<MIN_NET_RR:
        return None,'G7',f'scale-out conservative net {conservative_net:.2f}R < {MIN_NET_RR:.2f}R',g
    g['G7']=f'structural stop; scale-out conservative net={conservative_net:.2f}R'
    return {'direction':direction,'entry':entry,'sl':stop,'tp1':tp1,'tp2':tp2,
            'D':distance,'atr15':a,'cost_R':cost_r,'net_rr':conservative_net,
            'regime':reg,'level':name,'sweep_extreme':extreme,'signal_ts':sig[0]},'OK','',g


def risk_state(h):
    closed=h['closed']
    if len(closed)>=20 and sum(t['net_r']>0 for t in closed[-20:])<10:
        return 'PAUSE',0.,'rolling 20-trade win rate < 50%'
    eq=float(h['equity']); peak=max(float(h['peak_equity']),eq)
    dd=(peak-eq)/peak if peak>0 else 1.
    if dd>=.15: return 'HALT',0.,f'drawdown={dd:.1%}'
    if dd>=.12: return 'PAUSE',0.,f'drawdown={dd:.1%}'
    today=datetime.now(timezone.utc).date().isoformat()
    recent=[x for x in closed if x.get('exit_iso','')[:10]==today]
    if len(recent)>=3 and all(x['net_r']<=0 for x in recent[-3:]):
        return 'PAUSE',0.,'3 consecutive losses today'
    return 'OK',(.5 if dd>=.10 else 1.),f'drawdown={dd:.1%}'


def position(sig,h,mult):
    equity=float(h['equity']); risk_usdt=equity*min(RISK,.02)*mult
    qty=risk_usdt/(sig['D']+2*(FEE+SLIP)*sig['entry'])
    qty=min(qty,equity*MAX_LEVERAGE/sig['entry'])
    return qty,qty*sig['entry'],qty*sig['D']


def close_fill(t,fraction,price,ts,reason):
    """fraction is fraction of INITIAL position; charge fees/slippage on actual fill notional."""
    if fraction<=0: return
    side=1 if t['direction']=='LONG' else -1
    gross_r=fraction*side*(price-t['entry'])/t['D']
    cost_r=fraction*(FEE+SLIP)*(t['entry']+price)/t['D']
    t['gross_r']+=gross_r; t['net_r']+=gross_r-cost_r
    t['remaining']=max(0.,t['remaining']-fraction)
    t.setdefault('fills',[]).append({'ts':ts,'fraction':fraction,'price':price,'reason':reason})


def monitor(h,cs5,reg_now=None):
    """Persist fill state. Uses conservative STOP-first ordering within each 5m candle.
    Processes only new bars; no replay. Regime exit is at latest confirmed 5m close.
    """
    done=[]
    for t in h['open']:
        side=1 if t['direction']=='LONG' else -1
        bars=[c for c in cs5 if c[0]>=t['signal_ts']+TF15_MS and c[0]>t.get('last_5m_ts',0)]
        for c in bars:
            ts,_,hi,lo,cl,_=c
            t['last_5m_ts']=ts
            # Gap-through stop filled at 5m open if worse, not at impossible stop price.
            stop=t['active_stop']
            hit=(lo<=stop) if side==1 else (hi>=stop)
            if hit:
                fill=min(c[1],stop) if side==1 else max(c[1],stop)
                close_fill(t,t['remaining'],fill,ts,'STOP')
                break
            # Conservative target order, cannot infer exact intrabar sequence.
            if not t['tp1_done'] and (hi>=t['tp1'] if side==1 else lo<=t['tp1']):
                close_fill(t,.5,t['tp1'],ts,'TP1'); t['tp1_done']=True
                t['active_stop']=t['entry']
            if t['tp1_done'] and not t['tp2_done'] and (hi>=t['tp2'] if side==1 else lo<=t['tp2']):
                close_fill(t,.3,t['tp2'],ts,'TP2'); t['tp2_done']=True
            if t['tp2_done'] and t['remaining']>1e-9:
                # Update stop for NEXT candle, not current one.
                candidate=(hi-1.5*t['atr15']) if side==1 else (lo+1.5*t['atr15'])
                t['active_stop']=max(t['active_stop'],candidate) if side==1 else min(t['active_stop'],candidate)
            mfe=(hi-t['entry'])/t['D'] if side==1 else (t['entry']-lo)/t['D']
            t['mfe_r']=max(t.get('mfe_r',0),mfe)
            if not t['tp1_done'] and ts>=t['signal_ts']+TF15_MS+8*TF15_MS and t['mfe_r']<.5:
                close_fill(t,t['remaining'],cl,ts,'TIMEOUT'); break
        if t['remaining']>1e-9 and reg_now is not None and reg_now not in (('STRONG_UP','WEAK_UP') if side==1 else ('STRONG_DN','WEAK_DN')) and bars:
            close_fill(t,t['remaining'],bars[-1][4],bars[-1][0],'REGIME_SHIFT')
        if t['remaining']<=1e-9:
            t['status']='CLOSED'; t['exit_iso']=datetime.fromtimestamp(t['last_5m_ts']/1000,timezone.utc).isoformat()
            # Original risk capital fixed at entry, not latest account equity.
            pnl=t['net_r']*t['risk_capital']
            h['equity']+=pnl; h['peak_equity']=max(h['peak_equity'],h['equity'])
            t['pnl_usdt']=pnl; h['closed'].append(t); done.append(t)
    h['open']=[t for t in h['open'] if t['remaining']>1e-9]
    return done


def scan(h,tick,c15,c1h,c4h):
    failures=Counter(); diagnostics={}
    state,mult,reason=risk_state(h)
    if state!='OK': return [],{'Kill switch':reason}
    if len(h['open'])>=MAX_OPEN: return [],{'Max open':'reached'}
    if not c15 or h.get('last_scan_ts')==c15[-1][0]: return [],{'Scan':'already processed'}
    created=[]
    for side in ('LONG','SHORT'):
        sig,gate,detail,gates=gate_pipeline(c15,c1h,c4h,tick,side)
        diagnostics[side]={'first_failed_gate':gate,'detail':detail,'gates':gates}
        failures[f'{side} {gate}']+=1
        if sig is None: continue
        if len(h['open'])>=MAX_OPEN: break
        qty,notional,risk_cap=position(sig,h,mult)
        if qty<=0: continue
        t={**sig,'id':f'{SYMBOL}|{side}|{sig["signal_ts"]}',
           'qty_btc_equivalent':qty,'notional_usdt':notional,'risk_capital':risk_cap,
           'remaining':1.,'active_stop':sig['sl'],'tp1_done':False,'tp2_done':False,
           'gross_r':0.,'net_r':0.,'mfe_r':0.,'last_5m_ts':0,
           'status':'OPEN','gates':gates}
        if any(x['id']==t['id'] for x in h['open']+h['closed']): continue
        h['open'].append(t); created.append(t)
    h['last_scan_ts']=c15[-1][0]
    h['last_diagnostics']=diagnostics
    return created,diagnostics


def main():
    h=load(); tick=tick_size()
    c15,c1h,c4h,cs5=(candles('15m',300),candles('1H',300),candles('4H',200),candles('5m',300))
    if not c15 or not c1h or not c4h:
        LOG.error('Missing OHLCV; no signals or monitoring'); return
    now_ms=int(time.time()*1000)
    if now_ms-(c15[-1][0]+TF15_MS)>2*TF15_MS:
        LOG.error('Stale 15m candles; no new signals'); return
    close_ms=c15[-1][0]+TF15_MS
    h1=[c for c in c1h if c[0]+TF1H_MS<=close_ms]
    h4=[c for c in c4h if c[0]+TF4H_MS<=close_ms]
    current_reg,_=regime(h1,h4)
    # Do not simulate exits when 5m history cannot cover the full unprocessed period.
    if h['open']:
        earliest=min(t.get('last_5m_ts') or t['signal_ts']+TF15_MS-1 for t in h['open'])
        if not cs5 or cs5[0][0]>earliest+TF5_MS:
            LOG.error('5m monitoring gap; trade states left unchanged. Reconcile manually.'); save(h); return
    done=monitor(h,cs5,current_reg)
    for t in done: notify(f'ALPHAQUANT V6.2 CLOSED {t["direction"]} net {t["net_r"]:+.3f}R / {t["pnl_usdt"]:+.2f} USDT')
    created,diagnostics=scan(h,tick,c15,c1h,c4h)
    for t in created:
        notify(f'ALPHAQUANT V6.2 PROXY SIGNAL {t["direction"]}\nEntry reference {t["entry"]:.2f} | SL {t["sl"]:.2f}\nTP1 {t["tp1"]:.2f} | TP2 {t["tp2"]:.2f}\nRisk {t["risk_capital"]:.2f} USDT | Regime {t["regime"]}\nOHLCV proxy, signal only; no order submitted.')
    LOG.info('Regime=%s, new=%d, closed=%d, diagnostics=%s',current_reg,len(created),len(done),diagnostics)
    save(h)

if __name__=='__main__': main()
