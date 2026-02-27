import asyncio
import logging
import os
from datetime import datetime
from typing import List, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import settings
from db import (
    add_proxy,
    add_user,
    count_proxies,
    get_proxies_for_user,
    get_stats,
    init_db,
    is_admin,
    is_whitelisted,
    list_proxies,
    list_users,
    remove_proxy,
    remove_user,
    update_stats,
)
from processing import (
    AccountEntry,
    Scenario,
    ensure_user_directories,
    parse_input_file_content,
    run_batch_for_user,
    write_result_files,
    build_proxy,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


router = Router()


class ProxyStates(StatesGroup):
    adding = State()
    removing = State()


class ProcessStates(StatesGroup):
    waiting_for_file = State()


class AdminStates(StatesGroup):
    adding_user = State()
    removing_user = State()


def main_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="⚙️ Manage Proxies", callback_data="menu_proxies")],
        [InlineKeyboardButton(text="📂 Process Accounts", callback_data="menu_process")],
        [InlineKeyboardButton(text="📊 View Stats", callback_data="menu_stats")],
    ]
    if is_admin:
        buttons.append(
            [InlineKeyboardButton(text="🛡 Admin Panel", callback_data="menu_admin")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def proxies_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add Proxies", callback_data="proxy_add")],
            [InlineKeyboardButton(text="📃 List Proxies", callback_data="proxy_list")],
            [InlineKeyboardButton(text="🗑 Remove Proxy", callback_data="proxy_remove")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="back_main")],
        ]
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📃 List Users", callback_data="admin_list")],
            [InlineKeyboardButton(text="➕ Add User", callback_data="admin_add")],
            [InlineKeyboardButton(text="🗑 Remove User", callback_data="admin_remove")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="back_main")],
        ]
    )


async def ensure_authorized(message: Message, user_id: int | None = None) -> bool:
    if user_id is None:
        user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return False
    # Always trust the configured main admin ID
    if user_id == settings.main_admin_id:
        return True
    if await is_admin(user_id):
        return True
    if not await is_whitelisted(user_id):
        await message.answer(
            "⛔ You are not authorized to use this bot.\n"
            "Please contact the main admin for access."
        )
        return False
    return True


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    if user_id == settings.main_admin_id:
        await add_user(user_id, admin=True)

    if not await is_whitelisted(user_id):
        if user_id == settings.main_admin_id:
            await message.answer(
                "👋 Main admin recognized.\n\n"
                "Tap **🛡 Admin Panel** below to manage whitelisted users.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb(is_admin=True),
            )
        else:
            await message.answer(
                "⛔ You are not on the whitelist.\n"
                "Ask the main admin to add you to the bot."
            )
        return

    is_admin_flag = await is_admin(user_id)
    await message.answer(
        "🤖 <b>Telegram 2FA Reset Bot</b> is ready!\n\n"
        "Use the buttons below to:\n"
        "• ⚙️ Manage your SOCKS5 proxies\n"
        "• 📂 Process account lists\n"
        "• 📊 View your statistics",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(is_admin=is_admin_flag),
    )


