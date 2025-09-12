import logging
import asyncio
import sqlite3
import sys
import os
import json
from pathlib import Path
import psutil
import aiohttp
import time
import random
from datetime import datetime, time as dt_time, timedelta, timezone
from aiogram import Bot, Dispatcher, types
from aiogram import exceptions
from aiogram.types import Update
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InputFile, BufferedInputFile
from aiogram.fsm.storage import memory
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import phonenumbers
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Режим тестирования
TEST_MODE = "--test" in sys.argv

if TEST_MODE:
    logger.warning("⚠️ Бот запущен в ТЕСТОВОМ РЕЖИМЕ! Все ограничения отключены.")

# Загрузка переменных окружения
load_dotenv()

# Конфигурация
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAIN_ADMINS = [int(admin_id) for admin_id in os.getenv("MAIN_ADMINS", "").split(",") if admin_id]
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS" "").split(",") if admin_id]
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN")
CRYPTOBOT_ASSET = "USDT"
DATABASE = "multi_service_bot.db"
EXCHANGE_RATE = 90  # Курс доллара к рублю
REFERRAL_BONUS = 0.1  # 0.1$ за приглашение

# Тарифы
WHATSAPP_RATES = {1: 8.0, 2: 10.0, 3: 12.0}  # 1 час - 8$, 2 часа - 10$, 3 часа - 12$
MAX_RATE = 9.0
SMS_RATE = 0.3

# Статусы работы сервисов
SERVICE_STATUS = {
    "whatsapp": "активен",
    "max": "активен", 
    "sms": "активен"
}

# Уровни рейтинга
RATING_LEVELS = {
    1: (0, 5),
    2: (6, 15),
    3: (16, 30),
    4: (31, 50),
    5: (51, 75),
    6: (76, 105),
    7: (106, 140),
    8: (141, 180),
    9: (181, 225),
    10: (226, float('inf'))
}

# Тайминги
MOSCOW_TZ = timezone(timedelta(hours=3))
MAX_HOLD_DURATION = 10800  # 3 часа для WhatsApp
MAX_HOLD_DURATION_MAX = 900  # 15 минут для MAX

# Создаем директории
TEMP_DIR = Path("temp_photos")
TEMP_DIR.mkdir(exist_ok=True, parents=True)

# Инициализация бота
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = memory.MemoryStorage()
dp = Dispatcher(storage=storage)

# Класс для ожидающих подтверждений
class PendingConfirmations:
    def __init__(self):
        self.pending = {}
        self.active_timers = set()
        
    def add(self, user_id, account_id):
        if account_id in self.active_timers:
            return False
        self.pending[user_id] = {
            'account_id': account_id,
            'timestamp': time.time(),
            'blocked': False
        }
        self.active_timers.add(account_id)
        return True
        
    def check_and_block(self, user_id):
        if user_id in self.pending and not self.pending[user_id]['blocked']:
            if time.time() - self.pending[user_id]['timestamp'] > 180:  # 3 минуты
                self.pending[user_id]['blocked'] = True
                return True
        return False
    
    def remove(self, user_id):
        if user_id in self.pending:
            account_id = self.pending[user_id]['account_id']
            self.active_timers.discard(account_id)
            del self.pending[user_id]

pending_confirmations = PendingConfirmations()

# Состояния FSM
class Form(StatesGroup):
    referral_source = State()
    whatsapp_phone = State()
    max_phone = State()
    sms_text = State()
    sms_proof = State()
    support_message = State()
    admin_message = State()
    admin_broadcast = State()
    add_admin = State()
    change_status = State()
    service_select = State()
    warn_user = State()
    code = State()
    account_status = State()
    add_balance = State()
    max_code = State()
    sms_work_message = State()
    admin_sms_message = State()
    whatsapp_code = State()
    max_user_code = State()
    sms_complete = State()

# Вспомогательные функции
def is_main_admin(user_id: int) -> bool:
    return user_id in MAIN_ADMINS

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in MAIN_ADMINS

def save_admin_ids():
    """Сохраняет ID администраторов в .env файл"""
    env_path = Path('.env')
    env_content = env_path.read_text() if env_path.exists() else ''
    
    lines = env_content.split('\n')
    new_lines = []
    admin_ids_line = f"ADMIN_IDS={','.join(map(str, ADMIN_IDS))}"
    
    found = False
    for line in lines:
        if line.startswith('ADMIN_IDS='):
            new_lines.append(admin_ids_line)
            found = True
        else:
            new_lines.append(line)
    
    if not found:
        new_lines.append(admin_ids_line)
    
    env_path.write_text('\n'.join(new_lines))

def get_service_status(service: str) -> str:
    return SERVICE_STATUS.get(service, "неизвестен")

def update_service_status(service: str, status: str):
    """Устанавливает статус сервиса"""
    if service in SERVICE_STATUS:
        SERVICE_STATUS[service] = status

def calculate_level(numbers_count: int) -> tuple:
    """Рассчитывает уровень пользователя на основе количества номеров"""
    for level, (min_num, max_num) in RATING_LEVELS.items():
        if min_num <= numbers_count <= max_num:
            return level, numbers_count - min_num, max_num - min_num + 1
    return 1, 0, 5

def rub_to_usd(rub_amount: float) -> float:
    return rub_amount / EXCHANGE_RATE

def usd_to_rub(usd_amount: float) -> float:
    return usd_amount * EXCHANGE_RATE

def validate_phone(phone: str) -> bool:
    try:
        parsed = phonenumbers.parse(phone)
        return phonenumbers.is_valid_number(parsed)
    except:
        return False

def get_user_referral_source(user_id: int) -> str:
    """Получает источник реферала пользователя"""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT referral_source FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        if result and result[0]:
            return result[0]
        return "не указан"

