#!/usr/bin/env python3
"""Frozen synthetic regression set, first evaluated independently on 2026-09-10."""
from collections import Counter,defaultdict
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.domain.search_cluster_classifier import classify

def main():
    data=json.loads((Path(__file__).parent/'fixtures/search_cluster_cleaner_holdout.json').read_text())
    profiles={p['nm_id']:dict(p,source=p['compatibility_source'],verified_at=p['verified_at']+'T00:00:00Z') for p in data['profiles']}
    matrix=Counter();group=defaultdict(Counter);sku=defaultdict(Counter);results=[]
    for c in data['cases']:
        r=classify(c['query'],profiles[c['nm_id']]);actual=r['verdict'];expected=c['expected']
        results.append(r);matrix[(expected,actual)]+=1;group[c['family']][actual]+=1;sku[str(c['nm_id'])][actual]+=1
        assert not (actual=='exclude' and expected!='exclude'),f"false exclude {c['id']}"
        assert not (actual=='allow' and expected!='allow'),f"false allow {c['id']}"
    for c,r in zip(reversed(data['cases']),reversed(results)): assert classify(c['query'],profiles[c['nm_id']])==r
    decisive=sum(n for (expected,_),n in matrix.items() if expected!='review')
    correct_auto=sum(n for (expected,actual),n in matrix.items() if expected==actual and expected!='review')
    assert len(data['cases'])>=200 and len(profiles)>=6 and len({p['kind'] for p in profiles.values()})==3
    assert correct_auto/decisive>=.95
    print(json.dumps(dict(cases=len(results),matrix={f'{a}->{b}':n for (a,b),n in sorted(matrix.items())},auto_rate=correct_auto/decisive,permutation_equal=True,by_family=group,by_sku=sku),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
