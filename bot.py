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
from io import BytesIO

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
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id]
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN")
CRYPTOBOT_ASSET = "USDT"
CRYPTOBOT_URL = "https://pay.crypt.bot/"
DATABASE = "multi_service_bot.db"
EXCHANGE_RATE = 90  # Курс доллара к рублю
REFERRAL_BONUS = 0.1  # 0.1$ за приглашение
PROCESSING_DELAY = 3600  # 1 час для автоподтверждения заявок
MAX_CHECKS_PER_BATCH = 50  # Максимум чеков за одну обработку

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
    crypto_pay_amount = State()
    withdraw_request = State()
    process_withdrawals = State()
    admin_message_text = State()

# Вспомогательные функции
def is_main_admin(user_id: int) -> bool:
    return user_id in MAIN_ADMINS

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in MAIN_ADMINS

def save_admin_ids():
    """Сохраняет ID администраторов в .env файл"""
    env_path = Path('.env')
    
    # Читаем существующий файл или создаем новый
    if env_path.exists():
        env_content = env_path.read_text()
    else:
        env_content = ''
    
    lines = env_content.split('\n')
    new_lines = []
    admin_ids_line = f"ADMIN_IDS={','.join(map(str, ADMIN_IDS))}"
    
    found = False
    for line in lines:
        if line.startswith('ADMIN_IDS='):
            new_lines.append(admin_ids_line)
            found = True
        elif line.strip():  # Пропускаем пустые строки
            new_lines.append(line)
    
    if not found:
        new_lines.append(admin_ids_line)
    
    # Добавляем пустую строку в конце, если ее нет
    if new_lines and new_lines[-1].strip():
        new_lines.append('')
    
    env_path.write_text('\n'.join(new_lines))
    logger.info(f"Admin IDs saved: {ADMIN_IDS}")

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

# Функции для работы с CryptoPay
async def create_crypto_pay_invoice(amount_usd: float, description: str = "Пополнение баланса бота") -> dict:
    """Создает инвойс в CryptoPay"""
    try:
        headers = {
            "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
            "Content-Type": "application/json"
        }
        
        data = {
            "asset": CRYPTOBOT_ASSET,
            "amount": str(amount_usd),
            "description": description,
            "hidden_message": "Пополнение баланса бота",
            "paid_btn_name": "viewItem",
            "paid_btn_url": "https://t.me/your_bot",
            "payload": "admin_topup",
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 3600  # 1 час
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CRYPTOBOT_URL}api/createInvoice",
                headers=headers,
                json=data
            ) as response:
                result = await response.json()
                if result.get("ok"):
                    return result.get("result")
                else:
                    logger.error(f"CryptoPay error: {result}")
                    return None
                    
    except Exception as e:
        logger.error(f"Error creating CryptoPay invoice: {e}")
        return None

async def create_cryptopay_check(user_id: int, amount: float, description: str):
    """Создает чек в CryptoPay для выплаты"""
    try:
        headers = {
            "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
            "Content-Type": "application/json"
        }
        
        payload = {
            "asset": CRYPTOBOT_ASSET,
            "amount": str(amount),
            "description": f"Выплата пользователю {description} (ID: {user_id})",
            "payload": str(user_id),
            "public_key": True
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CRYPTOBOT_URL}api/createCheck", 
                headers=headers, 
                json=payload
            ) as resp:
                data = await resp.json()
                
                if data.get("ok"):
                    check = data["result"]
                    return check["check_id"], check.get("url") or check.get("bot_check_url"), check.get("expires_at", "")
        
        return None, None, None
        
    except Exception as e:
        logger.error(f"Error creating CryptoPay check: {e}")
        return None, None, None

async def process_withdrawals_batch(admin_id: int):
    """Обрабатывает выплаты партиями по 50 чеков"""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Получаем подтвержденные заявки на вывод
        cursor.execute("""
            SELECT wr.id, wr.user_id, wr.amount_usd, u.username
            FROM withdraw_requests wr
            JOIN users u ON wr.user_id = u.user_id
            WHERE wr.status = 'confirmed' AND wr.invoice_id IS NULL
            ORDER BY wr.created_at ASC
            LIMIT ?
        """, (MAX_CHECKS_PER_BATCH,))
        
        pending_requests = cursor.fetchall()
        
        if not pending_requests:
            await bot.send_message(admin_id, "❌ Нет подтвержденных заявок на вывод.")
            return 0, 0
        
        processed_count = 0
        failed_count = 0
        
        for request in pending_requests:
            request_id, user_id, amount_usd, username = request
            
            try:
                # Создаем чек в CryptoPay
                check_id, check_url, expires_at = await create_cryptopay_check(
                    user_id, amount_usd, username or str(user_id))
                
                if check_id and check_url:
                    # Обновляем запись в базе
                    cursor.execute("""
                        UPDATE withdraw_requests 
                        SET status = 'paid', 
                            invoice_id = ?,
                            invoice_url = ?,
                            expires_at = ?,
                            confirmed_at = datetime('now'),
                            paid_at = datetime('now')
                        WHERE id = ?
                    """, (check_id, check_url, expires_at, request_id))
                    
                    processed_count += 1
                    
                    # Отправляем чек пользователю
                    try:
                        await bot.send_message(
                            user_id,
                            f"💰 Ваша выплата {amount_usd:.2f}$ готова!\n\n"
                            f"🔗 Ссылка на чек: {check_url}\n"
                            f"⏰ Действителен до: {expires_at}"
                        )
                    except Exception as e:
                        logger.error(f"Error sending check to user {user_id}: {e}")
                else:
                    failed_count += 1
                    
            except Exception as e:
                logger.error(f"Error processing request {request_id}: {e}")
                failed_count += 1
        
        conn.commit()
        
        return processed_count, failed_count

async def confirm_withdraw_request(user_id: int, amount: float):
    """Автоматическое подтверждение заявки через 1 час"""
    await asyncio.sleep(PROCESSING_DELAY)
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Проверяем статус заявки
        cursor.execute("""
            SELECT status FROM withdraw_requests 
            WHERE user_id = ? AND amount_usd = ? AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
        """, (user_id, amount))
        
        result = cursor.fetchone()
        if result:
            # Подтверждаем заявку
            cursor.execute("""
                UPDATE withdraw_requests 
                SET status = 'confirmed', confirmed_at = datetime('now')
                WHERE user_id = ? AND amount_usd = ? AND status = 'pending'
            """, (user_id, amount))
            
            # Начисляем опыт за заявку
            cursor.execute("""
                UPDATE users 
                SET level = level + 1 
                WHERE user_id = ?
            """, (user_id,))
            
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id, 
                    f"✅ Ваша заявка на вывод {amount:.2f}$ подтверждена!\n\n"
                    f"Ожидайте выплату в течение 24 часов."
                )
            except Exception as e:
                logger.error(f"Error notifying user: {e}")
            
            # Обрабатываем реферальные выплаты
            await process_referral_payout(user_id, amount)

async def process_referral_payout(user_id: int, amount: float):
    """Обработка реферальных процентов при выводе"""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Получаем реферера пользователя
        cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        
        if result and result[0]:
            referrer_id = result[0]
            
            # Расчет реферального процента (5%)
            referral_amount = round(amount * 0.05, 2)
            
            if referral_amount > 0:
                # Начисляем реферальное вознаграждение
                cursor.execute("""
                    UPDATE users 
                    SET balance_usd = balance_usd + ?
                    WHERE user_id = ?
                """, (referral_amount, referrer_id))
                
                conn.commit()
                
                # Уведомляем реферера
                try:
                    await bot.send_message(
                        referrer_id,
                        f"💸 Реферальное вознаграждение! {referral_amount:.2f}$"
                    )
                except Exception as e:
                    logger.error(f"Error notifying referrer: {e}")

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
            captcha_passed INTEGER DEFAULT 1,
            registration_date TEXT DEFAULT CURRENT_TIMESTAMP
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
            paid_at DATETIME,
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
            username TEXT,
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
            ('users', 'captcha_passed'),
            ('users', 'registration_date'),
            ('withdraw_requests', 'paid_at'),
            ('support_tickets', 'username')
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
        f"╚ Ваши номера в очереди: {whatsapp_count}\n\n"
        
        f"╔ <b>MAX (9$/15мин)</b>\n"
        f"╚ Статус работы: {max_status}\n\n"
        
        f"╔ <b>SMS WORK (0.3$/1sms)</b>\n"
        f"╚ Статус работы: {sms_status}\n\n"
        
        f"╔ <b>Баланс:</b> {balance_usd:.2f}$ ({balance_rub:.2f}₽)\n"
        f"╠ <b>Уровень:</b> {level}\n"
        f"╚ <b>Предупреждения:</b> {warnings}/5\n\n"
    )
    
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(text="☎️ Сдать WhatsApp", callback_data="add_whatsapp"),
        types.InlineKeyboardButton(text="🤖 Сдать MAX", callback_data="add_max")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="💬 SMS WORK", callback_data="sms_work_menu"),
        types.InlineKeyboardButton(text="💰 Вывод средств", callback_data="withdraw_request")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="⚠️ Поддержка", callback_data="support"),
        types.InlineKeyboardButton(text="🤵‍♂️ Профиль", callback_data="profile")
    )
    
    if is_admin(user_id):
        builder.row(
            types.InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_panel")
        )
    
    return menu_text, builder.as_markup()

