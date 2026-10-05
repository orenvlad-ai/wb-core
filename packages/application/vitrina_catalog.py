"""Reporting identities come from nomenclature, not the manual Balance list."""
from dataclasses import replace

from packages.application.stock_catalog_scope import require_stock_catalog_scope
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item


def reporting_config(db_path, configured, *, canonical_groups=False):
    from packages.application.web_vitrina_window_read_context import borrowed_operational_connection
    from packages.application.stock_catalog_scope import read_stock_catalog_scope
    conn = borrowed_operational_connection(db_path)
    scope = read_stock_catalog_scope(conn) if conn is not None else require_stock_catalog_scope(db_path)
    if not scope['complete']:
        raise ValueError('reporting_nomenclature_identity_incomplete')
    existing = {item.nm_id: item for item in configured}
    result = []
    for index, item in enumerate(scope['items'], 1):
        nm = item['nm_id']
        if nm in existing:
            result.append(replace(existing[nm], enabled=True, **({"group": item.get("product_type") or "other"} if canonical_groups else {})))
        else:
            result.append(ConfigV2Item(nm, True, item.get('nomenclature_name') or str(nm),
                                      item.get('product_type') or ('other' if canonical_groups else 'Каталог'),
                                      max((x.display_order for x in configured), default=0) + index))
    # A catalog inconsistency must not silently retire an already reported SKU.
    known = set(scope['nm_ids'])
    result.extend(item for item in configured if item.enabled and item.nm_id not in known)
    return sorted(result, key=lambda x: (x.display_order, x.nm_id)), scope


def reporting_groups(conn):
    """Current registry identity/order; never create schema on a read path."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sheet_vitrina_v1_sku_groups'").fetchone():
        return []
    columns = {r[1] for r in conn.execute("PRAGMA table_info(sheet_vitrina_v1_sku_groups)")}
    order = "display_order" if "display_order" in columns else "0"
    return [{"group_key": row[0], "label": row[1], "display_order": row[2], "is_active": bool(row[3])}
            for row in conn.execute("SELECT group_key,label," + order + ",is_active FROM sheet_vitrina_v1_sku_groups ORDER BY " + ("display_order," if "display_order" in columns else "") + "group_key")]
