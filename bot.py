import asyncio
import logging
import os
import html
from datetime import datetime, timedelta
import pytz
import aiosqlite
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN = "8908372460:AAG8tNdwASQwspMTFwEs09ERRfQkpFfBlnM"
DB_NAME = "reputation_bot.db"

# ID главного разработчика
DEV_ID = 5962570763
# Пользователи с начальным бейджем верификации
VERIFIED_USERS = [5962570763, 8829405095]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ==================== БАЗА ДАННЫХ ====================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                description TEXT DEFAULT 'Описание не установлено',
                voices INTEGER DEFAULT 1,
                super_voices INTEGER DEFAULT 1,
                badges TEXT DEFAULT '',
                is_unlimited INTEGER DEFAULT 0,
                last_vote_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reputation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER,
                to_user_id INTEGER,
                value INTEGER,
                is_super INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

        # Выдаем дефолтные бейджи верификации для указанных ID
        for uid in VERIFIED_USERS:
            async with db.execute("SELECT badges FROM users WHERE user_id = ?", (uid,)) as cursor:
                row = await cursor.fetchone()
                if row and not row[0]:
                    await db.execute("UPDATE users SET badges = '☑️' WHERE user_id = ?", (uid,))
        await db.commit()


async def get_or_create_user(user: types.User):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user.id,)) as cursor:
            row = await cursor.fetchone()
            username = user.username.lower() if user.username else None
            
            if not row:
                default_badge = "☑️" if user.id in VERIFIED_USERS else ""
                await db.execute(
                    "INSERT INTO users (user_id, username, full_name, voices, super_voices, badges) VALUES (?, ?, ?, 1, 1, ?)",
                    (user.id, username, user.full_name, default_badge)
                )
                await db.commit()
                return False  # Новый пользователь
            else:
                await db.execute(
                    "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                    (username, user.full_name, user.id)
                )
                await db.commit()
                return True   # Уже зарегистрирован


async def get_user_by_id(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()


async def get_user_by_username(username: str):
    clean_username = username.lstrip("@").lower()
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE username = ?", (clean_username,)) as cursor:
            return await cursor.fetchone()


async def get_reputation_stats(user_id: int):
    now = datetime.now(pytz.utc)
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        # Обычный рейтинг (только is_super = 0)
        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ? AND is_super = 0", (user_id,)) as c:
            total = (await c.fetchone())[0]

        # Суперрейтинг (только is_super = 1)
        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ? AND is_super = 1", (user_id,)) as c:
            super_total = (await c.fetchone())[0]

        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ? AND created_at >= ?", (user_id, day_ago)) as c:
            daily = (await c.fetchone())[0]

        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ? AND created_at >= ?", (user_id, week_ago)) as c:
            weekly = (await c.fetchone())[0]

        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ? AND created_at >= ?", (user_id, month_ago)) as c:
            monthly = (await c.fetchone())[0]

    return total, super_total, daily, weekly, monthly


async def get_votes_today(from_id: int, to_id: int) -> int:
    now = datetime.now(pytz.utc)
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=pytz.utc).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM reputation_logs WHERE from_user_id = ? AND to_user_id = ? AND created_at >= ?",
            (from_id, to_id, start_of_day)
        ) as c:
            res = await c.fetchone()
            return res[0] if res else 0


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def format_user_info(user_row, total, super_total, daily, weekly, monthly) -> str:
    user_id = user_row["user_id"]
    name = html.escape(user_row["full_name"])
    user_link = f'<a href="tg://user?id={user_id}">{name}</a>'
    desc = html.escape(user_row["description"])
    
    badges = user_row["badges"] if user_row["badges"] else ""
    badges_str = f" {badges}" if badges else ""

    return (
        f"информация о {user_link}{badges_str}\n\n"
        f"рейтинг: {total}\n"
        f"суперрейтинг: {super_total}\n"
        f"рейтинг за день {daily}/за неделю {weekly}/за месяц {monthly}\n\n"
        f"{desc}"
    )


async def resolve_target_user(message: types.Message, command: CommandObject):
    if message.reply_to_message:
        target_tg_user = message.reply_to_message.from_user
        await get_or_create_user(target_tg_user)
        return await get_user_by_id(target_tg_user.id)
    
    if command and command.args:
        arg = command.args.split()[0]
        if arg.startswith("@"):
            user_row = await get_user_by_username(arg)
            if not user_row:
                await message.reply("Пользователь не найден в базе данных бота. Ему нужно сначала написать боту /start.")
                return None
            return user_row

    await message.reply("Ответьте на сообщение пользователя или укажите его @username.")
    return None


# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    is_exists = await get_or_create_user(message.from_user)
    if not is_exists:
        await message.reply("Привет! Ты зарегистрирован в системе рейтинга. Тебе выдан 1 голос!")
    else:
        await message.reply("добро пожаловать")


@dp.message(Command("ownersss"))
async def cmd_ownersss(message: types.Message):
    await message.reply("владелец - @aviff, совладелец - @MR3anoi")


@dp.message(Command("aboutme"))
async def cmd_aboutme(message: types.Message, command: CommandObject):
    await get_or_create_user(message.from_user)
    if not command.args:
        await message.reply("Укажите текст описания. Пример: /aboutme Привет, я разработчик!")
        return
    
    new_desc = command.args.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET description = ? WHERE user_id = ?", (new_desc, message.from_user.id))
        await db.commit()
    
    await message.reply("Ваше описание успешно обновлено!")


@dp.message(Command("myinfo"))
async def cmd_myinfo(message: types.Message):
    await get_or_create_user(message.from_user)
    user_row = await get_user_by_id(message.from_user.id)
    total, super_total, daily, weekly, monthly = await get_reputation_stats(message.from_user.id)
    text = format_user_info(user_row, total, super_total, daily, weekly, monthly)
    await message.reply(text, parse_mode=ParseMode.HTML)


@dp.message(Command("information"))
async def cmd_information(message: types.Message, command: CommandObject):
    await get_or_create_user(message.from_user)
    target_user = await resolve_target_user(message, command)
    if not target_user:
        return
    
    total, super_total, daily, weekly, monthly = await get_reputation_stats(target_user["user_id"])
    text = format_user_info(target_user, total, super_total, daily, weekly, monthly)
    await message.reply(text, parse_mode=ParseMode.HTML)


@dp.message(Command("myvoice"))
async def cmd_myvoice(message: types.Message):
    await get_or_create_user(message.from_user)
    user_row = await get_user_by_id(message.from_user.id)
    await message.reply(f"Остаток голосов: {user_row['voices']} | Супер-голосов: {user_row['super_voices']}")