async def whatsapp_code_keyboard(account_id: int):
    """Клавиатура для подтверждения входа в WhatsApp"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Я вошёл",
            callback_data=f"whatsapp_entered:{account_id}"
        ),
        types.InlineKeyboardButton(
            text="❌ Не могу войти",
            callback_data=f"whatsapp_failed:{account_id}"
        )
    )
    
    return builder.as_markup()

async def max_user_code_keyboard(account_id: int):
    """Клавиатура для отправки кода MAX пользователем"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="📨 Отправить код администратору",
            callback_data=f"send_max_user_code:{account_id}"
        )
    )
    
    return builder.as_markup()

async def whatsapp_admin_keyboard(account_id: int):
    """Клавиатура для администратора WhatsApp"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="📨 Отправить код",
            callback_data=f"send_whatsapp_code:{account_id}"
        )
    )
    
    builder.row(
        types.InlineKeyboardButton(
            text="❌ Отметить слетевшим",
            callback_data=f"mark_failed:{account_id}"
        ),
        types.InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=f"reject_whatsapp:{account_id}"
        )
    )
    
    return builder.as_markup()

async def whatsapp_admin_confirm_keyboard(account_id: int):
    """Клавиатура для подтверждения холда WhatsApp администратором"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Активировать холд",
            callback_data=f"confirm_whatsapp_hold:{account_id}"
        ),
        types.InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"reject_whatsapp:{account_id}"
        )
    )
    
    return builder.as_markup()

async def max_admin_keyboard(account_id: int):
    """Клавиатура для администратора MAX"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Принять",
            callback_data=f"accept_max:{account_id}"
        ),
        types.InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"reject_max:{account_id}"
        )
    )
    
    return builder.as_markup()

async def max_admin_code_keyboard(account_id: int):
    """Клавиатура для администратора после получения кода MAX"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Вошёл (начать холд)",
            callback_data=f"max_entered:{account_id}"
        ),
        types.InlineKeyboardButton(
            text="❌ Не вошёл (Заново)",
            callback_data=f"max_failed:{account_id}"
        )
    )
    
    return builder.as_markup()

async def sms_work_menu_keyboard():
    """Клавиатура меню SMS WORK"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="📨 Отправить заявку",
            callback_data="add_sms"
        )
    )
    
    builder.row(
        types.InlineKeyboardButton(
            text="🔙 Вернуться в главное меню",
            callback_data="back_to_menu"
        )
    )
    
    return builder.as_markup()

async def sms_work_active_keyboard(work_id: int):
    """Клавиатура для активной SMS работы"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Завершить SMS WORK",
            callback_data=f"sms_complete:{work_id}"
        )
    )
    
    return builder.as_markup()

async def sms_work_accept_keyboard(work_id: int):
    """Клавиатура для принятия рабочего сообщения SMS"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Принять",
            callback_data=f"sms_accept:{work_id}"
        ),
        types.InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"sms_reject:{work_id}"
        )
    )
    
    return builder.as_markup()

async def sms_proof_keyboard(work_id: int):
    """Клавиатура для отправки доказательств SMS работы"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="📸 Отправить доказательства",
            callback_data=f"sms_send_proof:{work_id}"
        )
    )
    
    return builder.as_markup()

async def sms_admin_keyboard(work_id: int):
    """Клавиатура для администратора SMS работы"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Принять",
            callback_data=f"sms_accept:{work_id}"
        ),
        types.InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"sms_reject:{work_id}"
        )
    )
    
    return builder.as_markup()

async def sms_admin_proof_keyboard(work_id: int):
    """Клавиатура для подтверждения доказательств SMS работы администратором"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Принять",
            callback_data=f"sms_confirm_proof:{work_id}"
        ),
        types.InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"sms_reject_proof:{work_id}"
        )
    )
    
    return builder.as_markup()

async def copy_message_keyboard(text: str):
    """Клавиатура для копирования сообщения"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(
            text="📋 Скопировать сообщение",
            callback_data=f"copy_text:{text[:50]}..."
        )
    )
    
    return builder.as_markup()

async def admin_panel_keyboard():
    builder = InlineKeyboardBuilder()
    
    builder.row(
        types.InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"),
        types.InlineKeyboardButton(text="✉️ Сообщение", callback_data="admin_message")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="🔄 Статус сервисов", callback_data="admin_status"),
        types.InlineKeyboardButton(text="👥 Добавить админа", callback_data="admin_add")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="💰 Выплаты", callback_data="admin_payouts"),
        types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="⚠️ Выдать предупреждение", callback_data="admin_warn"),
        types.InlineKeyboardButton(text="💵 Пополнить баланс", callback_data="admin_add_balance")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="📱 WhatsApp аккаунты", callback_data="admin_whatsapp_accounts"),
        types.InlineKeyboardButton(text="🤖 MAX аккаунты", callback_data="admin_max_accounts")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="💬 SMS работы", callback_data="admin_sms_works")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="⏰ Активные холды", callback_data="admin_active_hold"),
        types.InlineKeyboardButton(text="🚨 Сообщить о слёте", callback_data="admin_report_failed")
    )
    
    builder.row(
        types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")
    )
    
    return builder.as_markup()

async def whatsapp_accounts_keyboard():
    """Клавиатура для выбора WhatsApp аккаунтов"""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, phone, status 
            FROM whatsapp_numbers 
            WHERE status = 'pending' 
            ORDER BY created_at DESC
        """)
        accounts = cursor.fetchall()
    
    builder = InlineKeyboardBuilder()
    
    for account in accounts:
        account_id, phone, status = account
        builder.row(
            types.InlineKeyboardButton(
                text=f"📱 {phone}",
                callback_data=f"whatsapp_account:{account_id}"
            )
        )
    
    builder.row(
        types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")
    )
    
    return builder.as_markup()

async def max_accounts_keyboard():
    """Клавиатура для выбора MAX аккаунтов"""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, phone, status 
            FROM max_numbers 
            WHERE status = 'pending' 
            ORDER BY created_at DESC
        """)
        accounts = cursor.fetchall()
    
    builder = InlineKeyboardBuilder()
    
    for account in accounts:
        account_id, phone, status = account
        builder.row(
            types.InlineKeyboardButton(
                text=f"🤖 {phone}",
                callback_data=f"max_account:{account_id}"
            )
        )
    
    builder.row(
        types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")
    )
    
    return builder.as_markup()

async def sms_works_keyboard():
    """Клавиатура для выбора SMS работ"""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, text, status 
            FROM sms_works 
            WHERE status = 'pending' 
            ORDER BY created_at DESC
        """)
        works = cursor.fetchall()
    
    builder = InlineKeyboardBuilder()
    
    for work in works:
        work_id, user_id, text, status = work
        display_text = str(text)[:20] + "..." if text else "Без текста..."
        builder.row(
            types.InlineKeyboardButton(
                text=f"💬 {display_text}",
                callback_data=f"sms_work:{work_id}"
            )
        )
    
    builder.row(
        types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")
    )
    
    return builder.as_markup()

async def failed_accounts_keyboard():
    """Клавиатура для выбора аккаунтов для отметки о слёте"""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # WhatsApp аккаунты на холде
        cursor.execute("""
            SELECT id, phone, status 
            FROM whatsapp_numbers 
            WHERE status IN ('hold_active', 'active')
            ORDER BY hold_start DESC
        """)
        whatsapp_accounts = cursor.fetchall()
        
        # MAX аккаунты на холде
        cursor.execute("""
            SELECT id, phone, status 
            FROM max_numbers 
            WHERE status = 'active'
            ORDER BY hold_start DESC
        """)
        max_accounts = cursor.fetchall()
    
    builder = InlineKeyboardBuilder()
    
    # WhatsApp аккаунты
    for account in whatsapp_accounts:
        account_id, phone, status = account
        builder.row(
            types.InlineKeyboardButton(
                text=f"📱 {phone} ({status})",
                callback_data=f"report_failed_whatsapp:{account_id}"
            )
        )
    
    # MAX аккаунты
    for account in max_accounts:
        account_id, phone, status = account
        builder.row(
            types.InlineKeyboardButton(
                text=f"🤖 {phone}",
                callback_data=f"report_failed_max:{account_id}"
            )
        )
    
    if not whatsapp_accounts and not max_accounts:
        builder.row(
            types.InlineKeyboardButton(
                text="❌ Нет активных аккаунтов",
                callback_data="no_accounts"
            )
        )
    
    builder.row(
        types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")
    )
    
    return builder.as_markup()

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    # Проверяем, есть ли реферальный параметр
    if len(message.text.split()) > 1:
        referrer_id = message.text.split()[1]
        try:
            referrer_id = int(referrer_id)
        except ValueError:
            referrer_id = None
    else:
        referrer_id = None
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Проверяем, существует ли пользователь
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cursor.fetchone():
            # Добавляем нового пользователя
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, referrer_id)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, username, first_name, last_name, referrer_id))
            
            # Если есть реферер, добавляем запись в таблицу рефералов
            if referrer_id:
                try:
                    cursor.execute("""
                        INSERT INTO referrals (referrer_id, referred_id)
                        VALUES (?, ?)
                    """, (referrer_id, user_id))
                    
                    # Начисляем бонус рефереру
                    cursor.execute("""
                        UPDATE users 
                        SET balance_usd = balance_usd + ?
                        WHERE user_id = ?
                    """, (REFERRAL_BONUS, referrer_id))
                    
                    # Уведомляем реферера
                    try:
                        await bot.send_message(
                            referrer_id,
                            f"🎉 По вашей ссылке зарегистрировался новый пользователь!\n"
                            f"Вам начислен бонус: {REFERRAL_BONUS}$"
                        )
                    except:
                        pass
                        
                except sqlite3.IntegrityError:
                    pass  # Уже есть такой реферал
        
        conn.commit()
    
    # Показываем главное меню
    menu_text, reply_markup = await main_menu(user_id)
    await message.answer(menu_text, reply_markup=reply_markup)

