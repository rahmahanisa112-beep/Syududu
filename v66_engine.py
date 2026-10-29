"""V6.6 event-driven research, no orders. All events use confirmed candles only."""
from collections import Counter
import research_engine as b

SWEEP_TTL=13
BOS_TTL=8
CONFIRM_TTL=6

def step(state, bars, h1, h4, tick, direction, audit):
    """Called at close of each 15m candle. Returns (new_state, candidate_or_None).
    State machine: WAIT_SWEEP -> WAIT_RECLAIM -> WAIT_BOS -> WAIT_CONFIRM.
    """
    side=1 if direction=="LONG" else -1
    now=bars[-1]; ts=now[0]
    def reject(reason):
        audit[reason]+=1
        return None,None
    if state and ts-state['sweep_ts']>SWEEP_TTL*b.TF15_MS:
        audit['expired_sweep']+=1
        state=None
    if state and state['phase']=='WAIT_RECLAIM':
        if (side*(now[4]-state['level'])>0):
            state['phase']='WAIT_BOS';state['reclaim_ts']=ts
            audit['reclaimed']+=1
        elif ts-state['sweep_ts']>=2*b.TF15_MS:
            return reject('reclaim_timeout')
    if state and state['phase']=='WAIT_BOS':
        if ts-state['reclaim_ts']>BOS_TTL*b.TF15_MS:
            return reject('bos_timeout')
        pivot=state['pivot']
        a=b.atr(bars)[-1]
        vol=sum(c[5] for c in bars[-21:-1])/max(1,len(bars[-21:-1]))
        if a and side*(now[4]-pivot)>0.05*a and now[5]>=1.2*vol:
            state['phase']='WAIT_CONFIRM';state['bos_ts']=ts
            audit['bos_confirmed']+=1
    if state and state['phase']=='WAIT_CONFIRM':
        if ts-state['bos_ts']>CONFIRM_TTL*b.TF15_MS:
            return reject('confirmation_timeout')
        # Confirmation may happen on BOS candle or later; no future bars used.
        vw=b.session_vwap(bars)
        rs=b.rsi(bars); k,d=b.stoch_rsi(rs)
        i=len(bars)-1
        cross=(all(v is not None for v in (k[i],d[i],k[i-1],d[i-1]))
            and ((k[i-1]<=d[i-1] and k[i]>d[i] and k[i]<60) if side==1
                 else (k[i-1]>=d[i-1] and k[i]<d[i] and k[i]>40)))
        if cross: state['last_cross_ts']=ts
        if vw is None or side*(now[4]-vw)<=0 or ts-state.get('last_cross_ts',-10**18)>CONFIRM_TTL*b.TF15_MS:
            audit['waiting_momentum']+=1
            return state,None
        audit['momentum_confirmed']+=1
        a=b.atr(bars)[-1]
        if not a or a<=0:return reject('atr_unavailable')
        stop=state['extreme']-side*max(tick,0.1*a)
        D=side*(now[4]-stop)
        if D<=0 or D>1.5*a:return reject('g7_structural_stop')
        cost=2*(b.FEE+b.SLIP)*now[4]/D
        if 1.1-cost<b.MIN_NET_RR:return reject('g7_net_rr')
        if not b.session_ok(ts):
            audit['outside_session_at_entry']+=1
            return state,None
        reg,_=b.regime(h1,h4)
        if reg not in (('STRONG_UP','WEAK_UP') if side==1 else ('STRONG_DN','WEAK_DN')):
            audit['regime_at_entry']+=1
            return state,None
        sig={'direction':direction,'entry':now[4],'sl':stop,'D':D,'atr15':a,
             'regime':reg,'signal_ts':ts,'sweep_ts':state['sweep_ts'],
             'bos_ts':state['bos_ts'],'level':state['level_name']}
        audit['signals']+=1
        return None,sig
    if state:return state,None
    # New sweep: levels and pivots are frozen from BEFORE current bar.
    if len(bars)<120 or len(h1)<210 or len(h4)<20:return None,None
    if not b.session_ok(ts):return None,None
    reg,_=b.regime(h1,h4)
    if reg not in (('STRONG_UP','WEAK_UP') if side==1 else ('STRONG_DN','WEAK_DN')):
        return None,None
    a=b.atr(bars[:-1])[-1]
    if not a:return None,None
    levels=[('1H_20',min(c[3] for c in h1[-20:]) if side==1 else max(c[2] for c in h1[-20:]))]
    # Current day's Asian range, based strictly on bars before current candle.
    from datetime import datetime,timezone
    day=datetime.fromtimestamp(ts/1000,timezone.utc).date()
    asia=[c for c in bars[:-1] if datetime.fromtimestamp(c[0]/1000,timezone.utc).date()==day and datetime.fromtimestamp(c[0]/1000,timezone.utc).hour<8]
    if asia:levels.append(('ASIA',min(c[3] for c in asia) if side==1 else max(c[2] for c in asia)))
    prior=bars[:-1]
    piv=b.pivots(prior,high=(side==1))
    if not piv:return None,None
    # Proxy volume/pressure measured at sweep close, with prior volume baseline.
    dp=b.delta_proxy(bars,tick)
    window=dp[-97:-1]
    import math
    mu=sum(window)/len(window)
    sd=math.sqrt(sum((v-mu)**2 for v in window)/len(window))
    volumes=sorted(c[5] for c in bars[-21:-1])
    pressure=side*dp[-1]<-0.75*sd
    volume=bool(volumes and now[5]>=1.2*volumes[len(volumes)//2])
    for name,level in levels:
        breach=(now[3]<level-0.1*a if side==1 else now[2]>level+0.1*a)
        if not breach:continue
        audit['sweeps_detected']+=1
        if not pressure or not volume:
            audit['g4_proxy_rejected']+=1
            return None,None
        audit['sweeps_qualified']+=1
        state={'phase':'WAIT_RECLAIM','sweep_ts':ts,'level':level,'level_name':name,
               'pivot':prior[piv[-1]][2 if side==1 else 3],
               'extreme':now[3] if side==1 else now[2]}
        # Allow reclaim on the sweep candle itself.
        if side*(now[4]-level)>0:
            state['phase']='WAIT_BOS';state['reclaim_ts']=ts
            audit['reclaimed']+=1
        return state,None
    return None,None
