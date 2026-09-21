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

# Токен бота
BOT_TOKEN = "8908372460:AAG8tNdwASQwspMTFwEs09ERRfQkpFfBlnM"
DB_NAME = "reputation_bot.db"

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
                last_vote_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reputation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER,
                to_user_id INTEGER,
                value INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def get_or_create_user(user: types.User):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user.id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                username = user.username.lower() if user.username else None
                await db.execute(
                    "INSERT INTO users (user_id, username, full_name, voices) VALUES (?, ?, ?, 1)",
                    (user.id, username, user.full_name)
                )
                await db.commit()
            else:
                username = user.username.lower() if user.username else None
                await db.execute(
                    "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                    (username, user.full_name, user.id)
                )
                await db.commit()


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
        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ?", (user_id,)) as c:
            total = (await c.fetchone())[0]

        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ? AND created_at >= ?", (user_id, day_ago)) as c:
            daily = (await c.fetchone())[0]

        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ? AND created_at >= ?", (user_id, week_ago)) as c:
            weekly = (await c.fetchone())[0]

        async with db.execute("SELECT COALESCE(SUM(value), 0) FROM reputation_logs WHERE to_user_id = ? AND created_at >= ?", (user_id, month_ago)) as c:
            monthly = (await c.fetchone())[0]

    return total, daily, weekly, monthly


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

def format_user_info(user_row, total, daily, weekly, monthly) -> str:
    user_id = user_row["user_id"]
    name = html.escape(user_row["full_name"])
    user_link = f'<a href="tg://user?id={user_id}">{name}</a>'
    desc = html.escape(user_row["description"])

    return (
        f"информация о {user_link}\n\n"
        f"рейтинг: {total}\n"
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
    await get_or_create_user(message.from_user)
    await message.reply("Привет! Ты зарегистрирован в системе рейтинга. Тебе выдан 1 голос!")


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
    total, daily, weekly, monthly = await get_reputation_stats(message.from_user.id)
    text = format_user_info(user_row, total, daily, weekly, monthly)
    await message.reply(text, parse_mode=ParseMode.HTML)


@dp.message(Command("information"))
async def cmd_information(message: types.Message, command: CommandObject):
    await get_or_create_user(message.from_user)
    target_user = await resolve_target_user(message, command)
    if not target_user:
        return
    
    total, daily, weekly, monthly = await get_reputation_stats(target_user["user_id"])
    text = format_user_info(target_user, total, daily, weekly, monthly)
    await message.reply(text, parse_mode=ParseMode.HTML)


@dp.message(Command("myvoice"))
async def cmd_myvoice(message: types.Message):
    await get_or_create_user(message.from_user)
    user_row = await get_user_by_id(message.from_user.id)
    await message.reply(f"Остаток голосов: {user_row['voices']}")


@dp.message(Command("sendvoice"))
async def cmd_sendvoice(message: types.Message, command: CommandObject):
    await get_or_create_user(message.from_user)
    sender = await get_user_by_id(message.from_user.id)
    
    if sender["voices"] < 1:
        await message.reply("У вас нет доступных голосов для передачи.")
        return

    target = await resolve_target_user(message, command)
    if not target:
        return

    if target["user_id"] == sender["user_id"]:
        await message.reply("Нельзя передавать голоса самому себе.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET voices = voices - 1 WHERE user_id = ?", (sender["user_id"],))
        await db.execute("UPDATE users SET voices = voices + 1 WHERE user_id = ?", (target["user_id"],))
        await db.commit()

    target_name = html.escape(target["full_name"])
    await message.reply(f"Вы успешно передали 1 голос пользователю {target_name}!", parse_mode=ParseMode.HTML)


async def process_rep(message: types.Message, command: CommandObject, delta: int):
    await get_or_create_user(message.from_user)
    voter = await get_user_by_id(message.from_user.id)

    if voter["voices"] < 1:
        await message.reply("У вас недостаточно голосов для голосования.")
        return

    target = await resolve_target_user(message, command)
    if not target:
        return

    if target["user_id"] == voter["user_id"]:
        await message.reply("Нельзя голосовать за самого себя!")
        return

    votes_today = await get_votes_today(voter["user_id"], target["user_id"])
    if votes_today >= 3:
        await message.reply("Нельзя голосовать за одного и того же человека больше 3 раз в день.")
        return

    now_str = datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET voices = voices - 1, last_vote_at = ? WHERE user_id = ?", (now_str, voter["user_id"]))
        await db.execute(
            "INSERT INTO reputation_logs (from_user_id, to_user_id, value, created_at) VALUES (?, ?, ?, ?)",
            (voter["user_id"], target["user_id"], delta, now_str)
        )
        await db.commit()

    action_text = "повысили" if delta > 0 else "понизили"
    target_name = html.escape(target["full_name"])
    await message.reply(f"Вы успешно {action_text} рейтинг пользователю {target_name}!", parse_mode=ParseMode.HTML)


@dp.message(Command("plusrep"))
async def cmd_plusrep(message: types.Message, command: CommandObject):
    await process_rep(message, command, delta=1)


@dp.message(Command("minusrep"))
async def cmd_minusrep(message: types.Message, command: CommandObject):
    await process_rep(message, command, delta=-1)


# ==================== КРОН-ЗАДАЧА (12:00 МСК) ====================

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
    
