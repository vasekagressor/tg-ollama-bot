import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from ollama import AsyncClient

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = "8687731705:AAGcXYBRX_jv-MgXh855xYidump48evZosA"
OLLAMA_API_KEY = "86d5492d5a6946eebddf1986f90a929d.Li4O2QcqB9yxC8nC96wKCI4f"
OLLAMA_HOST    = "https://ollama.com"
MODEL          = "gpt-oss:120b-cloud"

DEFAULT_TRIGGER = "вась"

# Владелец бота — единственный, кто может управлять админами.
# Узнать свой ID можно у @userinfobot в Telegram.
OWNER_ID = 117346464  # <-- ЗАМЕНИ

# Стартовый список запрещённых слов (сидируется один раз при создании БД).
# Дальше управляется командами /ban_add и /ban_del.
DEFAULT_BANNED_WORDS = ["меф", "гаш", "соль", "порошок"]

SYSTEM_PROMPT = "Ты полезный ассистент. Отвечай кратко и по делу на русском языке."
MAX_HISTORY   = 20

DB_PATH = Path("bot.db")
# ===================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("vasya-bot")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.MARKDOWN)
dp = Dispatcher()

ollama = AsyncClient(
    host=OLLAMA_HOST,
    headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"},
)

bot_id: int | None = None


# ==================== БАЗА ДАННЫХ ====================

