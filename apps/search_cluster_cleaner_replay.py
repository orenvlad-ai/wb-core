#!/usr/bin/env python3
"""Private historical replay/import verification. No input data is committed.

Reads the agreed audit bundle, reconstructs labels from the audit/interview
(not from classifier output), verifies groups 375/310, and tests the operational
import in an isolated temporary directory. Output contains private query text.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.storage_registry import StoreRegistry
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.contracts.search_cluster_cleaner import Account,digest
from packages.domain.search_cluster_classifier import classify,VERSION


def main() -> None:
    ap=argparse.ArgumentParser();ap.add_argument("--audit-root",type=Path,required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--verified-at",required=True);args=ap.parse_args()
    root=args.audit_root.resolve();output=args.output.resolve()
    if output==root or root in output.parents: raise SystemExit("output must not modify the audit bundle")
    source_hashes={}
    def read(name):
        raw=(root/name).read_bytes();source_hashes[name]=hashlib.sha256(raw).hexdigest();return json.loads(raw)
    saved=read("Детерминированный прогон/results.json")
    ps=read("Детерминированный прогон/profiles.json")
    audit=read("sources/final-classified.json")
    interview=read("Интервью/согласованные-93.json")
    receipts=read("Применение/confirmed-results.json")
    verified=read("Интервью/Применение исключений/verified.json")
    resolved=read("Детерминированный прогон/resolved-375.json")
    decisions={(str(c["id"]),r["Кластер"]):("exclude" if r["action"] in {"исключить","оставить исключённым"} else "allow") for c in audit for r in c["records"]}
    for row in interview: decisions[(str(row["campaign_id"]),row["cluster"]) ]="exclude" if row["desired_excluded"] else "allow"
    selected375={(str(r["campaign_id"]),r["query"]) for r in resolved}
    explicit310={(str(r["campaign"]),r["cluster"]) for r in receipts}|{(str(r["campaign_id"]),r["cluster"]) for r in interview}
    profiles=[dict(p,nm_id=int(nm),version=1,source=f"approved audit: {p['compatibility_source']}",verified_at=args.verified_at) for nm,p in ps.items()]
    by_profile={p["nm_id"]:p for p in profiles}
    assert len(saved)==8207 and len(decisions)==8207
    assert len(selected375)==375 and len(explicit310)==310
    assert len({(r['campaign_id'],r['sku'],r['query']) for r in saved})==8207
    mismatches=[];predictions=[];baseline=[]
    for row in saved:
        key=(str(row["campaign_id"]),row["query"]);expected=decisions[key]
        assert expected==row["reference"],f"historical reference mismatch {key}"
        prediction=classify(row["query"],by_profile[int(row["sku"])])
        answer=dict(advert_id=int(row["campaign_id"]),nm_id=int(row["sku"]),query=row["query"],expected=expected,**prediction)
        predictions.append(answer)
        if prediction["verdict"]!=expected:mismatches.append(answer)
        baseline.append(dict(advert_id=int(row["campaign_id"]),nm_id=int(row["sku"]),query=row["query"],decision=expected,observed_state="excluded" if row["current_excluded"] else "active",provenance=dict(source=row["reference_source"],audit_reference_key=list(key),explicit310=key in explicit310,resolved375=key in selected375,source_hashes=source_hashes)))
    # Replay inputs contain no reference/status/statistics; shuffle checks independence.
    order=list(range(len(saved)));random.Random(72).shuffle(order)
    for i in order:
        row=saved[i];assert classify(row["query"],by_profile[int(row["sku"])])=={k:v for k,v in predictions[i].items() if k not in {"advert_id","nm_id","query","expected"}}
    with tempfile.TemporaryDirectory(prefix="cleaner-baseline-replay-") as temporary:
        store=CleanerStore(StoreRegistry(Path(temporary)));app=KeywordCleaner(store,Account("offline-replay","private-audit"));app.initialize(generation="local-replay")
        imported=app.import_baseline(baseline,profiles,provenance=dict(source_hashes=source_hashes),expected_digest=digest(baseline),ready=False)
        repeated=app.import_baseline(baseline,profiles,provenance=dict(source_hashes=source_hashes),expected_digest=digest(baseline),ready=False)
        with store.read() as c:
            counts={t:c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("cleaner_baselines","cleaner_profiles","cleaner_observations","cleaner_auto_decisions","cleaner_write_operations","cleaner_runs")}
            labels=dict(c.execute("SELECT verdict,count(*) FROM cleaner_auto_decisions GROUP BY verdict"))
            baseline_ready=c.execute("SELECT baseline_ready FROM cleaner_settings").fetchone()[0]
            assert counts["cleaner_baselines"]==counts["cleaner_observations"]==counts["cleaner_auto_decisions"]==8207
            assert counts["cleaner_profiles"]==33 and counts["cleaner_write_operations"]==counts["cleaner_runs"]==0 and not baseline_ready
        assert repeated["replayed"]
    result=dict(classifier_version=VERSION,classifier_sha256=hashlib.sha256(Path("packages/domain/search_cluster_classifier.py").read_bytes()).hexdigest(),source_hashes=source_hashes,rows=len(saved),matches=len(saved)-len(mismatches),mismatches=mismatches,verdicts=dict(Counter(p["verdict"] for p in predictions)),groups={"resolved375":dict(rows=375,matches=sum((str(p['advert_id']),p['query']) in selected375 and p['verdict']==p['expected'] for p in predictions)),"explicit310":dict(rows=310,matches=sum((str(p['advert_id']),p['query']) in explicit310 and p['verdict']==p['expected'] for p in predictions))},permutation_equal=True,imported=imported,import_counts=counts,import_labels=labels,baseline_ready=False,network_calls=0,scope="Historical development corpus, not an independent future-quality guarantee")
    output.mkdir(parents=True,exist_ok=True);output.chmod(0o700)
    for name,value in (("replay.json",result),("predictions.json",predictions),("baseline-import-candidate.json",baseline)):
        path=output/name;path.write_text(json.dumps(value,ensure_ascii=False,indent=2));path.chmod(0o600)
    print(json.dumps({k:v for k,v in result.items() if k not in {"source_hashes","mismatches"}},ensure_ascii=False,indent=2))
    if mismatches:raise SystemExit(1)

if __name__=="__main__":main()
