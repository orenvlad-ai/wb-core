"""Read two labelled buyer prices with the saved official Chrome profile.

No cookies leave Chrome.  The scalar SPP input stays empty: an account page and
two visible prices do not establish the expected buyer identity or comparable
variant, destination and payment context for a global SPP calculation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
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


class BatchInterrupted(BaseException):
    """Escape card-level error handling so the browser cleanup always runs."""


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


def _read_product_in_batch(pipe: auth.ChromePipe, nm_id: int, account_target_id: str, *, batch_deadline: float) -> dict[str, Any]:
    """Read one card without closing the shared authenticated Chrome process."""
    result = _unknown(nm_id)
    session = str(pipe.call("Target.attachToTarget", {"targetId": account_target_id, "flatten": True}).get("sessionId") or "")
    if not session:
        return result
    try:
        # Never carry one /lk proof over an entire batch.  Each card must still
        # belong to the authenticated profile after a fresh account-page check.
        pipe.call("Page.navigate", {"url": "https://www.wildberries.ru/lk"}, session=session)
        proof_deadline = min(time.monotonic() + 8, batch_deadline)
        proved = False
        while time.monotonic() < proof_deadline:
            if _evaluate(pipe, session, auth.SURFACE_EXPRESSION) == "account" and pipe.visible_surface() == "account":
                proved = True
                break
            time.sleep(0.5)
        if not proved:
            result.update(status="login_redirect", reason="buyer_login_required", session_status="login_redirect",
                          authenticated_session_proof=False)
            return result
        result.update(session_status="authenticated_surface", authenticated_session_proof=True,
                      session_checked_at=datetime.now(timezone.utc).isoformat())
        pipe.call("Page.navigate", {"url": f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"}, session=session)
    finally:
        pipe.call("Target.detachFromTarget", {"sessionId": session})
    previous: tuple[float, float] | None = None
    stable = 0
    deadline = min(time.monotonic() + 35, batch_deadline)
    while time.monotonic() < deadline:
        target_id = _product_target(pipe, nm_id)
        if target_id:
            session = str(pipe.call("Target.attachToTarget", {"targetId": target_id, "flatten": True}).get("sessionId") or "")
            if session:
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
                        result.update(status="observed", reason="buyer_price_context_not_verified",
                                      wallet_price=wallet, normal_price=nonwallet,
                                      measured_at=datetime.now(timezone.utc).isoformat(), stable=True,
                                      stable_read_count=2, proof="visible_price_detail_wallet_nonwallet")
                        return result
        time.sleep(1)
    return result


def _read_batch_in_runner(nm_ids: list[int], *, on_result: Any = None) -> list[dict[str, Any]]:
    """One headed Chrome, one login proof, then bounded sequential card reads."""
    if os.geteuid() == 0 or not 1 <= len(nm_ids) <= 40 or any(not 0 < nm_id < 1_000_000_000_000 for nm_id in nm_ids):
        return [_unknown(nm_id, "invalid_nm_id") for nm_id in nm_ids]
    env = {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "DISPLAY": auth.DISPLAY,
        "XAUTHORITY": str(runtime.STATE / "price.Xauthority"), "HOME": str(runtime.PROFILE),
        "XDG_CONFIG_HOME": str(runtime.PROFILE), "XDG_CACHE_HOME": str(runtime.PROFILE),
    }
    xvfb = chrome = openbox = None
    pipe = None
    def emit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if on_result is not None:
            for row in rows:
                on_result(row)
        return rows
    try:
        batch_deadline = time.monotonic() + 300
        xauth = runtime.STATE / "price.Xauthority"
        xauth.touch(mode=0o600)
        os.chmod(xauth, 0o600)
        subprocess.run(["xauth", "-f", str(xauth), "add", auth.DISPLAY,
                        "MIT-MAGIC-COOKIE-1", os.urandom(16).hex()], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        xvfb = auth._spawn(["Xvfb", auth.DISPLAY, "-screen", "0", "1600x900x24",
                            "-nolisten", "tcp", "-auth", str(xauth)], "price-xvfb.log", env=env)
        auth._wait_display(xvfb)
        openbox = auth._spawn(["openbox", "--sm-disable"], "price-openbox.log", env=env)
        chrome, pipe = auth._launch_chrome(runtime.CHROME, env, run_id="buyer-price-batch", generation=1, diagnostic=False)
        deadline = min(time.monotonic() + 35, batch_deadline)
        while time.monotonic() < deadline:
            if pipe.visible_surface() == "account":
                break
            time.sleep(1)
        else:
            return emit([dict(_unknown(nm_id, "buyer_login_required"), session_status="login_redirect") for nm_id in nm_ids])
        targets = pipe.call("Target.getTargets").get("targetInfos") or []
        account = next((target for target in targets[:30] if target.get("type") == "page" and
                        urlparse(str(target.get("url") or "")).hostname == "www.wildberries.ru" and
                        urlparse(str(target.get("url") or "")).path.startswith("/lk")), None)
        if not account:
            return emit([_unknown(nm_id, "buyer_login_required") for nm_id in nm_ids])
        account_target_id = str(account.get("targetId") or "")
        results: list[dict[str, Any]] = []
        consecutive_failures = 0
        profile_stat = runtime.PROFILE.stat()
        profile_identity = (profile_stat.st_dev, profile_stat.st_ino)
        for position, nm_id in enumerate(nm_ids):
            current_profile = runtime.PROFILE.stat()
            if (current_profile.st_dev, current_profile.st_ino) != profile_identity:
                rest = [_unknown(item, "buyer_profile_generation_changed") for item in nm_ids[position:]]
                results.extend(emit(rest))
                break
            if time.monotonic() >= batch_deadline:
                rest = [_unknown(item, "authenticated_price_batch_budget_exhausted") for item in nm_ids[position:]]
                results.extend(rest)
                if on_result is not None:
                    for row in rest:
                        on_result(row)
                break
            try:
                row = _read_product_in_batch(pipe, nm_id, account_target_id, batch_deadline=batch_deadline)
            except Exception:
                row = _unknown(nm_id, "authenticated_price_probe_failed")
            results.append(row)
            if on_result is not None:
                on_result(row)
            consecutive_failures = 0 if row.get("status") == "observed" else consecutive_failures + 1
            if row.get("session_status") == "login_redirect" or row.get("status") in {"login_redirect", "security_challenge"}:
                rest = [_unknown(item, "buyer_login_required") for item in nm_ids[position + 1:]]
                results.extend(rest)
                if on_result is not None:
                    for remaining in rest:
                        on_result(remaining)
                break
            if consecutive_failures >= 3:
                rest = [_unknown(item, "authenticated_price_batch_probe_failures") for item in nm_ids[position + 1:]]
                results.extend(emit(rest))
                break
        return results
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
            raise RuntimeError("buyer batch browser cleanup failed")


def _parse_batch_lines(output: str | bytes, nm_ids: list[int]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    requested = set(nm_ids)
    content = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    for line in content.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict) and type(row.get("nm_id")) is int and row["nm_id"] in requested:
            rows.setdefault(row["nm_id"], row)
    return rows


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


def read_prices(nm_ids: list[int]) -> list[dict[str, Any]]:
    """Read a bounded batch while holding the durable profile lock throughout."""
    if not 1 <= len(nm_ids) <= 40 or len(set(nm_ids)) != len(nm_ids) or any(not 0 < item < 1_000_000_000_000 for item in nm_ids):
        raise ValueError("buyer price batch requires 1..40 distinct nm_ids")
    def unavailable(reason: str) -> list[dict[str, Any]]:
        references = current_context_references()
        return [{**_unknown(nm_id, reason), **references} for nm_id in nm_ids]
    if os.geteuid() != 0:
        return unavailable("authenticated_price_probe_failed")
    try:
        with runtime.profile_operation_lock() as guard_fd:
            with auth._shared_start_lock():
                if auth.raw_status().get("running") or legacy.read_recovery_status(legacy.load_recovery_config_from_env(), with_probe=False).get("running"):
                    return unavailable("buyer_session_automation_busy")
                runtime.ensure_runner_idle()
            if runtime._available(Path("/")) < runtime._root_reserve():
                return unavailable("buyer_chrome_storage_reserve")
            runtime.ensure_runtime()
            user = pwd.getpwnam(runtime.USER)
            command = ["setpriv", "--reuid", str(user.pw_uid), "--regid", str(user.pw_gid), "--clear-groups",
                       sys.executable, str(Path(__file__).resolve()), "read-batch", "--nm-ids", ",".join(map(str, nm_ids))]
            output: str | bytes = ""
            child_returncode: int | None = None
            try:
                child = subprocess.Popen(command, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"},
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                         pass_fds=(guard_fd,))
                try:
                    output, _ = child.communicate(timeout=390)
                    child_returncode = child.returncode
                except subprocess.TimeoutExpired:
                    # The child handles SIGTERM by unwinding through its
                    # staged Chrome/X cleanup before its lock is released.
                    child.terminate()
                    try:
                        output, _ = child.communicate(timeout=90)
                        child_returncode = child.returncode
                    except subprocess.TimeoutExpired as error:
                        output = error.stdout or ""
                        child.kill()
                        try:
                            child.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        child_returncode = child.returncode
            except OSError:
                output = ""
            # A failed child may already have measured useful rows.  Preserve
            # those rows and mark only the unmeasured remainder unavailable.
            rows = _parse_batch_lines(output, nm_ids)
            lifecycle = "clean" if child_returncode == 0 else "interrupted" if child_returncode == 75 else "cleanup_failed"
            try:
                runtime.ensure_runner_idle()
            except (OSError, RuntimeError):
                lifecycle = "resource_busy"
            try:
                stat = runtime.PROFILE.stat()
                profile_reference = hashlib.sha256(f"{stat.st_dev}:{stat.st_ino}".encode()).hexdigest()[:24]
            except OSError:
                profile_reference = ""
                lifecycle = "resource_busy"
            status = auth.raw_status()
            auth_run_reference = str(status.get("run_id") or "") if status.get("status") == "completed" and status.get("login_confirmed") else ""
            result = []
            for nm_id in nm_ids:
                row = rows.get(nm_id, _unknown(nm_id, "authenticated_price_batch_incomplete"))
                row.update(profile_reference=profile_reference, auth_run_reference=auth_run_reference,
                           reader_lifecycle_status=lifecycle)
                result.append(row)
            return result
    except BlockingIOError:
        return unavailable("buyer_session_automation_busy")
    except (OSError, RuntimeError, ValueError):
        return unavailable("authenticated_price_probe_failed")


def current_context_references() -> dict[str, str]:
    """Opaque login/profile generation, including early failed reader admission."""
    try:
        status = auth.raw_status()
        auth_run_reference = str(status.get("run_id") or "")[:100]
    except (OSError, RuntimeError, ValueError):
        auth_run_reference = ""
    try:
        stat = runtime.PROFILE.stat()
        profile_reference = hashlib.sha256(f"{stat.st_dev}:{stat.st_ino}".encode()).hexdigest()[:24]
    except OSError:
        profile_reference = ""
    return {"auth_run_reference": auth_run_reference, "profile_reference": profile_reference}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("read", "read-batch"))
    parser.add_argument("--nm-id", type=int)
    parser.add_argument("--nm-ids", default="")
    arguments = parser.parse_args()
    if arguments.command == "read":
        if arguments.nm_id is None:
            parser.error("--nm-id is required for read")
        try:
            result: Any = _read_in_runner(arguments.nm_id)
        except Exception:
            result = _unknown(arguments.nm_id, "authenticated_price_probe_failed")
    else:
        try:
            nm_ids = [int(item) for item in arguments.nm_ids.split(",") if item]
        except ValueError:
            parser.error("--nm-ids must be comma-separated positive integers")
        try:
            def interrupt_batch(_signum: int, _frame: Any) -> None:
                raise BatchInterrupted()
            signal.signal(signal.SIGTERM, interrupt_batch)
            _read_batch_in_runner(nm_ids, on_result=lambda row: print(json.dumps(row, separators=(",", ":")), flush=True))
        except BatchInterrupted:
            raise SystemExit(75)  # Gracefully unwound through staged cleanup.
        except Exception:
            raise SystemExit(1)
        return
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
