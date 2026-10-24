#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accum_engine.py — 吸筹跟踪库技术引擎（宪法 v1.2 / 五粮液吸筹库协议）
用法: python3 scripts/accum_engine.py [CODE ...]   # 无参数 = 全部 14 只
输出: 每只股票的紧凑报告(stdout) + /tmp/acc/<code>.json (供文件生成器使用)
数据: data/daily_text/<code>_<name>_250d.txt (250 日块, EM 不复权口径)
"""
import re, math, bisect, datetime, json, sys, os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = os.path.join(ROOT, "data", "daily_text")

def load(code):
    path = None
    for f in os.listdir(DT):
        if f.startswith(code + "_") and f.endswith("_250d.txt"):
            path = os.path.join(DT, f); name = re.sub(r"^%s_(.*)_250d\.txt$" % code, r"\1", f)
    if not path: return None, None, None
    txt = open(path, encoding="utf-8").read()
    ms = list(re.finditer(r"Date:\s*(\d{4}-\d{2}-\d{2})", txt))
    rows = []
    for i, m in enumerate(ms):
        seg = txt[m.start(): ms[i+1].start() if i+1 < len(ms) else len(txt)]
        g = lambda nm: (float(re.search(nm+r":\s*([0-9.\-]+)", seg).group(1)) if re.search(nm+r":\s*([0-9.\-]+)", seg) else None)
        mc = re.search(r"\d{8}\s*\n\s*\n([0-9.]+)\s*\n\s*\n([\-+0-9.]+)\s+([\-+0-9.]+%)", seg)
        if not mc: continue
        r = dict(d=m.group(1), c=float(mc.group(1)), pct=float(mc.group(3).rstrip('%')),
                 o=g("今开"), prev=g("昨收"), h=g("最高价"), l=g("最低价"), to=g("换手率"), vr=g("量比"),
                 vol=g("成交量")*100, amt=g("成交额")*1e8, nei=g("内盘")*100, wai=g("外盘")*100,
                 prof=g("获利比例"), avg=g("平均成本"), pe=g("动态市盈率"), mcap=g("总市值"))
        c90 = re.search(r"90%成本:\s*([0-9.\-]+)-([0-9.]+)\s*\n\s*\n集中度:\s*([0-9.]+)%", seg)
        if c90: r["b90lo"], r["b90hi"], r["conc90"] = float(c90.group(1)), float(c90.group(2)), float(c90.group(3))
        c70 = re.search(r"70%成本:\s*([0-9.\-]+)-([0-9.]+)\s*\n\s*\n集中度:\s*([0-9.]+)%", seg)
        if c70: r["b70lo"], r["b70hi"], r["conc70"] = float(c70.group(1)), float(c70.group(2)), float(c70.group(3))
        mas = {}
        for k in (5,10,20,30,40,50,60,70,80,90):
            mm = re.search(rf"MA{k}:\s*([0-9.]+)\s*\((上升|下降)\)", seg)
            if mm: mas[k] = (float(mm.group(1)), 1 if mm.group(2)=="上升" else -1)
        r["mas"] = mas
        rows.append(r)
    return name, rows, None

def sma(x, n): return [sum(x[i-n+1:i+1])/n if i >= n-1 else None for i in range(len(x))]
def ema_s(x, p):
    k = 2/(p+1); out=[]; e=None
    for v in x: e = v if e is None else v*k+e*(1-k); out.append(e)
    return out
def slope20(x, i):
    w = x[i-19:i+1]; mx = list(range(20)); mm = sum(mx)/20; wm = sum(w)/20
    return sum((a-mm)*(b-wm) for a, b in zip(mx, w))/sum((a-mm)**2 for a in mx)
def lin_slope(xs):
    n=len(xs); mx=list(range(n)); mm=sum(mx)/n; wm=sum(xs)/n
    return sum((a-mm)*(b-wm) for a,b in zip(mx,xs))/sum((a-mm)**2 for a in mx) if n>1 else 0
def rs_fn(rets, size):
    vals=[]
    for s in range(0, len(rets)-size+1, size):
        w = rets[s:s+size]; m = sum(w)/size; dev=[x-m for x in w]
        cum=0; cs=[]
        for d in dev: cum+=d; cs.append(cum)
        R=max(cs)-min(cs); S=(sum(x*x for x in dev)/size)**0.5
        if S>0: vals.append(R/S)
    return sum(vals)/len(vals) if vals else None
def hurst(rets, sizes):
    pairs=[(math.log(s), math.log(rs_fn(rets,s))) for s in sizes if len(rets)>=s and rs_fn(rets,s)]
    if len(pairs)<2: return None
    lx=[p[0] for p in pairs]; ly=[p[1] for p in pairs]
    mx=sum(lx)/len(lx); my=sum(ly)/len(ly)
    return sum((a-mx)*(b-my) for a,b in zip(lx,ly))/sum((a-mx)**2 for a in lx)

def analyze(code):
    name, rows, _ = load(code)
    if not rows: return None
    n=len(rows); ds=[r["d"] for r in rows]; at=ds.index
    H=[r["h"] for r in rows]; L=[r["l"] for r in rows]; C=[r["c"] for r in rows]
    V=[r["vol"] for r in rows]; O=[r["o"] for r in rows]
    i=at(ds[-1]); last=rows[i]
    shares = last["mcap"]*1e8/last["c"]/1e8 if last["mcap"] else None
    out={"code":code,"name":name,"basis":ds[-1],"n":n,"range":f"{ds[0]}..{ds[-1]}"}
    # ex-div detect
    exd=[]
    for k in range(1,n):
        raw=(C[k]/C[k-1]-1)*100
        if rows[k]["pct"] is not None and abs(raw-rows[k]["pct"])>2:
            exd.append({"d":ds[k],"gap":round(rows[k]["prev"]-C[k-1],3)})
    out["exdiv"]=exd
    out["snap"]={k:last.get(k) for k in ("c","pct","o","h","l","vol","amt","vr","to","nei","wai","prof","avg","pe","mcap")}
    out["snap"]["shares_yi"]=round(shares,1) if shares else None
    out["ma"]={str(k):{"v":last["mas"][k][0],"dir":last["mas"][k][1],"gap":round((last["c"]-last["mas"][k][0])/last["mas"][k][0]*100,2)} for k in last["mas"]}
    # chips trajectory
    traj=[]
    seen=set()
    for d in ds:
        key=d[:7]
        if d[8:10] in ("01","15","28","30") or d>=ds[-8]:
            if key not in seen or d>=ds[-8]:
                rr=rows[at(d)]
                traj.append({"d":d,"c":rr["c"],"prof":rr["prof"],"avg":rr["avg"],"conc90":rr.get("conc90"),"conc70":rr.get("conc70"),"b70":[rr.get("b70lo"),rr.get("b70hi")],"b90":[rr.get("b90lo"),rr.get("b90hi")]})
                seen.add(key)
    out["chips_traj"]=traj[-14:]
    c90s=[rows[at(d)]["conc90"] for d in ds if rows[at(d)]["conc90"] is not None]
    c70s=[rows[at(d)]["conc70"] for d in ds if rows[at(d)]["conc70"] is not None]
    out["conc"]={"conc90":last.get("conc90"),"conc70":last.get("conc70"),
                 "slope90_20d":round(lin_slope(c90s[-20:]),4),"slope90_60d":round(lin_slope(c90s[-60:]),4),
                 "slope70_20d":round(lin_slope(c70s[-20:]),4),"slope70_60d":round(lin_slope(c70s[-60:]),4),
                 "avgcost_lock_wks":None}
    # avgcost stability: weeks with |avg - mode| tiny
    avgs=[r["avg"] for r in rows if r["avg"]]
    if len(avgs)>=30:
        sd=(sum((x-sum(avgs[-30:])/30)**2 for x in avgs[-30:])/30)**0.5
        out["conc"]["avgcost_sd30"]=round(sd,3)
    # battery v1
    obv=[0]
    for k in range(1,n): obv.append(obv[-1]+(V[k] if C[k]>C[k-1] else -V[k] if C[k]<C[k-1] else 0))
    ad=[0]
    for r in rows:
        rng=r["h"]-r["l"] or 1
        ad.append(ad[-1]+((r["c"]-r["l"])-(r["h"]-r["c"]))/rng*r["vol"])
    ad=ad[1:]
    cmf=[]
    for k in range(n):
        if k<19: cmf.append(None); continue
        mf=sum((((rows[j]["c"]-rows[j]["l"])-(rows[j]["h"]-rows[j]["c"]))/((rows[j]["h"]-rows[j]["l"]) or 1))*rows[j]["vol"] for j in range(k-19,k+1))
        cmf.append(mf/sum(V[k-19:k+1]))
    mfi=[]
    for k in range(n):
        if k<14: mfi.append(None); continue
        pos=neg=0.0
        for j in range(k-13,k+1):
            tp=(H[j]+L[j]+C[j])/3; ptp=(H[j-1]+L[j-1]+C[j-1])/3; fl=tp*V[j]
            if tp>ptp: pos+=fl
            elif tp<ptp: neg+=fl
        mfi.append(100-100/(1+pos/neg) if neg>0 else 100.0)
    rsi=[None]*n; gains=[max(C[k]-C[k-1],0) for k in range(1,n)]; losses=[max(C[k-1]-C[k],0) for k in range(1,n)]
    for k in range(14,n):
        if k==14: ag=sum(gains[:14])/14; al=sum(losses[:14])/14
        else: ag=(ag*13+gains[k-1])/14; al=(al*13+losses[k-1])/14
        rsi[k]=100-100/(1+(ag/al if al>0 else 1e9))
    kv=[50.0]*n; dv=[50.0]*n; jv=[50.0]*n
    for k in range(8,n):
        hh=max(H[k-8:k+1]); ll=min(L[k-8:k+1]); rsv=100*(C[k]-ll)/(hh-ll) if hh>ll else 50
        kv[k]=kv[k-1]*2/3+rsv/3; dv[k]=dv[k-1]*2/3+kv[k]/3; jv[k]=3*kv[k]-2*dv[k]
    e12=ema_s(C,12); e26=ema_s(C,26); dif=[a-b for a,b in zip(e12,e26)]; dea=ema_s(dif,9); hist=[2*(a-b) for a,b in zip(dif,dea)]
    wmap={}
    for r in rows: wmap[datetime.date.fromisoformat(r["d"]).isocalendar()[:2]]=r["c"]
    wc=list(wmap.values()); we12=ema_s(wc,12); we26=ema_s(wc,26); wdif=[a-b for a,b in zip(we12,we26)]; wdea=ema_s(wdif,9); whist=[2*(a-b) for a,b in zip(wdif,wdea)]
    band=[]
    for k in range(n):
        if k<19: band.append(None); continue
        win=C[k-19:k+1]; mu=sum(win)/20; sd=(sum((x-mu)**2 for x in win)/20)**0.5
        band.append((mu,sd,4*sd/mu*100,(C[k]-(mu-2*sd))/(4*sd)*100 if sd>0 else 50))
    bws=[b[2] for b in band if b]
    mu,sd,bw,pb=band[i]
    # divergence scan: last-60d lowest vs prior swing low
    div={"obv":None,"rsi":None,"low60":None,"low_prev":None}
    if n>120:
        low60=min(range(n-60,n), key=lambda k:L[k])
        cand=range(max(0,low60-90), max(1,low60-20))
        p_low=min(cand, key=lambda k:L[k]) if cand else None
        if p_low is not None:
            div={"obv":obv[low60]>obv[p_low],"rsi":(rsi[low60] or 0)>(rsi[p_low] or 0),
                 "low60":{"d":ds[low60],"L":L[low60],"obv":round(obv[low60]),"rsi":round(rsi[low60] or 0,1),"volx":round(V[low60]/(sum(V[low60-19:low60+1])/20),2)},
                 "low_prev":{"d":ds[p_low],"L":L[p_low],"obv":round(obv[p_low]),"rsi":round(rsi[p_low] or 0,1)}}
    out["v1"]={"obv":round(obv[i]),"obv_slope20":round(slope20(obv,i)),"obv_pctile":round(sum(1 for v in obv if v<=obv[i])/n*100),
               "ad_slope20":round(slope20(ad,i)),"cmf20":round(cmf[i],3),"mfi14":round(mfi[i],1),
               "rsi14":round(rsi[i],1),"kdj":[round(kv[i]),round(dv[i]),round(jv[i])],
               "macd":{"dif":round(dif[i],3),"dea":round(dea[i],3),"hist":round(hist[i],3),"whist":round(whist[-1],3)},
               "boll":{"mid":round(mu,2),"bw":round(bw,2),"bw_pctile":round(sum(1 for v in bws if v<=bw)/len(bws)*100),"pb":round(pb,1)},
               "divergence":div}
    # v2
    tr=[0.0]; pdm=[0.0]; ndm=[0.0]
    for k in range(1,n):
        tr.append(max(H[k]-L[k], abs(H[k]-C[k-1]), abs(L[k]-C[k-1])))
        up=H[k]-H[k-1]; dn=L[k-1]-L[k]
        pdm.append(up if up>dn and up>0 else 0.0); ndm.append(dn if dn>up and dn>0 else 0.0)
    def wilder(x,p=14):
        o=[None]*n; s=sum(x[1:p+1]); o[p]=s
        for k in range(p+1,n): s=s-s/14+x[k]; o[k]=s
        return o
    TRs,PDMs,NDMs=wilder(tr),wilder(pdm),wilder(ndm)
    pdi=[100*PDMs[k]/TRs[k] if TRs[k] else None for k in range(n)]
    ndi=[100*NDMs[k]/TRs[k] if TRs[k] else None for k in range(n)]
    dx=[100*abs(pdi[k]-ndi[k])/(pdi[k]+ndi[k]) if pdi[k] and ndi[k] and (pdi[k]+ndi[k]) else None for k in range(n)]
    adx=[None]*n; a=None
    for k in range(27,n):
        if k==27: a=sum(x for x in dx[14:28] if x is not None)/14
        else: a=(a*13+(dx[k] or 0))/14
        adx[k]=a
    cci=[None]*n
    for k in range(19,n):
        tp=[(H[j]+L[j]+C[j])/3 for j in range(k-19,k+1)]; m=sum(tp)/20; md=sum(abs(x-m) for x in tp)/20
        cci[k]=(tp[-1]-m)/(0.015*md) if md else 0
    wr=[(max(H[k-13:k+1])-C[k])/(max(H[k-13:k+1])-min(L[k-13:k+1]))*100 for k in range(13,n)]
    roc=[(C[k]/C[k-12]-1)*100 for k in range(12,n)]
    sar=[None]*n; tu=C[1]>C[0]; ep=H[1] if tu else L[1]; sar[1]=L[0] if tu else H[0]; af=0.02
    for k in range(2,n):
        sar[k]=sar[k-1]+af*(ep-sar[k-1])
        if tu:
            if H[k]>ep: ep=H[k]; af=min(af+0.02,0.2)
            if L[k]<sar[k]: tu=False; sar[k]=ep; ep=L[k]; af=0.02
        else:
            if L[k]<ep: ep=L[k]; af=min(af+0.02,0.2)
            if H[k]>sar[k]: tu=True; sar[k]=ep; ep=H[k]; af=0.02
    flip=None
    for k in range(n-1,1,-1):
        if (C[k]>sar[k])!=(C[k-1]>sar[k-1]): flip=ds[k]; break
    dch=max(H[i-19:i+1]); dcl=min(L[i-19:i+1])
    psy=sum(1 for k in range(i-11,i+1) if C[k]>C[k-1])/12*100
    arn=i-25
    AR=sum(H[k]-O[k] for k in range(arn,i+1))/sum(O[k]-L[k] for k in range(arn,i+1))*100
    BR=sum(H[k]-C[k-1] for k in range(arn,i+1))/sum(C[k-1]-L[k] for k in range(arn,i+1))*100
    avs=sum(V[k] for k in range(arn,i+1) if C[k]>C[k-1]); bvs=sum(V[k] for k in range(arn,i+1) if C[k]<C[k-1]); cvs=sum(V[k] for k in range(arn,i+1) if C[k]==C[k-1])
    VR=(avs+cvs/2)/(bvs+cvs/2)*100
    pvt=[0.0]
    for k in range(1,n): pvt.append(pvt[-1]+(C[k]-C[k-1])/C[k-1]*V[k])
    emv=[0.0]
    for k in range(1,n):
        br_=(V[k]/1e4)/((H[k]-L[k]) or 1)
        emv.append(((H[k]+L[k])/2-(H[k-1]+L[k-1])/2)/br_ if br_ else 0)
    t3=ema_s(ema_s(ema_s(C,15),15),15)
    trix=[(t3[k]/t3[k-1]-1)*100 for k in range(1,n)]
    fh=max(H); fl=min(L)
    gaps=[]
    for k in range(1,n):
        if O[k]>H[k-1]*1.001: gaps.append(("up",ds[k],H[k-1]))
        elif O[k]<L[k-1]*0.999: gaps.append(("dn",ds[k],L[k-1]))
    unf=[]
    for gg in gaps:
        k=at(gg[1])
        if gg[0]=="up" and min(L[k:])>gg[2]: unf.append(("up",gg[1],gg[2]))
        if gg[0]=="dn" and max(H[k:])<gg[2]: unf.append(("dn",gg[1],gg[2]))
    vwap20=sum((H[k]+L[k]+C[k])/3*V[k] for k in range(i-19,i+1))/sum(V[i-19:i+1])
    h52=max(H); l52=min(L)
    rolls=[sum(V[k-20:k]) for k in range(20,n+1)]; vpct=bisect.bisect(rolls,sum(V[i-19:i+1]))/len(rolls)*100
    rets=[math.log(C[k]/C[k-1]) for k in range(1,n)]
    vol20=(sum(x*x for x in rets[-20:])/20)**0.5*(252**0.5)*100
    vols20=[(sum(x*x for x in rets[kk-19:kk+1])/20)**0.5*(252**0.5)*100 for kk in range(20,n)]
    xs=C[i-19:i+1]; ys=V[i-19:i+1]; xm=sum(xs)/20; ym=sum(ys)/20
    corr=sum((a-xm)*(b-ym) for a,b in zip(xs,ys))/math.sqrt(sum((a-xm)**2 for a in xs)*sum((b-ym)**2 for b in ys))
    out["v2"]={"adx":round(adx[i],1),"pdi":round(pdi[i],1),"ndi":round(ndi[i],1),"cci":round(cci[i]),
               "wr":round(wr[-1],1),"roc12":round(roc[-1],2),"sar":round(sar[i],2),"sar_flip":flip,
               "donchian":[round(dcl,2),round(dch,2)],"donch_pos":round((C[i]-dcl)/(dch-dcl)*100),
               "psy":round(psy),"ar":round(AR),"br":round(BR),"vr":round(VR),
               "pvt_slope":round(slope20(pvt,i)),"emv14":round(sum(emv[i-13:i+1])/14),"trix":round(trix[-1],3),
               "fib":{"hi":fh,"lo":fl,"r236":round(fl+(fh-fl)*0.236,2),"r382":round(fl+(fh-fl)*0.382,2),"retrace_pct":round((C[i]-fl)/(fh-fl)*100)},
               "gaps":{"total":len(gaps),"unfilled":[list(x) for x in unf]},
               "vwap20":round(vwap20,2),"w52_pos":round((C[i]-l52)/(h52-l52)*100),
               "vol_pctile":round(vpct),"vola20":round(vol20),"vola_pctile":round(bisect.bisect(vols20,vol20)/len(vols20)*100),
               "pv_corr":round(corr,2)}
    # v3 hurst
    hs={}
    for st in range(0,len(rets)-120+1,20):
        hh=hurst(rets[st:st+120],[10,20,40])
        if hh: hs[ds[st+120]]=round(hh,3)
    out["v3"]={"hurst250":round(hurst(rets,[10,20,40,80]),3),"hurst60":round(hurst(rets[-60:],[10,20,30]),3),"rolling":hs}
    # vp 60d
    prof=defaultdict(float)
    for r in rows[n-60:]:
        prof[round((r["h"]+r["l"]+r["c"])/3)]+=r["vol"]
    tot=sum(prof.values()); poc=max(prof,key=prof.get)
    top=sorted(prof,key=prof.get,reverse=True); va=[]; acc=0
    for k in top:
        va.append(k); acc+=prof[k]
        if acc>=tot*0.7: break
    out["vp"]={"poc":poc,"va":[min(va),max(va)],"dist":{str(k):round(prof[k]/tot*100) for k in sorted(prof)}}
    box=sum(V[k] for k in range(n-60,n))
    out["turnover60_pct"]=round(box/1e4/shares*100,1) if shares else None
    out["net10"]=[{"d":d,"net":round(rows[at(d)]["wai"]-rows[at(d)]["nei"])} for d in ds[-10:]]
    json.dump(out, open(f"{os.environ.get('ACC_OUT_DIR','/tmp/acc')}/{code}.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
    # compact report
    s=out["snap"]; v1=out["v1"]; v2=out["v2"]; v3=out["v3"]; cn=out["conc"]
    print(f"\n===== {code} {name} 基准{out['basis']} =====")
    print(f"P={s['c']} {s['pct']}% vol={s['vol']:,.0f}万 量比{s['vr']} 换手{s['to']}% 获利{s['prof']}% 成本{s['avg']} PE字段{s['pe']} 市值{s['mcap']}亿 股本{s['shares_yi']}亿")
    if exd: print(f"除息日: {[(e['d'],e['gap']) for e in exd]}")
    ma_s=" ".join(f"MA{k}{v['v']}{'↑' if v['dir']>0 else '↓'}({v['gap']:+.1f}%)" for k,v in sorted(out["ma"].items(), key=lambda x:int(x[0])))
    print(f"MA: {ma_s}")
    print(f"conc90={cn['conc90']} Δ20d斜率={cn['slope90_20d']:+.4f} Δ60d={cn['slope90_60d']:+.4f} | conc70={cn['conc70']} Δ20d={cn['slope70_20d']:+.4f} | 成本sd30={cn.get('avgcost_sd30')}")
    print(f"OBV={v1['obv']:,}(20日斜率{v1['obv_slope20']:+,}, 分位{v1['obv_pctile']}%) CMF20={v1['cmf20']} MFI={v1['mfi14']} RSI={v1['rsi14']} KDJ={v1['kdj']}")
    print(f"MACD 日柱{v1['macd']['hist']:+} 周柱{v1['macd']['whist']:+} | 布林带宽{v1['boll']['bw']}%(分位{v1['boll']['bw_pctile']}%) %B={v1['boll']['pb']}")
    dd=v1["divergence"]
    if dd.get("low60"):
        lo,lp=dd["low60"],dd["low_prev"]
        print(f"背离: 低点{lo['d']} L={lo['L']}(量{lo['volx']}×) vs 前低{lp['d']} L={lp['L']} → OBV{'✓' if dd['obv'] else '✗'} RSI{'✓' if dd['rsi'] else '✗'}")
    print(f"ADX={v2['adx']} +DI{v2['pdi']}/-DI{v2['ndi']} CCI={v2['cci']} SAR={v2['sar']}({'多' if s['c']>v2['sar'] else '空'},翻转{v2['sar_flip']}) 52周位{v2['w52_pos']}% 量分位{v2['vol_pctile']}% 波动分位{v2['vola_pctile']}% 量价相关{v2['pv_corr']} H60={v3['hurst60']} H250={v3['hurst250']}")
    unf=v2["gaps"]["unfilled"]
    print(f"未回补缺口{len(unf)}个: {[(x[0],x[1],x[2]) for x in unf[:6]]}")
    print(f"VP60: POC={out['vp']['poc']} VA={out['vp']['va']} 换手60日={out['turnover60_pct']}% 近10日净外盘日数={sum(1 for x in out['net10'] if x['net']>0)}/10")
    ch=out["chips_traj"]
    print("筹码尾迹: " + " | ".join(f"{x['d'][5:]}:{x['prof']:.0f}%/{x['conc90']}" for x in ch[-6:]))
    return out

def main():
    OUT = os.environ.get("ACC_OUT_DIR", "/tmp/acc")  # output dir overridable for automation (auto_run.py)
    os.makedirs(OUT, exist_ok=True)
    codes = sys.argv[1:]
    if not codes:
        codes = sorted({f[:6] for f in os.listdir(DT) if f.endswith("_250d.txt")})
    for c in codes:
        try: analyze(c)
        except Exception as e:
            print(f"===== {c} ERROR: {e!r} =====")

if __name__ == "__main__":
    main()
