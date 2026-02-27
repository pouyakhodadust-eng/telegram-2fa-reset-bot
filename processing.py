import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import socks
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import StringSession

from config import settings


logger = logging.getLogger(__name__)


# Prefer codes that appear after the "Telegram code:" label,
# but fall back to any 5‑digit sequence if needed.
TELEGRAM_CODE_REGEX = re.compile(r"Telegram code:\s*(\d{5})")
GENERIC_CODE_REGEX = re.compile(r"\b(\d{5})\b")


@dataclass
class AccountEntry:
    phone: str
    sms_url: str


@dataclass
class BatchResult:
    no_2fa: List[AccountEntry] = field(default_factory=list)
    reset_success: List[AccountEntry] = field(default_factory=list)
    reset_timer: List[AccountEntry] = field(default_factory=list)
    reset_failed: List[AccountEntry] = field(default_factory=list)
    email_required: List[AccountEntry] = field(default_factory=list)


class Scenario:
    NO_2FA = "no_2fa"
    RESET_SUCCESS = "reset_success"
    RESET_TIMER = "reset_timer"
    RESET_FAILED = "reset_failed"
    EMAIL_REQUIRED = "email_required"


def parse_input_file_content(content: str) -> List[AccountEntry]:
    entries: List[AccountEntry] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "----" not in line:
            logger.warning("Skipping malformed line (missing ----): %s", line)
            continue
        phone, url = line.split("----", 1)
        phone = phone.strip()
        url = url.strip()
        if not phone or not url:
            logger.warning("Skipping malformed line (empty phone or URL): %s", line)
            continue
        entries.append(AccountEntry(phone=phone, sms_url=url))
    return entries


def _extract_code_from_text(raw_text: str) -> Optional[str]:
    text = raw_text.strip()

    # Try JSON format first
    try:
        data = json.loads(text)
        content = (
            data.get("data", {})
            .get("fields", {})
            .get("content", "")
        )
        if content:
            text = content
    except json.JSONDecodeError:
        pass

    # 1) Prefer codes explicitly labeled as "Telegram code: 12345"
    codes = TELEGRAM_CODE_REGEX.findall(text)
    # 2) Otherwise, fall back to any 5‑digit sequence in the text
    if not codes:
        codes = GENERIC_CODE_REGEX.findall(text)

    if not codes:
        return None

    return codes[-1]


async def fetch_sms_code(
    sms_url: str,
    max_attempts: int = 12,
    poll_interval: float = 5.0,
    timeout: float = 15.0,
) -> str:
    """
    Poll the SMS API up to `max_attempts` times (default 12 x 5s = 60s)
    waiting for a verification code to appear.
    """
    last_raw = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, max_attempts + 1):
            resp = await client.get(sms_url)
            resp.raise_for_status()
            last_raw = resp.text.strip()

            code = _extract_code_from_text(last_raw)
            if code:
                logger.info("SMS code extracted on attempt %d/%d", attempt, max_attempts)
                return code

            logger.debug(
                "Attempt %d/%d: no code yet, raw response: %s",
                attempt, max_attempts, last_raw[:200],
            )

            if attempt < max_attempts:
                await asyncio.sleep(poll_interval)

    raise ValueError(
        f"Could not extract verification code after {max_attempts} attempts. "
        f"Last response: {last_raw[:200]}"
    )


def build_proxy(
    host: str, port: int, username: Optional[str], password: Optional[str]
) -> Tuple[int, str, int, bool, Optional[str], Optional[str]]:
    # Telethon expects (proxy_type, addr, port, rdns, username, password)
    return (
        socks.SOCKS5,
        host,
        int(port),
        True,
        username,
        password,
    )


