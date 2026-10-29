#!/usr/bin/env python3
"""ALPHAQUANT V6.2 compatible backtester. OHLCV proxy; 15m conservative fills.
No order execution. Uses signal_bot.gate_pipeline and signal_bot.close_fill.
"""
import argparse, csv, json, os, sys, time, math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import requests
import research_engine as bot

M15,H1,H4=bot.TF15_MS,bot.TF1H_MS,bot.TF4H_MS
URL=os.getenv('OKX_BASE_URL',bot.URL).rstrip('/')
S=requests.Session(); S.headers.update({'User-Agent':'ALPHAQUANT-V62-backtest'})

def fetch_history(bar,start,end):
    out={}; cursor=end; last=None
    while True:
        for attempt in range(5):
            try:
                r=S.get(URL+'/api/v5/market/history-candles',params={'instId':bot.SYMBOL,'bar':bar,'limit':'100','after':str(cursor)},timeout=25)
                r.raise_for_status(); payload=r.json()
                if payload.get('code')!='0': raise RuntimeError(str(payload))
                rows=payload.get('data',[]); break
            except (requests.RequestException,ValueError,RuntimeError) as e:
                if attempt==4: raise RuntimeError(f'OKX {bar} fetch failed: {e}') from e
                time.sleep(min(2**attempt,10))
        if not rows: break
        oldest=min(int(x[0]) for x in rows)
        for x in rows:
            try:
                ts=int(x[0])
                if start<=ts<end and x[8]=='1': out[ts]=[ts,*[float(x[k]) for k in (1,2,3,4,5)]]
            except (IndexError,TypeError,ValueError): pass
        if oldest<=start or oldest>=cursor or oldest==last: break
        last=cursor; cursor=oldest
        time.sleep(.11)
    return [out[k] for k in sorted(out)]

def simulate(t,bar):
    """One 15m candle. Stop first, then targets; trailing applies next bar."""
    side=1 if t['direction']=='LONG' else -1
    ts,op,hi,lo,cl,_=bar
    stop=t['active_stop']
    if lo<=stop if side==1 else hi>=stop:
        fill=min(op,stop) if side==1 else max(op,stop)
        bot.close_fill(t,t['remaining'],fill,ts,'STOP')
        return
    if not t['tp1_done'] and (hi>=t['tp1'] if side==1 else lo<=t['tp1']):
        bot.close_fill(t,.5,t['tp1'],ts,'TP1'); t['tp1_done']=True; t['active_stop']=t['entry']
    if t['tp1_done'] and not t['tp2_done'] and (hi>=t['tp2'] if side==1 else lo<=t['tp2']):
        bot.close_fill(t,.3,t['tp2'],ts,'TP2'); t['tp2_done']=True
    if t['tp2_done'] and t['remaining']>1e-9:
        candidate=(hi-1.5*t['atr15']) if side==1 else (lo+1.5*t['atr15'])
        t['active_stop']=max(t['active_stop'],candidate) if side==1 else min(t['active_stop'],candidate)
    t['mfe_r']=max(t['mfe_r'], ((hi-t['entry']) if side==1 else (t['entry']-lo))/t['D'])
    if not t['tp1_done'] and ts>=t['signal_ts']+M15+8*M15 and t['mfe_r']<.5:
        bot.close_fill(t,t['remaining'],cl,ts,'TIMEOUT')