async def notify_admins(message: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """Отправляет уведомление всем администраторам"""
    all_admins = set(MAIN_ADMINS + ADMIN_IDS)
    if not all_admins:
        logger.warning("Нет администраторов для уведомления")
        return
    
    for admin_id in all_admins:
        if admin_id is None:
            continue
        try:
            await bot.send_message(
                admin_id, 
                message, 
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления админа {admin_id}: {e}")

async def notify_admin(admin_id: int, message: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """Отправляет уведомление конкретному администратору"""
    try:
        await bot.send_message(
            admin_id, 
            message, 
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления админа {admin_id}: {e}")

# Инициализация базы данных
def init_db():
    try:
        conn = sqlite3.connect(DATABASE, timeout=30)
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            referral_source TEXT,
            balance_usd REAL DEFAULT 0,
            balance_rub REAL DEFAULT 0,
            level INTEGER DEFAULT 1,
            warnings INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            whatsapp_numbers INTEGER DEFAULT 0,
            max_numbers INTEGER DEFAULT 0,
            sms_messages INTEGER DEFAULT 0,
            total_earned_usd REAL DEFAULT 0,
            total_earned_rub REAL DEFAULT 0,
            referrer_id INTEGER,
            captcha_passed INTEGER DEFAULT 1
        )
        """)
        
        # Таблица номеров WhatsApp
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            phone TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            hold_start TEXT,
            hold_hours INTEGER DEFAULT 3,
            completed BOOLEAN DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            failed_at TEXT,
            admin_id INTEGER,
            code_text TEXT,
            code_sent BOOLEAN DEFAULT 0,
            code_entered BOOLEAN DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """)
        
        # Таблица номеров MAX
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS max_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            phone TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            hold_start TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            admin_id INTEGER,
            user_code TEXT,
            code_sent BOOLEAN DEFAULT 0,
            code_entered BOOLEAN DEFAULT 0,
            admin_accepted BOOLEAN DEFAULT 0,
            completed BOOLEAN DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """)
        
        # Таблица SMS работ
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sms_works (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            admin_id INTEGER,
            text TEXT,
            work_message TEXT,
            proof_photo TEXT,
            status TEXT DEFAULT 'pending',
            amount REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT,
            completed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(admin_id) REFERENCES users(user_id)
        )
        """)
        
        # Таблица рефералов
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(referrer_id) REFERENCES users(user_id),
            FOREIGN KEY(referred_id) REFERENCES users(user_id)
        )
        """)
        
        # Таблица заявки на вывод
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount_usd REAL,
            amount_rub REAL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            invoice_id TEXT,
            invoice_url TEXT,
            expires_at TEXT,
            confirmed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """)
        
        # Таблица поддержки
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            status TEXT DEFAULT 'open',
            admin_id INTEGER,
            response TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            responded_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(admin_id) REFERENCES users(user_id)
        )
        """)
        
        # Проверяем существование столбцов и добавляем их при необходимости
        columns_to_check = [
            ('whatsapp_numbers', 'completed'),
            ('whatsapp_numbers', 'failed_at'),
            ('whatsapp_numbers', 'admin_id'),
            ('whatsapp_numbers', 'code_text'),
            ('whatsapp_numbers', 'code_sent'),
            ('whatsapp_numbers', 'code_entered'),
            ('max_numbers', 'admin_id'),
            ('max_numbers', 'user_code'),
            ('max_numbers', 'code_sent'),
            ('max_numbers', 'code_entered'),
            ('max_numbers', 'admin_accepted'),
            ('max_numbers', 'completed'),
            ('sms_works', 'work_message'),
            ('sms_works', 'completed_at'),
            ('users', 'referrer_id'),
            ('users', 'captcha_passed')
        ]
        
        for table, column in columns_to_check:
            try:
                cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
            except sqlite3.OperationalError:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {get_column_type(column)}")
        
        conn.commit()
        logger.info("Database initialization completed successfully")
        
    except sqlite3.Error as e:
        logger.error(f"Database initialization failed: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()

def get_column_type(column_name):
    """Возвращает тип столбца по его имени"""
    type_map = {
        'completed': 'BOOLEAN DEFAULT 0',
        'failed_at': 'TEXT',
        'admin_id': 'INTEGER',
        'code_text': 'TEXT',
        'code_sent': 'BOOLEAN DEFAULT 0',
        'code_entered': 'BOOLEAN DEFAULT 0',
        'user_code': 'TEXT',
        'admin_accepted': 'BOOLEAN DEFAULT 0',
        'work_message': 'TEXT',
        'completed_at': 'TEXT',
        'referrer_id': 'INTEGER',
        'captcha_passed': 'INTEGER DEFAULT 1'
    }
    return type_map.get(column_name, 'TEXT')

# Клавиатуры
async def main_menu(user_id: int):
    # Проверяем, указан ли источник у пользователя
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT referral_source FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone()
        
        if user_data and not user_data[0]:
            # Если источник не указан, просим выбрать
            builder = InlineKeyboardBuilder()
            builder.row(
                types.InlineKeyboardButton(text="WaCash", callback_data="source:wacash"),
                types.InlineKeyboardButton(text="WhatsApp Dealers", callback_data="source:whatsappdealers")
            )
            
            return (
                "👋 <b>Добро пожаловать!</b>\n\n"
                "Пожалуйста, выберите источник, откуда вы о нас узнали:",
                builder.as_markup()
            )
    
    # Получаем статистику пользователя
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT balance_usd, level, warnings, whatsapp_numbers, max_numbers, sms_messages
            FROM users WHERE user_id = ?
        """, (user_id,))
        user_data = cursor.fetchone()
        
    if user_data:
        balance_usd, level, warnings, whatsapp_count, max_count, sms_count = user_data
        balance_rub = usd_to_rub(balance_usd)
    else:
        balance_usd = balance_rub = level = warnings = whatsapp_count = max_count = sms_count = 0
    
    # Получаем общую очередь WhatsApp
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM whatsapp_numbers WHERE status = 'active'")
        total_whatsapp_queue = cursor.fetchone()[0]
    
    # Получаем статусы сервисов
    whatsapp_status = get_service_status("whatsapp")
    max_status = get_service_status("max")
    sms_status = get_service_status("sms")
    
    # Форматируем текст главного меню
    menu_text = (
        "✨ <b>Главное меню</b>\n\n"
        
        f"╔ <b>WhatsApp (8-12$/3ч)</b>\n"
        f"╠ Общая очередь: {total_whatsapp_queue}\n"
        f"╚ Ваши номера: {whatsapp_count}\n"
        f"╔ <b>MAX (9$/15мин)</b>\n"
        f"╠ Статус: {max_status}\n"
        f"╚ Ваши номера: {max_count}\n"
        f"╔ <b>SMS (0.3$/шт)</b>\n"
        f"╠ Статус: {sms_status}\n"
        f"╚ Ваши сообщения: {sms_count}\n\n"
        
        f"💰 <b>Баланс:</b> {balance_usd:.2f}$ ({balance_rub:.2f}₽)\n"
        f"📊 <b>Уровень:</b> {level}\n"
        f"⚠️ <b>Предупреждения:</b> {warnings}/3\n\n"
        
        "💡 <b>Выберите действие:</b>"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📱 WhatsApp", callback_data="service:whatsapp"),
        types.InlineKeyboardButton(text="🔵 MAX", callback_data="service:max")
    )
    builder.row(
        types.InlineKeyboardButton(text="💬 SMS", callback_data="service:sms"),
        types.InlineKeyboardButton(text="💰 Баланс", callback_data="balance")
    )
    builder.row(
        types.InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals"),
        types.InlineKeyboardButton(text="ℹ️ Информация", callback_data="info")
    )
    builder.row(
        types.InlineKeyboardButton(text="🛠 Поддержка", callback_data="support")
    )
    
    if is_admin(user_id):
        builder.row(
            types.InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")
        )
    
    return menu_text, builder.as_markup()

def back_to_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu"))
    return builder.as_markup()

def service_keyboard(service: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="➕ Добавить номер", callback_data=f"add_{service}"),
        types.InlineKeyboardButton(text="📋 Мои номера", callback_data=f"my_{service}")
    )
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu"))
    return builder.as_markup()

def sms_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✍️ Отправить работу", callback_data="send_sms_work"),
        types.InlineKeyboardButton(text="📋 Мои работы", callback_data="my_sms_works")
    )
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu"))
    return builder.as_markup()