@router.message(Command("add_user"))
async def cmd_add_user(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not user_id or not await is_admin(user_id):
        await message.answer("Only the main admin can add users.")
        return

    if not command.args:
        await message.answer("Usage: /add_user <telegram_user_id>")
        return

    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("User ID must be a numeric Telegram user ID.")
        return

    await add_user(target_id, admin=False)
    await message.answer(f"User {target_id} has been whitelisted.")


@router.message(Command("remove_user"))
async def cmd_remove_user(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not user_id or not await is_admin(user_id):
        await message.answer("Only the main admin can remove users.")
        return

    if not command.args:
        await message.answer("Usage: /remove_user <telegram_user_id>")
        return

    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("User ID must be a numeric Telegram user ID.")
        return

    if target_id == settings.main_admin_id:
        await message.answer("You cannot remove the main admin.")
        return

    await remove_user(target_id)
    await message.answer(f"User {target_id} has been removed from whitelist.")


@router.callback_query(F.data == "admin_list")
async def cb_admin_list(callback: CallbackQuery) -> None:
    if not callback.message or not callback.from_user:
        return
    user_id = callback.from_user.id
    if not await is_admin(user_id):
        await callback.answer("Only the main admin can use this panel.", show_alert=True)
        return

    await callback.answer()
    users = await list_users()
    if not users:
        await callback.message.answer("ℹ️ No users found.")
        return

    lines = []
    for u in users:
        line = f"{u['user_id']}: "
        flags = []
        if u["is_admin"]:
            flags.append("admin")
        if u["is_whitelisted"]:
            flags.append("whitelisted")
        line += ", ".join(flags) if flags else "no flags"
        lines.append(line)

    await callback.message.answer("📃 <b>Users</b>:\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "admin_add")
async def cb_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or not callback.from_user:
        return
    user_id = callback.from_user.id
    if not await is_admin(user_id):
        await callback.answer("Only the main admin can use this panel.", show_alert=True)
        return

    await state.set_state(AdminStates.adding_user)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Add user</b>\n\n"
        "Send the Telegram <b>numeric</b> user ID you want to whitelist.",
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "admin_remove")
async def cb_admin_remove(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or not callback.from_user:
        return
    user_id = callback.from_user.id
    if not await is_admin(user_id):
        await callback.answer("Only the main admin can use this panel.", show_alert=True)
        return

    await state.set_state(AdminStates.removing_user)
    await callback.answer()
    await callback.message.answer(
        "🗑 <b>Remove user</b>\n\n"
        "Send the Telegram <b>numeric</b> user ID you want to remove from the whitelist.",
        parse_mode=ParseMode.HTML,
    )


@router.message(AdminStates.adding_user)
async def handle_admin_add_user(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("⛔ Only the main admin can add users.")
        return

    if not message.text:
        await message.answer("⚠️ Please send a numeric Telegram user ID.")
        return

    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ User ID must be a numeric Telegram user ID.")
        return

    await add_user(target_id, admin=False)
    await state.clear()
    await message.answer(f"✅ User <code>{target_id}</code> has been whitelisted.", parse_mode=ParseMode.HTML, reply_markup=admin_menu_kb())


@router.message(AdminStates.removing_user)
async def handle_admin_remove_user(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("⛔ Only the main admin can remove users.")
        return

    if not message.text:
        await message.answer("⚠️ Please send a numeric Telegram user ID.")
        return

    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ User ID must be a numeric Telegram user ID.")
        return

    if target_id == settings.main_admin_id:
        await message.answer("⛔ You cannot remove the main admin.")
        return

    await remove_user(target_id)
    await state.clear()
    await message.answer(f"✅ User <code>{target_id}</code> has been removed from whitelist.", parse_mode=ParseMode.HTML, reply_markup=admin_menu_kb())


@router.message(Command("list_users"))
async def cmd_list_users(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not user_id or not await is_admin(user_id):
        await message.answer("Only the main admin can view user list.")
        return

    users = await list_users()
    if not users:
        await message.answer("No users found.")
        return

    lines = []
    for u in users:
        line = f"{u['user_id']}: "
        flags = []
        if u["is_admin"]:
            flags.append("admin")
        if u["is_whitelisted"]:
            flags.append("whitelisted")
        line += ", ".join(flags) if flags else "no flags"
        lines.append(line)

    await message.answer("Users:\n" + "\n".join(lines))


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery) -> None:
    if not callback.message or not callback.from_user:
        return
    user_id = callback.from_user.id
    is_admin_flag = await is_admin(user_id)
    await callback.answer()
    await callback.message.answer(
        "🏠 Back to main menu:",
        reply_markup=main_menu_kb(is_admin=is_admin_flag),
    )


@router.callback_query(F.data == "menu_proxies")
async def cb_menu_proxies(callback: CallbackQuery) -> None:
    if not callback.message:
        return
    if not await ensure_authorized(callback.message, user_id=callback.from_user.id if callback.from_user else None):
        return
    await callback.answer()
    await callback.message.answer(
        "⚙️ <b>Proxy management</b>\n\n"
        "Use the buttons below to add, list, or remove your SOCKS5 proxies.",
        parse_mode=ParseMode.HTML,
        reply_markup=proxies_menu_kb(),
    )


@router.callback_query(F.data == "menu_stats")
async def cb_menu_stats(callback: CallbackQuery) -> None:
    if not callback.message:
        return
    if not await ensure_authorized(callback.message, user_id=callback.from_user.id if callback.from_user else None):
        return

    await callback.answer()
    message = callback.message
    s = await get_stats(message.from_user.id)
    if not s:
        await message.answer("ℹ️ No statistics available yet.")
        return

    text = (
        f"Total accounts processed: {s['total_processed']}\n"
        f"- No 2FA: {s['no_2fa']}\n"
        f"- Reset success: {s['reset_success']}\n"
        f"- Reset timer: {s['reset_timer']}\n"
        f"- Reset failed: {s['reset_failed']}\n"
        f"- Email required: {s['email_required']}"
    )
    await message.answer("📊 <b>Your stats</b>:\n\n" + text, parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "menu_admin")
async def cb_menu_admin(callback: CallbackQuery) -> None:
    if not callback.message or not callback.from_user:
        return
    user_id = callback.from_user.id
    if not await is_admin(user_id):
        await callback.answer("Only the main admin can use this panel.", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        "🛡 <b>Admin panel</b>\n\nChoose an action:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_kb(),
    )


@router.message(Command("proxies"))
async def cmd_proxies(message: Message) -> None:
    if not await ensure_authorized(message):
        return

    text = (
        "⚙️ <b>Proxy management</b>\n\n"
        "Use the inline <b>Manage Proxies</b> menu instead of commands.\n"
        "Tap the buttons below to add, list, or remove proxies."
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=proxies_menu_kb())


@router.callback_query(F.data == "proxy_add")
async def cb_proxy_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    if not await ensure_authorized(callback.message, user_id=callback.from_user.id if callback.from_user else None):
        return

    await state.set_state(ProxyStates.adding)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Add SOCKS5 proxies</b>\n\n"
        "Send your proxies in the format:\n"
        "`host:port` or `host:port:username:password`.\n"
        "You can send multiple proxies separated by spaces or new lines.",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.callback_query(F.data == "proxy_list")
async def cb_proxy_list(callback: CallbackQuery) -> None:
    if not callback.message:
        return
    if not await ensure_authorized(callback.message, user_id=callback.from_user.id if callback.from_user else None):
        return

    await callback.answer()
    message = callback.message
    proxies = await list_proxies(message.from_user.id)
    if not proxies:
        await message.answer("⚠️ You have no proxies configured.")
        return

    lines = []
    for p in proxies:
        auth = ""
        if p["username"]:
            auth = f"{p['username']}:***@"
        lines.append(f"{p['id']}: {auth}{p['host']}:{p['port']}")

    await message.answer("📃 <b>Your proxies</b>:\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "proxy_remove")
async def cb_proxy_remove(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    if not await ensure_authorized(callback.message, user_id=callback.from_user.id if callback.from_user else None):
        return

    await state.set_state(ProxyStates.removing)
    await callback.answer()
    await callback.message.answer(
        "🗑 <b>Remove proxy</b>\n\n"
        "Send the proxy ID to remove (you can send multiple IDs separated by spaces or new lines).",
        parse_mode=ParseMode.HTML,
    )


def parse_proxy_string(raw: str) -> Tuple[str, int, str | None, str | None]:
    parts = raw.strip().split(":")
    if len(parts) not in (2, 4):
        raise ValueError("Proxy must be host:port or host:port:username:password")
    host = parts[0]
    try:
        port = int(parts[1])
    except ValueError:
        raise ValueError("Port must be an integer") from None
    username = parts[2] if len(parts) == 4 else None
    password = parts[3] if len(parts) == 4 else None
    return host, port, username, password


@router.message(ProxyStates.adding)
async def handle_add_proxy_text(message: Message, state: FSMContext) -> None:
    if not await ensure_authorized(message):
        return

    if not message.text:
        await message.answer("⚠️ Please send proxies as text.")
        return

    raw = message.text.replace("\n", " ").strip()
    parts = [p for p in raw.split(" ") if p.strip()]

    added = 0
    errors: List[str] = []
    for p in parts:
        try:
            host, port, username, password = parse_proxy_string(p)
            await add_proxy(message.from_user.id, host, port, username, password)
            added += 1
        except Exception as e:
            errors.append(f"{p} -> {e}")

    await state.clear()

    reply = f"Added {added} proxies."
    if errors:
        reply += "\n\n❌ Failed entries:\n" + "\n".join(errors)

    await message.answer("✅ " + reply, reply_markup=proxies_menu_kb())


@router.message(ProxyStates.removing)
async def handle_remove_proxy_text(message: Message, state: FSMContext) -> None:
    if not await ensure_authorized(message):
        return

    if not message.text:
        await message.answer("⚠️ Please send one or more proxy IDs.")
        return

    raw = message.text.replace("\n", " ").strip()
    parts = [p for p in raw.split(" ") if p.strip()]

    removed = 0
    errors: List[str] = []
    for p in parts:
        try:
            proxy_id = int(p)
            await remove_proxy(message.from_user.id, proxy_id)
            removed += 1
        except ValueError:
            errors.append(f"{p} -> not an integer ID")

    await state.clear()

    reply = f"Requested removal of {removed} proxies."
    if errors:
        reply += "\n\n⏭ Skipped entries:\n" + "\n".join(errors)

    await message.answer("✅ " + reply, reply_markup=proxies_menu_kb())


@router.message(Command("add_proxy"))
async def cmd_add_proxy(message: Message, command: CommandObject) -> None:
    if not await ensure_authorized(message):
        return

    if not command.args:
        await message.answer(
            "Usage: /add_proxy host:port or host:port:username:password\n"
            "You can also add multiple proxies separated by spaces or new lines."
        )
        return

    raw = command.args.replace("\n", " ").strip()
    parts = [p for p in raw.split(" ") if p.strip()]

    added = 0
    errors: List[str] = []
    for p in parts:
        try:
            host, port, username, password = parse_proxy_string(p)
            await add_proxy(message.from_user.id, host, port, username, password)
            added += 1
        except Exception as e:
            errors.append(f"{p} -> {e}")

    reply = f"Added {added} proxies."
    if errors:
        reply += "\nFailed entries:\n" + "\n".join(errors)

    await message.answer(reply)


@router.message(Command("remove_proxy"))
async def cmd_remove_proxy(message: Message, command: CommandObject) -> None:
    if not await ensure_authorized(message):
        return

    if not command.args:
        await message.answer("Usage: /remove_proxy <proxy_id>")
        return

    try:
        proxy_id = int(command.args.strip())
    except ValueError:
        await message.answer("Proxy ID must be an integer.")
        return

    await remove_proxy(message.from_user.id, proxy_id)
    await message.answer(f"Proxy {proxy_id} removed (if it existed).")


@router.message(Command("list_proxies"))
async def cmd_list_proxies(message: Message) -> None:
    if not await ensure_authorized(message):
        return

    proxies = await list_proxies(message.from_user.id)
    if not proxies:
        await message.answer("You have no proxies configured.")
        return

    lines = []
    for p in proxies:
        auth = ""
        if p["username"]:
            auth = f"{p['username']}:***@"
        lines.append(f"{p['id']}: {auth}{p['host']}:{p['port']}")

    await message.answer("Your proxies:\n" + "\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not await ensure_authorized(message):
        return

    s = await get_stats(message.from_user.id)
    if not s:
        await message.answer("ℹ️ No statistics available yet.")
        return

    text = (
        f"Total accounts processed: {s['total_processed']}\n"
        f"- No 2FA: {s['no_2fa']}\n"
        f"- Reset success: {s['reset_success']}\n"
        f"- Reset timer: {s['reset_timer']}\n"
        f"- Reset failed: {s['reset_failed']}\n"
        f"- Email required: {s['email_required']}"
    )
    await message.answer("📊 <b>Your stats</b>:\n\n" + text, parse_mode=ParseMode.HTML)


async def _process_accounts_from_document(message: Message, doc: Document) -> None:
    if not await ensure_authorized(message):
        return

    proxies_cnt = await count_proxies(message.from_user.id)
    if proxies_cnt < 2:
        await message.answer(
            f"⚠️ You have {proxies_cnt} proxies configured.\n"
            "At least 2 SOCKS5 proxies are required before processing."
        )
        return

    if not doc.file_name.lower().endswith(".txt"):
        await message.answer("⚠️ Only .txt files are supported.")
        return

    await message.answer(
        "⏳ Downloading file and starting processing...\n"
        "You will receive a summary and result files when done ✅"
    )

    # Download file to user-specific directory
    user_dir = ensure_user_directories(message.from_user.id)
    os.makedirs(user_dir, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    input_path = os.path.join(user_dir, f"input_{timestamp}.txt")

    bot = message.bot
    file = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file.file_path)

    with open(input_path, "wb") as f:
        f.write(file_bytes.read())

    with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    entries = parse_input_file_content(content)
    if not entries:
        await message.answer("⚠️ The file contained no valid `phone----url` lines.")
        return

    proxies_db = await get_proxies_for_user(message.from_user.id)
    proxy_tuples: List[Tuple[int, str, int, bool, str | None, str | None]] = [
        build_proxy(host=p[1], port=p[2], username=p[3], password=p[4]) for p in proxies_db
    ]

    # Limit concurrency to number of proxies actually available
    if not proxy_tuples:
        await message.answer("⚠️ You have no valid proxies configured.")
        return

    chat_id = message.chat.id
    batch_id = timestamp

    async def run_and_report() -> None:
        try:
            results = await run_batch_for_user(
                user_id=message.from_user.id,
                entries=entries,
                proxies=proxy_tuples,
            )

            files = write_result_files(
                user_id=message.from_user.id,
                batch_id=batch_id,
                results=results,
            )

            total = len(entries)
            no_2fa = len(results.no_2fa)
            reset_success = len(results.reset_success)
            reset_timer = len(results.reset_timer)
            reset_failed = len(results.reset_failed)
            email_required = len(results.email_required)

            await update_stats(
                user_id=message.from_user.id,
                total_delta=total,
                no_2fa=no_2fa,
                reset_success=reset_success,
                reset_timer=reset_timer,
                reset_failed=reset_failed,
                email_required=email_required,
            )

            summary = (
                f"Batch completed.\n"
                f"Total accounts processed: {total}\n"
                f"- No 2FA: {no_2fa}\n"
                f"- Reset success: {reset_success}\n"
                f"- Reset timer: {reset_timer}\n"
                f"- Reset failed: {reset_failed}\n"
                f"- Email required: {email_required}"
            )

            await bot.send_message(chat_id, summary)

            # Send result files as documents
            for key, path in files.items():
                if os.path.exists(path):
                    await bot.send_document(
                        chat_id,
                        document=open(path, "rb"),
                        caption=f"{key}.txt",
                    )
        except Exception as e:
            logger.exception("Error during batch processing: %s", e)
            await bot.send_message(
                chat_id,
                "An unexpected error occurred during processing. "
                "Some accounts may not have been processed.",
            )

    asyncio.create_task(run_and_report())


@router.message(Command("process"))
async def cmd_process(message: Message) -> None:
    if not message.document:
        await message.answer(
            "📂 Please attach a .txt file containing lines in the form:\n"
            "`+phone----sms_api_url`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await _process_accounts_from_document(message, message.document)


@router.callback_query(F.data == "menu_process")
async def cb_menu_process(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    if not await ensure_authorized(callback.message, user_id=callback.from_user.id if callback.from_user else None):
        return

    proxies_cnt = await count_proxies(callback.from_user.id)
    if proxies_cnt < 2:
        await callback.answer()
        await callback.message.answer(
            f"⚠️ You have {proxies_cnt} proxies configured.\n"
            "At least 2 SOCKS5 proxies are required before processing."
        )
        return

    await state.set_state(ProcessStates.waiting_for_file)
    await callback.answer()
    await callback.message.answer(
        "📂 <b>Process accounts</b>\n\n"
        "Send a .txt file containing lines in the form:\n"
        "`+phone----sms_api_url`.\n"
        "I will start processing as soon as I receive the file ✅",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(StateFilter(ProcessStates.waiting_for_file), F.document)
async def handle_process_document(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _process_accounts_from_document(message, message.document)


@router.message(StateFilter(ProcessStates.waiting_for_file))
async def handle_process_non_document(message: Message) -> None:
    await message.answer(
        "📂 Please send a .txt file containing lines in the form:\n"
        "`+phone----sms_api_url`.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def main() -> None:
    os.makedirs(settings.user_data_dir, exist_ok=True)
    os.makedirs(settings.logs_dir, exist_ok=True)

    await init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