def backtest(c15,c1,c4,tick,days,start_test):
    counters=Counter(); trades=[]; active=None; equity=bot.EQUITY; peak=equity; curve=[]
    i1=i4=0; gate_samples={}; signals=0
    for i,bar in enumerate(c15):
        close=bar[0]+M15
        while i1<len(c1) and c1[i1][0]+H1<=close: i1+=1
        while i4<len(c4) and c4[i4][0]+H4<=close: i4+=1
        h1=c1[max(0,i1-500):i1]; h4=c4[max(0,i4-200):i4]
        # Position entered at next candle open; first bar processed on its own iteration.
        if active is not None:
            if active.get('pending'):
                active['pending']=False
                side=1 if active['direction']=='LONG' else -1
                active['entry']=bar[1]
                active['D']=side*(active['entry']-active['sl'])
                if active['D']<=0 or not math.isfinite(active['D']):
                    counters['Invalid next-open geometry']+=1; active=None
                else:
                    active['tp1']=active['entry']+side*active['D']
                    active['tp2']=active['entry']+side*2*active['D']
                    active['risk_capital']=equity*min(bot.RISK,.02)
            if active is not None:
                simulate(active,bar)
                if active['remaining']>1e-9 and len(h1)>=210 and len(h4)>=20:
                    reg,_=bot.regime(h1,h4)
                    allow=('STRONG_UP','WEAK_UP') if active['direction']=='LONG' else ('STRONG_DN','WEAK_DN')
                    if reg not in allow:
                        bot.close_fill(active,active['remaining'],bar[4],bar[0],'REGIME_SHIFT')
                if active['remaining']<=1e-9:
                    pnl=active['net_r']*active['risk_capital']; equity+=pnl; peak=max(peak,equity)
                    trades.append({'time':datetime.fromtimestamp(active['signal_ts']/1000,timezone.utc).isoformat(),
                        'direction':active['direction'],'regime':active['regime'],'entry':active['entry'],
                        'sl':active['sl'],'gross_r':active['gross_r'],'net_r':active['net_r'],
                        'pnl_usdt':pnl,'equity_usdt':equity,'exit_time':datetime.fromtimestamp(bar[0]/1000,timezone.utc).isoformat(),
                        'exit_reason':active['fills'][-1]['reason']})
                    curve.append(equity); active=None
        if active is not None or bar[0]<start_test or i<260 or len(h1)<210 or len(h4)<20 or i==len(c15)-1: continue
        window=c15[max(0,i-299):i+1]
        for direction in ('LONG','SHORT'):
            try: sig,gate,detail,_=bot.gate_pipeline(window,h1,h4,tick,direction)
            except Exception as e: raise RuntimeError(f'gate_pipeline error at {bar[0]} {direction}: {e}') from e
            counters[f'{direction} {gate}']+=1
            if sig is None:
                gate_samples.setdefault(f'{direction} {gate}',detail)
                continue
            signals+=1
            active={**sig,'remaining':1.,'active_stop':sig['sl'],'tp1_done':False,'tp2_done':False,
                'gross_r':0.,'net_r':0.,'mfe_r':0.,'fills':[],'pending':True}
            break
    return trades,counters,gate_samples,signals,active is not None

def main():
    p=argparse.ArgumentParser(); p.add_argument('--days',type=int,default=365); p.add_argument('--end',default=''); p.add_argument('--out',default='bt_out'); p.add_argument('--telegram',action='store_true'); args=p.parse_args()
    if args.days<1: p.error('--days must be positive')
    end=int(datetime.strptime(args.end,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()*1000) if args.end else int(time.time()*1000)
    start=end-args.days*86400000; warm=start-90*86400000
    print('ALPHAQUANT V6.2 compatible backtest | mode:',bot.MODE,flush=True)
    if bot.MODE=='STRICT': print('STRICT: unavailable macro/order flow means zero trades by design.',flush=True)
    data=[]
    for tf in ('15m','1H','4H'):
        print('Downloading',tf,flush=True); cs=fetch_history(tf,warm,end); print(tf,len(cs),flush=True); data.append(cs)
    if any(len(x)==0 for x in data): raise SystemExit('Missing candle data; check OKX availability')
    tick=bot.tick_size(); trades,gates,samples,signals,unfinished=backtest(*data,tick,args.days,start)
    n=len(trades); wins=sum(x['net_r']>0 for x in trades); total=sum(x['net_r'] for x in trades)
    profit=sum(max(0,x['net_r']) for x in trades); loss=-sum(min(0,x['net_r']) for x in trades)
    eq=peak=bot.EQUITY; dd=0
    for t in trades:
        eq=t['equity_usdt']; peak=max(peak,eq); dd=max(dd,(peak-eq)/peak if peak else 0)
    lines=[f'Mode: {bot.MODE} | Period: {args.days} days | Regime adaptive: {bot.REGIME_ADAPTIVE} | Sweep lookback: {bot.SWEEP_LOOKBACK}',f'Completed trades: {n} | Signals: {signals} | Unfinished: {unfinished}',
        f'Win rate: {wins/n:.2%}' if n else 'Win rate: N/A',f'Net expectancy: {total/n:+.4f} R/trade' if n else 'Net expectancy: N/A',
        f'Profit factor: {profit/loss:.3f}' if loss else 'Profit factor: N/A',f'Max drawdown (closed trades): {dd:.2%}',
        f'Ablation G4: {bot.ABLATE_G4} | Ablation G6: {bot.ABLATE_G6} | RESEARCH ONLY', 'First failed gates (each side):']+[f'  {k}: {v} | e.g. {samples.get(k,"")}' for k,v in gates.most_common()]
    Path(args.out).mkdir(parents=True,exist_ok=True)
    Path(args.out,'summary.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    Path(args.out,'trades.json').write_text(json.dumps(trades,indent=2),encoding='utf-8')
    Path(args.out,'gates.json').write_text(json.dumps({'counts':dict(gates),'examples':samples},indent=2),encoding='utf-8')
    print('\n'.join(lines),flush=True)
    if args.telegram and bot.TOKEN and bot.CHAT: bot.notify('ALPHAQUANT V6.2 BACKTEST\n'+'\n'.join(lines[:7]))

if __name__=='__main__': main()
