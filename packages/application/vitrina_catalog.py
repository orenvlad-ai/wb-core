"""Reporting identities come from nomenclature, not the manual Balance list."""
from dataclasses import replace

from packages.application.stock_catalog_scope import require_stock_catalog_scope
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item


def reporting_config(db_path, configured):
    scope = require_stock_catalog_scope(db_path)
    existing = {item.nm_id: item for item in configured}
    result = []
    for index, item in enumerate(scope['items'], 1):
        nm = item['nm_id']
        if nm in existing:
            result.append(replace(existing[nm], enabled=True))
        else:
            result.append(ConfigV2Item(nm, True, item.get('nomenclature_name') or str(nm),
                                      item.get('product_type') or 'Каталог',
                                      max((x.display_order for x in configured), default=0) + index))
    # A catalog inconsistency must not silently retire an already reported SKU.
    known = set(scope['nm_ids'])
    result.extend(item for item in configured if item.enabled and item.nm_id not in known)
    return sorted(result, key=lambda x: (x.display_order, x.nm_id)), scope
