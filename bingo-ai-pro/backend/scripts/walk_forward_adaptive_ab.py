from __future__ import annotations
import argparse, json, math, random
from collections import defaultdict
from statistics import mean
from database.collector_store import get_draw_history
from services.voting_engine import build_voting_candidates_from_draws

MODELS=("laowanjia","hotcold","missing","pattern","balance")
KEYS={"laowanjia":"laowanjia_weight","hotcold":"hot_cold_weight","missing":"missing_weight","pattern":"pattern_weight","balance":"balance_weight"}

def nums(d):
    out=[]
    for v in d.get("numbers") or []:
        try:n=int(v)
        except Exception:continue
        if 1<=n<=80 and n not in out:out.append(n)
    return out

def hits(a,b): return len(set(a)&set(b))

def ci(v):
    if not v:return {"mean":0.0,"low":0.0,"high":0.0}
    a=mean(v)
    if len(v)<2:return {"mean":a,"low":a,"high":a}
    se=math.sqrt(sum((x-a)**2 for x in v)/(len(v)-1)/len(v))
    return {"mean":a,"low":a-1.96*se,"high":a+1.96*se}

def weights(perf,version):
    if any(len(perf[m])<20 for m in MODELS):return None
    av={m:mean(perf[m][-100:]) for m in MODELS}; center=mean(av.values()) or 1
    return {"strategy":"v7_models","version":version,**{KEYS[m]:max(.5,min(1.5,av[m]/center)) for m in MODELS}}

def run(draws,warmup=100,seed=20260925):
    clean=[{**d,"issue":str(d.get("issue")),"numbers":nums(d)} for d in draws if len(nums(d))==20 and str(d.get("issue") or "").isdigit()]
    clean.sort(key=lambda d:int(d["issue"]))
    perf=defaultdict(list); rows=[]; version=0
    for i in range(warmup,len(clean)):
        history=list(reversed(clean[max(0,i-100):i])); target=clean[i]; official=target["numbers"]
        off=build_voting_candidates_from_draws(history,None)
        adaptive=weights(perf,version+1)
        on=build_voting_candidates_from_draws(history,adaptive)
        if adaptive:version+=1
        rng=random.Random(f"{seed}:{target['issue']}"); rnd=rng.sample(range(1,81),20)
        os=off["ranked_candidates"]; ns=on["ranked_candidates"]
        for m in MODELS:
            cand=(off.get("model_scores",{}).get(m) or {}).get("candidate_numbers") or []
            perf[m].append(hits(cand[:20],official))
        rows.append({"issue":target["issue"],"adaptive_enabled":adaptive is not None,
          "off20":hits(os[:20],official),"on20":hits(ns[:20],official),"random20":hits(rnd,official),
          "off5":hits(os[:5],official),"on5":hits(ns[:5],official),"random5":hits(rnd[:5],official)})
    avg=lambda k: mean(r[k] for r in rows) if rows else 0
    return {"summary":{"issues":len(rows),"warmup":warmup,"adaptive_active_issues":sum(r["adaptive_enabled"] for r in rows),
      "off20":avg("off20"),"on20":avg("on20"),"random20":avg("random20"),
      "off5":avg("off5"),"on5":avg("on5"),"random5":avg("random5"),
      "paired_on_minus_off_20":ci([r["on20"]-r["off20"] for r in rows]),
      "paired_on_minus_off_5":ci([r["on5"]-r["off5"] for r in rows]),\n      "model_top20":{m:(mean(perf[m]) if perf[m] else 0) for m in MODELS},\n      "model_recent100_top20":{m:(mean(perf[m][-100:]) if perf[m] else 0) for m in MODELS},\n      "final_adaptive_weights":weights(perf,version+1)},"rows":rows}

def main():
    p=argparse.ArgumentParser();p.add_argument("--limit",type=int,default=2000);p.add_argument("--warmup",type=int,default=100);a=p.parse_args()
    print(json.dumps(run(get_draw_history(a.limit),a.warmup)["summary"],ensure_ascii=False,indent=2))
if __name__=="__main__":main()