def balance_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="💳 Вывести", callback_data="withdraw"),
        types.InlineKeyboardButton(text="📊 Статистика", callback_data="balance_stats")
    )
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu"))
    return builder.as_markup()

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        types.InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")
    )
    builder.row(
        types.InlineKeyboardButton(text="📱 WhatsApp номера", callback_data="admin_whatsapp"),
        types.InlineKeyboardButton(text="🔵 MAX номера", callback_data="admin_max")
    )
    builder.row(
        types.InlineKeyboardButton(text="💬 SMS работы", callback_data="admin_sms"),
        types.InlineKeyboardButton(text="💳 Выводы", callback_data="admin_withdrawals")
    )
    builder.row(
        types.InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings"),
        types.InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")
    )
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu"))
    return builder.as_markup()

def admin_settings_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add"),
        types.InlineKeyboardButton(text="➖ Удалить админа", callback_data="admin_remove")
    )
    builder.row(
        types.InlineKeyboardButton(text="🔄 Статус сервисов", callback_data="admin_service_status"),
        types.InlineKeyboardButton(text="⚠️ Предупредить", callback_data="admin_warn")
    )
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel"))
    return builder.as_markup()

def cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return builder.as_markup()

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    # Проверяем реферальную ссылку
    referrer_id = None
    if len(message.text.split()) > 1:
        try:
            referrer_id = int(message.text.split()[1])
        except ValueError:
            pass
    
    # Добавляем пользователя в базу
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name, last_name) 
            VALUES (?, ?, ?, ?)
        """, (user_id, username, first_name, last_name))
        
        # Если есть реферал
        if referrer_id and referrer_id != user_id:
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
            if cursor.fetchone():
                cursor.execute("""
                    INSERT OR IGNORE INTO referrals (referrer_id, referred_id)
                    VALUES (?, ?)
                """, (referrer_id, user_id))
                cursor.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id))
                
                # Начисляем бонус рефереру
                cursor.execute("UPDATE users SET balance_usd = balance_usd + ? WHERE user_id = ?", 
                              (REFERRAL_BONUS, referrer_id))
        
        conn.commit()
    
    menu_text, reply_markup = await main_menu(user_id)
    await message.answer(menu_text, reply_markup=reply_markup)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("❌ У вас нет прав администратора.")
        return
    
    await message.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_keyboard())

# Обработчики колбэков
@dp.callback_query(lambda c: c.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    menu_text, reply_markup = await main_menu(user_id)
    await callback.message.edit_text(menu_text, reply_markup=reply_markup)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("source:"))
async def referral_source_callback(callback: CallbackQuery):
    source = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET referral_source = ? WHERE user_id = ?", (source, user_id))
        conn.commit()
    
    await callback.answer(f"Источник установлен: {source}")
    
    # Показываем главное меню
    menu_text, reply_markup = await main_menu(user_id)
    await callback.message.edit_text(menu_text, reply_markup=reply_markup)

@dp.callback_query(lambda c: c.data == "balance")
async def balance_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance_usd, balance_rub FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone()
        
    if user_data:
        balance_usd, balance_rub = user_data
    else:
        balance_usd = balance_rub = 0
    
    text = (
        f"💰 <b>Ваш баланс</b>\n\n"
        f"💵 Доллары: {balance_usd:.2f}$\n"
        f"₽ Рубли: {balance_rub:.2f}₽\n\n"
        f"💡 Курс обмена: 1$ = {EXCHANGE_RATE}₽"
    )
    
    await callback.message.edit_text(text, reply_markup=balance_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "withdraw")
async def withdraw_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance_usd FROM users WHERE user_id = ?", (user_id,))
        balance = cursor.fetchone()[0]
    
    if balance < 1.0:
        await callback.answer("❌ Минимальная сумма вывода: 1$", show_alert=True)
        return
    
    text = (
        f"💳 <b>Вывод средств</b>\n\n"
        f"💰 Доступно: {balance:.2f}$\n"
        f"💡 Минимальная сумма: 1$\n\n"
        f"📝 <b>Для вывода средств:</b>\n"
        f"1. Укажите сумму в долларах\n"
        f"2. Средства будут переведены на ваш USDT (TRC20) кошелек\n"
        f"3. Вывод обрабатывается в течение 24 часов\n\n"
        f"💬 По вопросам вывода обращайтесь в поддержку"
    )
    
    await callback.message.edit_text(text, reply_markup=back_to_main_keyboard())
    await state.set_state(Form.add_balance)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "balance_stats")
async def balance_stats_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT balance_usd, total_earned_usd, whatsapp_numbers, max_numbers, sms_messages
            FROM users WHERE user_id = ?
        """, (user_id,))
        user_data = cursor.fetchone()
        
    if user_data:
        balance_usd, total_earned, whatsapp_count, max_count, sms_count = user_data
    else:
        balance_usd = total_earned = whatsapp_count = max_count = sms_count = 0
    
    text = (
        f"📊 <b>Статистика заработка</b>\n\n"
        f"💰 Текущий баланс: {balance_usd:.2f}$\n"
        f"💵 Всего заработано: {total_earned:.2f}$\n\n"
        f"📱 WhatsApp номеров: {whatsapp_count}\n"
        f"🔵 MAX номеров: {max_count}\n"
        f"💬 SMS сообщений: {sms_count}\n\n"
        f"💡 Уровень: {calculate_level(whatsapp_count + max_count + sms_count)[0]}"
    )
    
    await callback.message.edit_text(text, reply_markup=back_to_main_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "referrals")
async def referrals_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM referrals WHERE referrer_id = ?
        """, (user_id,))
        ref_count = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT SUM(balance_usd) FROM users 
            WHERE referrer_id = ? AND balance_usd > 0
        """, (user_id,))
        ref_earned = cursor.fetchone()[0] or 0
    
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start={user_id}"
    
    text = (
        f"👥 <b>Реферальная система</b>\n\n"
        f"🔗 Ваша ссылка:\n<code>{ref_link}</code>\n\n"
        f"📊 Статистика:\n"
        f"• Приглашено: {ref_count} человек\n"
        f"• Заработано: {ref_earned:.2f}$\n\n"
        f"💡 За каждого приглашенного друга вы получаете {REFERRAL_BONUS}$\n"
        f"💰 Друзья получают стартовый бонус при регистрации"
    )
    
    await callback.message.edit_text(text, reply_markup=back_to_main_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "info")
async def info_callback(callback: CallbackQuery):
    # Получаем статистику сервисов
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM whatsapp_numbers WHERE status = 'active'")
        whatsapp_queue = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM max_numbers WHERE status = 'active'")
        max_queue = cursor.fetchone()[0]
    
    whatsapp_status = get_service_status("whatsapp")
    max_status = get_service_status("max")
    sms_status = get_service_status("sms")
    
    text = (
        f"ℹ️ <b>Информация о сервисе</b>\n\n"
        
        f"📱 <b>WhatsApp</b>\n"
        f"• Статус: {whatsapp_status}\n"
        f"• Очередь: {whatsapp_queue} номеров\n"
        f"• Тарифы: 1ч - {WHATSAPP_RATES[1]}$, 2ч - {WHATSAPP_RATES[2]}$, 3ч - {WHATSAPP_RATES[3]}$\n\n"
        
        f"🔵 <b>MAX</b>\n"
        f"• Статус: {max_status}\n"
        f"• Очередь: {max_queue} номеров\n"
        f"• Тариф: {MAX_RATE}$ за 15 минут\n\n"
        
        f"💬 <b>SMS</b>\n"
        f"• Статус: {sms_status}\n"
        f"• Тариф: {SMS_RATE}$ за сообщение\n\n"
        
        f"💡 <b>Общая информация:</b>\n"
        f"• Работаем 24/7\n"
        f"• Автоматические выплаты\n"
        f"• Поддержка онлайн\n"
        f"• Реферальная система\n\n"
        
        f"📞 <b>Поддержка:</b> @support_username"
    )
    
    await callback.message.edit_text(text, reply_markup=back_to_main_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "support")