@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.answer()
    menu_text, reply_markup = await main_menu(callback.from_user.id)
    await callback.message.edit_text(menu_text, reply_markup=reply_markup)

@dp.callback_query(lambda c: c.data.startswith("source:"))
async def set_referral_source(callback: CallbackQuery):
    source = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET referral_source = ? WHERE user_id = ?",
            (source, user_id)
        )
        conn.commit()
    
    await callback.answer(f"Источник установлен: {source}")
    
    # Показываем главное меню
    menu_text, reply_markup = await main_menu(user_id)
    await callback.message.edit_text(menu_text, reply_markup=reply_markup)

@dp.callback_query(lambda c: c.data == "add_whatsapp")
async def add_whatsapp(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    # Проверяем статус сервиса
    if get_service_status("whatsapp") != "активен":
        await callback.message.answer("⚠️ Сервис WhatsApp временно недоступен.")
        return
    
    await callback.message.answer(
        "📱 <b>Добавление WhatsApp аккаунта</b>\n\n"
        "Отправьте номер телефона в международном формате:\n"
        "<code>+79991234567</code>",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.whatsapp_phone)

@dp.message(Form.whatsapp_phone)
async def process_whatsapp_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    user_id = message.from_user.id
    
    if not validate_phone(phone):
        await message.answer(
            "❌ Неверный формат номера. Пожалуйста, отправьте номер в международном формате:\n"
            "<code>+79991234567</code>",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Проверяем, не добавлен ли уже этот номер
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM whatsapp_numbers WHERE phone = ?", (phone,))
        if cursor.fetchone():
            await message.answer("❌ Этот номер уже добавлен в систему.")
            await state.clear()
            return
    
    # Добавляем номер в базу (ИСПРАВЛЕННАЯ ВЕРСИЯ)
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO whatsapp_numbers (user_id, phone, status, admin_id)
            VALUES (?, ?, 'pending', ?)
        """, (user_id, phone, None))
        account_id = cursor.lastrowid
        conn.commit()
    
    await message.answer(
        f"✅ Номер {phone} добавлен в очередь WhatsApp.\n"
        "Ожидайте подтверждения от администратора."
    )
    
    # Уведомляем администраторов
    await notify_admins(
        f"📱 <b>Новый WhatsApp аккаунт</b>\n\n"
        f"Номер: {phone}\n"
        f"Пользователь: @{message.from_user.username or 'без username'} (ID: {user_id})\n\n"
        f"Источник: {referral_source}\n\n"
        f"Для подтверждения перейдите в админ-панель.",
        reply_markup=await admin_panel_keyboard()
    )
    
    await state.clear()

@dp.callback_query(lambda c: c.data == "add_max")
async def add_max(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    # Проверяем статус сервиса
    if get_service_status("max") != "активен":
        await callback.message.answer("⚠️ Сервис MAX временно недоступен.")
        return
    
    await callback.message.answer(
        "🤖 <b>Добавление MAX аккаунта</b>\n\n"
        "Отправьте номер телефона в международном формате:\n"
        "<code>+79991234567</code>",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.max_phone)

@dp.message(Form.max_phone)
async def process_max_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    user_id = message.from_user.id
    
    if not validate_phone(phone):
        await message.answer(
            "❌ Неверный формат номера. Пожалуйста, отправьте номер в международном формате:\n"
            "<code>+79991234567</code>",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Проверяем, не добавлен ли уже этот номер
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM max_numbers WHERE phone = ?", (phone,))
        if cursor.fetchone():
            await message.answer("❌ Этот номер уже добавлен в систему.")
            await state.clear()
            return
    
    # Добавляем номер в базу
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO max_numbers (user_id, phone, status)
            VALUES (?, ?, 'pending')
        """, (user_id, phone))
        conn.commit()
    
    await message.answer(
        f"✅ Номер {phone} добавлен в очередь MAX.\n"
        "Ожидайте подтверждения от администратора."
    )
    
    # Уведомляем администраторов
    await notify_admins(
        f"🤖 <b>Новый MAX аккаунт</b>\n\n"
        f"Номер: {phone}\n"
        f"Пользователь: @{message.from_user.username or 'без username'} (ID: {user_id})\n\n"
        f"Источник: {referral_source}\n\n"
        f"Выберите действие:",
        reply_markup=await max_admin_keyboard(cursor.lastrowid)
    )
    
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("accept_max:"))
async def accept_max(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE max_numbers 
            SET status = 'accepted', admin_id = ?
            WHERE id = ?
        """, (admin_id, account_id))
        
        cursor.execute("""
            SELECT user_id, phone FROM max_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            
            # Обновляем счетчик пользователя
            cursor.execute("""
                UPDATE users SET max_numbers = max_numbers + 1 
                WHERE user_id = ?
            """, (user_id,))
            
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"✅ Ваш MAX аккаунт {phone} был принят администратором!\n\n"
                    f"Теперь вам нужно отправить код из SMS администраторам:",
                    reply_markup=await max_user_code_keyboard(account_id)
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("✅ Аккаунт принят")
    await callback.message.edit_text(
        f"✅ Вы приняли MAX аккаунт {phone}\n\n"
        f"Ожидайте код от пользователя...",
        reply_markup=None
    )

@dp.callback_query(lambda c: c.data.startswith("reject_max:"))
async def reject_max(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, phone FROM max_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            
            cursor.execute("DELETE FROM max_numbers WHERE id = ?", (account_id,))
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"❌ Ваш MAX аккаунт {phone} был отклонен администратором."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("❌ Аккаунт отклонен")
    await callback.message.delete()

@dp.callback_query(lambda c: c.data.startswith("send_max_user_code:"))
async def send_max_user_code(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT admin_id, phone FROM max_numbers 
            WHERE id = ? AND user_id = ? AND status = 'accepted'
        """, (account_id, user_id))
        
        result = cursor.fetchone()
        if not result:
            await callback.answer("❌ Аккаунт не найден или не принят")
            return
        
        admin_id, phone = result
    
    await callback.message.answer(
        "📨 <b>Отправка кода администратору</b>\n\n"
        "Пожалуйста, введите код из SMS, который пришел на номер MAX:",
        parse_mode=ParseMode.HTML
    )
    
    await state.update_data(account_id=account_id, admin_id=admin_id)
    await state.set_state(Form.max_user_code)

@dp.message(Form.max_user_code)
async def process_max_user_code(message: Message, state: FSMContext):
    code = message.text.strip()
    user_id = message.from_user.id
    data = await state.get_data()
    account_id = data.get('account_id')
    admin_id = data.get('admin_id')
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE max_numbers 
            SET user_code = ?, code_entered = 1
            WHERE id = ? AND user_id = ?
        """, (code, account_id, user_id))
        
        cursor.execute("""
            SELECT phone FROM max_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            phone = result[0]
            conn.commit()
            
            # Уведомляем администратора
            await notify_admin(
                admin_id,
                f"📨 <b>Получен код от пользователя</b>\n\n"
                f"Аккаунт: {phone}\n"
                f"Код: <code>{code}</code>\n\n"
                f"Пользователь: @{message.from_user.username or 'без username'} (ID: {user_id})\n\n"
                f"Выберите действие:",
                reply_markup=await max_admin_code_keyboard(account_id)
            )
            
            await message.answer(
                "✅ Код успешно отправлен администратору!\n"
                "Ожидайте подтверждения входа..."
            )
    
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("max_entered:"))
async def max_entered(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE max_numbers 
            SET status = 'active', hold_start = datetime('now'), admin_accepted = 1
            WHERE id = ? AND admin_id = ?
        """, (account_id, admin_id))
        
        cursor.execute("""
            SELECT user_id, phone FROM max_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"✅ Ваш MAX аккаунт {phone} успешно активирован!\n\n"
                    f"Холд начат. Начисление произойдет через 15 минут, если аккаунт останется активным."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("✅ Холд активирован")
    await callback.message.edit_text(
        f"✅ Холд для MAX аккаунта {phone} активирован!\n\n"
        f"Начисление произойдет через 15 минут.",
        reply_markup=None
    )

@dp.callback_query(lambda c: c.data.startswith("max_failed:"))
async def max_failed(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, phone FROM max_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            
            # Сбрасываем статус для повторной попытки
            cursor.execute("""
                UPDATE max_numbers 
                SET status = 'accepted', user_code = NULL, code_entered = 0
                WHERE id = ?
            """, (account_id,))
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"❌ Не удалось войти в MAX аккаунт {phone}.\n\n"
                    f"Пожалуйста, попробуйте снова отправить код:",
                    reply_markup=await max_user_code_keyboard(account_id)
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("❌ Попытка входа неудачна")
    await callback.message.edit_text(
        f"❌ Попытка входа в MAX аккаунт {phone} неудачна.\n\n"
        f"Ожидаем новый код от пользователя...",
        reply_markup=None
    )

@dp.callback_query(lambda c: c.data == "sms_work_menu")
async def sms_work_menu(callback: CallbackQuery):
    await callback.answer()
    
    # Проверяем статус сервиса
    if get_service_status("sms") != "активен":
        await callback.message.answer("⚠️ Сервис SMS WORK временно недоступен.")
        return
    
    await callback.message.answer(
        "💬 <b>SMS WORK</b>\n\n"
        "Отправка SMS сообщений за вознаграждение.\n"
        f"Ставка: {SMS_RATE}$ за 1 сообщение\n\n"
        "Для начала работы отправьте заявку:",
        reply_markup=await sms_work_menu_keyboard(),
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(lambda c: c.data == "add_sms")
async def add_sms(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    
    # Создаем заявку на SMS работу
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sms_works (user_id, status)
            VALUES (?, 'pending')
        """, (user_id,))
        conn.commit()
    
    # Уведомляем администраторов
    await notify_admins(
        f"💬 <b>Новая заявка SMS WORK</b>\n\n"
        f"Пользователь: @{callback.from_user.username or 'без username'} (ID: {user_id})\n\n"
        f"Источник: {referral_source}\n\n"
        f"Выберите действие:",
        reply_markup=await sms_work_accept_keyboard(cursor.lastrowid)
    )
    
    await callback.message.answer(
        "✅ Заявка на SMS WORK отправлена!\n"
        "Ожидайте подтверждения от администратора."
    )

@dp.callback_query(lambda c: c.data.startswith("sms_accept:"))
async def sms_accept(callback: CallbackQuery, state: FSMContext):
    work_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE sms_works 
            SET status = 'accepted', admin_id = ?
            WHERE id = ?
        """, (admin_id, work_id))
        
        cursor.execute("""
            SELECT user_id FROM sms_works WHERE id = ?
        """, (work_id,))
        result = cursor.fetchone()
        
        if result:
            user_id = result[0]
            conn.commit()
            
            # Просим администратора ввести текст для рассылки
            await callback.message.answer(
                "📝 <b>Введите текст для рассылки:</b>",
                parse_mode=ParseMode.HTML
            )
            
            await state.update_data(work_id=work_id, user_id=user_id)
            await state.set_state(Form.admin_sms_message)
    
    await callback.answer("✅ Заявка принята")

@dp.callback_query(lambda c: c.data.startswith("sms_reject:"))
async def sms_reject(callback: CallbackQuery):
    work_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id FROM sms_works WHERE id = ?
        """, (work_id,))
        result = cursor.fetchone()
        
        if result:
            user_id = result[0]
            
            cursor.execute("DELETE FROM sms_works WHERE id = ?", (work_id,))
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    "❌ Ваша заявка на SMS WORK была отклонена администратором."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("❌ Заявка отклонена")
    await callback.message.delete()

@dp.message(Form.admin_sms_message)
async def process_admin_sms_message(message: Message, state: FSMContext):
    text = message.text
    data = await state.get_data()
    work_id = data.get('work_id')
    user_id = data.get('user_id')
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE sms_works 
            SET text = ?, work_message = ?
            WHERE id = ?
        """, (text, text, work_id))
        conn.commit()
    
    # Отправляем текст пользователю
    try:
        await bot.send_message(
            user_id,
            f"📨 <b>Текст для рассылки:</b>\n\n"
            f"<code>{text}</code>\n\n"
            f"Используйте моноширинный шрифт для отправки.",
            parse_mode=ParseMode.HTML,
            reply_markup=await copy_message_keyboard(text)
        )
        
        await bot.send_message(
            user_id,
            "✅ Ваша заявка на SMS WORK принята!\n\n"
            "Вы можете завершить работу в любое время:",
            reply_markup=await sms_work_active_keyboard(work_id)
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения пользователю: {e}")
        await message.answer("❌ Не удалось отправить сообщение пользователю.")
    
    await message.answer("✅ Текст отправлен пользователю!")
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("sms_complete:"))
async def sms_complete(callback: CallbackQuery, state: FSMContext):
    work_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status FROM sms_works 
            WHERE id = ? AND user_id = ?
        """, (work_id, user_id))
        
        result = cursor.fetchone()
        if not result or result[0] != 'accepted':
            await callback.answer("❌ Работа не найдена или не принята")
            return
    
    await callback.message.answer(
        "📸 <b>Завершение SMS WORK</b>\n\n"
        "Пожалуйста, отправьте скриншоты, на которых видно:\n"
        "• Текст рассылки\n"
        "• Номер получателя\n\n"
        "Отправьте все фото одним сообщением:",
        parse_mode=ParseMode.HTML
    )
    
    await state.update_data(work_id=work_id)
    await state.set_state(Form.sms_proof)

