#!/usr/bin/env python3
"""V6.6 event-driven 15m backtest. Research only; conservative stop-first fills."""
import argparse,json,math,time
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import research_engine as b
from backtest_v65 import fetch_history,simulate
from v66_engine import step

def run(c15,c1,c4,tick,start):
    states={'LONG':None,'SHORT':None};audit=Counter()
    trades=[]; active=None; equity=b.EQUITY; peak=equity; maxdd=0; signals=0
    i1=i4=0
    for i,bar in enumerate(c15):
        close=bar[0]+b.TF15_MS
        while i1<len(c1) and c1[i1][0]+b.TF1H_MS<=close:i1+=1
        while i4<len(c4) and c4[i4][0]+b.TF4H_MS<=close:i4+=1
        h1=c1[max(0,i1-500):i1];h4=c4[max(0,i4-200):i4]
        if active:
            if active.get('pending'):
                active['pending']=False
                active['entry']=bar[1]
                side=1 if active['direction']=='LONG' else -1
                active['D']=side*(active['entry']-active['sl'])
                if active['D']<=0 or active['D']>1.5*active['atr15']:
                    audit['invalid_next_open_geometry']+=1;active=None
                else:
                    active['tp1']=active['entry']+side*active['D']
                    active['tp2']=active['entry']+side*2*active['D']
                    active['risk_capital']=equity*min(b.RISK,.02)
            if active:
                simulate(active,bar)
                if active['remaining']>1e-9 and len(h1)>=210 and len(h4)>=20:
                    reg,_=b.regime(h1,h4)
                    if reg not in (('STRONG_UP','WEAK_UP') if active['direction']=='LONG' else ('STRONG_DN','WEAK_DN')):
                        b.close_fill(active,active['remaining'],bar[4],bar[0],'REGIME_SHIFT')
                if active['remaining']<=1e-9:
                    pnl=active['net_r']*active['risk_capital'];equity+=pnl
                    peak=max(peak,equity);maxdd=max(maxdd,(peak-equity)/peak)
                    trades.append({'signal_ts':active['signal_ts'],'direction':active['direction'],
                        'sweep_ts':active['sweep_ts'],'bos_ts':active['bos_ts'],
                        'entry':active['entry'],'stop':active['sl'],
                        'net_r':active['net_r'],'pnl_usdt':pnl,'equity':equity,
                        'exit_reason':active['fills'][-1]['reason']})
                    active=None
        if bar[0]<start or i<260 or i==len(c15)-1:continue
        window=c15[max(0,i-299):i+1]
        for side in ('LONG','SHORT'):
            states[side],sig=step(states[side],window,h1,h4,tick,side,audit)
            if sig and active is None:
                signals+=1
                active={**sig,'remaining':1.,'active_stop':sig['sl'],
                    'tp1_done':False,'tp2_done':False,'gross_r':0.,'net_r':0.,
                    'mfe_r':0.,'fills':[],'pending':True}
                states={'LONG':None,'SHORT':None}
                break
            elif sig:audit['signal_while_position_open']+=1
    return trades,audit,signals,maxdd,active is not None

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--days',type=int,default=30)
    p.add_argument('--end',default='')
    p.add_argument('--out',default='bt_out')
    args=p.parse_args()
    if args.days<1:p.error('--days must be positive')
    end=int(datetime.strptime(args.end,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()*1000) if args.end else int(time.time()*1000)
    start=end-args.days*86400000;warm=start-90*86400000
    data=[]
    for tf in ('15m','1H','4H'):
        print('Fetching',tf,flush=True)
        cs=fetch_history(tf,warm,end)
        print(tf,len(cs),flush=True)
        data.append(cs)
    if any(not d for d in data):raise RuntimeError('Missing OKX candles')
    trades,audit,signals,dd,unfinished=run(*data,b.tick_size(),start)
    n=len(trades);net=sum(t['net_r'] for t in trades)
    gains=sum(max(0,t['net_r']) for t in trades)
    losses=-sum(min(0,t['net_r']) for t in trades)
    report={'version':'6.6','days':args.days,'mode':'PROXY',
        'signals':signals,'completed_trades':n,'unfinished':unfinished,
        'win_rate':sum(t['net_r']>0 for t in trades)/n if n else None,
        'expectancy_r':net/n if n else None,
        'profit_factor':gains/losses if losses else None,
        'max_closed_trade_drawdown':dd,'audit':dict(audit),
        'limitations':['OHLCV order flow is a proxy','stop-first on ambiguous 15m candles',
            'no funding, spread history, liquidation or intrabar event order',
            'not a validated live strategy']}
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    (out/'summary.json').write_text(json.dumps(report,indent=2))
    (out/'trades.json').write_text(json.dumps(trades,indent=2))
    (out/'events.json').write_text(json.dumps(dict(audit),indent=2))
    (out/'summary.txt').write_text('\n'.join(f'{k}: {v}' for k,v in report.items())+'\n')
    print((out/'summary.txt').read_text())
if __name__=='__main__':main()