async def support_callback(callback: CallbackQuery, state: FSMContext):
    text = (
        "🛠 <b>Поддержка</b>\n\n"
        "💬 Опишите вашу проблему или вопрос. Мы ответим в течение 24 часов.\n\n"
        "📝 <b>В вашем сообщении укажите:</b>\n"
        "• Ваш ID: <code>{user_id}</code>\n"
        "• Сервис (WhatsApp/MAX/SMS)\n"
        "• Подробное описание проблемы\n\n"
        "❌ Для отмены нажмите кнопку ниже"
    ).format(user_id=callback.from_user.id)
    
    await callback.message.edit_text(text, reply_markup=cancel_keyboard())
    await state.set_state(Form.support_message)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("service:"))
async def service_callback(callback: CallbackQuery):
    service = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    service_status = get_service_status(service)
    
    if service_status != "активен":
        await callback.answer(f"❌ Сервис {service} временно не доступен", show_alert=True)
        return
    
    if service == "sms":
        text = f"💬 <b>SMS сервис</b>\n\nОтправляйте SMS сообщения и получайте {SMS_RATE}$ за каждое!\n\n📝 Процесс:\n1. Получаете задание\n2. Отправляете SMS\n3. Присылаете скриншот\n4. Получаете оплату"
        await callback.message.edit_text(text, reply_markup=sms_keyboard())
    else:
        service_name = "WhatsApp" if service == "whatsapp" else "MAX"
        rates = WHATSAPP_RATES if service == "whatsapp" else {1: MAX_RATE}
        rate_text = "\n".join([f"{hours}ч - {rate}$" for hours, rate in rates.items()]) if service == "whatsapp" else f"{MAX_RATE}$ за 15 минут"
        
        text = (
            f"{'📱' if service == 'whatsapp' else '🔵'} <b>{service_name} сервис</b>\n\n"
            f"💰 Тарифы:\n{rate_text}\n\n"
            f"📝 Процесс:\n"
            f"1. Добавляете номер\n"
            f"2. Получаете код\n"
            f"3. Вводите код\n"
            f"4. Получаете оплату"
        )
        await callback.message.edit_text(text, reply_markup=service_keyboard(service))
    
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("add_"))
async def add_service_callback(callback: CallbackQuery, state: FSMContext):
    service = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    service_status = get_service_status(service)
    if service_status != "активен":
        await callback.answer(f"❌ Сервис {service} временно не доступен", show_alert=True)
        return
    
    if service == "whatsapp":
        text = (
            "📱 <b>Добавление WhatsApp номера</b>\n\n"
            "📝 Введите номер телефона в международном формате:\n"
            "• Пример: +79123456789\n"
            "• Номер должен быть действительным\n"
            "• На номер должен быть установлен WhatsApp\n\n"
            "❌ Для отмены нажмите кнопку ниже"
        )
        await state.set_state(Form.whatsapp_phone)
    elif service == "max":
        text = (
            "🔵 <b>Добавление MAX номера</b>\n\n"
            "📝 Введите номер телефона в международном формате:\n"
            "• Пример: +79123456789\n"
            "• Номер должен быть действительным\n"
            "• На номер должен быть установлен MAX\n\n"
            "❌ Для отмены нажмите кнопку ниже"
        )
        await state.set_state(Form.max_phone)
    
    await callback.message.edit_text(text, reply_markup=cancel_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("my_"))
async def my_service_callback(callback: CallbackQuery):
    service = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        if service == "whatsapp":
            cursor.execute("""
                SELECT id, phone, status, hold_start, hold_hours 
                FROM whatsapp_numbers 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            """, (user_id,))
            numbers = cursor.fetchall()
            
            if not numbers:
                text = "📱 <b>Ваши WhatsApp номера</b>\n\nУ вас пока нет добавленных номеров."
            else:
                text = "📱 <b>Ваши WhatsApp номера</b>\n\n"
                for num_id, phone, status, hold_start, hold_hours in numbers:
                    status_emoji = "✅" if status == "completed" else "⏳" if status == "active" else "❌"
                    status_text = "завершен" if status == "completed" else "в процессе" if status == "active" else "отклонен"
                    text += f"{status_emoji} {phone} - {status_text}\n"
                    if hold_start and status == "active":
                        hold_end = datetime.fromisoformat(hold_start) + timedelta(hours=hold_hours)
                        text += f"   ⏰ До: {hold_end.strftime('%H:%M')}\n"
        
        elif service == "max":
            cursor.execute("""
                SELECT id, phone, status, hold_start 
                FROM max_numbers 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            """, (user_id,))
            numbers = cursor.fetchall()
            
            if not numbers:
                text = "🔵 <b>Ваши MAX номера</b>\n\nУ вас пока нет добавленных номеров."
            else:
                text = "🔵 <b>Ваши MAX номера</b>\n\n"
                for num_id, phone, status, hold_start in numbers:
                    status_emoji = "✅" if status == "completed" else "⏳" if status == "active" else "❌"
                    status_text = "завершен" if status == "completed" else "в процессе" if status == "active" else "отклонен"
                    text += f"{status_emoji} {phone} - {status_text}\n"
                    if hold_start and status == "active":
                        hold_end = datetime.fromisoformat(hold_start) + timedelta(minutes=15)
                        text += f"   ⏰ До: {hold_end.strftime('%H:%M')}\n"
        
        elif service == "sms":
            cursor.execute("""
                SELECT id, text, status, amount, created_at 
                FROM sms_works 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            """, (user_id,))
            works = cursor.fetchall()
            
            if not works:
                text = "💬 <b>Ваши SMS работы</b>\n\nУ вас пока нет отправленных работ."
            else:
                text = "💬 <b>Ваши SMS работы</b>\n\n"
                for work_id, work_text, status, amount, created_at in works:
                    status_emoji = "✅" if status == "completed" else "⏳" if status == "pending" else "❌"
                    status_text = "оплачено" if status == "completed" else "на проверке" if status == "pending" else "отклонено"
                    text += f"{status_emoji} {work_text[:30]}... - {amount}$ ({status_text})\n"
    
    await callback.message.edit_text(text, reply_markup=back_to_main_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "send_sms_work")
async def send_sms_work_callback(callback: CallbackQuery, state: FSMContext):
    text = (
        "💬 <b>Отправка SMS работы</b>\n\n"
        "📝 Введите текст SMS сообщения, которое вы отправили:\n\n"
        "💡 <b>Требования:</b>\n"
        "• Минимум 10 символов\n"
        "• Без ссылок и рекламы\n"
        "• Только текст\n\n"
        "❌ Для отмены нажмите кнопку ниже"
    )
    
    await callback.message.edit_text(text, reply_markup=cancel_keyboard())
    await state.set_state(Form.sms_text)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_panel")