@dp.message(Form.sms_proof)
async def process_sms_proof(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("❌ Пожалуйста, отправьте скриншоты.")
        return
    
    data = await state.get_data()
    work_id = data.get('work_id')
    user_id = message.from_user.id
    
    # Сохраняем информацию о фото
    photo_id = message.photo[-1].file_id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE sms_works 
            SET proof_photo = ?, status = 'proof_pending', completed_at = datetime('now')
            WHERE id = ? AND user_id = ?
        """, (photo_id, work_id, user_id))
        
        cursor.execute("""
            SELECT admin_id, text FROM sms_works WHERE id = ?
        """, (work_id,))
        result = cursor.fetchone()
        
        if result:
            admin_id, text = result
            conn.commit()
            
            # Уведомляем администратора
            try:
                await bot.send_photo(
                    admin_id,
                    photo=photo_id,
                    caption=f"📸 <b>Доказательства SMS WORK</b>\n\n"
                           f"Работа ID: {work_id}\n"
                           f"Текст: {text[:100]}...\n"
                           f"Пользователь: @{message.from_user.username or 'без username'} (ID: {user_id})\n\n"
                           f"Проверьте доказательства:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=await sms_admin_proof_keyboard(work_id)
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления администратора: {e}")
    
    await message.answer(
        "✅ Доказательства отправлены на проверку!\n"
        "Ожидайте подтверждения администратора."
    )
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("sms_confirm_proof:"))
async def sms_confirm_proof(callback: CallbackQuery):
    work_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, text FROM sms_works 
            WHERE id = ? AND status = 'proof_pending'
        """, (work_id,))
        
        result = cursor.fetchone()
        if result:
            user_id, text = result
            
            # Начисляем вознаграждение
            amount = SMS_RATE
            
            cursor.execute("""
                UPDATE sms_works 
                SET status = 'completed', amount = ?, processed_at = datetime('now')
                WHERE id = ?
            """, (amount, work_id))
            
            cursor.execute("""
                UPDATE users 
                SET balance_usd = balance_usd + ?, 
                    sms_messages = sms_messages + 1,
                    total_earned_usd = total_earned_usd + ?
                WHERE user_id = ?
            """, (amount, amount, user_id))
            
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"✅ Ваша SMS WORK завершена!\n\n"
                    f"Начислено: {amount:.2f}$\n"
                    f"Текст: {text[:100]}..."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("✅ Доказательства приняты")
    await callback.message.edit_text(
        f"✅ Доказательства SMS WORK приняты!\n\n"
        f"Пользователю начислено: {amount:.2f}$",
        reply_markup=None
    )