async def process_account_on_proxy(
    api_id: int,
    api_hash: str,
    proxy: Tuple[int, str, int, bool, Optional[str], Optional[str]],
    account: AccountEntry,
) -> Tuple[str, Optional[int]]:
    """
    Process a single account on a single proxy.

    Returns (scenario, wait_seconds_or_none).
    """
    phone = account.phone
    masked_phone = phone[:-4] + "****" if len(phone) > 4 else phone

    client = TelegramClient(StringSession(), api_id, api_hash, proxy=proxy)
    try:
        await client.connect()

        # Step 1: Send login code request (via proxy).
        # force_sms=True makes Telethon first send the normal request,
        # then resend it to force SMS delivery instead of in-app code.
        await client.send_code_request(phone, force_sms=True)

        # Step 2–3: Fetch SMS verification code from SMS API (no proxy)
        code = await fetch_sms_code(account.sms_url)

        # Step 4: Log into the account using the extracted code via SOCKS5 proxy
        try:
            await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            # 2FA required, attempt reset
            try:
                result = await client(functions.account.ResetPasswordRequest())
            except errors.RPCError as e:
                # Heuristic: if error mentions email, treat as email-required
                msg = (e.message or "").upper()
                if "EMAIL" in msg:
                    logger.info(
                        "Account %s: 2FA reset requires email (%s)", masked_phone, e
                    )
                    return Scenario.EMAIL_REQUIRED, None

                logger.warning(
                    "Account %s: 2FA reset failed with RPC error: %s",
                    masked_phone,
                    e,
                )
                return Scenario.RESET_FAILED, None

            if isinstance(result, types.account.ResetPasswordOk):
                logger.info("Account %s: 2FA reset succeeded immediately", masked_phone)
                return Scenario.RESET_SUCCESS, None
            if isinstance(result, types.account.ResetPasswordRequestedWait):
                wait_seconds = max(
                    0, int(result.until_date - datetime.now(timezone.utc).timestamp())
                )
                logger.info(
                    "Account %s: 2FA reset requested, waiting period %s seconds",
                    masked_phone,
                    wait_seconds,
                )
                return Scenario.RESET_TIMER, wait_seconds
            if isinstance(result, types.account.ResetPasswordFailedWait):
                wait_seconds = max(
                    0, int(result.until_date - datetime.now(timezone.utc).timestamp())
                )
                logger.info(
                    "Account %s: 2FA reset failed, must wait %s seconds",
                    masked_phone,
                    wait_seconds,
                )
                return Scenario.RESET_FAILED, wait_seconds

            logger.warning(
                "Account %s: 2FA reset returned unexpected result: %r",
                masked_phone,
                result,
            )
            return Scenario.RESET_FAILED, None

        # If sign-in succeeded without password, no 2FA
        if await client.is_user_authorized():
            logger.info("Account %s: login successful, no 2FA", masked_phone)
            try:
                await client.log_out()
            except Exception:
                # Non-critical
                pass
            return Scenario.NO_2FA, None

        logger.warning(
            "Account %s: sign-in did not complete as expected, treating as failed",
            masked_phone,
        )
        return Scenario.RESET_FAILED, None

    except Exception as e:
        logger.exception("Account %s: unexpected error during processing: %s", masked_phone, e)
        return Scenario.RESET_FAILED, None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def worker(
    api_id: int,
    api_hash: str,
    proxy_tuple: Tuple[int, str, int, bool, Optional[str], Optional[str]],
    queue: "asyncio.Queue[AccountEntry]",
    results: BatchResult,
    lock: asyncio.Lock,
) -> None:
    while True:
        account: AccountEntry = await queue.get()
        try:
            scenario, _wait = await process_account_on_proxy(
                api_id=api_id,
                api_hash=api_hash,
                proxy=proxy_tuple,
                account=account,
            )
            async with lock:
                if scenario == Scenario.NO_2FA:
                    results.no_2fa.append(account)
                elif scenario == Scenario.RESET_SUCCESS:
                    results.reset_success.append(account)
                elif scenario == Scenario.RESET_TIMER:
                    results.reset_timer.append(account)
                elif scenario == Scenario.EMAIL_REQUIRED:
                    results.email_required.append(account)
                else:
                    results.reset_failed.append(account)
        finally:
            queue.task_done()


async def run_batch_for_user(
    user_id: int,
    entries: List[AccountEntry],
    proxies: List[Tuple[int, str, int, bool, Optional[str], Optional[str]]],
) -> BatchResult:
    results = BatchResult()
    queue: asyncio.Queue[AccountEntry] = asyncio.Queue()
    for entry in entries:
        queue.put_nowait(entry)

    lock = asyncio.Lock()
    tasks: List[asyncio.Task] = []
    for proxy in proxies:
        task = asyncio.create_task(
            worker(
                api_id=settings.api_id,
                api_hash=settings.api_hash,
                proxy_tuple=proxy,
                queue=queue,
                results=results,
                lock=lock,
            )
        )
        tasks.append(task)

    await queue.join()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    return results


def ensure_user_directories(user_id: int) -> str:
    base_dir = os.path.join(settings.user_data_dir, str(user_id))
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


def write_result_files(
    user_id: int,
    batch_id: str,
    results: BatchResult,
) -> Dict[str, str]:
    base_dir = ensure_user_directories(user_id)
    batch_dir = os.path.join(base_dir, batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    def _write(name: str, items: List[AccountEntry]) -> str:
        path = os.path.join(batch_dir, f"{name}.txt")
        with open(path, "w", encoding="utf-8") as f:
            for acc in items:
                f.write(f"{acc.phone}----{acc.sms_url}\n")
        return path

    files = {
        "no_2fa": _write("no_2fa", results.no_2fa),
        "reset_success": _write("reset_success", results.reset_success),
        "reset_timer": _write("reset_timer", results.reset_timer),
        "reset_failed": _write("reset_failed", results.reset_failed),
        "email_required": _write("email_required", results.email_required),
    }
    return files