async def admin_panel_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    await callback.message.edit_text("⚙️ <b>Админ-панель</b>", reply_markup=admin_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Общая статистика
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM users WHERE balance_usd > 0")
        active_users = cursor.fetchone()[0]
        
        cursor.execute("SELECT SUM(balance_usd) FROM users")
        total_balance = cursor.fetchone()[0] or 0
        
        # Статистика по сервисам
        cursor.execute("SELECT COUNT(*) FROM whatsapp_numbers WHERE status = 'completed'")
        completed_whatsapp = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM max_numbers WHERE status = 'completed'")
        completed_max = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM sms_works WHERE status = 'completed'")
        completed_sms = cursor.fetchone()[0]
        
        # Очереди
        cursor.execute("SELECT COUNT(*) FROM whatsapp_numbers WHERE status = 'active'")
        active_whatsapp = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM max_numbers WHERE status = 'active'")
        active_max = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM sms_works WHERE status = 'pending'")
        pending_sms = cursor.fetchone()[0]
    
    text = (
        f"📊 <b>Статистика системы</b>\n\n"
        
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего: {total_users}\n"
        f"• Активных: {active_users}\n"
        f"• Общий баланс: {total_balance:.2f}$\n\n"
        
        f"📈 <b>Выполнено работ:</b>\n"
        f"• WhatsApp: {completed_whatsapp}\n"
        f"• MAX: {completed_max}\n"
        f"• SMS: {completed_sms}\n\n"
        
        f"⏳ <b>Текущие очереди:</b>\n"
        f"• WhatsApp: {active_whatsapp}\n"
        f"• MAX: {active_max}\n"
        f"• SMS на проверке: {pending_sms}\n\n"
        
        f"🔄 <b>Статусы сервисов:</b>\n"
        f"• WhatsApp: {get_service_status('whatsapp')}\n"
        f"• MAX: {get_service_status('max')}\n"
        f"• SMS: {get_service_status('sms')}"
    )
    
    await callback.message.edit_text(text, reply_markup=admin_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_users")
async def admin_users_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, username, balance_usd, level, warnings 
            FROM users 
            ORDER BY created_at DESC 
            LIMIT 10
        """)
        users = cursor.fetchall()
    
    if not users:
        text = "👥 <b>Пользователи</b>\n\nНет зарегистрированных пользователей."
    else:
        text = "👥 <b>Последние 10 пользователей</b>\n\n"
        for user in users:
            user_id, username, balance, level, warnings = user
            username = username or "Без username"
            text += f"👤 {username} (ID: {user_id})\n"
            text += f"💰 {balance:.2f}$ | 📊 {level} ур. | ⚠️ {warnings}/3\n\n"
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📋 Все пользователи", callback_data="admin_all_users"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_whatsapp")
async def admin_whatsapp_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT wn.id, u.user_id, u.username, wn.phone, wn.status, wn.hold_start, wn.hold_hours
            FROM whatsapp_numbers wn
            LEFT JOIN users u ON wn.user_id = u.user_id
            WHERE wn.status = 'active'
            ORDER BY wn.created_at DESC
        """)
        numbers = cursor.fetchall()
    
    if not numbers:
        text = "📱 <b>Активные WhatsApp номера</b>\n\nНет активных номеров."
    else:
        text = "📱 <b>Активные WhatsApp номера</b>\n\n"
        for num_id, user_id, username, phone, status, hold_start, hold_hours in numbers:
            username = username or "Без username"
            text += f"📞 {phone} (👤 {username})\n"
            text += f"🆔 {num_id} | Статус: {status}\n"
            if hold_start:
                hold_end = datetime.fromisoformat(hold_start) + timedelta(hours=hold_hours)
                text += f"⏰ До: {hold_end.strftime('%H:%M')}\n"
            text += "\n"
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="✅ Завершенные", callback_data="admin_whatsapp_completed"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_max")
async def admin_max_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT mn.id, u.user_id, u.username, mn.phone, mn.status, mn.hold_start
            FROM max_numbers mn
            LEFT JOIN users u ON mn.user_id = u.user_id
            WHERE mn.status = 'active'
            ORDER BY mn.created_at DESC
        """)
        numbers = cursor.fetchall()
    
    if not numbers:
        text = "🔵 <b>Активные MAX номера</b>\n\nНет активных номеров."
    else:
        text = "🔵 <b>Активные MAX номера</b>\n\n"
        for num_id, user_id, username, phone, status, hold_start in numbers:
            username = username or "Без username"
            text += f"📞 {phone} (👤 {username})\n"
            text += f"🆔 {num_id} | Статус: {status}\n"
            if hold_start:
                hold_end = datetime.fromisoformat(hold_start) + timedelta(minutes=15)
                text += f"⏰ До: {hold_end.strftime('%H:%M')}\n"
            text += "\n"
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="✅ Завершенные", callback_data="admin_max_completed"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_sms")
async def admin_sms_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sw.id, u.user_id, u.username, sw.text, sw.status, sw.amount, sw.created_at
            FROM sms_works sw
            LEFT JOIN users u ON sw.user_id = u.user_id
            WHERE sw.status = 'pending'
            ORDER BY sw.created_at DESC
        """)
        works = cursor.fetchall()
    
    if not works:
        text = "💬 <b>SMS работы на проверке</b>\n\nНет работ на проверке."
    else:
        text = "💬 <b>SMS работы на проверке</b>\n\n"
        for work_id, user_id, username, work_text, status, amount, created_at in works:
            username = username or "Без username"
            text += f"📝 {work_text[:50]}...\n"
            text += f"👤 {username} | 💰 {amount}$\n"
            text += f"🆔 {work_id} | 📅 {created_at[:10]}\n\n"
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="✅ Завершенные", callback_data="admin_sms_completed"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_withdrawals")
async def admin_withdrawals_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT wr.id, u.user_id, u.username, wr.amount_usd, wr.status, wr.created_at
            FROM withdraw_requests wr
            LEFT JOIN users u ON wr.user_id = u.user_id
            WHERE wr.status = 'pending'
            ORDER BY wr.created_at DESC
        """)
        withdrawals = cursor.fetchall()
    
    if not withdrawals:
        text = "💳 <b>Заявки на вывод</b>\n\nНет pending заявок."
    else:
        text = "💳 <b>Заявки на вывод (pending)</b>\n\n"
        for w_id, user_id, username, amount, status, created_at in withdrawals:
            username = username or "Без username"
            text += f"👤 {username} (ID: {user_id})\n"
            text += f"💰 {amount:.2f}$ | 📅 {created_at[:10]}\n"
            text += f"🆔 {w_id} | Статус: {status}\n\n"
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="✅ Выполненные", callback_data="admin_withdrawals_completed"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_settings")
async def admin_settings_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    text = (
        "⚙️ <b>Настройки администратора</b>\n\n"
        f"👑 Главные админы: {', '.join(map(str, MAIN_ADMINS))}\n"
        f"👥 Админы: {', '.join(map(str, ADMIN_IDS))}\n\n"
        "💡 Выберите действие:"
    )
    
    await callback.message.edit_text(text, reply_markup=admin_settings_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_service_status")
async def admin_service_status_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    text = (
        "🔄 <b>Статусы сервисов</b>\n\n"
        f"📱 WhatsApp: {get_service_status('whatsapp')}\n"
        f"🔵 MAX: {get_service_status('max')}\n"
        f"💬 SMS: {get_service_status('sms')}\n\n"
        "Выберите сервис для изменения статуса:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📱 WhatsApp", callback_data="change_status:whatsapp"),
        types.InlineKeyboardButton(text="🔵 MAX", callback_data="change_status:max")
    )
    builder.row(
        types.InlineKeyboardButton(text="💬 SMS", callback_data="change_status:sms"),
        types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_settings")
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_broadcast_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    text = (
        "📢 <b>Рассылка сообщений</b>\n\n"
        "Введите сообщение для рассылки всем пользователям:\n\n"
        "💡 Поддерживается HTML разметка\n"
        "❌ Для отмены нажмите кнопку ниже"
    )
    
    await callback.message.edit_text(text, reply_markup=cancel_keyboard())
    await state.set_state(Form.admin_broadcast)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add")
async def admin_add_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_main_admin(user_id):
        await callback.answer("❌ Только главный администратор может добавлять админов.")
        return
    
    text = (
        "➕ <b>Добавление администратора</b>\n\n"
        "Введите ID пользователя, которого хотите сделать администратором:\n\n"
        "💡 ID можно получить с помощью команды /id\n"
        "❌ Для отмены нажмите кнопку ниже"
    )
    
    await callback.message.edit_text(text, reply_markup=cancel_keyboard())
    await state.set_state(Form.add_admin)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_remove")
async def admin_remove_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_main_admin(user_id):
        await callback.answer("❌ Только главный администратор может удалять админов.")
        return
    
    if not ADMIN_IDS:
        text = "➖ <b>Удаление администратора</b>\n\nНет дополнительных администраторов для удаления."
    else:
        text = "➖ <b>Удаление администратора</b>\n\nВыберите администратора для удаления:\n\n"
        for admin_id in ADMIN_IDS:
            try:
                admin_user = await bot.get_chat(admin_id)
                username = admin_user.username or "Без username"
                text += f"👤 {username} (ID: {admin_id})\n"
            except:
                text += f"👤 ID: {admin_id} (пользователь не найден)\n"
    
    builder = InlineKeyboardBuilder()
    for admin_id in ADMIN_IDS:
        builder.row(types.InlineKeyboardButton(
            text=f"❌ Удалить {admin_id}", 
            callback_data=f"remove_admin:{admin_id}"
        ))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_settings"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("remove_admin:"))
async def remove_admin_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_main_admin(user_id):
        await callback.answer("❌ Только главный администратор может удалять админов.")
        return
    
    admin_id_to_remove = int(callback.data.split(":")[1])
    
    if admin_id_to_remove in ADMIN_IDS:
        ADMIN_IDS.remove(admin_id_to_remove)
        save_admin_ids()
        text = f"✅ Администратор {admin_id_to_remove} удален."
    else:
        text = "❌ Администратор не найден в списке."
    
    await callback.answer(text, show_alert=True)
    await admin_remove_callback(callback)

@dp.callback_query(lambda c: c.data == "admin_service_status")
async def admin_service_status_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    text = (
        "🔄 <b>Изменение статуса сервисов</b>\n\n"
        "Текущие статусы:\n"
        f"• WhatsApp: {get_service_status('whatsapp')}\n"
        f"• MAX: {get_service_status('max')}\n"
        f"• SMS: {get_service_status('sms')}\n\n"
        "Выберите сервис для изменения статуса:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📱 WhatsApp", callback_data="change_status:whatsapp"),
        types.InlineKeyboardButton(text="🔵 MAX", callback_data="change_status:max")
    )
    builder.row(
        types.InlineKeyboardButton(text="💬 SMS", callback_data="change_status:sms"),
        types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_settings")
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("change_status:"))
async def change_status_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    service = callback.data.split(":")[1]
    current_status = get_service_status(service)
    
    text = (
        f"🔄 <b>Изменение статуса {service}</b>\n\n"
        f"Текущий статус: {current_status}\n\n"
        "Выберите новый статус:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✅ Активен", callback_data=f"set_status:{service}:активен"),
        types.InlineKeyboardButton(text="⛔ Неактивен", callback_data=f"set_status:{service}:неактивен")
    )
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_service_status"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("set_status:"))
async def set_status_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    _, service, status = callback.data.split(":")
    update_service_status(service, status)
    
    text = f"✅ Статус {service} изменен на: {status}"
    await callback.answer(text, show_alert=True)
    await admin_service_status_callback(callback)

@dp.callback_query(lambda c: c.data == "admin_warn")
async def admin_warn_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    text = (
        "⚠️ <b>Предупреждение пользователю</b>\n\n"
        "Введите ID пользователя, которого хотите предупредить:\n\n"
        "💡 ID можно получить с помощью команды /id\n"
        "❌ Для отмены нажмите кнопку ниже"
    )
    
    await callback.message.edit_text(text, reply_markup=cancel_keyboard())
    await state.set_state(Form.warn_user)
    await callback.answer()

# Обработчики состояний
@dp.message(Form.whatsapp_phone)
async def process_whatsapp_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    user_id = message.from_user.id
    
    if not validate_phone(phone):
        await message.answer(
            "❌ Неверный формат номера. Используйте международный формат:\n"
            "• Пример: +79123456789\n\n"
            "Попробуйте еще раз:",
            reply_markup=cancel_keyboard()
        )
        return
    
    # Проверяем, не добавлен ли уже этот номер
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM whatsapp_numbers WHERE phone = ?", (phone,))
        if cursor.fetchone():
            await message.answer(
                "❌ Этот номер уже добавлен в систему.\n\n"
                "Введите другой номер:",
                reply_markup=cancel_keyboard()
            )
            return
    
    # Выбираем тариф
    builder = InlineKeyboardBuilder()
    for hours, rate in WHATSAPP_RATES.items():
        builder.row(types.InlineKeyboardButton(
            text=f"{hours} час - {rate}$", 
            callback_data=f"whatsapp_rate:{hours}:{rate}"
        ))
    builder.row(types.InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    
    await state.update_data(phone=phone)
    await message.answer(
        f"📱 <b>Номер принят:</b> {phone}\n\n"
        "Выберите тариф (время удержания номера):",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(lambda c: c.data.startswith("whatsapp_rate:"))
async def whatsapp_rate_callback(callback: CallbackQuery, state: FSMContext):
    _, hours, rate = callback.data.split(":")
    hours = int(hours)
    rate = float(rate)
    user_id = callback.from_user.id
    
    data = await state.get_data()
    phone = data.get('phone')
    
    if not phone:
        await callback.answer("❌ Ошибка: номер не найден")
        return
    
    # Добавляем номер в базу
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO whatsapp_numbers (user_id, phone, hold_hours, status)
            VALUES (?, ?, ?, 'pending')
        """, (user_id, phone, hours))
        conn.commit()
    
    # Уведомляем администраторов
    admin_message = (
        f"📱 <b>Новый WhatsApp номер</b>\n\n"
        f"👤 Пользователь: @{callback.from_user.username or 'Без username'} (ID: {user_id})\n"
        f"📞 Номер: {phone}\n"
        f"⏰ Время: {hours} часов\n"
        f"💰 Стоимость: {rate}$\n\n"
        f"🆔 ID записи: {cursor.lastrowid}"
    )
    
    await notify_admins(admin_message)
    
    await callback.message.edit_text(
        f"✅ <b>Номер добавлен!</b>\n\n"
        f"📞 {phone}\n"
        f"⏰ {hours} часов\n"
        f"💰 {rate}$\n\n"
        f"📋 Номер поставлен в очередь. Ожидайте код подтверждения от администратора."
    )
    
    await state.clear()
    await callback.answer()