@dp.callback_query(lambda c: c.data.startswith("sms_reject_proof:"))
async def sms_reject_proof(callback: CallbackQuery):
    work_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id FROM sms_works 
            WHERE id = ? AND status = 'proof_pending'
        """, (work_id,))
        
        result = cursor.fetchone()
        if result:
            user_id = result[0]
            
            cursor.execute("DELETE FROM sms_works WHERE id = ?", (work_id,))
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    "❌ Ваши доказательства SMS WORK были отклонены администратором."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("❌ Доказательства отклонены")
    await callback.message.delete()

@dp.callback_query(lambda c: c.data == "withdraw_request")
async def withdraw_request(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    # Получаем баланс пользователя
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance_usd FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        
        if not result or result[0] < 1.0:
            await callback.answer("❌ Минимальная сумма для вывода: 1.0$")
            return
        
        balance = result[0]
    
    await callback.answer()
    await callback.message.answer(
        f"💰 <b>Заявка на вывод средств</b>\n\n"
        f"Доступный баланс: {balance:.2f}$\n"
        f"Минимальная сумма: 1.0$\n\n"
        "Введите сумму для вывода:",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.withdraw_request)

@dp.message(Form.withdraw_request)
async def process_withdraw_request(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    try:
        amount = float(message.text.strip())
        if amount < 1.0:
            await message.answer("❌ Минимальная сумма для вывода: 1.0$")
            return
    except ValueError:
        await message.answer("❌ Неверный формат суммы. Введите число.")
        return
    
    # Проверяем баланс
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance_usd FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        
        if not result or result[0] < amount:
            await message.answer("❌ Недостаточно средств на балансе.")
            await state.clear()
            return
        
        balance = result[0]
        
        # Создаем заявку на вывод
        amount_rub = usd_to_rub(amount)
        cursor.execute("""
            INSERT INTO withdraw_requests (user_id, amount_usd, amount_rub, status)
            VALUES (?, ?, ?, 'pending')
        """, (user_id, amount, amount_rub))
        
        # Списываем средства с баланса
        cursor.execute("""
            UPDATE users SET balance_usd = balance_usd - ? WHERE user_id = ?
        """, (amount, user_id))
        
        conn.commit()
    
    await message.answer(
        f"✅ Заявка на вывод {amount:.2f}$ создана!\n\n"
        f"Обработка займет до 1 часа. Вы получите уведомление."
    )
    
    # Уведомляем администраторов
    await notify_admins(
        f"💰 <b>Новая заявка на вывод</b>\n\n"
        f"Пользователь: @{message.from_user.username or 'без username'} (ID: {user_id})\n"
        f"Сумма: {amount:.2f}$\n\n"
        f"Статус: ожидание подтверждения"
    )
    
    # Запускаем таймер для автоматического подтверждения
    asyncio.create_task(confirm_withdraw_request(user_id, amount))
    
    await state.clear()

@dp.callback_query(lambda c: c.data == "profile")
async def show_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT balance_usd, level, warnings, whatsapp_numbers, max_numbers, sms_messages, 
                   total_earned_usd, created_at
            FROM users WHERE user_id = ?
        """, (user_id,))
        result = cursor.fetchone()
        
        if not result:
            await callback.answer("❌ Пользователь не найден")
            return
        
        balance, level, warnings, whatsapp_count, max_count, sms_count, total_earned, reg_date = result
        
        # Получаем количество активных номеров
        cursor.execute("""
            SELECT COUNT(*) FROM whatsapp_numbers 
            WHERE user_id = ? AND status IN ('hold_active', 'active')
        """, (user_id,))
        active_whatsapp = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM max_numbers 
            WHERE user_id = ? AND status = 'active'
        """, (user_id,))
        active_max = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM sms_works 
            WHERE user_id = ? AND status = 'active'
        """, (user_id,))
        active_sms = cursor.fetchone()[0]
        
        # Получаем реферальную статистику
        cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
        referrals_count = cursor.fetchone()[0]
    
    profile_text = (
        f"👤 <b>Профиль</b>\n\n"
        f"💰 Баланс: {balance:.2f}$\n"
        f"💸 Всего заработано: {total_earned:.2f}$\n"
        f"📊 Уровень: {level}\n\n"
        
        f"📱 <b>Активные номера:</b>\n"
        f"   • WhatsApp: {active_whatsapp}\n"
        f"   • MAX: {active_max}\n"
        f"   • SMS WORK: {active_sms}\n\n"
        
        f"📈 <b>Статистика:</b>\n"
        f"   • WhatsApp сдано: {whatsapp_count}\n"
        f"   • MAX сдано: {max_count}\n"
        f"   • SMS отправлено: {sms_count}\n"
        f"   • Рефералов: {referrals_count}\n\n"
        
        f"⚠️ Предупреждения: {warnings}/5\n"
        f"📅 Регистрация: {reg_date}"
    )
    
    await callback.message.edit_text(profile_text, parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data == "support")
