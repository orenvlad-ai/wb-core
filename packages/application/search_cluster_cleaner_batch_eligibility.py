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
        admitted = fixture_admission
        approved_profiles = {}
    else:
        from apps.search_cluster_cleaner_stage_e import _package, _admitted_targets
        try:
            config = json.loads(config_path.read_text(encoding='utf-8'))
            if set(config) != {'seller_id', 'account_scope', 'generation', 'owner_username', 'approved_package_path'}:
                raise ValueError('unexpected config')
            if (config['seller_id'] != cleaner.account.seller_id or config['account_scope'] != cleaner.account.account_scope
                    or config['generation'] != generation or config['owner_username'] != cleaner.owner_username):
                raise ValueError('Stage E identity mismatch')
            package = _package(Path(config['approved_package_path']), cleaner.account, generation)
            admitted = package['manual_admission']
            verified = [Target(row['advert_id'], row['nm_id'], contract_verified=True) for row in admitted if row.get('state') == 'verified']
            _admitted_targets(package, verified, config_path.parent)
            approved_profiles = {p.nm_id: p for p in (Profile.parse(row) for row in package['profiles'])}
        except (OSError, ValueError, TypeError, KeyError, CleanerError) as exc:
            raise CleanerError('manual_admission_unavailable', 'Не удалось проверить допуск ручной чистки', 409) from exc
    return {(row['advert_id'], row['nm_id']): row for row in admitted if row.get('state') == 'verified'}, approved_profiles


def eligibility_rows(cleaner, generation: str, catalog: list[Target], *, fixture_admission=None, config_path: Path = CONFIG_PATH) -> list[dict]:
    admitted, approved_profiles = approved_context(cleaner, generation, fixture_admission=fixture_admission, config_path=config_path)
    current = {}
    for target in catalog:
        key = (target.advert_id, target.nm_id)
        if key in current:
            raise CleanerError('campaign_catalog_duplicate', 'WB вернул повтор пары кампании и товара', 409)
        current[key] = target
    result = []
    with cleaner.store.read() as c:
        for key in sorted(set(current) | set(admitted)):
            target = current.get(key)
            profile = cleaner._profile(c, key[1])
            held = c.execute('SELECT reason FROM cleaner_target_holds WHERE account=? AND target=?', (cleaner.key, f'{key[0]}:{key[1]}')).fetchone()
            approved = approved_profiles.get(key[1])
            status_code = target.status if target else None
            status = {9: 'active', 11: 'paused', 7: 'completed', -1: 'archive'}.get(status_code, 'other')
            reason = None
            if key not in admitted: reason = 'not_approved'
            elif target is None: reason = 'campaign_sku_missing'
            elif status not in {'active', 'paused'}: reason = 'unsupported_campaign_status'
            elif target.payment_type != 'cpm': reason = 'payment_type_not_cpm'
            elif target.bid_type != 'manual' or not target.contract_verified: reason = 'contract_not_verified'
            elif profile is None: reason = 'profile_required'
            elif fixture_admission is None and (approved is None or approved.semantic_fingerprint != profile.semantic_fingerprint): reason = 'manual_profile_mismatch'
            elif held: reason = 'target_held'
            result.append(dict(advert_id=key[0], nm_id=key[1], campaign_name=target.name if target else '',
                               product_title=profile_title(profile, key[1]), status_code=status_code, status=status,
                               eligible=reason is None, reason=reason, admitted=key in admitted,
                               profile_ready=bool(profile and (fixture_admission is not None or (approved and approved.semantic_fingerprint == profile.semantic_fingerprint)))))
    return result


def category_contract() -> dict:
    return {'active': {'selectable': True}, 'paused': {'selectable': True},
            'completed': {'selectable': False, 'reason': 'unsupported_campaign_status'},
            'archive': {'selectable': False, 'reason': 'unsupported_campaign_status'}}