@dp.message(Form.max_phone)
async def process_max_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    user_id = message.from_user.id
    
    if not validate_phone(phone):
        await message.answer(
            "❌ Неверный формат номера. Используйте международный формат:\n"
            "• Пример: +79123456789\n\n"
            "Попробуйте еще раз:",
            reply_markup=cancel_keyboard()
        )
        return
    
    # Проверяем, не добавлен ли уже этот номер
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM max_numbers WHERE phone = ?", (phone,))
        if cursor.fetchone():
            await message.answer(
                "❌ Этот номер уже добавлен в систему.\n\n"
                "Введите другой номер:",
                reply_markup=cancel_keyboard()
            )
            return
    
    # Добавляем номер в базу
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO max_numbers (user_id, phone, status)
            VALUES (?, ?, 'pending')
        """, (user_id, phone))
        conn.commit()
    
    # Уведомляем администраторов
    admin_message = (
        f"🔵 <b>Новый MAX номер</b>\n\n"
        f"👤 Пользователь: @{message.from_user.username or 'Без username'} (ID: {user_id})\n"
        f"📞 Номер: {phone}\n"
        f"💰 Стоимость: {MAX_RATE}$\n\n"
        f"🆔 ID записи: {cursor.lastrowid}"
    )
    
    await notify_admins(admin_message)
    
    await message.answer(
        f"✅ <b>Номер добавлен!</b>\n\n"
        f"📞 {phone}\n"
        f"💰 {MAX_RATE}$\n\n"
        f"📋 Номер поставлен в очередь. Ожидайте код подтверждения от администратора."
    )
    
    await state.clear()

@dp.message(Form.sms_text)
async def process_sms_text(message: Message, state: FSMContext):
    text = message.text.strip()
    user_id = message.from_user.id
    
    if len(text) < 10:
        await message.answer(
            "❌ Текст слишком короткий. Минимум 10 символов.\n\n"
            "Введите текст сообщения еще раз:",
            reply_markup=cancel_keyboard()
        )
        return
    
    if any(url in text for url in ['http://', 'https://', 'www.', '.com', '.ru']):
        await message.answer(
            "❌ Текст содержит ссылки. Это запрещено.\n\n"
            "Введите текст без ссылок:",
            reply_markup=cancel_keyboard()
        )
        return
    
    await state.update_data(sms_text=text)
    await message.answer(
        f"📝 <b>Текст принят:</b>\n{text[:100]}...\n\n"
        "Теперь отправьте скриншот отправленного SMS сообщения:",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(Form.sms_proof)

@dp.message(Form.sms_proof)
async def process_sms_proof(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    if not message.photo:
        await message.answer(
            "❌ Это не изображение. Отправьте скриншот SMS сообщения:",
            reply_markup=cancel_keyboard()
        )
        return
    
    data = await state.get_data()
    sms_text = data.get('sms_text')
    
    # Сохраняем фото
    photo = message.photo[-1]
    photo_file = await bot.get_file(photo.file_id)
    photo_path = TEMP_DIR / f"sms_proof_{user_id}_{int(time.time())}.jpg"
    
    await bot.download_file(photo_file.file_path, photo_path)
    
    # Добавляем работу в базу
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sms_works (user_id, text, proof_photo, status, amount)
            VALUES (?, ?, ?, 'pending', ?)
        """, (user_id, sms_text, str(photo_path), SMS_RATE))
        conn.commit()
    
    # Уведомляем администраторов
    admin_message = (
        f"💬 <b>Новая SMS работа</b>\n\n"
        f"👤 Пользователь: @{message.from_user.username or 'Без username'} (ID: {user_id})\n"
        f"📝 Текст: {sms_text[:50]}...\n"
        f"💰 Стоимость: {SMS_RATE}$\n\n"
        f"🆔 ID работы: {cursor.lastrowid}"
    )
    
    await notify_admins(admin_message)
    
    await message.answer(
        f"✅ <b>Работа отправлена на проверку!</b>\n\n"
        f"📝 Текст: {sms_text[:50]}...\n"
        f"💰 Стоимость: {SMS_RATE}$\n\n"
        f"📋 Ожидайте проверки администратора."
    )
    
    await state.clear()

