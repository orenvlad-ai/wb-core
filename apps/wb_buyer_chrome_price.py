"""Read two labelled buyer prices with the saved official Chrome profile.

No cookies leave Chrome.  The scalar SPP input stays empty: an account page and
two visible prices do not establish the expected buyer identity or comparable
variant, destination and payment context for a global SPP calculation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_buyer_chrome_auth as auth  # noqa: E402
from apps import wb_buyer_chrome_runtime as runtime  # noqa: E402
from apps import wb_buyer_session_recovery as legacy  # noqa: E402


# The popup is the sole price source. Neither crossed-out reference prices nor
# the separate business offer can silently become a buyer payment price.
PRICE_DETAIL = r"""(() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
      s.visibility === 'visible' && Number(s.opacity) > 0;
  };
  const money = text => {
    const match = String(text || '').match(/\d[\d\s\u00a0.,]*\s*₽/);
    if (!match) return null;
    let value = match[0].replace(/[^\d,.]/g, '').replace(',', '.');
    if (/\.\d{3}(?:\.|$)/.test(value)) value = value.replace(/\./g, '');
    const number = Number(value);
    return Number.isFinite(number) && number > 0 && number < 10000000 ? number : null;
  };
  const title = [...document.querySelectorAll('h1,h2,h3,h4,h5,[class*="title"]')]
    .find(el => visible(el) && String(el.innerText || '').trim() === 'Детализация цены');
  if (!title) return {wallet:null, nonwallet:null};
  let popup = title;
  for (let index = 0; index < 6 && popup.parentElement; index++) {
    const next = popup.parentElement, text = String(next.innerText || '');
    if (text.length > 2400) break;
    popup = next;
    if (/с WB Кошельком/i.test(text) && /без WB Кошелька/i.test(text)) break;
  }
  // Read visible text nodes, excluding reference prices even when the strike
  // comes from a CSS class. Flattening innerText first loses that distinction.
  const pieces = [], walker = document.createTreeWalker(popup, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && pieces.length < 120) {
    const node = walker.currentNode;
    let element = node.parentElement, excluded = false;
    if (!element || !visible(element)) continue;
    while (element && popup.contains(element)) {
      const style = getComputedStyle(element);
      if (!visible(element) || ['DEL', 'S'].includes(element.tagName) ||
          style.textDecorationLine.includes('line-through')) { excluded = true; break; }
      if (element === popup) break;
      element = element.parentElement;
    }
    if (!excluded && String(node.nodeValue || '').trim()) pieces.push(String(node.nodeValue).trim());
  }
  const content = pieces.join(' ');
  const labelled = /(\d[\d\s\u00a0.,]*\s*₽)\s*(с\s*WB\s*Кошельком|без\s*WB\s*Кошелька)/gi;
  const result = {wallet:null, nonwallet:null};
  for (const match of content.matchAll(labelled)) {
    const value = money(match[1]);
    if (value === null) continue;
    const key = /^без/i.test(match[2]) ? 'nonwallet' : 'wallet';
    if (result[key] !== null && result[key] !== value) return {wallet:null, nonwallet:null};
    result[key] = value;
  }
  return result;
})()"""

OPEN_DETAIL = r"""(() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility === 'visible';
  };
  if ([...document.querySelectorAll('h1,h2,h3,h4,h5')].some(el =>
      visible(el) && String(el.innerText || '').trim() === 'Детализация цены')) return true;
  const choices = [...document.querySelectorAll('[class*="price"]')]
    .filter(el => visible(el) && /^\s*[\d\s\u00a0]+\s*₽\s*$/.test(String(el.innerText || '')) &&
      !String(el.closest('del')?.innerText || '').trim()).slice(0, 20);
  const control = choices.find(el => {
    const rect = el.getBoundingClientRect();
    return rect.x > innerWidth * .4 && rect.y < innerHeight * .8;
  });
  if (!control) return false;
  const clickable = control.closest('button,[role="button"],a') || control;
  clickable.click();
  return true;
})()"""


def _unknown(nm_id: int, reason: str = "authenticated_price_unavailable") -> dict[str, Any]:
    return {
        "status": "price_unavailable", "reason": reason, "nm_id": nm_id,
        "authenticated_buyer_price": None, "wallet_price": None, "normal_price": None,
        "payment_context": "wallet_and_nonwallet_separate", "destination_context": {"status": "unknown"},
        "variant_context": {"status": "unknown"}, "measured_at": datetime.now(timezone.utc).isoformat(),
        "source_method": "chrome_visible_price_detail", "source_endpoint": "https://www.wildberries.ru/",
        "session_status": "probe_error", "account_fingerprint_available": False,
        "authenticated_session_proof": False, "persistent_profile": True,
    }


def _labelled_prices(value: Any) -> tuple[float | None, float | None]:
    if not isinstance(value, Mapping):
        return None, None
    def valid(raw: Any) -> float | None:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        number = float(raw)
        return number if 0 < number < 10_000_000 else None
    return valid(value.get("wallet")), valid(value.get("nonwallet"))


def _evaluate(pipe: auth.ChromePipe, session: str, expression: str) -> Any:
    result = pipe.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": False}, session=session)
    if result.get("exceptionDetails"):
        return None
    value = result.get("result")
    return value.get("value") if isinstance(value, Mapping) else None


def _product_target(pipe: auth.ChromePipe, nm_id: int) -> str:
    expected = f"/catalog/{nm_id}/detail.aspx"
    targets = pipe.call("Target.getTargets").get("targetInfos") or []
    for target in targets[:30]:
        parsed = urlparse(str(target.get("url") or ""))
        if target.get("type") == "page" and parsed.hostname == "www.wildberries.ru" and parsed.path == expected:
            return str(target.get("targetId") or "")
    return ""


def _read_in_runner(nm_id: int) -> dict[str, Any]:
    if os.geteuid() == 0 or not 0 < nm_id < 1_000_000_000_000:
        return _unknown(nm_id, "invalid_nm_id")
    result = _unknown(nm_id)
    env = {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "DISPLAY": auth.DISPLAY,
        "XAUTHORITY": str(runtime.STATE / "price.Xauthority"), "HOME": str(runtime.PROFILE),
        "XDG_CONFIG_HOME": str(runtime.PROFILE), "XDG_CACHE_HOME": str(runtime.PROFILE),
    }
    xvfb = chrome = openbox = None
    pipe = None
    try:
        xauth = runtime.STATE / "price.Xauthority"
        xauth.touch(mode=0o600)
        os.chmod(xauth, 0o600)
        subprocess.run(["xauth", "-f", str(xauth), "add", auth.DISPLAY, "MIT-MAGIC-COOKIE-1", os.urandom(16).hex()], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        xvfb = auth._spawn(["Xvfb", auth.DISPLAY, "-screen", "0", "1600x900x24", "-nolisten", "tcp", "-auth", str(xauth)], "price-xvfb.log", env=env)
        auth._wait_display(xvfb)
        openbox = auth._spawn(["openbox", "--sm-disable"], "price-openbox.log", env=env)
        chrome, pipe = auth._launch_chrome(runtime.CHROME, env, run_id="buyer-price", generation=1, diagnostic=False)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if pipe.visible_surface() == "account":
                result.update(session_status="authenticated_surface", authenticated_session_proof=True, session_checked_at=datetime.now(timezone.utc).isoformat())
                break
            time.sleep(1)
        else:
            result.update(session_status="login_redirect", reason="buyer_login_required")
            return result
        targets = pipe.call("Target.getTargets").get("targetInfos") or []
        account = next((target for target in targets[:30] if target.get("type") == "page" and
                        urlparse(str(target.get("url") or "")).hostname == "www.wildberries.ru" and
                        urlparse(str(target.get("url") or "")).path.startswith("/lk")), None)
        if not account:
            return result
        session = str(pipe.call("Target.attachToTarget", {"targetId": account["targetId"], "flatten": True}).get("sessionId") or "")
        if not session:
            return result
        try:
            pipe.call("Page.navigate", {"url": f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"}, session=session)
        finally:
            pipe.call("Target.detachFromTarget", {"sessionId": session})
        previous = None
        stable = 0
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            target_id = _product_target(pipe, nm_id)
            if not target_id:
                time.sleep(1)
                continue
            session = str(pipe.call("Target.attachToTarget", {"targetId": target_id, "flatten": True}).get("sessionId") or "")
            if not session:
                time.sleep(1)
                continue
            try:
                prices = _evaluate(pipe, session, PRICE_DETAIL)
                wallet, nonwallet = _labelled_prices(prices)
                if wallet is None or nonwallet is None:
                    _evaluate(pipe, session, OPEN_DETAIL)
            finally:
                pipe.call("Target.detachFromTarget", {"sessionId": session})
            if wallet is not None and nonwallet is not None:
                current = (wallet, nonwallet)
                stable = stable + 1 if current == previous else 1
                previous = current
                if stable >= 2:
                    result.update(status="observed", reason="buyer_price_context_not_verified", wallet_price=wallet, normal_price=nonwallet,
                                  measured_at=datetime.now(timezone.utc).isoformat(), stable=True, stable_read_count=2,
                                  proof="visible_price_detail_wallet_nonwallet")
                    return result
            time.sleep(1)
        return result
    finally:
        cleanup_failed = False
        for cleanup in (
            lambda: auth._stop_chrome(chrome),
            lambda: pipe.close() if pipe is not None else None,
            lambda: auth._stop_process(openbox),
            lambda: auth._stop_process(xvfb),
        ):
            try:
                cleanup()
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise RuntimeError("buyer price browser cleanup failed")


def read_price(nm_id: int) -> dict[str, Any]:
    """Synchronous, single-flight call from the existing application contract."""
    if os.geteuid() != 0:
        return _unknown(nm_id, "authenticated_price_probe_failed")
    try:
        with runtime.profile_operation_lock() as guard_fd:
            with auth._shared_start_lock():
                if auth.raw_status().get("running") or legacy.read_recovery_status(legacy.load_recovery_config_from_env(), with_probe=False).get("running"):
                    return _unknown(nm_id, "buyer_session_automation_busy")
                runtime.ensure_runner_idle()
            if runtime._available(Path("/")) < runtime._root_reserve():
                return _unknown(nm_id, "buyer_chrome_storage_reserve")
            runtime.ensure_runtime()
            user = pwd.getpwnam(runtime.USER)
            command = ["setpriv", "--reuid", str(user.pw_uid), "--regid", str(user.pw_gid), "--clear-groups",
                       sys.executable, str(Path(__file__).resolve()), "read", "--nm-id", str(nm_id)]
            try:
                child = subprocess.run(command, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"},
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=105, check=True,
                                       pass_fds=(guard_fd,))
                value = json.loads(child.stdout)
                return value if isinstance(value, dict) else _unknown(nm_id)
            except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                return _unknown(nm_id, "authenticated_price_probe_failed")
    except BlockingIOError:
        return _unknown(nm_id, "buyer_session_automation_busy")
    except (OSError, RuntimeError, ValueError):
        return _unknown(nm_id, "authenticated_price_probe_failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("read",))
    parser.add_argument("--nm-id", type=int, required=True)
    arguments = parser.parse_args()
    try:
        result = _read_in_runner(arguments.nm_id)
    except Exception:
        result = _unknown(arguments.nm_id, "authenticated_price_probe_failed")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