@dp.message(Command("sendvoice"))
async def cmd_sendvoice(message: types.Message, command: CommandObject):
    await get_or_create_user(message.from_user)
    sender = await get_user_by_id(message.from_user.id)
    
    if sender["voices"] < 1 and not sender["is_unlimited"]:
        await message.reply("У вас нет доступных голосов для передачи.")
        return

    target = await resolve_target_user(message, command)
    if not target:
        return

    if target["user_id"] == sender["user_id"]:
        await message.reply("Нельзя передавать голоса самому себе.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        if not sender["is_unlimited"]:
            await db.execute("UPDATE users SET voices = voices - 1 WHERE user_id = ?", (sender["user_id"],))
        await db.execute("UPDATE users SET voices = voices + 1 WHERE user_id = ?", (target["user_id"],))
        await db.commit()

    target_name = html.escape(target["full_name"])
    await message.reply(f"Вы успешно передали 1 голос пользователю {target_name}!", parse_mode=ParseMode.HTML)


async def process_rep(message: types.Message, command: CommandObject, delta: int, is_super: bool = False):
    await get_or_create_user(message.from_user)
    voter = await get_user_by_id(message.from_user.id)

    if is_super:
        if voter["super_voices"] < 1 and not voter["is_unlimited"]:
            await message.reply("У вас нет доступных супер-голосов.")
            return
    else:
        if voter["voices"] < 1 and not voter["is_unlimited"]:
            await message.reply("У вас недостаточно голосов для голосования.")
            return

    target = await resolve_target_user(message, command)
    if not target:
        return

    if target["user_id"] == voter["user_id"]:
        await message.reply("Нельзя голосовать за самого себя!")
        return

    if not voter["is_unlimited"]:
        votes_today = await get_votes_today(voter["user_id"], target["user_id"])
        if votes_today >= 3:
            await message.reply("Нельзя голосовать за одного и того же человека больше 3 раз в день.")
            return

    now_str = datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        if not voter["is_unlimited"]:
            if is_super:
                await db.execute("UPDATE users SET super_voices = super_voices - 1, last_vote_at = ? WHERE user_id = ?", (now_str, voter["user_id"]))
            else:
                await db.execute("UPDATE users SET voices = voices - 1, last_vote_at = ? WHERE user_id = ?", (now_str, voter["user_id"]))
        
        await db.execute(
            "INSERT INTO reputation_logs (from_user_id, to_user_id, value, is_super, created_at) VALUES (?, ?, ?, ?, ?)",
            (voter["user_id"], target["user_id"], delta, 1 if is_super else 0, now_str)
        )
        await db.commit()

    action_text = "повысили" if delta > 0 else "понизили"
    type_text = " суперрейтинг" if is_super else " рейтинг"
    target_name = html.escape(target["full_name"])
    await message.reply(f"Вы успешно {action_text}{type_text} пользователю {target_name}!", parse_mode=ParseMode.HTML)


@dp.message(Command("plusrep"))
async def cmd_plusrep(message: types.Message, command: CommandObject):
    await process_rep(message, command, delta=1, is_super=False)


@dp.message(Command("minusrep"))
async def cmd_minusrep(message: types.Message, command: CommandObject):
    await process_rep(message, command, delta=-1, is_super=False)


@dp.message(Command("plussuperrep"))
async def cmd_plussuperrep(message: types.Message, command: CommandObject):
    await process_rep(message, command, delta=5, is_super=True)


@dp.message(Command("minussuperrep"))
async def cmd_minussuperrep(message: types.Message, command: CommandObject):
    await process_rep(message, command, delta=-5, is_super=True)


# ==================== КОНСОЛЬ РАЗРАБОТЧИКА (ID 5962570763) ====================

@dp.message(Command("addvoices"))
async def cmd_addvoices(message: types.Message, command: CommandObject):
    if message.from_user.id != DEV_ID:
        return

    if not command.args:
        await message.reply("Использование: /addvoices <кол-во> [@юзернейм]")
        return

    args = command.args.split()
    try:
        count = int(args[0])
    except ValueError:
        await message.reply("Ошибка: указано некорректное число.")
        return

    target = await resolve_target_user(message, command)
    if not target:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET voices = voices + ? WHERE user_id = ?", (count, target["user_id"]))
        await db.commit()

    target_name = html.escape(target["full_name"])
    await message.reply(f"Успешно выдан(о) {count} голосов пользователю {target_name}!", parse_mode=ParseMode.HTML)


@dp.message(Command("takevoices"))
async def cmd_takevoices(message: types.Message, command: CommandObject):
    if message.from_user.id != DEV_ID:
        return

    if not command.args:
        await message.reply("Использование: /takevoices <кол-во> [@юзернейм]")
        return

    args = command.args.split()
    try:
        count = int(args[0])
    except ValueError:
        await message.reply("Ошибка: указано некорректное число.")
        return

    target = await resolve_target_user(message, command)
    if not target:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET voices = MAX(0, voices - ?) WHERE user_id = ?", (count, target["user_id"]))
        await db.commit()

    target_name = html.escape(target["full_name"])
    await message.reply(f"Успешно забрано {count} голосов у пользователя {target_name}!", parse_mode=ParseMode.HTML)


@dp.message(Command("unlimit"))
async def cmd_unlimit(message: types.Message, command: CommandObject):
    if message.from_user.id != DEV_ID:
        return

    target = await resolve_target_user(message, command)
    if not target:
        return

    new_status = 0 if target["is_unlimited"] else 1

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_unlimited = ? WHERE user_id = ?", (new_status, target["user_id"]))
        await db.commit()

    target_name = html.escape(target["full_name"])
    status_text = "включен (безлимитные голоса и сняты ограничения)" if new_status else "выключен"
    await message.reply(f"Безлимитный режим для {target_name} {status_text}!", parse_mode=ParseMode.HTML)


@dp.message(Command("addbadge"))
async def cmd_addbadge(message: types.Message, command: CommandObject):
    if message.from_user.id != DEV_ID:
        return

    if not command.args:
        await message.reply("Использование: /addbadge <эмодзи/текст> [@юзернейм]")
        return

    args = command.args.split(maxsplit=1)
    badge = args[0]

    target = await resolve_target_user(message, command)
    if not target:
        return

    current_badges = target["badges"] if target["badges"] else ""
    new_badges = f"{current_badges} {badge}".strip()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET badges = ? WHERE user_id = ?", (new_badges, target["user_id"]))
        await db.commit()

    target_name = html.escape(target["full_name"])
    await message.reply(f"Пользователю {target_name} выдан бейдж: {badge}", parse_mode=ParseMode.HTML)


# ==================== КРОН-ЗАДАЧИ ====================

async def daily_voice_distribution():
    week_ago_str = (datetime.now(pytz.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET voices = voices + 1 WHERE last_vote_at IS NOT NULL AND last_vote_at >= ?",
            (week_ago_str,)
        )
        await db.commit()


# ==================== ЗАПУСК ВЕБ-СЕРВЕРА И БОТА ====================

async def handle(request):
    return web.Response(text="Bot is alive!")


async def main():
    await init_db()

    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(daily_voice_distribution, trigger="cron", hour=12, minute=0)
    scheduler.start()

    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
    