@dp.message(Form.support_message)
async def process_support_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    support_text = message.text
    
    # Сохраняем обращение в базу
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO support_tickets (user_id, message, status)
            VALUES (?, ?, 'open')
        """, (user_id, support_text))
        conn.commit()
    
    # Уведомляем администраторов
    admin_message = (
        f"🛠 <b>Новое обращение в поддержку</b>\n\n"
        f"👤 Пользователь: @{message.from_user.username or 'Без username'} (ID: {user_id})\n"
        f"📝 Сообщение:\n{support_text}\n\n"
        f"🆔 ID обращения: {cursor.lastrowid}"
    )
    
    await notify_admins(admin_message)
    
    await message.answer(
        "✅ <b>Ваше обращение принято!</b>\n\n"
        "Мы ответим вам в течение 24 часов.\n"
        "Спасибо за обращение!",
        reply_markup=back_to_main_keyboard()
    )
    
    await state.clear()

@dp.message(Form.admin_broadcast)
async def process_admin_broadcast(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("❌ У вас нет прав администратора.")
        await state.clear()
        return
    
    broadcast_text = message.text
    
    # Подтверждение рассылки
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✅ Да, отправить", callback_data=f"confirm_broadcast:{message.message_id}"),
        types.InlineKeyboardButton(text="❌ Нет, отменить", callback_data="admin_panel")
    )
    
    await state.update_data(broadcast_text=broadcast_text)
    await message.answer(
        f"📢 <b>Подтверждение рассылки</b>\n\n"
        f"Сообщение:\n{broadcast_text}\n\n"
        f"Отправить всем пользователям?",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(lambda c: c.data.startswith("confirm_broadcast:"))
async def confirm_broadcast_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ У вас нет прав администратора.")
        return
    
    message_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    broadcast_text = data.get('broadcast_text')
    
    if not broadcast_text:
        await callback.answer("❌ Текст рассылки не найден")
        return
    
    # Получаем всех пользователей
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = cursor.fetchall()
    
    total_users = len(users)
    successful = 0
    failed = 0
    
    await callback.message.edit_text(f"📢 <b>Начата рассылка...</b>\n\nПользователей: {total_users}")
    
    # Рассылаем сообщения
    for user in users:
        try:
            await bot.send_message(user[0], broadcast_text, parse_mode=ParseMode.HTML)
            successful += 1
        except Exception as e:
            failed += 1
        await asyncio.sleep(0.1)  # Чтобы не превысить лимиты Telegram
    
    await callback.message.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"📊 Статистика:\n"
        f"• Всего пользователей: {total_users}\n"
        f"• Успешно: {successful}\n"
        f"• Не удалось: {failed}"
    )
    
    await state.clear()
    await callback.answer()

@dp.message(Form.add_admin)
async def process_add_admin(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_main_admin(user_id):
        await message.answer("❌ Только главный администратор может добавлять админов.")
        await state.clear()
        return
    
    try:
        new_admin_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ Неверный формат ID. ID должен быть числом.\n\n"
            "Введите ID пользователя:",
            reply_markup=cancel_keyboard()
        )
        return
    
    if new_admin_id in ADMIN_IDS:
        await message.answer(
            "❌ Этот пользователь уже является администратором.\n\n"
            "Введите другой ID:",
            reply_markup=cancel_keyboard()
        )
        return
    
    if new_admin_id in MAIN_ADMINS:
        await message.answer(
            "❌ Этот пользователь уже является главным администратором.\n\n"
            "Введите другой ID:",
            reply_markup=cancel_keyboard()
        )
        return
    
    # Проверяем, существует ли пользователь
    try:
        user = await bot.get_chat(new_admin_id)
        ADMIN_IDS.append(new_admin_id)
        save_admin_ids()
        
        await message.answer(
            f"✅ Пользователь {user.first_name} (@{user.username or 'Без username'}) добавлен в администраторы."
        )
        
        # Уведомляем нового админа
        try:
            await bot.send_message(
                new_admin_id,
                "🎉 <b>Вас назначили администратором!</b>\n\n"
                "Теперь у вас есть доступ к админ-панели бота.",
                parse_mode=ParseMode.HTML
            )
        except:
            pass
            
    except Exception as e:
        await message.answer(
            f"❌ Не удалось найти пользователя с ID {new_admin_id}.\n\n"
            "Введите корректный ID:",
            reply_markup=cancel_keyboard()
        )
        return
    
    await state.clear()

@dp.message(Form.warn_user)
async def process_warn_user(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("❌ У вас нет прав администратора.")
        await state.clear()
        return
    
    try:
        target_user_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ Неверный формат ID. ID должен быть числом.\n\n"
            "Введите ID пользователя:",
            reply_markup=cancel_keyboard()
        )
        return
    
    if target_user_id == user_id:
        await message.answer(
            "❌ Нельзя предупредить самого себя.\n\n"
            "Введите ID другого пользователя:",
            reply_markup=cancel_keyboard()
        )
        return
    
    # Проверяем, существует ли пользователь
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT warnings FROM users WHERE user_id = ?", (target_user_id,))
        user_data = cursor.fetchone()
        
        if not user_data:
            await message.answer(
                f"❌ Пользователь с ID {target_user_id} не найден.\n\n"
                "Введите корректный ID:",
                reply_markup=cancel_keyboard()
            )
            return
        
        current_warnings = user_data[0]
        new_warnings = current_warnings + 1
        
        if new_warnings >= 3:
            # Блокируем пользователя
            cursor.execute("UPDATE users SET warnings = 3 WHERE user_id = ?", (target_user_id,))
            text = f"✅ Пользователь {target_user_id} заблокирован (3/3 предупреждений)."
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    target_user_id,
                    "🚫 <b>Вы были заблокированы!</b>\n\n"
                    "Вы получили 3 предупреждения и больше не можете пользоваться ботом.",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
        else:
            cursor.execute("UPDATE users SET warnings = ? WHERE user_id = ?", (new_warnings, target_user_id))
            text = f"✅ Пользователь {target_user_id} предупрежден. Текущее количество: {new_warnings}/3"
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    target_user_id,
                    f"⚠️ <b>Вы получили предупреждение!</b>\n\n"
                    f"Текущее количество предупреждений: {new_warnings}/3\n"
                    f"При достижении 3 предупреждений аккаунт будет заблокирован.",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
        
        conn.commit()
    
    await message.answer(text)
    await state.clear()

@dp.message(Form.add_balance)
async def process_add_balance(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    try:
        amount = float(message.text.strip())
        if amount < 1.0:
            await message.answer(
                "❌ Минимальная сумма вывода: 1$\n\n"
                "Введите сумму еще раз:",
                reply_markup=cancel_keyboard()
            )
            return
        
        # Проверяем баланс
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT balance_usd FROM users WHERE user_id = ?", (user_id,))
            balance = cursor.fetchone()[0]
            
            if amount > balance:
                await message.answer(
                    f"❌ Недостаточно средств. Доступно: {balance:.2f}$\n\n"
                    "Введите меньшую сумму:",
                    reply_markup=cancel_keyboard()
                )
                return
            
            # Создаем заявку на вывод
            amount_rub = usd_to_rub(amount)
            cursor.execute("""
                INSERT INTO withdraw_requests (user_id, amount_usd, amount_rub, status)
                VALUES (?, ?, ?, 'pending')
            """, (user_id, amount, amount_rub))
            conn.commit()
        
        # Уведомляем администраторов
        admin_message = (
            f"💳 <b>Новая заявка на вывод</b>\n\n"
            f"👤 Пользователь: @{message.from_user.username or 'Без username'} (ID: {user_id})\n"
            f"💰 Сумма: {amount:.2f}$ ({amount_rub:.2f}₽)\n\n"
            f"🆔 ID заявки: {cursor.lastrowid}"
        )
        
        await notify_admins(admin_message)
        
        await message.answer(
            f"✅ <b>Заявка на вывод создана!</b>\n\n"
            f"💰 Сумма: {amount:.2f}$\n"
            f"📋 Статус: на рассмотрении\n\n"
            f"Обработка заявки занимает до 24 часов."
        )
        
    except ValueError:
        await message.answer(
            "❌ Неверный формат суммы. Введите число:\n\n"
            "Пример: 5.50",
            reply_markup=cancel_keyboard()
        )
        return
    
    await state.clear()

# Обработка текстовых сообщений
@dp.message()
async def handle_text(message: Message):
    user_id = message.from_user.id
    
    # Проверяем, есть ли пользователь в базе
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT referral_source FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone()
        
        if not user_data:
            # Если пользователя нет в базе, отправляем приветствие
            await cmd_start(message, None)
            return
        
        if not user_data[0]:
            # Если источник не указан, просим выбрать
            builder = InlineKeyboardBuilder()
            builder.row(
                types.InlineKeyboardButton(text="WaCash", callback_data="source:wacash"),
                types.InlineKeyboardButton(text="WhatsApp Dealers", callback_data="source:whatsappdealers")
            )
            
            await message.answer(
                "👋 <b>Добро пожаловать!</b>\n\n"
                "Пожалуйста, выберите источник, откуда вы о нас узнали:",
                reply_markup=builder.as_markup()
            )
            return
    
    # Если пользователь есть и источник указан, показываем главное меню
    menu_text, reply_markup = await main_menu(user_id)
    await message.answer(menu_text, reply_markup=reply_markup)

# Обработка ошибок
@dp.errors()
async def errors_handler(update: Update, exception: Exception):
    logger.error(f"Ошибка при обработке update {update}: {exception}")
    return True

# Запуск бота
async def main():
    # Инициализация базы данных
    init_db()
    
    # Запуск бота
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