def db_init() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS history (
            chat_id  INTEGER PRIMARY KEY,
            messages TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS requests (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT    NOT NULL,
            user_id   INTEGER,
            username  TEXT,
            nickname  TEXT,
            chat_id   INTEGER,
            trigger   TEXT,
            prompt    TEXT,
            response  TEXT
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id  INTEGER PRIMARY KEY,
            username TEXT,
            nickname TEXT,
            added_at TEXT NOT NULL,
            added_by INTEGER
        );

        CREATE TABLE IF NOT EXISTS banned_words (
            word     TEXT PRIMARY KEY,
            added_at TEXT NOT NULL,
            added_by INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id);
        CREATE INDEX IF NOT EXISTS idx_requests_ts   ON requests(timestamp);
        """)
        conn.commit()

    # Сидируем дефолтные запрещённые слова, если таблица пуста
    if not banned_list():
        for w in DEFAULT_BANNED_WORDS:
            banned_add(w, added_by=0)
        log.info("Засеяны дефолтные запрещённые слова: %s", DEFAULT_BANNED_WORDS)


# ---------- settings ----------
def setting_get(key: str, default: str | None = None) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def setting_set(key: str, value: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def get_trigger() -> str:
    return setting_get("current_trigger", DEFAULT_TRIGGER) or DEFAULT_TRIGGER


def set_trigger(value: str) -> None:
    setting_set("current_trigger", value)


# ---------- history ----------
def history_load(chat_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT messages FROM history WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            pass
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def history_save(chat_id: int, messages: list[dict]) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO history(chat_id, messages) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET messages=excluded.messages",
            (chat_id, json.dumps(messages, ensure_ascii=False)),
        )
        conn.commit()


def history_reset(chat_id: int) -> None:
    history_save(chat_id, [{"role": "system", "content": SYSTEM_PROMPT}])


def trim_history(msgs: list[dict]) -> list[dict]:
    if len(msgs) <= MAX_HISTORY + 1:
        return msgs
    return [msgs[0]] + msgs[-MAX_HISTORY:]


# ---------- requests log ----------
def log_request(
    user_id: int | None,
    username: str | None,
    nickname: str | None,
    chat_id: int,
    trigger: str,
    prompt: str,
    response: str | None,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO requests(timestamp, user_id, username, nickname, chat_id, trigger, prompt, response) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.utcnow().isoformat(timespec="seconds"),
                user_id, username, nickname, chat_id, trigger, prompt, response,
            ),
        )
        conn.commit()


# ---------- admins ----------
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    """True, если user — owner или в таблице admins."""
    if is_owner(user_id):
        return True
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
        return row is not None


def admin_add(user_id: int, username: str | None, nickname: str | None, added_by: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        try:
            conn.execute(
                "INSERT INTO admins(user_id, username, nickname, added_at, added_by) "
                "VALUES(?, ?, ?, ?, ?)",
                (user_id, username, nickname, datetime.utcnow().isoformat(timespec="seconds"), added_by),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def admin_remove(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount > 0


def admin_list() -> list[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT user_id, username, nickname, added_at FROM admins ORDER BY added_at"
        ).fetchall()


# ---------- banned_words ----------
def banned_list() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT word FROM banned_words ORDER BY word").fetchall()
    return [r[0] for r in rows]


def banned_add(word: str, added_by: int) -> bool:
    word = word.strip().lower()
    if not word:
        return False
    with sqlite3.connect(DB_PATH) as conn:
        try:
            conn.execute(
                "INSERT INTO banned_words(word, added_at, added_by) VALUES(?, ?, ?)",
                (word, datetime.utcnow().isoformat(timespec="seconds"), added_by),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def banned_remove(word: str) -> bool:
    word = word.strip().lower()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM banned_words WHERE word=?", (word,))
        conn.commit()
        return cur.rowcount > 0


def find_banned(text: str) -> str | None:
    """Проверяет текст по списку из БД. Возвращает найденное слово или None."""
    low = text.lower()
    for word in banned_list():
        if word in low:
            return word
    return None


# ==================== ХЭНДЛЕРЫ ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    trig = get_trigger()
    await message.answer(
        "Привет! Я бот-обёртка над облачной моделью Ollama.\n\n"
        f"Способы обращения:\n"
        f"1. Обычное сообщение: `{trig} <вопрос>`\n"
        f"2. Reply на моё сообщение: просто напиши вопрос\n\n"
        "Команды:\n"
        "/reset — сбросить контекст\n"
        "/trigger — показать/сменить триггер\n"
        "/help — помощь"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    trig = get_trigger()
    text = (
        f"Как писать боту:\n"
        f"• `{trig} <промпт>` — обычное сообщение\n"
        f"• Reply на моё сообщение — без префикса\n"
        f"• /reset — очистить историю\n"
        f"• /trigger — показать текущий триггер\n"
        f"• /trigger <слово> — сменить триггер (админ)\n\n"
        f"Сейчас триггер: `{trig}`"
    )

    if is_admin(message.from_user.id):
        text += (
            "\n\n👑 Команды админа:\n"
            "• /admins — список админов\n"
            "• /ban_list — показать запрещённые слова\n"
            "• /ban_add <слово> — добавить в бан\n"
            "• /ban_del <слово> — убрать из бана\n"
        )
    if is_owner(message.from_user.id):
        text += (
            "• /admin_add <user_id> — добавить админа (только owner)\n"
            "• /admin_del <user_id> — снять админа (только owner)\n"
        )

    await message.answer(text)


# ---------- /trigger ----------
@dp.message(Command("trigger"))
async def cmd_trigger(message: Message):
    args = message.text.split(maxsplit=1)
    new_trigger = args[1].strip().lower() if len(args) > 1 else ""

    if not new_trigger:
        await message.reply(f"Текущий триггер: `{get_trigger()}`")
        return

    if not is_admin(message.from_user.id):
        await message.reply("🚫 Менять триггер может только админ.")
        return

    if len(new_trigger) < 2:
        await message.reply("❌ Триггер должен быть минимум 2 символа.")
        return
    if len(new_trigger) > 20:
        await message.reply("❌ Триггер не должен быть длиннее 20 символов.")
        return
    if " " in new_trigger:
        await message.reply("❌ Триггер не может содержать пробелы.")
        return

    old = get_trigger()
    set_trigger(new_trigger)
    log.info("Триггер изменён: '%s' -> '%s' (by %s)", old, new_trigger, message.from_user.id)

    await message.reply(
        f"✅ Триггер изменён: `{old}` → `{new_trigger}`\n"
        f"(сохранён в базе, переживёт перезапуск)\n\n"
        f"Теперь пиши так: `{new_trigger} привет`"
    )


# ---------- /reset ----------
@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, удалить", callback_data="reset_yes"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data="reset_no"),
        ]]
    )
    await message.answer(
        "⚠️ Точно ли вы хотите удалить историю чата?\n"
        "Весь контекст диалога будет потерян.",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.in_({"reset_yes", "reset_no"}))
async def on_reset_callback(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if callback.data == "reset_yes":
        history_reset(chat_id)
        await callback.message.edit_text("🧹 История чата очищена.")
    else:
        await callback.message.edit_text("Отменено. История сохранена.")
    await callback.answer()


# ---------- /admins ----------
@dp.message(Command("admins"))
async def cmd_admins(message: Message):
    if not is_admin(message.from_user.id):
        return

    rows = admin_list()
    lines = [f"👑 Owner: `{OWNER_ID}`"]
    if rows:
        lines.append("\n🛡 Админы:")
        for uid, username, nickname, added_at in rows:
            label = nickname or "—"
            uname = f" ({username})" if username else ""
            lines.append(f"• `{uid}` — {label}{uname} — с {added_at}")
    else:
        lines.append("\n🛡 Админов нет.")

    await message.answer("\n".join(lines))


# ---------- /admin_add ----------
@dp.message(Command("admin_add"))
async def cmd_admin_add(message: Message):
    if not is_owner(message.from_user.id):
        await message.reply("🚫 Только владелец может добавлять админов.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "Использование: `/admin_add <user_id>`\n"
            "Например: `/admin_add 123456789`\n\n"
            "Узнать user_id можно у @userinfobot."
        )
        return

    try:
        target_id = int(args[1].strip())
    except ValueError:
        await message.reply("❌ user_id должен быть числом.")
        return

    if target_id == OWNER_ID:
        await message.reply("ℹ️ Это владелец, он и так главный.")
        return

    username = None
    nickname = None
    try:
        chat = await bot.get_chat(target_id)
        if chat.username:
            username = f"@{chat.username}"
        parts = [chat.first_name or "", chat.last_name or ""]
        nickname = " ".join(p for p in parts if p) or None
    except Exception as e:
        log.warning("Не удалось получить инфо о %s: %s", target_id, e)

    added = admin_add(target_id, username, nickname, added_by=message.from_user.id)
    if added:
        await message.reply(f"✅ Пользователь `{target_id}` назначен админом.")
    else:
        await message.reply(f"ℹ️ Пользователь `{target_id}` уже админ.")


# ---------- /admin_del ----------
@dp.message(Command("admin_del"))
async def cmd_admin_del(message: Message):
    if not is_owner(message.from_user.id):
        await message.reply("🚫 Только владелец может снимать админов.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: `/admin_del <user_id>`")
        return

    try:
        target_id = int(args[1].strip())
    except ValueError:
        await message.reply("❌ user_id должен быть числом.")
        return

    if target_id == OWNER_ID:
        await message.reply("🚫 Владельца снять нельзя.")
        return

    removed = admin_remove(target_id)
    if removed:
        await message.reply(f"✅ Пользователь `{target_id}` больше не админ.")
    else:
        await message.reply(f"ℹ️ Пользователь `{target_id}` не был админом.")


# ---------- /ban_list ----------
@dp.message(Command("ban_list"))
async def cmd_ban_list(message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("🚫 Только owner и админы могут смотреть список.")
        return

    words = banned_list()
    if not words:
        await message.answer("📭 Список запрещённых слов пуст.")
        return

    lines = ["🚫 Запрещённые слова:"]
    for w in words:
        lines.append(f"• `{w}`")
    await message.answer("\n".join(lines))


# ---------- /ban_add ----------
@dp.message(Command("ban_add"))
async def cmd_ban_add(message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("🚫 Только owner и админы могут менять список.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: `/ban_add <слово>`")
        return

    word = args[1].strip().lower()
    if not word or " " in word:
        await message.reply("❌ Слово не должно содержать пробелов.")
        return

    added = banned_add(word, added_by=message.from_user.id)
    if added:
        log.info("Слово '%s' добавлено в бан (by %s)", word, message.from_user.id)
        await message.reply(f"✅ Слово `{word}` добавлено в бан.")
    else:
        await message.reply(f"ℹ️ Слово `{word}` уже в списке.")


# ---------- /ban_del ----------
@dp.message(Command("ban_del"))
async def cmd_ban_del(message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("🚫 Только owner и админы могут менять список.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: `/ban_del <слово>`")
        return

    word = args[1].strip().lower()
    removed = banned_remove(word)
    if removed:
        log.info("Слово '%s' убрано из бана (by %s)", word, message.from_user.id)
        await message.reply(f"✅ Слово `{word}` убрано из бана.")
    else:
        await message.reply(f"ℹ️ Слова `{word}` в списке нет.")


# ---------- Запрос к модели ----------
async def ask_model(message: Message, prompt: str, trigger_used: str):
    chat_id = message.chat.id
    msgs = history_load(chat_id)
    msgs.append({"role": "user", "content": prompt})
    msgs = trim_history(msgs)

    placeholder = await message.reply("⏳ Думаю...")

    user = message.from_user
    user_id = user.id if user else None
    username = f"@{user.username}" if user and user.username else None
    nickname_parts = []
    if user:
        if user.first_name:
            nickname_parts.append(user.first_name)
        if user.last_name:
            nickname_parts.append(user.last_name)
    nickname = " ".join(nickname_parts) if nickname_parts else None

    answer: str | None = None
    try:
        response = await ollama.chat(model=MODEL, messages=msgs)
        answer = response["message"]["content"]
        msgs.append({"role": "assistant", "content": answer})
        history_save(chat_id, msgs)

        for i in range(0, len(answer), 4000):
            chunk = answer[i:i + 4000]
            if i == 0:
                await placeholder.edit_text(chunk)
            else:
                await message.reply(chunk)

    except Exception as e:
        log.exception("Ошибка при запросе к Ollama")
        answer = f"ERROR: {e}"
        await placeholder.edit_text(f"❌ Ошибка: {e}")

    log_request(
        user_id=user_id,
        username=username,
        nickname=nickname,
        chat_id=chat_id,
        trigger=trigger_used,
        prompt=prompt,
        response=answer,
    )


# ---------- Основной хэндлер ----------
@dp.message(F.text)
async def on_message(message: Message):
    text = message.text.strip()
    trig = get_trigger()

    is_reply_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_id
    )

    if text.lower().startswith(trig.lower()):
        prompt = text[len(trig):].strip()
        trigger_used = trig
    elif is_reply_to_bot:
        prompt = text
        trigger_used = "-"
    else:
        return

    if not prompt:
        await message.reply(
            f"Напиши промпт после `{trig}`, например: `{trig} как дела?` "
            f"или ответь (reply) на моё сообщение."
        )
        return

    # Фильтр запрещённых слов (список из БД)
    banned = find_banned(prompt)
    if banned:
        log.warning(
            "Запрещённое слово '%s' от user_id=%s (@%s)",
            banned,
            message.from_user.id,
            message.from_user.username,
        )
        await message.reply(
            "🚫 В запросе есть запрещённое слово.\n"
            "Переформулируй, пожалуйста."
        )
        return

    await ask_model(message, prompt, trigger_used)


# ==================== ТОЧКА ВХОДА ====================

async def main():
    global bot_id
    db_init()
    me = await bot.get_me()
    bot_id = me.id
    log.info(
        "Бот запущен: @%s (id=%s). Модель: %s. Триггер: %s. Owner: %s",
        me.username, me.id, MODEL, get_trigger(), OWNER_ID,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())