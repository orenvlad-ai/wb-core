#!/usr/bin/env python3
"""Bootstrap owner gets current and future sections after server-side auth only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.adapters import registry_upload_http_entrypoint as web


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def session(username: str, role: str, secret: str, *, expires: int = 60) -> SimpleNamespace:
    payload = encoded(json.dumps(dict(u=username, r=role, exp=int(time.time()) + expires)).encode())
    signature = encoded(hmac.new(secret.encode(), payload.encode('ascii'), hashlib.sha256).digest())
    return SimpleNamespace(headers={'Cookie': f'{web.WEB_AUTH_COOKIE_NAME}={payload}.{signature}'})


def main() -> None:
    secret = 'synthetic-owner-acl-secret'
    salt = b'synthetic-owner-acl-salt'
    password = 'synthetic-password'
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 260_000)
    config = dict(enabled=True, session_secret=secret,
                  operator=dict(username='bootstrap-owner', role='admin', display_name='Owner',
                                password_hash=f'pbkdf2_sha256$260000${encoded(salt)}${encoded(digest)}',
                                allowed_sections=['settings']),
                  supplier=dict(enabled=True, username='supplier'))
    login = web._match_web_auth_principal('bootstrap-owner', password, config, None)
    assert login and login.get('trusted_bootstrap_owner') is True
    assert web._match_web_auth_principal('bootstrap-owner', 'wrong-password', config, None) is None
    owner = web._authenticated_web_user(session('bootstrap-owner', 'admin', secret), config)
    assert owner and owner.get('trusted_bootstrap_owner') is True
    assert web._authenticated_web_user(session('bootstrap-owner', 'admin', secret, expires=-1), config) is None
    assert web._authenticated_web_user(session('bootstrap-owner', 'admin', 'wrong-secret'), config) is None

    runtime_admin = dict(username='runtime-admin', role='admin', allowed_sections=['settings'],
                         manage_users=True, is_active=True)
    with patch.object(web, '_load_runtime_user_by_username', return_value=runtime_admin):
        ordinary = web._authenticated_web_user(session('runtime-admin', 'admin', secret), config)
    assert ordinary and ordinary.get('trusted_bootstrap_owner') is not True
    supplier = web._authenticated_web_user(session('supplier', 'supplier', secret), config)
    assert supplier and supplier.get('trusted_bootstrap_owner') is not True

    future_section = 'future_product_section'
    future_tabs = dict(web.WEB_AUTH_UNIFIED_TAB_SECTIONS, future=future_section)
    with patch.object(web, 'WEB_AUTH_SECTION_IDS', (*web.WEB_AUTH_SECTION_IDS, future_section)), \
         patch.object(web, 'WEB_AUTH_UNIFIED_TAB_SECTIONS', future_tabs):
        assert web._user_has_section_access(owner, future_section)
        assert 'future' in web._allowed_unified_tabs_for_user(owner)
        assert not web._user_has_section_access(ordinary, future_section)
        assert 'future' not in web._allowed_unified_tabs_for_user(ordinary)
        assert not web._user_has_section_access(supplier, future_section)
        assert future_section in web._env_principal_user_records(config)[0]['allowed_sections']
        with patch.object(web, '_required_section_for_path', return_value=future_section):
            assert web._user_can_access_path(owner, '/v1/future-section')
            assert not web._user_can_access_path(ordinary, '/v1/future-section')
            assert not web._user_can_access_path(supplier, '/v1/future-section')

    ads_path = '/v1/sheet-vitrina-v1/ads/keyword-cleaner/summary'
    assert web._user_can_access_path(owner, ads_path)
    assert not web._user_can_access_path(ordinary, ads_path)
    assert not web._user_can_access_path(supplier, ads_path)
    assert web.WEB_AUTH_SECTION_ADS in web._user_allowed_sections(owner)
    assert web.WEB_AUTH_SECTION_ADS not in web._user_allowed_sections(ordinary)
    assert set(web.WEB_AUTH_SECTION_IDS) == set(web._user_allowed_sections(owner))
    print('registry_upload_http_entrypoint_owner_acl_smoke: ok')


if __name__ == '__main__':
    main()