async def show_support(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    await callback.answer()
    await callback.message.answer(
        "💬 <b>Поддержка</b>\n\n"
        "Напишите ваш вопрос или проблему, и мы ответим в ближайшее время:",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.support_message)

@dp.message(Form.support_message)
async def process_support_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    support_text = message.text
    
    # Сохраняем обращение в базу
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO support_tickets (user_id, username, message, status)
            VALUES (?, ?, ?, 'open')
        """, (user_id, username, support_text))
        conn.commit()
    
    # Уведомляем администраторов
    await notify_admins(
        f"🆘 <b>Новое обращение в поддержку</b>\n\n"
        f"Пользователь: @{username or 'без username'} (ID: {user_id})\n\n"
        f"Сообщение:\n{support_text}\n\n"
        f"Статус: открыто"
    )
    
    await message.answer(
        "✅ Ваше обращение отправлено в поддержку!\n\n"
        "Мы ответим вам в ближайшее время."
    )
    await state.clear()

@dp.message(Command("active_hold"))
async def show_active_hold(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # WhatsApp аккаунты на холде
        cursor.execute("""
            SELECT wn.id, wn.phone, wn.hold_start, wn.status, 
                   u.user_id, u.username,
                   CASE 
                       WHEN wn.hold_start IS NOT NULL THEN 
                           (julianday('now') - julianday(wn.hold_start)) * 24 
                       ELSE 0 
                   END as hours_held
            FROM whatsapp_numbers wn
            LEFT JOIN users u ON wn.user_id = u.user_id
            WHERE wn.status IN ('hold_active', 'active')
            ORDER BY wn.hold_start DESC
        """)
        
        whatsapp_accounts = cursor.fetchall()
        
        # MAX аккаунты на холде
        cursor.execute("""
            SELECT mn.id, mn.phone, mn.hold_start, mn.status,
                   u.user_id, u.username,
                   CASE 
                       WHEN mn.hold_start IS NOT NULL THEN 
                           (julianday('now') - julianday(mn.hold_start)) * 24 * 60 
                       ELSE 0 
                   END as minutes_held
            FROM max_numbers mn
            LEFT JOIN users u ON mn.user_id = u.user_id
            WHERE mn.status = 'active'
            ORDER BY mn.hold_start DESC
        """)
        
        max_accounts = cursor.fetchall()
    
    # Формируем ответ
    response = "📊 <b>Аккаунты на холде</b>\n\n"
    
    # WhatsApp аккаунты
    response += f"📱 <b>WhatsApp ({len(whatsapp_accounts)})</b>:\n"
    if whatsapp_accounts:
        for account in whatsapp_accounts:
            account_id, phone, hold_start, status, user_id, username, hours_held = account
            hold_time = f"{hours_held:.1f} ч." if hold_start else "не начат"
            response += (
                f"• {phone} (ID: {account_id})\n"
                f"  👤 @{username or 'нет'} (ID: {user_id})\n"
                f"  ⏰ {hold_start or 'нет'} ({hold_time})\n"
                f"  📊 {status}\n\n"
            )
    else:
        response += "   Нет аккаунтов на холде\n\n"
    
    # MAX аккаунты
    response += f"🤖 <b>MAX ({len(max_accounts)})</b>:\n"
    if max_accounts:
        for account in max_accounts:
            account_id, phone, hold_start, status, user_id, username, minutes_held = account
            hold_time = f"{minutes_held:.1f} мин." if hold_start else "не начат"
            response += (
                f"• {phone} (ID: {account_id})\n"
                f"  👤 @{username or 'нет'} (ID: {user_id})\n"
                f"  ⏰ {hold_start or 'нет'} ({hold_time})\n\n"
            )
    else:
        response += "   Нет аккаунтов на холде\n"
    
    await message.answer(response, parse_mode=ParseMode.HTML)

@dp.message(Command("user_hold"))
async def user_hold_info(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        return
    
    if not command.args:
        await message.answer("❌ Укажите ID пользователя: /user_hold <user_id>")
        return
    
    try:
        user_id = int(command.args)
    except ValueError:
        await message.answer("❌ Неверный формат ID пользователя")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Информация о пользователе
        cursor.execute("""
            SELECT username, balance_usd, level 
            FROM users WHERE user_id = ?
        """, (user_id,))
        user_info = cursor.fetchone()
        
        if not user_info:
            await message.answer("❌ Пользователь не найден")
            return
        
        username, balance, level = user_info
        
        # Активные холды WhatsApp
        cursor.execute("""
            SELECT id, phone, hold_start, status,
                   (julianday('now') - julianday(hold_start)) * 24 as hours_held
            FROM whatsapp_numbers 
            WHERE user_id = ? AND status IN ('hold_active', 'active')
        """, (user_id,))
        whatsapp_holds = cursor.fetchall()
        
        # Активные холды MAX
        cursor.execute("""
            SELECT id, phone, hold_start, status,
                   (julianday('now') - julianday(hold_start)) * 24 * 60 as minutes_held
            FROM max_numbers 
            WHERE user_id = ? AND status = 'active'
        """, (user_id,))
        max_holds = cursor.fetchall()
        
        # История завершенных холдов
        cursor.execute("""
            SELECT COUNT(*) FROM whatsapp_numbers 
            WHERE user_id = ? AND status = 'completed'
        """, (user_id,))
        whatsapp_completed = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM max_numbers 
            WHERE user_id = ? AND status = 'completed'
        """, (user_id,))
        max_completed = cursor.fetchone()[0]
    
    response = (
        f"👤 <b>Информация о холдах пользователя</b>\n\n"
        f"Пользователь: @{username or 'нет'} (ID: {user_id})\n"
        f"Баланс: {balance:.2f}$ | Уровень: {level}\n\n"
    )
    
    # Активные холды WhatsApp
    response += f"📱 <b>WhatsApp активные ({len(whatsapp_holds)})</b>:\n"
    if whatsapp_holds:
        for account in whatsapp_holds:
            account_id, phone, hold_start, status, hours_held = account
            response += f"• {phone} ({hours_held:.1f} ч.) - {status}\n"
    else:
        response += "   Нет активных холдов\n"
    
    # Активные холды MAX
    response += f"\n🤖 <b>MAX активные ({len(max_holds)})</b>:\n"
    if max_holds:
        for account in max_holds:
            account_id, phone, hold_start, status, minutes_held = account
            response += f"• {phone} ({minutes_held:.1f} мин.)\n"
    else:
        response += "   Нет активных холдов\n"
    
    # Статистика
    response += (
        f"\n📊 <b>Статистика:</b>\n"
        f"   • WhatsApp завершено: {whatsapp_completed}\n"
        f"   • MAX завершено: {max_completed}\n"
        f"   • Всего: {whatsapp_completed + max_completed}"
    )
    
    await message.answer(response, parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.edit_text(
        "👑 <b>Админ-панель</b>\n\n"
        "Выберите действие:",
        reply_markup=await admin_panel_keyboard(),
        parse_mode=ParseMode.HTML
    )

# Обработчики для WhatsApp (аналогично MAX)
@dp.callback_query(lambda c: c.data.startswith("whatsapp_account:"))
async def whatsapp_account_detail(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT wn.phone, wn.status, u.user_id, u.username
            FROM whatsapp_numbers wn
            LEFT JOIN users u ON wn.user_id = u.user_id
            WHERE wn.id = ?
        """, (account_id,))
        
        result = cursor.fetchone()
        if result:
            phone, status, user_id, username = result
            
            text = (
                f"📱 <b>WhatsApp аккаунт #{account_id}</b>\n\n"
                f"Номер: {phone}\n"
                f"Статус: {status}\n"
                f"Пользователь: @{username or 'без username'} (ID: {user_id})\n\n"
                f"Выберите действие:"
            )
            
            await callback.message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=await whatsapp_admin_keyboard(account_id)
            )
        else:
            await callback.answer("❌ Аккаунт не найден")

@dp.callback_query(lambda c: c.data.startswith("send_whatsapp_code:"))
async def send_whatsapp_code(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.message.answer(
        "📨 <b>Отправка кода пользователю</b>\n\n"
        "Пожалуйста, отправьте код из SMS (фотографией):",
        parse_mode=ParseMode.HTML
    )
    
    await state.update_data(account_id=account_id)
    await state.set_state(Form.whatsapp_code)

@dp.message(Form.whatsapp_code)
async def process_whatsapp_code(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("❌ Пожалуйста, отправьте код фотографией.")
        return
    
    data = await state.get_data()
    account_id = data.get('account_id')
    admin_id = message.from_user.id
    
    # Сохраняем фото
    photo_id = message.photo[-1].file_id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE whatsapp_numbers 
            SET code_sent = 1
            WHERE id = ?
        """, (account_id,))
        
        cursor.execute("""
            SELECT user_id, phone FROM whatsapp_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            conn.commit()
            
            # Отправляем фото пользователю
            try:
                await bot.send_photo(
                    user_id,
                    photo=photo_id,
                    caption=f"📨 <b>Код для WhatsApp аккаунта</b>\n\n"
                           f"Номер: {phone}\n\n"
                           f"Введите этот код в приложении WhatsApp и подтвердите вход:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=await whatsapp_code_keyboard(account_id)
                )
            except Exception as e:
                logger.error(f"Ошибка отправки кода пользователю: {e}")
                await message.answer("❌ Не удалось отправить код пользователю.")
                return
    
    await message.answer("✅ Код отправлен пользователю!")
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("whatsapp_entered:"))
async def whatsapp_entered(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE whatsapp_numbers 
            SET code_entered = 1, status = 'active'
            WHERE id = ? AND user_id = ?
        """, (account_id, user_id))
        
        # Получаем информацию об аккаунте
        cursor.execute("""
            SELECT phone FROM whatsapp_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            phone = result[0]
            conn.commit()
            
            # Уведомляем ВСЕХ администраторов
            admin_message = (
                f"✅ <b>Пользователь вошел в WhatsApp</b>\n\n"
                f"Аккаунт ID: {account_id}\n"
                f"Номер: {phone}\n"
                f"Пользователь: @{callback.from_user.username or 'без username'} (ID: {user_id})\n\n"
                f"Подтвердите активацию холда:"
            )
            
            # Создаем клавиатуру для подтверждения
            keyboard = await whatsapp_admin_confirm_keyboard(account_id)
            
            # Отправляем всем администраторам
            await notify_admins(admin_message, reply_markup=keyboard)
    
    await callback.answer("✅ Вход подтвержден")
    
    # Отправляем новое сообщение пользователю
    try:
        await callback.message.answer(
            "✅ Вы подтвердили вход в WhatsApp!\n\n"
            "Ожидайте подтверждения активации холда от администратора."
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения пользователю: {e}")

@dp.callback_query(lambda c: c.data.startswith("whatsapp_failed:"))
async def whatsapp_failed(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT phone FROM whatsapp_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            phone = result[0]
            
            cursor.execute("DELETE FROM whatsapp_numbers WHERE id = ?", (account_id,))
            conn.commit()
            
            # Уведомляем всех администраторов
            admin_message = (
                f"❌ <b>Пользователь не смог войти в WhatsApp</b>\n\n"
                f"Аккаунт ID: {account_id}\n"
                f"Номер: {phone}\n"
                f"Пользователь: @{callback.from_user.username or 'без username'} (ID: {user_id})\n\n"
                f"Аккаунт удален из системы."
            )
            
            await notify_admins(admin_message)
    
    await callback.answer("❌ Ошибка входа")
    
    # Отправляем новое сообщение пользователю
    try:
        await callback.message.answer(
            "❌ Вы сообщили об ошибке входа.\n"
            "Аккаунт удален из системы."
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения пользователю: {e}")

@dp.callback_query(lambda c: c.data.startswith("confirm_whatsapp_hold:"))
async def confirm_whatsapp_hold(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE whatsapp_numbers 
            SET hold_start = datetime('now'), status = 'hold_active'
            WHERE id = ?
        """, (account_id,))
        
        cursor.execute("""
            SELECT user_id, phone FROM whatsapp_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"✅ Холд для WhatsApp аккаунта {phone} активирован!\n\n"
                    f"Начисление произойдет после завершения холда."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
            
            # Уведомляем всех администраторов об успешном подтверждении
            admin_message = (
                f"✅ <b>Холд WhatsApp активирован</b>\n\n"
                f"Аккаунт ID: {account_id}\n"
                f"Номер: {phone}\n"
                f"Пользователь: ID {user_id}\n"
                f"Администратор: @{callback.from_user.username or 'без username'}"
            )
            
            await notify_admins(admin_message)
    
    await callback.answer("✅ Холд активирован")
    
    # Редактируем сообщение с кнопками
    try:
        await callback.message.edit_text(
            f"✅ Холд для WhatsApp аккаунта {phone} активирован!",
            reply_markup=None
        )
    except Exception as e:
        logger.error(f"Ошибка редактирования сообщения: {e}")

@dp.callback_query(lambda c: c.data.startswith("reject_whatsapp:"))
async def reject_whatsapp(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, phone FROM whatsapp_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            
            cursor.execute("DELETE FROM whatsapp_numbers WHERE id = ?", (account_id,))
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"❌ Ваш WhatsApp аккаунт {phone} был отклонен администратором."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
            
            # Уведомляем всех администраторов об отклонении
            admin_message = (
                f"❌ <b>WhatsApp аккаунт отклонен</b>\n\n"
                f"Аккаунт ID: {account_id}\n"
                f"Номер: {phone}\n"
                f"Пользователь: ID {user_id}\n"
                f"Администратор: @{callback.from_user.username or 'без username'}"
            )
            
            await notify_admins(admin_message)
    
    await callback.answer("❌ Аккаунт отклонен")
    
    # Удаляем сообщение с кнопками
    try:
        await callback.message.delete()
    except Exception as e:
        logger.error(f"Ошибка удаления сообщения: {e}")

@dp.callback_query(lambda c: c.data == "admin_add")
async def admin_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.answer(
        "👥 <b>Добавление администратора</b>\n\n"
        "Отправьте ID пользователя, которого хотите сделать администратором:",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.add_admin)

@dp.message(Form.add_admin)
async def process_add_admin(message: Message, state: FSMContext):
    try:
        new_admin_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный формат ID. Отправьте числовой ID пользователя.")
        return
    
    if new_admin_id in ADMIN_IDS:
        await message.answer("❌ Этот пользователь уже является администратором.")
        await state.clear()
        return
    
    # Добавляем администратора
    ADMIN_IDS.append(new_admin_id)
    save_admin_ids()
    
    await message.answer(f"✅ Пользователь {new_admin_id} добавлен в список администраторов.")
    
    # Уведомляем нового администратора
    try:
        await bot.send_message(
            new_admin_id,
            "🎉 Вас назначили администратором бота!\n\n"
            "Теперь у вас есть доступ к админ-панели."
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления нового администратора: {e}")
    
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_status")
async def admin_status(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    
    # Получаем текущие статусы
    whatsapp_status = get_service_status("whatsapp")
    max_status = get_service_status("max")
    sms_status = get_service_status("sms")
    
    text = (
        "🔄 <b>Статус сервисов</b>\n\n"
        f"📱 WhatsApp: {whatsapp_status}\n"
        f"🤖 MAX: {max_status}\n"
        f"💬 SMS WORK: {sms_status}\n\n"
        "Выберите сервис для изменения статуса:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📱 WhatsApp", callback_data="change_status:whatsapp"),
        types.InlineKeyboardButton(text="🤖 MAX", callback_data="change_status:max")
    )
    builder.row(
        types.InlineKeyboardButton(text="💬 SMS WORK", callback_data="change_status:sms")
    )
    builder.row(
        types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data.startswith("change_status:"))
async def change_status_menu(callback: CallbackQuery, state: FSMContext):
    service = callback.data.split(":")[1]
    
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    
    current_status = get_service_status(service)
    
    text = (
        f"🔄 <b>Изменение статуса {service.upper()}</b>\n\n"
        f"Текущий статус: {current_status}\n\n"
        "Выберите новый статус:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✅ Активен", callback_data=f"set_status:{service}:активен"),
        types.InlineKeyboardButton(text="❌ Неактивен", callback_data=f"set_status:{service}:неактивен")
    )
    builder.row(
        types.InlineKeyboardButton(text="⏸️ На паузе", callback_data=f"set_status:{service}:на паузе")
    )
    builder.row(
        types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_status")
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data.startswith("set_status:"))
async def set_service_status(callback: CallbackQuery):
    data_parts = callback.data.split(":")
    service = data_parts[1]
    status = data_parts[2]
    
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    # Обновляем статус
    update_service_status(service, status)
    
    await callback.answer(f"✅ Статус {service} изменен на '{status}'")
    
    # Возвращаемся к меню статусов
    whatsapp_status = get_service_status("whatsapp")
    max_status = get_service_status("max")
    sms_status = get_service_status("sms")
    
    text = (
        "🔄 <b>Статус сервисов</b>\n\n"
        f"📱 WhatsApp: {whatsapp_status}\n"
        f"🤖 MAX: {max_status}\n"
        f"💬 SMS WORK: {sms_status}\n\n"
        "Статус успешно обновлен! ✅"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📱 WhatsApp", callback_data="change_status:whatsapp"),
        types.InlineKeyboardButton(text="🤖 MAX", callback_data="change_status:max")
    )
    builder.row(
        types.InlineKeyboardButton(text="💬 SMS WORK", callback_data="change_status:sms")
    )
    builder.row(
        types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.answer(
        "📢 <b>Рассылка сообщения</b>\n\n"
        "Отправьте сообщение для рассылки всем пользователям:",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.admin_broadcast)

@dp.message(Form.admin_broadcast)
async def process_admin_broadcast(message: Message, state: FSMContext):
    broadcast_text = message.text
    admin_id = message.from_user.id
    
    # Получаем всех пользователей
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = cursor.fetchall()
    
    success_count = 0
    fail_count = 0
    
    for user in users:
        user_id = user[0]
        try:
            await bot.send_message(user_id, broadcast_text)
            success_count += 1
            await asyncio.sleep(0.1)  # Задержка чтобы не спамить
        except Exception as e:
            fail_count += 1
    
    await message.answer(
        f"✅ Рассылка завершена!\n\n"
        f"Успешно: {success_count}\n"
        f"Не удалось: {fail_count}"
    )
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Общая статистика
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM whatsapp_numbers")
        total_whatsapp = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM max_numbers")
        total_max = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM sms_works")
        total_sms = cursor.fetchone()[0]
        
        cursor.execute("SELECT SUM(balance_usd) FROM users")
        total_balance = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT SUM(total_earned_usd) FROM users")
        total_earned = cursor.fetchone()[0] or 0
        
        # Активные холды
        cursor.execute("SELECT COUNT(*) FROM whatsapp_numbers WHERE status IN ('hold_active', 'active')")
        active_whatsapp = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM max_numbers WHERE status = 'active'")
        active_max = cursor.fetchone()[0]
        
        # Очередь на подтверждение
        cursor.execute("SELECT COUNT(*) FROM whatsapp_numbers WHERE status = 'pending'")
        pending_whatsapp = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM max_numbers WHERE status = 'pending'")
        pending_max = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM sms_works WHERE status = 'pending'")
        pending_sms = cursor.fetchone()[0]
        
        # Заявки на вывод
        cursor.execute("SELECT COUNT(*) FROM withdraw_requests WHERE status = 'pending'")
        pending_withdrawals = cursor.fetchone()[0]
        
        cursor.execute("SELECT SUM(amount_usd) FROM withdraw_requests WHERE status = 'pending'")
        pending_withdrawals_amount = cursor.fetchone()[0] or 0
    
    stats_text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 <b>Пользователи:</b> {total_users}\n"
        f"💰 <b>Общий баланс:</b> {total_balance:.2f}$\n"
        f"💸 <b>Всего выплачено:</b> {total_earned:.2f}$\n\n"
        
        f"📱 <b>WhatsApp:</b>\n"
        f"   • Всего: {total_whatsapp}\n"
        f"   • Активные: {active_whatsapp}\n"
        f"   • В очереди: {pending_whatsapp}\n\n"
        
        f"🤖 <b>MAX:</b>\n"
        f"   • Всего: {total_max}\n"
        f"   • Активные: {active_max}\n"
        f"   • В очереди: {pending_max}\n\n"
        
        f"💬 <b>SMS WORK:</b>\n"
        f"   • Всего: {total_sms}\n"
        f"   • В очереди: {pending_sms}\n\n"
        
        f"💳 <b>Заявки на вывод:</b>\n"
        f"   • Ожидают: {pending_withdrawals}\n"
        f"   • Сумма: {pending_withdrawals_amount:.2f}$\n\n"
        
        f"⏰ <b>Активные холды:</b> {active_whatsapp + active_max}"
    )
    
    await callback.message.edit_text(stats_text, parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data == "admin_payouts")
async def admin_payouts(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Заявки на вывод
        cursor.execute("""
            SELECT COUNT(*), SUM(amount_usd) 
            FROM withdraw_requests 
            WHERE status = 'pending'
        """)
        pending_count, pending_amount = cursor.fetchone()
        pending_amount = pending_amount or 0
        
        cursor.execute("""
            SELECT COUNT(*), SUM(amount_usd) 
            FROM withdraw_requests 
            WHERE status = 'confirmed'
        """)
        confirmed_count, confirmed_amount = cursor.fetchone()
        confirmed_amount = confirmed_amount or 0
        
        cursor.execute("""
            SELECT COUNT(*), SUM(amount_usd) 
            FROM withdraw_requests 
            WHERE status = 'paid'
        """)
        paid_count, paid_amount = cursor.fetchone()
        paid_amount = paid_amount or 0
    
    payouts_text = (
        "💰 <b>Управление выплатами</b>\n\n"
        f"⏳ <b>Ожидают подтверждения:</b>\n"
        f"   • Заявок: {pending_count}\n"
        f"   • Сумма: {pending_amount:.2f}$\n\n"
        
        f"✅ <b>Подтвержденные:</b>\n"
        f"   • Заявок: {confirmed_count}\n"
        f"   • Сумма: {confirmed_amount:.2f}$\n\n"
        
        f"💸 <b>Выплаченные:</b>\n"
        f"   • Заявок: {paid_count}\n"
        f"   • Сумма: {paid_amount:.2f}$\n\n"
        
        "Выберите действие:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(
            text="🔄 Обработать выплаты",
            callback_data="process_payouts"
        )
    )
    builder.row(
        types.InlineKeyboardButton(
            text="📋 Список заявок",
            callback_data="show_payouts_list"
        )
    )
    builder.row(
        types.InlineKeyboardButton(
            text="🔙 Назад",
            callback_data="admin_panel"
        )
    )
    
    await callback.message.edit_text(payouts_text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data == "process_payouts")
async def process_payouts(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer("⏳ Обработка выплат...")
    
    # Обрабатываем выплаты партиями
    processed_count, failed_count = await process_withdrawals_batch(callback.from_user.id)
    
    if processed_count is None:
        await callback.message.answer("❌ Ошибка при обработке выплат.")
        return
    
    await callback.message.answer(
        f"✅ Обработка выплат завершена!\n\n"
        f"Успешно: {processed_count}\n"
        f"Ошибок: {failed_count}"
    )

@dp.callback_query(lambda c: c.data == "admin_add_balance")
async def admin_add_balance(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.answer(
        "💵 <b>Пополнение баланса бота</b>\n\n"
        "Введите сумму в долларах для пополнения:",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.crypto_pay_amount)

@dp.message(Form.crypto_pay_amount)
async def process_crypto_pay_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверная сумма. Введите положительное число.")
        return
    
    # Создаем инвойс в CryptoPay
    invoice = await create_crypto_pay_invoice(
        amount, 
        f"Пополнение баланса бота на {amount}$"
    )
    
    if not invoice or 'pay_url' not in invoice:
        await message.answer("❌ Ошибка создания платежной ссылки. Попробуйте позже.")
        await state.clear()
        return
    
    pay_url = invoice['pay_url']
    invoice_id = invoice['invoice_id']
    
    await message.answer(
        f"💳 <b>Ссылка для пополнения баланса бота</b>\n\n"
        f"Сумма: {amount:.2f}$\n\n"
        f"🔗 <a href='{pay_url}'>Перейти к оплате</a>\n\n"
        f"После оплаты баланс бота будет автоматически пополнен.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )
    
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_warn")
async def admin_warn(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.answer(
        "⚠️ <b>Выдать предупреждение</b>\n\n"
        "Введите ID пользователя:",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.warn_user)

@dp.message(Form.warn_user)
async def process_warn_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный формат ID. Введите числовой ID.")
        return
    
    # Проверяем существование пользователя
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username, warnings FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone()
        
        if not user_data:
            await message.answer("❌ Пользователь не найден.")
            await state.clear()
            return
        
        username, current_warnings = user_data
        
        # Добавляем предупреждение
        new_warnings = current_warnings + 1
        cursor.execute(
            "UPDATE users SET warnings = ? WHERE user_id = ?",
            (new_warnings, user_id)
        )
        conn.commit()
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            user_id,
            f"⚠️ <b>Вам выдано предупреждение!</b>\n\n"
            f"Текущее количество предупреждений: {new_warnings}/5\n"
            f"При достижении 5 предупреждений аккаунт будет заблокирован.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await message.answer(
        f"✅ Пользователю @{username or 'N/A'} (ID: {user_id}) выдано предупреждение.\n"
        f"Текущее количество: {new_warnings}/5"
    )
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_message")
async def admin_message(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.answer(
        "✉️ <b>Отправка сообщения пользователю</b>\n\n"
        "Введите ID пользователя:",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Form.admin_message)

@dp.message(Form.admin_message)
async def process_admin_message_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный формат ID. Введите числовой ID.")
        return
    
    await state.update_data(target_user_id=user_id)
    await message.answer(
        "Теперь введите сообщение для отправки:"
    )
    await state.set_state(Form.admin_message_text)

@dp.message(Form.admin_message_text)
async def process_admin_message_text(message: Message, state: FSMContext):
    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    message_text = message.text
    
    try:
        await bot.send_message(
            target_user_id,
            f"📨 <b>Сообщение от администратора:</b>\n\n{message_text}",
            parse_mode=ParseMode.HTML
        )
        await message.answer(f"✅ Сообщение отправлено пользователю ID: {target_user_id}")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить сообщение пользователю ID: {target_user_id}")
        logger.error(f"Ошибка отправки сообщения: {e}")
    
    await state.clear()

@dp.callback_query(lambda c: c.data == "show_payouts_list")
async def show_payouts_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Получаем последние 10 заявок
        cursor.execute("""
            SELECT wr.id, wr.user_id, wr.amount_usd, wr.status, wr.created_at, u.username
            FROM withdraw_requests wr
            JOIN users u ON wr.user_id = u.user_id
            ORDER BY wr.created_at DESC
            LIMIT 10
        """)
        
        withdrawals = cursor.fetchall()
    
    if not withdrawals:
        await callback.message.answer("❌ Нет заявок на вывод.")
        return
    
    response = "📋 <b>Последние заявки на вывод</b>\n\n"
    
    for withdraw in withdrawals:
        withdraw_id, user_id, amount, status, created_at, username = withdraw
        response += (
            f"🔹 ID: {withdraw_id}\n"
            f"👤 Пользователь: @{username or 'N/A'} (ID: {user_id})\n"
            f"💰 Сумма: {amount:.2f}$\n"
            f"📊 Статус: {status}\n"
            f"📅 Дата: {created_at}\n"
            f"────────────────────\n"
        )
    
    await callback.message.answer(response, parse_mode=ParseMode.HTML)

# Дополнительные обработчики для админ-панели
@dp.callback_query(lambda c: c.data == "admin_whatsapp_accounts")
async def admin_whatsapp_accounts(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.edit_text(
        "📱 <b>Управление WhatsApp аккаунтами</b>\n\n"
        "Выберите аккаунт для управления:",
        reply_markup=await whatsapp_accounts_keyboard(),
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(lambda c: c.data == "admin_max_accounts")
async def admin_max_accounts(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.edit_text(
        "🤖 <b>Управление MAX аккаунтами</b>\n\n"
        "Выберите аккаунт для управления:",
        reply_markup=await max_accounts_keyboard(),
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(lambda c: c.data == "admin_sms_works")
async def admin_sms_works(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    await callback.message.edit_text(
        "💬 <b>Управление SMS работ</b>\n\n"
        "Выберите работу для управления:",
        reply_markup=await sms_works_keyboard(),
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(lambda c: c.data == "admin_active_hold")
async def admin_active_hold(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Получаем статистику по холдам
        cursor.execute("""
            SELECT COUNT(*) FROM whatsapp_numbers 
            WHERE status IN ('hold_active', 'active')
        """)
        whatsapp_count = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM max_numbers 
            WHERE status = 'active'
        """)
        max_count = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM whatsapp_numbers 
            WHERE status = 'completed'
        """)
        whatsapp_completed = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM max_numbers 
            WHERE status = 'completed'
        """)
        max_completed = cursor.fetchone()[0]
    
    response = (
        "⏰ <b>Активные холды - Статистика</b>\n\n"
        f"📱 <b>WhatsApp:</b>\n"
        f"   • Активных: {whatsapp_count}\n"
        f"   • Завершенных: {whatsapp_completed}\n\n"
        f"🤖 <b>MAX:</b>\n"
        f"   • Активных: {max_count}\n"
        f"   • Завершенных: {max_completed}\n\n"
        "Используйте команды:\n"
        "/active_hold - детальная информация\n"
        "/check_failed - слетевшие аккаунты"
    )
    
    await callback.message.edit_text(response, parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data == "admin_report_failed")
