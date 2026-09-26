"""Read-only exact campaign eligibility for explicit manual batch selection."""
from __future__ import annotations

import json
from pathlib import Path

from packages.contracts.search_cluster_cleaner import CleanerError, Profile, Target
from packages.application.search_cluster_cleaner_web import profile_title

CONFIG_PATH = Path('/var/lib/wb-core/search-cluster-cleaner-admission/stage-e-config.json')


def approved_context(cleaner, generation: str, *, fixture_admission=None, config_path: Path = CONFIG_PATH):
    """Load the immutable admission upper bound; never bootstrap on GET."""
    if fixture_admission is not None:
        admitted = {(row['advert_id'], row['nm_id']) for row in fixture_admission if row.get('state') == 'verified'}
        return admitted, {}, set()
    else:
        from apps.search_cluster_cleaner_stage_e import _package, _approved_card_receipts, _approved_card_source
        try:
            config = json.loads(config_path.read_text(encoding='utf-8'))
            if set(config) != {'seller_id', 'account_scope', 'generation', 'owner_username', 'approved_package_path'}:
                raise ValueError('unexpected config')
            if (config['seller_id'] != cleaner.account.seller_id or config['account_scope'] != cleaner.account.account_scope
                    or config['generation'] != generation or config['owner_username'] != cleaner.owner_username):
                raise ValueError('Stage E identity mismatch')
            package = _package(Path(config['approved_package_path']), cleaner.account, generation)
            evidence = _approved_card_receipts(package, config_path.parent)
            source = _approved_card_source(package, config_path.parent)
            for nm_id, row in evidence.items():
                if nm_id in source and row['current_card_sha256'] != source[nm_id]['card_digest']:
                    raise ValueError('card source and evidence disagree')
            approved_profiles = {p.nm_id: p for p in (Profile.parse(row) for row in package['profiles'])}
        except (OSError, ValueError, TypeError, KeyError, CleanerError) as exc:
            raise CleanerError('manual_admission_unavailable', 'Не удалось проверить допуск ручной чистки', 409) from exc
    return set(), approved_profiles, set(source)


def eligibility_rows(cleaner, generation: str, catalog: list[Target], *, fixture_admission=None, config_path: Path = CONFIG_PATH) -> list[dict]:
    admitted, approved_profiles, approved_skus = approved_context(cleaner, generation, fixture_admission=fixture_admission, config_path=config_path)
    current = {}
    for target in catalog:
        if target.payment_type != 'cpm':
            continue  # CPC is outside this manual CPM batch, including counts.
        key = (target.advert_id, target.nm_id)
        if key in current:
            raise CleanerError('campaign_catalog_duplicate', 'WB вернул повтор пары кампании и товара', 409)
        current[key] = target
    result = []
    keys=sorted(current)
    with cleaner.store.read() as c:
        profiles={nm:cleaner._profile(c,nm) for nm in {key[1] for key in keys}}
        holds={key:c.execute('SELECT reason FROM cleaner_target_holds WHERE account=? AND target=?',
                             (cleaner.key,f'{key[0]}:{key[1]}')).fetchone() for key in keys}
    for key in keys:
        target = current[key]
        profile = profiles[key[1]]
        held = holds[key]
        approved = approved_profiles.get(key[1])
        status_code = target.status
        status = {9: 'active', 11: 'paused', 7: 'completed', -1: 'archive'}.get(status_code, 'other')
        sku_approved = key in admitted if fixture_admission is not None else key[1] in approved_skus and approved is not None
        reason = None
        if status not in {'active', 'paused'}: reason = 'unsupported_campaign_status'
        elif target.bid_type != 'manual' or not target.contract_verified: reason = 'contract_not_verified'
        elif profile is None: reason = 'profile_required'
        elif not sku_approved: reason = 'sku_not_approved'
        elif fixture_admission is None and (approved is None or approved.semantic_fingerprint != profile.semantic_fingerprint): reason = 'manual_profile_mismatch'
        elif held: reason = 'target_held'
        result.append(dict(advert_id=key[0], nm_id=key[1], campaign_name=target.name,
                           product_title=profile_title(profile or approved, key[1]), status_code=status_code, status=status,
                           payment_type='cpm',eligible=reason is None, reason=reason, admitted=sku_approved,
                           profile_ready=bool(profile and (fixture_admission is not None or (approved and approved.semantic_fingerprint == profile.semantic_fingerprint)))))
    return result


def category_contract() -> dict:
    return {'active': {'selectable': True}, 'paused': {'selectable': True},
            'completed': {'selectable': False, 'reason': 'unsupported_campaign_status'},
            'archive': {'selectable': False, 'reason': 'unsupported_campaign_status'}}
