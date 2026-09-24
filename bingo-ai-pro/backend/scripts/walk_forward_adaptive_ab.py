from __future__ import annotations
import argparse, json, math, random
from collections import defaultdict
from statistics import mean
from database.collector_store import get_draw_history
from services.voting_engine import build_voting_candidates_from_draws
from collections import Counter

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

def _rank_subset(model_scores, included=None, excluded=None):
    votes=Counter()
    for model_key, payload in model_scores.items():
        if included is not None and model_key not in included: continue
        if excluded is not None and model_key == excluded: continue
        confidence=float(payload.get("confidence") or 0)
        weight=max(1, confidence/20)
        for rank, number in enumerate(payload.get("candidate_numbers") or []):
            votes[number] += weight + max(0, 20-rank)*0.15
    return [number for number,_ in votes.most_common(20)]

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
        model_hits={}
        for m in MODELS:
            cand=(off.get("model_scores",{}).get(m) or {}).get("candidate_numbers") or []
            h20=hits(cand[:20],official); h5=hits(cand[:5],official)
            model_hits[m]={"hit20":h20,"hit5":h5}
            perf[m].append(h20)
        rows.append({"issue":target["issue"],"adaptive_enabled":adaptive is not None,
          "off20":hits(os[:20],official),"on20":hits(ns[:20],official),"random20":hits(rnd,official),
          "off5":hits(os[:5],official),"on5":hits(ns[:5],official),"random5":hits(rnd[:5],official),
          "model_hits":model_hits,"adaptive_weights":adaptive,
          "leave_one_out":{m:hits(_rank_subset(off.get("model_scores",{}),excluded=m),official) for m in MODELS},
          "core3":hits(_rank_subset(off.get("model_scores",{}),included={"hotcold","missing","balance"}),official),
          "core4_no_pattern":hits(_rank_subset(off.get("model_scores",{}),included={"laowanjia","hotcold","missing","balance"}),official),
          "core4_no_laowanjia":hits(_rank_subset(off.get("model_scores",{}),included={"hotcold","missing","pattern","balance"}),official)})
    avg=lambda k: mean(r[k] for r in rows) if rows else 0
    model_summary={m:{"hit20":mean(r["model_hits"][m]["hit20"] for r in rows) if rows else 0,
                      "hit5":mean(r["model_hits"][m]["hit5"] for r in rows) if rows else 0} for m in MODELS}
    adaptive_rows=[r for r in rows if r["adaptive_weights"]]
    # Strict chronological holdout: selection came from earlier observations;
    # report the newest third separately without using it to choose the subset.
    split=max(1,(len(rows)*2)//3)
    holdout=rows[split:]
    def havg(key):
        return mean(r[key] for r in holdout) if holdout else 0
    core3_d=[r["core3"]-r["off20"] for r in holdout]
    core3_r=[r["core3"]-r["random20"] for r in holdout]
    segments=[]
    for start in range(0,len(rows),100):
        seg=rows[start:start+100]
        if not seg: continue
        diffs_full=[r["core3"]-r["off20"] for r in seg]
        diffs_random=[r["core3"]-r["random20"] for r in seg]
        segments.append({"start_issue":seg[0]["issue"],"end_issue":seg[-1]["issue"],"issues":len(seg),
          "core3_top20":mean(r["core3"] for r in seg),"full5_top20":mean(r["off20"] for r in seg),
          "random20":mean(r["random20"] for r in seg),"core3_minus_full5":mean(diffs_full),
          "core3_minus_random":mean(diffs_random)})
    weight_summary={m:(mean(r["adaptive_weights"][KEYS[m]] for r in adaptive_rows) if adaptive_rows else 1.0) for m in MODELS}
    return {"summary":{"issues":len(rows),"warmup":warmup,"adaptive_active_issues":sum(r["adaptive_enabled"] for r in rows),
      "model_performance":model_summary,"mean_adaptive_multipliers":weight_summary,
      "candidate_subset_top20":{"hotcold_missing_balance":avg("core3"),"no_pattern":avg("core4_no_pattern"),"no_laowanjia":avg("core4_no_laowanjia")},
      "rolling_100":{"segments":segments,
        "positive_vs_full5":sum(s["core3_minus_full5"]>0 for s in segments),
        "positive_vs_random":sum(s["core3_minus_random"]>0 for s in segments)},
      "chronological_holdout":{"issues":len(holdout),"start_issue":holdout[0]["issue"] if holdout else None,"end_issue":holdout[-1]["issue"] if holdout else None,
        "core3_top20":havg("core3"),"full5_top20":havg("off20"),"random20":havg("random20"),
        "core3_minus_full5":ci(core3_d),"core3_minus_random":ci(core3_r)},
      "leave_one_out_top20":{m:mean(r["leave_one_out"][m] for r in rows) if rows else 0 for m in MODELS},
      "leave_one_out_delta_vs_full":{m:(mean(r["leave_one_out"][m] for r in rows)-avg("off20")) if rows else 0 for m in MODELS},
      "off20":avg("off20"),"on20":avg("on20"),"random20":avg("random20"),
      "off5":avg("off5"),"on5":avg("on5"),"random5":avg("random5"),
      "paired_on_minus_off_20":ci([r["on20"]-r["off20"] for r in rows]),
      "paired_on_minus_off_5":ci([r["on5"]-r["off5"] for r in rows]),
      "model_top20":{m:(mean(perf[m]) if perf[m] else 0) for m in MODELS},
      "model_recent100_top20":{m:(mean(perf[m][-100:]) if perf[m] else 0) for m in MODELS},
      "final_adaptive_weights":weights(perf,version+1)},"rows":rows}

def main():
    p=argparse.ArgumentParser();p.add_argument("--limit",type=int,default=2000);p.add_argument("--warmup",type=int,default=100);a=p.parse_args()
    print(json.dumps(run(get_draw_history(a.limit),a.warmup)["summary"],ensure_ascii=False,indent=2))
if __name__=="__main__":main()