async def admin_report_failed(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    await callback.answer()
    
    # Получаем количество активных аккаунтов
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM whatsapp_numbers WHERE status IN ('hold_active', 'active')")
        whatsapp_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM max_numbers WHERE status = 'active')")
        max_count = cursor.fetchone()[0]
    
    total_count = whatsapp_count + max_count
    
    await callback.message.edit_text(
        f"🚨 <b>Сообщить о слёте аккаунта</b>\n\n"
        f"📱 Активных WhatsApp: {whatsapp_count}\n"
        f"🤖 Активных MAX: {max_count}\n"
        f"📊 Всего активных: {total_count}\n\n"
        f"Выберите аккаунт, который слетел:",
        reply_markup=await failed_accounts_keyboard(),
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(lambda c: c.data.startswith("report_failed_whatsapp:"))
async def report_failed_whatsapp(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE whatsapp_numbers 
            SET status = 'failed', failed_at = datetime('now')
            WHERE id = ?
        """, (account_id,))
        
        cursor.execute("""
            SELECT user_id, phone FROM whatsapp_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"❌ Ваш WhatsApp аккаунт {phone} был помечен как слетевший администратором.\n\n"
                    f"Если это ошибка, обратитесь в поддержку."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
            
            # Уведомляем всех администраторов
            admin_message = (
                f"🚨 <b>WhatsApp аккаунт помечен как слетевший</b>\n\n"
                f"Аккаунт ID: {account_id}\n"
                f"Номер: {phone}\n"
                f"Пользователь: ID {user_id}\n"
                f"Администратор: @{callback.from_user.username or 'без username'}"
            )
            
            await notify_admins(admin_message)
            
            await callback.answer(f"✅ WhatsApp аккаунт {phone} помечен как слетевший")
            await callback.message.edit_text(
                f"✅ WhatsApp аккаунт {phone} успешно помечен как слетевший!",
                reply_markup=None
            )

@dp.callback_query(lambda c: c.data.startswith("report_failed_max:"))
async def report_failed_max(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет прав администратора")
        return
    
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE max_numbers 
            SET status = 'failed', completed = 0
            WHERE id = ?
        """, (account_id,))
        
        cursor.execute("""
            SELECT user_id, phone FROM max_numbers WHERE id = ?
        """, (account_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, phone = result
            conn.commit()
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f"❌ Ваш MAX аккаунт {phone} был помечен как слетевший администратором.\n\n"
                    f"Если это ошибка, обратитесь в поддержку."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления пользователя: {e}")
            
            # Уведомляем всех администраторов
            admin_message = (
                f"🚨 <b>MAX аккаунт помечен как слетевший</b>\n\n"
                f"Аккаунт ID: {account_id}\n"
                f"Номер: {phone}\n"
                f"Пользователь: ID {user_id}\n"
                f"Администратор: @{callback.from_user.username or 'без username'}"
            )
            
            await notify_admins(admin_message)
            
            await callback.answer(f"✅ MAX аккаунт {phone} помечен как слетевший")
            await callback.message.edit_text(
                f"✅ MAX аккаунт {phone} успешно помечен как слетевший!",
                reply_markup=None
            )

@dp.callback_query(lambda c: c.data == "no_accounts")
async def no_accounts(callback: CallbackQuery):
    await callback.answer("❌ Нет активных аккаунтов")
    await callback.message.edit_text(
        "❌ Нет активных аккаунтов на холде.\n"
        "Нечего отмечать как слетевшие.",
        reply_markup=None
    )

# Запуск бота
async def main():
    # Инициализация базы данных
    init_db()
    
    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
