import asyncio
import logging
import json
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    ReplyKeyboardRemove,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from shared import get_all_orders, ADMIN_ID, bot, STATUS_EMOJI_MAP, pluralize_days, get_full_name
from payment import payment_router
from executor_menu import executor_menu_router, is_executor, get_executor_menu_keyboard
from executor_menu import ExecutorStates

# Глобальная карта статусов для консистентности
STATUS_EMOJI_MAP = {
    "Редактируется": "📝",
    "Рассматривается": "🆕",
    "Ожидает подтверждения": "🤔",
    "Исполнитель найден": "🙋‍♂️",
    "Ожидает оплаты": "💳",
    "Принята": "✅",
    "В работе": "⏳",
    "Выполнена": "🎉",
    "Отменена": "❌",
}

# Загрузка переменных окружения
load_dotenv()

# Используем токен и ID из .env файла, но если их нет, используем "зашитые"
BOT_TOKEN = os.getenv("BOT_TOKEN", "7763016986:AAFW4Rwh012_bfh8Jt0E_zaq5abvzenr4bE")
# Добавляю EXECUTOR_IDS
EXECUTOR_IDS = [int(x) for x in os.getenv("EXECUTOR_IDS", "123456789").split(",") if x.strip().isdigit()]

ALLOWED_EXTENSIONS = {"pdf", "docx", "png", "jpeg", "jpg"}
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
admin_router = Router()
dp.include_router(admin_router)
executor_router = Router()
dp.include_router(executor_router)
dp.include_router(payment_router)
dp.include_router(executor_menu_router)

# Google Sheets
GOOGLE_SHEET_ID = "1D15yyPKHyN1Vw8eRnjT79xV28cwL_q5EIZa97tgTF2U"
GOOGLE_SHEET_HEADERS = [
    "Группа", "Университет", "Тип работы", "Методичка", "Задание", "Пример работы", "Дата сдачи", "Комментарий"
]

# --- FSM для админа ---
class AssignExecutor(StatesGroup):
    waiting_for_id = State()

class AdminApproval(StatesGroup):
    waiting_for_new_price = State()

# --- FSM для исполнителя ---
class ExecutorResponse(StatesGroup):
    waiting_for_price = State()
    waiting_for_deadline = State()
    waiting_for_comment = State()
    waiting_for_confirm = State()  # Новый этап

# --- Состояния (FSM) ---
class OrderState(StatesGroup):
    group_name = State()
    university_name = State()
    teacher_name = State()  # Новое состояние
    gradebook = State()     # Новое состояние
    subject = State()       # Новое состояние
    subject_other = State() # Новое состояние
    work_type = State()
    work_type_other = State()
    guidelines_choice = State()
    guidelines_upload = State()
    task_upload = State()
    example_choice = State()
    example_upload = State()
    deadline = State()
    comments = State()
    confirmation = State()

class AdminContact(StatesGroup):
    waiting_for_message = State()

class ClientRevision(StatesGroup):
    waiting_for_revision_comment = State()

# --- Клавиатуры ---
# --- FSM для настроек исполнителей ---
class AdminSettings(StatesGroup):
    waiting_for_executor_name = State()
    waiting_for_executor_id = State()
    waiting_for_delete_id = State()

    # --- Новый этап FSM для подтверждения ---
class ExecutorResponse(StatesGroup):
    waiting_for_price = State()
    waiting_for_deadline = State()
    waiting_for_comment = State()
    waiting_for_confirm = State()  # Новый этап

EXECUTORS_FILE = "executors.json"

def get_admin_settings_keyboard():
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить исполнителя", callback_data="admin_add_executor")],
        [InlineKeyboardButton(text="➖ Удалить исполнителя", callback_data="admin_delete_executor")],
        [InlineKeyboardButton(text="👥 Показать всех исполнителей", callback_data="admin_show_executors")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back_to_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_skip_keyboard_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="admin_skip_executor_name")]
    ])

def get_executors_list():
    if not os.path.exists(EXECUTORS_FILE):
        return []
    with open(EXECUTORS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return []

def save_executors_list(executors):
    with open(EXECUTORS_FILE, "w", encoding="utf-8") as f:
        json.dump(executors, f, ensure_ascii=False, indent=4)

def get_executors_info_keyboard():
    executors = get_executors_list()
    if not executors:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Нет исполнителей", callback_data="none")]])
    buttons = []
    for ex in executors:
        label = f"{ex.get('name') or 'Без ФИО'} | ID: {ex['id']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data="none")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_executors_delete_keyboard():
    executors = get_executors_list()
    if not executors:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Нет исполнителей", callback_data="none")]])
    buttons = []
    for ex in executors:
        label = f"{ex.get('name') or 'Без ФИО'} | ID: {ex['id']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admin_delete_executor_id_{ex['id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_executors_assign_keyboard(order_id):
    executors = get_executors_list()
    buttons = []
    if executors:
        for ex in executors:
            label = f"{ex.get('name') or 'Без ФИО'} | ID: {ex['id']}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"assign_executor_select_{ex['id']}")])
        buttons.append([InlineKeyboardButton(text="Ввести ID вручную", callback_data=f"assign_executor_manual_{order_id}")])
    # Добавляем кнопку 'Назад'
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_view_order_{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

@admin_router.message(F.text == "⚙️ Настройки")
async def admin_settings_menu(message: Message, state: FSMContext):
    if message.from_user.id != int(ADMIN_ID): return
    await state.clear()
    await message.answer("⚙️ Настройки исполнителей:", reply_markup=get_admin_settings_keyboard())

@admin_router.callback_query(F.data == "admin_settings")
async def admin_settings_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ Настройки исполнителей:", reply_markup=get_admin_settings_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data == "admin_add_executor")
async def admin_add_executor_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSettings.waiting_for_executor_name)
    await callback.message.edit_text("✍️ Введите ФИО исполнителя (или пропустите):", reply_markup=get_skip_keyboard_admin())
    await callback.answer()

@admin_router.callback_query(F.data == "admin_skip_executor_name", AdminSettings.waiting_for_executor_name)
async def admin_skip_executor_name(callback: CallbackQuery, state: FSMContext):
    await state.update_data(executor_name="")
    await state.set_state(AdminSettings.waiting_for_executor_id)
    await callback.message.edit_text("🔢 Введите ID исполнителя (обязательно):")
    await callback.answer()

@admin_router.message(AdminSettings.waiting_for_executor_name)
async def admin_executor_name_input(message: Message, state: FSMContext):
    await state.update_data(executor_name=message.text)
    await state.set_state(AdminSettings.waiting_for_executor_id)
    await message.answer("🔢 Введите ID исполнителя (обязательно):")

@admin_router.message(AdminSettings.waiting_for_executor_id)
async def admin_executor_id_input(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("ID должен быть числом. Попробуйте еще раз.")
        return
    executor_id = int(message.text)
    data = await state.get_data()
    name = data.get("executor_name", "")
    executors = get_executors_list()
    if any(ex['id'] == executor_id for ex in executors):
        await message.answer("Такой исполнитель уже есть.")
        return
    executors.append({"id": executor_id, "name": name})
    save_executors_list(executors)
    await state.clear()
    await message.answer("✅ Исполнитель добавлен!", reply_markup=get_admin_settings_keyboard())
    await message.answer("👥 Текущие исполнители:", reply_markup=get_executors_info_keyboard())

@admin_router.callback_query(F.data == "admin_delete_executor")
async def admin_delete_executor_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSettings.waiting_for_delete_id)
    await callback.message.edit_text("Выберите исполнителя для удаления:", reply_markup=get_executors_delete_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_delete_executor_id_"), AdminSettings.waiting_for_delete_id)
async def admin_delete_executor_confirm(callback: CallbackQuery, state: FSMContext):
    executor_id = int(callback.data.split("_")[-1])
    executors = get_executors_list()
    executors = [ex for ex in executors if ex['id'] != executor_id]
    save_executors_list(executors)
    await state.clear()
    await callback.message.edit_text("✅ Исполнитель удален!", reply_markup=get_admin_settings_keyboard())
    await callback.message.answer("👥 Текущие исполнители:", reply_markup=get_executors_info_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data == "admin_show_executors")
async def admin_show_executors(callback: CallbackQuery, state: FSMContext):
    executors = get_executors_list()
    if not executors:
        text = "Нет исполнителей."
    else:
        text = "👥 Текущие исполнители:\n\n" + "\n".join([
            f"{ex.get('name') or 'Без ФИО'} | ID: {ex['id']}" for ex in executors
        ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_settings")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()
@executor_router.callback_query(F.data == "executor_back_to_price", ExecutorResponse.waiting_for_deadline)
async def executor_back_to_price_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    await state.set_state(ExecutorResponse.waiting_for_price)
    await callback.message.edit_text("Отлично! Укажите вашу цену:", reply_markup=get_price_keyboard(order_id))
    await callback.answer()

@admin_router.callback_query(F.data == "admin_back_to_menu")
async def admin_back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Добро пожаловать в панель администратора!", reply_markup=None)
    await bot.send_message(callback.from_user.id, "Главное меню:", reply_markup=get_admin_keyboard())
    await callback.answer()


def get_admin_keyboard():
    buttons = [
        [KeyboardButton(text="📦 Все заказы")],
        [KeyboardButton(text="⚙️ Настройки")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_executor_confirm_keyboard(order_id):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_back_to_materials:{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_price_keyboard(order_id):
    buttons = [
        [InlineKeyboardButton(text=f"{i} ₽", callback_data=f"price_{i}") for i in range(500, 2501, 500)],
        [InlineKeyboardButton(text=f"{i} ₽", callback_data=f"price_{i}") for i in range(3000, 5001, 1000)],
        [InlineKeyboardButton(text="💬 Ввести вручную", callback_data="price_manual")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_back_to_invite:{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_deadline_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="1 день", callback_data="deadline_1 день"),
            InlineKeyboardButton(text="3 дня", callback_data="deadline_3 дня"),
            InlineKeyboardButton(text="До дедлайна", callback_data="deadline_До дедлайна"),
        ],
        [InlineKeyboardButton(text="💬 Ввести свой вариант", callback_data="deadline_manual")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="executor_back_to_price")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_main_reply_keyboard():
    buttons = [
        [KeyboardButton(text="🆕 Новая заявка"), KeyboardButton(text="📂 Мои заявки")],
        [KeyboardButton(text="❓ Помощь"), KeyboardButton(text="👨‍💻 Связаться с администратором")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_back_to_main_menu_keyboard():
    buttons = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_keyboard():
    buttons = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_yes_no_keyboard(prefix: str):
    """Возвращает клавиатуру с кнопками 'Да' и 'Нет'."""
    buttons = [
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"{prefix}_yes"),
            InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"{prefix}_no")
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_user_order_keyboard(order_id, status):
    buttons = []
    # Кнопка 'Оплатить' если статус 'Ожидает оплаты'
    if status == "Ожидает оплаты":
        buttons.append([InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay_{order_id}")])
    # Кнопка 'Отказаться' всегда
    buttons.append([InlineKeyboardButton(text="❌ Отказаться", callback_data=f"user_cancel_order:{order_id}")])
    # Кнопка 'К списку заявок'
    buttons.append([InlineKeyboardButton(text="⬅️ К списку заявок", callback_data="my_orders_list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_work_type_keyboard():
    buttons = [
        [InlineKeyboardButton(text="Контрольная", callback_data="work_type_Контрольная")],
        [InlineKeyboardButton(text="Расчётно-графическая", callback_data="work_type_Расчётно-графическая")],
        [InlineKeyboardButton(text="Курсовая", callback_data="work_type_Курсовая")],
        [InlineKeyboardButton(text="Тест", callback_data="work_type_Тест")],
        [InlineKeyboardButton(text="Отчёт", callback_data="work_type_Отчёт")],
        [InlineKeyboardButton(text="Диплом", callback_data="work_type_Диплом")],
        [InlineKeyboardButton(text="Другое (ввести вручную)", callback_data="work_type_other")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_subject_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="Мат. анализ", callback_data="subject_Математический анализ"),
            InlineKeyboardButton(text="Алгебра", callback_data="subject_Алгебра и геометрия")
        ],
        [
            InlineKeyboardButton(text="Программирование", callback_data="subject_Программирование"),
            InlineKeyboardButton(text="История", callback_data="subject_История России")
        ],
        [
            InlineKeyboardButton(text="Философия", callback_data="subject_Философия"),
            InlineKeyboardButton(text="Английский язык", callback_data="subject_Английский язык")
        ],
        [
            InlineKeyboardButton(text="Экономика", callback_data="subject_Экономическая теория"),
            InlineKeyboardButton(text="Русский язык", callback_data="subject_Русский язык и культура речи")
        ],
        [InlineKeyboardButton(text="Другое (ввести вручную)", callback_data="subject_other")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_skip_keyboard(prefix: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"skip_{prefix}")]
    ])
    
def get_confirmation_keyboard():
    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_order")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")],
        [InlineKeyboardButton(text="👨‍💻 Связаться с администратором", callback_data="contact_admin_in_order")]
        # Кнопка '⬅️ Назад' убрана на этапе подтверждения
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_final_approval_keyboard(order_id, price):
    buttons = [
        [InlineKeyboardButton(text=f"✅ Утвердить и отправить ({price} ₽)", callback_data=f"final_approve_{order_id}_{price}")],
        [InlineKeyboardButton(text="✏️ Изменить цену", callback_data=f"final_change_price_{order_id}")],
        [InlineKeyboardButton(text="❌ Отклонить предложение", callback_data=f"final_reject_{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_skip_comment_keyboard():
    buttons = [
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip_comment")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_order_keyboard(order, show_materials_button=True):
    buttons = []
    if 'order_id' not in order:
        # Возвращаем только кнопку 'Назад', чтобы не было KeyError
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    status = order.get('status')
    executor_is_admin = str(order.get('executor_id')) == str(ADMIN_ID)
    # Кнопка 'Выбрать исполнителя' только если статус 'Рассматривается' и исполнитель не админ
    if status == "Рассматривается" and not executor_is_admin:
        buttons.append([
            InlineKeyboardButton(text="👤 Выбрать исполнителя", callback_data=f"assign_executor_{order['order_id']}")
        ])
    # Кнопка 'Взять заказ' если статус 'Рассматривается' или 'Ожидает подтверждения'
    if status in ["Рассматривается", "Ожидает подтверждения"]:
        buttons.append([
            InlineKeyboardButton(text="❇️ Взять заказ", callback_data=f"admin_self_take_{order['order_id']}")
        ])
    # Кнопка "Сохранить в таблицу"
    buttons.append([InlineKeyboardButton(text="📊 Сохранить в таблицу", callback_data=f"admin_save_to_gsheet:{order['order_id']}")])
    # Кнопка "Удалить заявку"
    buttons.append([InlineKeyboardButton(text="❌ Отказаться от заявки", callback_data=f"admin_delete_order:{order['order_id']}")])
    # Кнопка "Посмотреть материалы заказа"
    has_files = order.get('guidelines_file') or order.get('task_file') or order.get('example_file') or order.get('task_text')
    if show_materials_button and has_files:
        buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order['order_id']}")])
    if not show_materials_button:
        buttons.append([InlineKeyboardButton(text="⬅️ Скрыть материалы", callback_data=f"admin_hide_materials:{order['order_id']}")])
    # Кнопка 'Назад' всегда последней
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@admin_router.callback_query(F.data == "admin_back")
async def admin_back_handler(callback: CallbackQuery, state: FSMContext):
    await show_admin_orders_list(callback)
    await callback.answer()

# --- Админ-панель ---

# Фильтр, чтобы эти хендлеры работали только для админа
@admin_router.message(Command("admin"))
async def cmd_admin_panel(message: Message, state: FSMContext):
    # Убедимся, что это админ
    if message.from_user.id != int(ADMIN_ID):
        return
    await state.clear()
    await message.answer(
        "Добро пожаловать в панель администратора!",
        reply_markup=get_admin_keyboard()
    )

async def show_admin_orders_list(message_or_callback):
    """Показывает список всех заказов для админа, используя edit_text для callback и answer для message."""
    user_id = message_or_callback.from_user.id
    if user_id != int(ADMIN_ID): return

    orders = get_all_orders()
    if not orders:
        if hasattr(message_or_callback, 'message'):
            await message_or_callback.message.edit_text("Пока нет ни одного заказа.")
        else:
            await message_or_callback.answer("Пока нет ни одного заказа.")
        return

    text = "Все заказы:"
    keyboard_buttons = []
    for order in reversed(orders[-20:]): # Показываем последние 20
        order_id = order['order_id']
        order_status = order.get('status', 'N/A')
        emoji = STATUS_EMOJI_MAP.get(order_status, "📄")
        work_type_raw = order.get('work_type', 'Заявка')
        work_type = work_type_raw.replace('work_type_', '')
        first_name = order.get('first_name', '')
        last_name = order.get('last_name', '')
        full_name = f"{first_name} {last_name}".strip()
        username = order.get('username', 'N/A')
        display_name = full_name if full_name else username
        # Добавляем статус после ФИО
        button_text = f"{emoji} {work_type} №{order_id} - {order_status}"
        keyboard_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"admin_view_order_{order_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    if hasattr(message_or_callback, 'message'):
        try:
            await message_or_callback.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            await message_or_callback.message.answer(text, reply_markup=keyboard)
    else:
        await message_or_callback.answer(text, reply_markup=keyboard)

@admin_router.message(F.text == "📦 Все заказы")
async def show_all_orders_handler(message_or_callback):
    await show_admin_orders_list(message_or_callback)

@admin_router.callback_query(F.data.startswith("admin_view_order_"))
async def admin_view_order_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != int(ADMIN_ID): return
    order_id = int(callback.data.split("_")[-1])
    orders = get_all_orders()
    target_order = next((order for order in orders if order['order_id'] == order_id), None)
    if not target_order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    status = target_order.get('status')

    if status == 'Ожидает подтверждения' and 'executor_offer' in target_order:
        offer = target_order['executor_offer']
        executor_full_name = offer.get('executor_full_name', 'Без имени')
        price = offer.get('price')
        deadline = offer.get('deadline', 'N/A')
        executor_comment = offer.get('executor_comment', 'Нет')
        subject = target_order.get('subject', 'Не указан')
        deadline_str = pluralize_days(deadline)

        admin_notification = f"""✅ Исполнитель {executor_full_name} готов взяться за заказ по предмету \"{subject}\"
    
<b>Предложенные условия:</b>
💰 <b>Цена:</b> {price} ₽
⏳ <b>Срок:</b> {deadline_str}
💬 <b>Комментарий исполнителя:</b> {executor_comment or 'Нет'}"""

        keyboard = get_admin_final_approval_keyboard(order_id, price)
        try:
            await callback.message.edit_text(admin_notification, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            await callback.message.answer(admin_notification, parse_mode="HTML", reply_markup=keyboard)

    elif status == 'Отправлен на проверку':
        submitted_work = target_order.get('submitted_work')
        submitted_at = target_order.get('submitted_at', '—')
        subject = target_order.get('subject', 'Не указан')
        work_type = target_order.get('work_type', 'Не указан').replace('work_type_', '')
        admin_text = f"Исполнитель выполнил заказ по предмету <b>{subject}</b>\nТип работы: <b>{work_type}</b>\nДата выполнения: <b>{submitted_at}</b>"
        admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Проверить работу", callback_data=f"admin_check_work_{order_id}")],
            [InlineKeyboardButton(text="Утвердить работу", callback_data=f"admin_approve_work_{order_id}")],
            [InlineKeyboardButton(text="Отказаться от работы", callback_data=f"admin_reject_work_{order_id}")]
        ])
        if submitted_work and submitted_work.get('file_id'):
            await callback.message.delete()
            await bot.send_document(
                callback.from_user.id,
                submitted_work['file_id'],
                caption=admin_text,
                parse_mode="HTML",
                reply_markup=admin_keyboard
            )
        else:
            await callback.message.edit_text(admin_text, parse_mode="HTML", reply_markup=admin_keyboard)
            
    elif status == "Утверждено администратором":
        full_name = get_full_name(target_order)
        header = f"Детали заказа №{order_id} от клиента ({full_name})\n"
        if target_order.get('creation_date'):
            header += f"Дата создания: {target_order.get('creation_date')}\n"
        
        group = target_order.get("group_name", "Не указана")
        university = target_order.get("university_name", "Не указан")
        teacher = target_order.get("teacher_name", "Не указан")
        gradebook = target_order.get("gradebook", "Не указан")
        subject = target_order.get("subject", "Не указан")
        work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
        work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
        guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
        task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
        example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
        deadline = target_order.get('deadline', 'Не указана')

        details_text = f"""{header}
Группа: {group}
ВУЗ: {university}
Преподаватель: {teacher}
Номер зачетки: {gradebook}
Предмет: {subject}
Тип работы: {work_type_str}
Методичка: {guidelines}
Задание: {task}
Пример: {example}
Дедлайн: {deadline}"""

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Вернуться к заявкам", callback_data="admin_back")]
        ])
        
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard)

    else: # --- Обычное поведение для остальных статусов ---
        summary_text = await build_summary_text(target_order)
        full_name = f"{target_order.get('first_name', '')} {target_order.get('last_name', '')}".strip()
        header = f"\n<b>Детали заказа №{order_id} от клиента ({full_name})</b>\n"
        if target_order.get('creation_date'):
            header += f"<b>Дата создания:</b> {target_order.get('creation_date')}\n"
        details_text = header + "\n" + summary_text
        show_materials_button = bool(
            target_order.get("guidelines_file") or
            target_order.get("task_file") or
            target_order.get("task_text") or
            target_order.get("example_file")
        )
        keyboard = get_admin_order_keyboard(target_order, show_materials_button=True)
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard, parse_mode="HTML")

    await callback.answer()

@admin_router.callback_query(F.data.startswith("assign_executor_"))
async def assign_executor_start_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != int(ADMIN_ID): return
    if callback.data.startswith("assign_executor_manual_"):
        try:
            order_id = int(callback.data.split("_")[-1])
        except ValueError:
            await callback.answer("Некорректный ID заказа.", show_alert=True)
            return
        await state.update_data(order_id=order_id)
        await assign_executor_manual_handler(callback, state)
        return
    try:
        order_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Некорректный ID заказа.", show_alert=True)
        return
    await state.update_data(order_id=order_id)
    executors = get_executors_list()
    if executors:
        await callback.message.edit_text(
            "Выберите исполнителя для назначения:",
            reply_markup=get_executors_assign_keyboard(order_id)
        )
        # Не ставим состояние FSM здесь!
    else:
        await callback.message.edit_text(
            "Ваш список исполнителей пуст.\n\nВы можете ввести ID исполнителя вручную:")
        await state.set_state(AssignExecutor.waiting_for_id)
    await callback.answer()

async def send_order_to_executor(message_or_callback, order_id: int, executor_id: int):
    """Находит заказ, присваивает исполнителя и отправляет ему уведомление (только через orders.json)."""
    orders = get_all_orders()
    target_order = None
    for order in orders:
        if order.get("order_id") == order_id:
            target_order = order
            break
    if not target_order:
        text = f"Критическая ошибка: заказ №{order_id} не найден для обновления."
        if hasattr(message_or_callback, 'message'):
            await message_or_callback.message.answer(text)
        else:
            await message_or_callback.answer(text)
        return

    target_order['status'] = "Ожидает подтверждения"
    target_order['executor_id'] = executor_id
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')
    subject = target_order.get('subject', 'Не указан')
    deadline = target_order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
         InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")],
    ])
    try:
        await bot.send_message(executor_id, executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
        success_text = f"✅ Предложение по заказу №{order_id} отправлено исполнителю с ID {executor_id}."
        if hasattr(message_or_callback, 'message'):
            await message_or_callback.message.answer(success_text)
        else:
            await message_or_callback.answer(success_text)
    except Exception as e:
        error_text = f"⚠️ Не удалось отправить уведомление исполнителю (ID: {executor_id}).\n\n<b>Ошибка:</b> {e}"
        target_order['status'] = "Рассматривается"
        target_order.pop('executor_id', None)
        with open("orders.json", "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=4)
        if hasattr(message_or_callback, 'message'):
            await message_or_callback.message.answer(error_text, parse_mode="HTML")
        else:
            await message_or_callback.answer(error_text, parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("assign_executor_select_"))
async def assign_executor_select_handler(callback: CallbackQuery, state: FSMContext):
    print("assign_executor_select_handler called", callback.data)
    # Проверяем, что после assign_executor_select_ действительно число (id)
    try:
        executor_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Некорректный ID исполнителя.", show_alert=True)
        return
    data = await state.get_data()
    order_id = data.get('order_id')
    orders = get_all_orders()
    target_order = None
    for order in orders:
        if order.get("order_id") == order_id:
            target_order = order
            break
    if not target_order:
        await callback.message.answer("Критическая ошибка: заказ не найден для обновления.")
        await state.clear()
        return
    # Назначаем исполнителя и меняем статус
    target_order['status'] = "Ожидает подтверждения"
    target_order['executor_id'] = executor_id
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    try:
        await callback.message.edit_text(
            f"✅ Предложение отправлено исполнителю с ID {executor_id} для заказа №{order_id}.",
            reply_markup=None
        )
    except Exception as e:
        if "message is not modified" in str(e):
            pass
        else:
            raise
    # Уведомление для исполнителя
    work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')
    subject = target_order.get('subject', 'Не указан')
    deadline = target_order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
         InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")]
    ])
    try:
        await bot.send_message(executor_id, executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
    except Exception as e:
        await callback.message.answer(f"⚠️ Не удалось отправить уведомление исполнителю (ID: {executor_id}). Ошибка: {e}")
        target_order['status'] = "Рассматривается"
        target_order.pop('executor_id', None)
        with open("orders.json", "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=4)
    await state.clear()

@admin_router.callback_query(F.data == "assign_executor_manual")
async def assign_executor_manual_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AssignExecutor.waiting_for_id)
    await callback.message.edit_text("Введите Telegram ID исполнителя:")
    await callback.answer()

@admin_router.message(AssignExecutor.waiting_for_id)
async def assign_executor_process_id_handler(message: Message, state: FSMContext):
    if message.from_user.id != int(ADMIN_ID): return
    
    if not message.text.isdigit():
        await message.answer("Ошибка: ID должен быть числом. Попробуйте еще раз.")
        return

    executor_id = int(message.text)
    data = await state.get_data()
    order_id = data.get('order_id')
    
    # Находим и обновляем заказ
    orders = get_all_orders()
    target_order = None
    for order in orders:
        if order.get("order_id") == order_id:
            order['status'] = "Ожидает подтверждения"
            order['executor_id'] = executor_id
            target_order = order
            break

    if not target_order:
        await message.answer("Критическая ошибка: заказ не найден для обновления.")
        await state.clear()
        return

    # Сохраняем обновленный список заказов
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    
    # Уведомляем всех
    await message.answer(f"✅ Предложение отправлено исполнителю с ID {executor_id} для заказа №{order_id}.")
    
    # Уведомление для исполнителя
    work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')
    subject = target_order.get('subject', 'Не указан')
    deadline = target_order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
         InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")]
    ])
    try:
        await bot.send_message(executor_id, executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить уведомление исполнителю (ID: {executor_id}). Ошибка: {e}")
        target_order['status'] = "Рассматривается"
        target_order.pop('executor_id', None)
        with open("orders.json", "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=4)
    await state.clear()


async def send_order_files_to_user(user_id: int, order_data: dict, with_details: bool = True):
    """Отправляет все файлы из заказа указанному пользователю."""
    if with_details:
        details_text = await build_summary_text(order_data)
        await bot.send_message(user_id, "<b>Детали заказа:</b>\n\n" + details_text, parse_mode="HTML")

    async def send_file(file_data, caption):
        if not file_data: return
        if file_data['type'] == 'photo':
            await bot.send_photo(user_id, file_data['id'], caption=caption)
        else:
            await bot.send_document(user_id, file_data['id'], caption=caption)

    await send_file(order_data.get('guidelines_file'), "📄 Методичка")
    
    if order_data.get('task_file'):
        await send_file(order_data.get('task_file'), "📑 Задание")
    elif order_data.get('task_text'):
        await bot.send_message(user_id, f"📑 Текст задания:\n\n{order_data['task_text']}")
    
    await send_file(order_data.get('example_file'), "📄 Пример работы")

# --- Вспомогательная функция для получения полного имени пользователя ---
def get_full_name(user_or_dict):
    if isinstance(user_or_dict, dict):
        first = user_or_dict.get('first_name', '')
        last = user_or_dict.get('last_name', '')
    else:
        first = getattr(user_or_dict, 'first_name', '')
        last = getattr(user_or_dict, 'last_name', '')
    full = f"{first} {last}".strip()
    return full if full else "Без имени"
# --- Логика исполнителя ---
@executor_router.callback_query(F.data.startswith("executor_accept_"))
async def executor_accept_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    orders = get_all_orders()
    target_order = None
    for o in orders:
        if o.get("order_id") == order_id:
            if o.get('executor_id') != callback.from_user.id:
                 await callback.answer("Это предложение не для вас или оно уже неактуально.", show_alert=True)
                 return
            o['status'] = "Исполнитель найден"
            target_order = o
            break
    if not target_order:
        await callback.answer("Это предложение уже неактуально.", show_alert=True)
        return
    # Сохраняем изменение статуса
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    await state.set_state(ExecutorResponse.waiting_for_price)
    await state.update_data(order_id=order_id)
    await callback.message.edit_text("Отлично! Укажите вашу цену:", reply_markup=get_price_keyboard(order_id))
    await callback.answer()

@executor_router.callback_query(F.data.startswith("price_"), ExecutorResponse.waiting_for_price)
async def executor_price_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    if callback.data == "price_manual":
        await callback.message.edit_text("Пожалуйста, введите цену вручную (только число):", reply_markup=get_price_keyboard(order_id))
        return
    price = callback.data.split("_")[-1]
    await state.update_data(price=price)
    await state.set_state(ExecutorResponse.waiting_for_deadline)
    # Получаем дедлайн от клиента
    from shared import get_all_orders
    orders = get_all_orders()
    order = next((o for o in orders if o.get('order_id') == order_id), None)
    client_deadline = order.get('deadline', 'Не указан') if order else 'Не указан'
    text = f"Цена принята. Теперь укажите срок выполнения: ⏳\nДедлайн: до {client_deadline}"
    await callback.message.edit_text(text, reply_markup=get_deadline_keyboard())
    await callback.answer()

@executor_router.message(ExecutorResponse.waiting_for_price)
async def executor_price_manual_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите только число.", reply_markup=get_price_keyboard(order_id))
        return
    await state.update_data(price=message.text)
    await state.set_state(ExecutorResponse.waiting_for_deadline)
    await message.answer("Цена принята. Теперь укажите срок выполнения:", reply_markup=get_deadline_keyboard())

@executor_router.callback_query(F.data.startswith("deadline_"), ExecutorResponse.waiting_for_deadline)
async def executor_deadline_handler(callback: CallbackQuery, state: FSMContext):
    if callback.data == "deadline_manual":
        await callback.message.edit_text("Пожалуйста, введите срок выполнения вручную:")
        return
    deadline = callback.data.split("_", 1)[-1]
    await state.update_data(deadline=deadline)
    await state.set_state(ExecutorResponse.waiting_for_comment)
    await callback.message.edit_text("Добавьте комментарий к заказу (или пропустите этот шаг):", reply_markup=get_executor_comment_keyboard())
    await callback.answer()
def get_executor_comment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip_executor_comment")]
    ])

@executor_router.message(ExecutorResponse.waiting_for_deadline)
async def executor_deadline_manual_handler(message: Message, state: FSMContext):
    await state.update_data(deadline=message.text)
    await state.set_state(ExecutorResponse.waiting_for_comment)
    await message.answer("Добавьте комментарий к заказу (или пропустите этот шаг):", reply_markup=get_executor_comment_keyboard())

# --- После ввода комментария показываем карточку подтверждения ---
@executor_router.message(ExecutorResponse.waiting_for_comment)
async def executor_comment_handler(message: Message, state: FSMContext):
    await state.update_data(executor_comment=message.text)
    fsm_data = await state.get_data()
    order_id = fsm_data.get('order_id')
    # Формируем карточку
    price = fsm_data.get('price', '—')
    deadline = fsm_data.get('deadline', '—')
    comment = fsm_data.get('executor_comment', '')
    # Склоняем дни, если это число
    def _pluralize_days(val):
        try:
            n = int(val)
            if 11 <= n % 100 <= 14:
                return f"{n} дней"
            elif n % 10 == 1:
                return f"{n} день"
            elif 2 <= n % 10 <= 4:
                return f"{n} дня"
            else:
                return f"{n} дней"
        except Exception:
            return str(val)
    deadline_str = _pluralize_days(deadline)
    text = f"<b>❗️ Проверьте ваши условия:</b>\n\n" \
           f"<b>🏷 Цена:</b> {price} ₽\n\n" \
           f"<b>🗓 Срок:</b> {deadline_str}\n\n" \
           f"<b>💬 Комментарий:</b> {comment or 'Нет'}"
    await state.set_state(ExecutorResponse.waiting_for_confirm)
    await message.answer(text, parse_mode="HTML", reply_markup=get_executor_final_confirm_keyboard(order_id))

@executor_router.callback_query(F.data == "skip_executor_comment", ExecutorResponse.waiting_for_comment)
async def executor_skip_comment_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(executor_comment="")
    fsm_data = await state.get_data()
    order_id = fsm_data.get('order_id')
    price = fsm_data.get('price', '—')
    deadline = fsm_data.get('deadline', '—')
    comment = ''
    def _pluralize_days(val):
        try:
            n = int(val)
            if 11 <= n % 100 <= 14:
                return f"{n} дней"
            elif n % 10 == 1:
                return f"{n} день"
            elif 2 <= n % 10 <= 4:
                return f"{n} дня"
            else:
                return f"{n} дней"
        except Exception:
            return str(val)
    deadline_str = _pluralize_days(deadline)
    text = f"<b>❗️ Проверьте ваши условия:</b>\n\n" \
           f"<b>🏷 Цена:</b> {price} ₽\n\n" \
           f"<b>🗓 Срок:</b> {deadline_str}\n\n" \
           f"<b>💬 Комментарий:</b> Нет"
    await state.set_state(ExecutorResponse.waiting_for_confirm)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_executor_final_confirm_keyboard(order_id))
    await callback.answer()

# --- Обработчик кнопки 'Отправить' ---
@executor_router.callback_query(F.data.startswith("executor_send_offer:"), ExecutorResponse.waiting_for_confirm)
async def executor_send_offer_handler(callback: CallbackQuery, state: FSMContext):
    fsm_data = await state.get_data()
    await send_offer_to_admin(callback.from_user, fsm_data)
    await callback.message.edit_text("✅ Ваши условия отправлены администратору. Ожидайте подтверждения.")
    await state.clear()
    await callback.answer()



@executor_router.message(ExecutorResponse.waiting_for_comment)
async def executor_comment_handler(message: Message, state: FSMContext):
    await state.update_data(executor_comment=message.text)
    fsm_data = await state.get_data()
    await send_offer_to_admin(message.from_user, fsm_data)
    await message.answer("✅ Ваши условия отправлены администратору. Ожидайте подтверждения.")
    await state.clear()

@executor_router.callback_query(F.data == "skip_executor_comment", ExecutorResponse.waiting_for_comment)
async def executor_skip_comment_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(executor_comment="")
    fsm_data = await state.get_data()
    await send_offer_to_admin(callback.from_user, fsm_data)
    await callback.message.edit_text("✅ Ваши условия отправлены администратору. Ожидайте подтверждения.")
    await state.clear()
    await callback.answer()



# --- Новая логика админа для утверждения ---

@admin_router.callback_query(F.data.startswith("final_change_price_"))
async def admin_change_price_start(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    await state.set_state(AdminApproval.waiting_for_new_price)
    await state.update_data(order_id=order_id, message_id=callback.message.message_id)
    await callback.message.edit_text("Введите новую цену (только число):")
    await callback.answer()

@admin_router.message(AdminApproval.waiting_for_new_price)
async def admin_process_new_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Неверный формат. Введите только число.")
        return

    new_price = int(message.text)
    fsm_data = await state.get_data()
    order_id = fsm_data.get('order_id')
    message_id = fsm_data.get('message_id')

    # Обновляем JSON
    orders = get_all_orders()
    executor_full_name = ''
    executor_deadline = ''
    for order in orders:
        if order.get("order_id") == order_id:
            order['executor_offer']['price'] = new_price
            executor_full_name = order['executor_offer'].get('executor_full_name', 'Без имени')
            executor_deadline = order['executor_offer'].get('deadline', 'N/A')
            break
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
        
    # Обновляем сообщение у админа
    executor_deadline_str = pluralize_days(executor_deadline)
    admin_notification = f"""
    ✅ Исполнитель {executor_full_name} готов взяться за заказ №{order_id}
    
    <b>Предложенные условия (цена изменена):</b>
    💰 <b>Цена:</b> {new_price} ₽
    ⏳ <b>Срок:</b> {executor_deadline_str}
    """
    await bot.edit_message_text(
        admin_notification, 
        chat_id=message.chat.id,
        message_id=message_id,
        parse_mode="HTML",
        reply_markup=get_admin_final_approval_keyboard(order_id, new_price)
    )
    await message.delete() # Удаляем сообщение с новой ценой от админа
    await state.clear()


@admin_router.callback_query(F.data.startswith("final_approve_"))
async def admin_final_approve(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    order_id = int(parts[2])
    price = int(parts[3])

    orders = get_all_orders()
    target_order = None
    for order in orders:
        if order.get("order_id") == order_id:
            order['status'] = "Ожидает оплаты"
            order['final_price'] = price
            target_order = order
            break
    
    if not target_order:
        await callback.answer("Ошибка: заказ не найден", show_alert=True)
        return
        
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    # Уведомление клиенту
    customer_id = target_order.get('user_id')
    if customer_id:
        deadline = target_order.get('executor_offer', {}).get('deadline') or target_order.get('deadline', '')
        deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
        work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')
        subject = target_order.get('subject', 'Не указан')
        customer_text = f"""
✅ Ваша заявка по предмету "{subject}"\nТип работы: {work_type}\nДедлайн: {deadline_str}

<b>Итоговая стоимость:</b> {price} ₽.
<b>Срок:</b> {deadline_str}
"""
        # Тут должна быть логика с реальной оплатой
        payment_button = InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay_{order_id}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[payment_button]])
        try:
            await bot.send_message(customer_id, customer_text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            await callback.message.answer(f"⚠️ Не удалось уведомить клиента {customer_id}")

    # Уведомление исполнителю
    executor_id = target_order.get('executor_offer', {}).get('executor_id')
    if executor_id:
        try:
            subject = target_order.get('subject', 'Не указан')
            await bot.send_message(executor_id, f'✅ Администратор утвердил ваши условия по заказу.\nПредмет: "{subject}"\nОжидаем оплату от клиента.')
        except Exception:
            await callback.message.answer(f"⚠️ Не удалось уведомить исполнителя {executor_id}")

    await callback.message.edit_text(f"✅ Предложение по заказу №{order_id} на сумму {price} ₽ отправлено клиенту. Ожидаем оплату...")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("final_reject_"))
async def admin_final_reject(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    
    orders = get_all_orders()
    target_order = None
    executor_id = None
    for order in orders:
        if order.get("order_id") == order_id:
            order['status'] = "Рассматривается" # Возвращаем к поиску
            executor_id = order.get('executor_offer', {}).get('executor_id')
            order.pop('executor_offer', None)
            target_order = order
            break

    if not target_order:
        await callback.answer("Ошибка: заказ не найден", show_alert=True)
        return

    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    
    if executor_id:
        try:
            await bot.send_message(executor_id, f"❌ Администратор отклонил ваши условия по заказу №{order_id}.")
        except Exception:
            pass # Не критично

    await callback.message.edit_text(f"❌ Вы отклонили предложение исполнителя по заказу №{order_id}. Заказ снова в поиске.")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_approve_work_"))
async def admin_approve_work_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split('_')[-1])
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)

    if not target_order or 'submitted_work' not in target_order:
        await callback.answer("Работа не найдена или была отозвана.", show_alert=True)
        return

    # Меняем статус
    target_order['status'] = "Утверждено администратором"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    # Отправляем клиенту
    customer_id = target_order.get('user_id')
    submitted_work = target_order.get('submitted_work')
    submitted_at = target_order.get('submitted_at', 'неизвестно')
    
    if customer_id and submitted_work:
        try:
            caption = f"✅ Ваша работа по заказу №{order_id} готова!\nДата выполнения: {submitted_at}"
            keyboard = get_client_work_approval_keyboard(order_id)
            await bot.send_document(
                chat_id=customer_id,
                document=submitted_work['file_id'],
                caption=caption,
                reply_markup=keyboard
            )
            await callback.message.edit_text(f"✅ Работа по заказу №{order_id} утверждена и отправлена клиенту.")
        except Exception as e:
            await callback.message.edit_text(f"⚠️ Не удалось отправить работу клиенту {customer_id}. Ошибка: {e}")
    else:
        await callback.message.edit_text("Не найден клиент или файл для отправки.")

    await callback.answer()

# --- Обработчики команд и главного меню ---

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    # Если исполнитель — показываем его меню
    if is_executor(message.from_user.id):
        await message.answer(
            "👋 Добро пожаловать в меню исполнителя!",
            reply_markup=get_executor_menu_keyboard()
        )
        return
    await message.answer(
        "👋 Здравствуйте! Я бот для приема заявок. Воспользуйтесь кнопками ниже.",
        reply_markup=get_main_reply_keyboard()
    )

@router.message(F.text == "❓ Помощь")
async def txt_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        'ℹ️ Нажмите кнопку "🆕 Новая заявка" и следуйте инструкциям, чтобы создать новую заявку.'
    )

@router.message(F.text == "👨‍💻 Связаться с администратором")
async def txt_contact_admin(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AdminContact.waiting_for_message)
    await message.answer(
        "✍️ Напишите ваше сообщение, и я отправлю его администратору.",
        reply_markup=get_back_to_main_menu_keyboard()
    )

@router.message(AdminContact.waiting_for_message)
async def universal_admin_message_handler(message: Message, state: FSMContext):
    if message.from_user.id == int(ADMIN_ID):
        # Это ответ администратора клиенту или исполнителю
        data = await state.get_data()
        user_id = data.get("reply_user_id")
        reply_msg_id = data.get("reply_msg_id")
        if user_id:
            # Если это исполнитель, отправляем с меню исполнителя
            if is_executor(user_id):
                await bot.send_message(user_id, f"💬 Ответ от администратора:\n\n{message.text}", reply_markup=get_executor_menu_keyboard())
            else:
                await bot.send_message(user_id, f"💬 Ответ от администратора:\n\n{message.text}")
            try:
                await bot.delete_message(ADMIN_ID, reply_msg_id)
            except:
                pass
            await message.answer("Сообщение отправлено и удалено из списка.")
        else:
            await message.answer("Ошибка: не найден пользователь для ответа.")
        await state.clear()
    else:
        # Это пользователь пишет админу
        admin_msg = await bot.send_message(
            ADMIN_ID,
            f"📩 Новое сообщение от пользователя {get_full_name(message.from_user)} (ID: {message.from_user.id}):\n\n"
            f'"{message.text}"',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply_user:{message.from_user.id}"),
                    InlineKeyboardButton(text="Удалить сообщение", callback_data="admin_delete_user_msg")
                ]
            ])
        )
        await state.clear()
        await state.update_data(
            last_user_msg_text=message.text,
            last_user_id=message.from_user.id
        )
        await message.answer(
            "✅ Ваше сообщение успешно отправлено администратору!",
            reply_markup=get_main_reply_keyboard()
        )

@router.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌Действие отменено")
    await callback.answer()


# --- Просмотр заявок ---

def get_user_orders(user_id: int) -> list:
    """Читает orders.json и возвращает список заявок для конкретного user_id."""
    file_path = "orders.json"
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            all_orders = json.load(f)
        except json.JSONDecodeError:
            return []
    
    if not isinstance(all_orders, list):
        return []

    user_orders = [order for order in all_orders if isinstance(order, dict) and order.get('user_id') == user_id]
    return user_orders

async def show_my_orders(message_or_callback: types.Message | types.CallbackQuery):
    """Отображает список заявок пользователя с кнопками для просмотра."""
    user_id = message_or_callback.from_user.id
    orders = get_user_orders(user_id)
    draft_orders_exist = any(o.get('status') == "Редактируется" for o in orders)

    if not orders:
        text = "У вас пока нет заявок."
        keyboard = None
    else:
        text = "Вот ваши заявки:"
        if draft_orders_exist:
            text = "У вас есть незавершенная заявка. Выберите ее, чтобы продолжить.\n\n" + text
        keyboard_buttons = []
        # Показываем последние 10 заявок, чтобы не перегружать интерфейс
        for order in reversed(orders[-10:]): 
            order_id = order['order_id']
            order_status = order.get('status', 'N/A')
            emoji = STATUS_EMOJI_MAP.get(order_status, "📄")
            
            work_type_raw = order.get('work_type', 'Заявка')
            work_type = work_type_raw.replace('work_type_', '')
            
            button_text = f"{emoji} Заявка  №{order_id} {work_type}  | {order_status}"
            
            keyboard_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"view_order_{order_id}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(text, reply_markup=keyboard)
    else:
        try:
            await message_or_callback.message.edit_text(text, reply_markup=keyboard)
            await message_or_callback.answer()
        except:
            await message_or_callback.message.answer(text, reply_markup=keyboard)
            await message_or_callback.answer()

@router.message(F.text == "📂 Мои заявки")
async def my_orders_handler(message: Message, state: FSMContext):
    await state.clear()
    await show_my_orders(message)

@router.callback_query(F.data == "my_orders_list")
async def back_to_my_orders_list_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_my_orders(callback)


@router.callback_query(F.data.startswith("view_order_"))
async def view_order_handler(callback: CallbackQuery, state: FSMContext):
    try:
        order_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка: неверный ID заявки.", show_alert=True)
        return
    user_id = callback.from_user.id
    orders = get_user_orders(user_id)
    target_order = next((order for order in orders if order['order_id'] == order_id), None)
    if not target_order:
        await callback.message.edit_text("Не удалось найти эту заявку или у вас нет к ней доступа.")
        await callback.answer()
        return
        
    # Если заявка в статусе "Редактируется", возвращаем пользователя к подтверждению
    if target_order.get('status') == "Редактируется":
        await state.set_data(target_order)
        await state.set_state(OrderState.confirmation)
        summary_text = await build_summary_text(target_order)
        await callback.message.edit_text(
            text=summary_text, 
            reply_markup=get_confirmation_keyboard(), 
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # --- Новый блок для "Утверждено администратором" ---
    if target_order.get('status') == "Утверждено администратором":
        submitted_work = target_order.get('submitted_work')
        submitted_at = target_order.get('submitted_at', 'неизвестно')
        if submitted_work:
            caption = f"✅ Ваша работа по заказу №{order_id} готова!\nДата выполнения: {submitted_at}"
            keyboard = get_client_work_approval_keyboard(order_id)
            # Удаляем старое сообщение, чтобы не дублировать информацию
            try:
                await callback.message.delete()
            except: # Если не получилось удалить (уже удалено) - не страшно
                pass
            await bot.send_document(
                chat_id=callback.from_user.id,
                document=submitted_work['file_id'],
                caption=caption,
                reply_markup=keyboard
            )
        else:
            await callback.message.edit_text("Ошибка: файл с работой не найден.")
        await callback.answer()
        return

    status = target_order.get('status', 'Не определен')
    status_text = f"{STATUS_EMOJI_MAP.get(status, '📄')} {status}"
    details_text = f"""
<b>Детали заявки №{target_order['order_id']}</b>

<b>Статус:</b> {status_text}

<b>Группа:</b> {target_order.get('group_name', 'Не указано')}
<b>Университет:</b> {target_order.get('university_name', 'Не указано')}
<b>Тип работы:</b> {target_order.get('work_type', 'Не указан')}
<b>Методичка:</b> {'✅ Да' if target_order.get('has_guidelines') else '❌ Нет'}
<b>Задание:</b> {'✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'}
<b>Пример работы:</b> {'✅ Да' if target_order.get('has_example') else '❌ Нет'}
<b>Дата сдачи:</b> {target_order.get('deadline', 'Не указана')}
<b>Комментарий:</b> {target_order.get('comments', 'Нет')}
    """
    keyboard = get_user_order_keyboard(order_id, status)
    await callback.message.edit_text(details_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()
# --- Процесс создания нового заказа ---

@router.message(F.text == "🆕 Новая заявка")
async def start_new_order(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(OrderState.group_name)
    await message.answer(
        "📝 Начнем создание заявки. \n\nПожалуйста, укажите название вашей группы.",
        reply_markup=get_back_keyboard()
    )

@router.message(OrderState.group_name)
async def process_group_name(message: Message, state: FSMContext):
    await state.update_data(group_name=message.text)
    await state.set_state(OrderState.university_name)
    await message.answer("🏫 Отлично! Теперь введите название вашего университета.", reply_markup=get_back_keyboard())

@router.message(OrderState.university_name)
async def process_university_name(message: Message, state: FSMContext):
    await state.update_data(university_name=message.text)
    await state.set_state(OrderState.teacher_name)
    await message.answer("👨‍🏫 Введите ФИО преподавателя:", reply_markup=get_back_keyboard())

@router.message(OrderState.teacher_name)
async def process_teacher_name(message: Message, state: FSMContext):
    await state.update_data(teacher_name=message.text)
    await state.set_state(OrderState.gradebook)
    await message.answer("📒 Введите номер и вариант зачетки (например: №24-15251):", reply_markup=get_back_keyboard())

@router.message(OrderState.gradebook)
async def process_gradebook(message: Message, state: FSMContext):
    await state.update_data(gradebook=message.text)
    await state.set_state(OrderState.subject)
    await message.answer("📚 Выберите или введите предмет:", reply_markup=get_subject_keyboard())

@router.callback_query(OrderState.subject, F.data.startswith("subject_"))
async def process_subject_choice(callback: CallbackQuery, state: FSMContext):
    subject = callback.data.split("_", 1)[-1]
    if subject == "other":
        await state.set_state(OrderState.subject_other)
        await callback.message.edit_text("📚 Введите название предмета:")
    else:
        await state.update_data(subject=subject)
        await state.set_state(OrderState.work_type)
        await callback.message.edit_text("📝 Выберите тип работы:", reply_markup=get_work_type_keyboard())
    await callback.answer()

@router.message(OrderState.subject_other)
async def process_subject_other_input(message: Message, state: FSMContext):
    await state.update_data(subject=message.text)
    await state.set_state(OrderState.work_type)
    await message.answer("📝 Выберите тип работы:", reply_markup=get_work_type_keyboard())

@router.callback_query(OrderState.work_type, F.data.startswith("work_type_"))
async def process_work_type_choice(callback: CallbackQuery, state: FSMContext):
    work_type = callback.data
    
    if work_type == "Другое (ввести вручную)":
        await state.set_state(OrderState.work_type_other)
        await callback.message.edit_text("Пожалуйста, введите тип работы вручную.", reply_markup=get_back_keyboard())
    else:
        await state.update_data(work_type=work_type)
        await state.set_state(OrderState.guidelines_choice)
        await callback.message.edit_text("📄 У вас есть методичка?", reply_markup=get_yes_no_keyboard("guidelines"))
    await callback.answer()

@router.message(OrderState.work_type_other)
async def process_work_type_other(message: Message, state: FSMContext):
    await state.update_data(work_type=message.text)
    await state.set_state(OrderState.guidelines_choice)
    # Просто отправляем новое сообщение вместо редактирования
    await message.answer("📄 У вас есть методичка?", reply_markup=get_yes_no_keyboard("guidelines"))


@router.callback_query(OrderState.guidelines_choice, F.data.startswith("guidelines_"))
async def process_guidelines_choice(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split("_")[1]
    if choice == "yes":
        await state.update_data(has_guidelines=True)
        await state.set_state(OrderState.guidelines_upload)
        await callback.message.edit_text("Пожалуйста, загрузите файл с методичкой (pdf, docx, png, jpeg).", reply_markup=get_back_keyboard())
    else:
        await state.update_data(has_guidelines=False, guidelines_file=None)
        await state.set_state(OrderState.task_upload)
        await callback.message.edit_text("Понял. Теперь, пожалуйста, загрузите файл с заданием (pdf, docx, png, jpeg) или просто опишите его текстом.", reply_markup=get_back_keyboard())
    await callback.answer()


@router.message(OrderState.guidelines_upload, F.document | F.photo)
async def process_guidelines_upload(message: Message, state: FSMContext):
    # Проверка документа
    if message.document:
        ext = os.path.splitext(message.document.file_name)[-1][1:].lower()
        if ext not in ALLOWED_EXTENSIONS:
            await message.answer("❌ Разрешены только файлы: pdf, docx, png, jpeg, jpg. Попробуйте еще раз.")
            return
        if message.document.file_size > MAX_FILE_SIZE:
            await message.answer("❌ Файл слишком большой. Максимальный размер — 15 МБ.")
            return
        guidelines_file = {'id': message.document.file_id, 'type': 'document'}
    else:
        # Проверка фото
        photo = message.photo[-1]
        if photo.file_size > MAX_FILE_SIZE:
            await message.answer("❌ Фото слишком большое. Максимальный размер — 15 МБ.")
            return
        guidelines_file = {'id': photo.file_id, 'type': 'photo'}
    await state.update_data(guidelines_file=guidelines_file)
    await state.set_state(OrderState.task_upload)
    await message.answer("✅ Методичка принята. Теперь, пожалуйста, загрузите файл с заданием (pdf, docx, png, jpeg) или просто опишите его текстом.", reply_markup=get_back_keyboard())


@router.message(OrderState.task_upload, F.text | F.document | F.photo)
async def process_task_upload(message: Message, state: FSMContext):
    if message.text:
        await state.update_data(task_text=message.text, task_file=None)
    else:
        if message.document:
            ext = os.path.splitext(message.document.file_name)[-1][1:].lower()
            if ext not in ALLOWED_EXTENSIONS:
                await message.answer("❌ Разрешены только файлы: pdf, docx, png, jpeg, jpg. Попробуйте еще раз.")
                return
            if message.document.file_size > MAX_FILE_SIZE:
                await message.answer("❌ Файл слишком большой. Максимальный размер — 15 МБ.")
                return
            task_file = {'id': message.document.file_id, 'type': 'document'}
        else:
            photo = message.photo[-1]
            if photo.file_size > MAX_FILE_SIZE:
                await message.answer("❌ Фото слишком большое. Максимальный размер — 15 МБ.")
                return
            task_file = {'id': photo.file_id, 'type': 'photo'}
        await state.update_data(task_file=task_file, task_text=None)
    await state.set_state(OrderState.example_choice)
    await message.answer("📑 Задание принято. У вас есть пример работы?", reply_markup=get_yes_no_keyboard("example"))


@router.callback_query(OrderState.example_choice, F.data.startswith("example_"))
async def process_example_choice(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split("_")[-1]
    if choice == "yes":
        await state.update_data(has_example=True)
        await state.set_state(OrderState.example_upload)
        await callback.message.edit_text("Пожалуйста, загрузите файл с примером (pdf, docx, pgn, jpeg).", reply_markup=get_back_keyboard())
    else: 
        await state.update_data(has_example=False, example_file=None)
        await state.set_state(OrderState.deadline)
        await callback.message.edit_text("🗓️ Укажите дату сдачи в формате ДД.ММ.ГГГГ.", reply_markup=get_back_keyboard())
    await callback.answer()


@router.message(OrderState.example_upload, F.document | F.photo)
async def process_example_upload(message: Message, state: FSMContext):
    if message.document:
        ext = os.path.splitext(message.document.file_name)[-1][1:].lower()
        if ext not in ALLOWED_EXTENSIONS:
            await message.answer("❌ Разрешены только файлы: pdf, docx, png, jpeg, jpg. Попробуйте еще раз.")
            return
        if message.document.file_size > MAX_FILE_SIZE:
            await message.answer("❌ Файл слишком большой. Максимальный размер — 15 МБ.")
            return
        example_file = {'id': message.document.file_id, 'type': 'document'}
    else:
        photo = message.photo[-1]
        if photo.file_size > MAX_FILE_SIZE:
            await message.answer("❌ Фото слишком большое. Максимальный размер — 15 МБ.")
            return
        example_file = {'id': photo.file_id, 'type': 'photo'}
    await state.update_data(example_file=example_file)
    await state.set_state(OrderState.deadline)
    await message.answer("✅ Пример принят. Укажите дату сдачи в формате ДД.ММ.ГГГГ.", reply_markup=get_back_keyboard())

@router.message(OrderState.deadline)
async def process_deadline(message: Message, state: FSMContext):
    try:
        # Простая проверка формата
        datetime.strptime(message.text, "%d.%m.%Y")
        await state.update_data(deadline=message.text)
        await state.set_state(OrderState.comments)
        await message.answer(
            "💬 Отлично. Теперь введите ваши комментарии по работе (например, по оформлению, преподавателю и т.д.) или нажмите 'Пропустить'", 
            reply_markup=get_skip_comment_keyboard()
        )
    except ValueError:
        await message.answer("❌ Неверный формат даты. Пожалуйста, введите дату в формате ДД.ММ.ГГГГ.", reply_markup=get_back_keyboard())

@router.callback_query(F.data == "skip_comment", OrderState.comments)
async def skip_comment_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(comments="Нет")
    data = await state.get_data()
    # Не сохраняем черновик! Просто показываем подтверждение
    summary_text = await build_summary_text(data)
    await state.set_state(OrderState.confirmation)
    await callback.message.edit_text(summary_text, reply_markup=get_confirmation_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.message(OrderState.comments)
async def process_comments(message: Message, state: FSMContext):
    await state.update_data(comments=message.text)
    data = await state.get_data()
    # Не сохраняем черновик! Просто показываем подтверждение
    summary_text = await build_summary_text(data)
    await state.set_state(OrderState.confirmation)
    await message.answer(text=summary_text, reply_markup=get_confirmation_keyboard(), parse_mode="HTML")


async def build_summary_text(data: dict) -> str:
    """Строит текст с итоговой информацией о заявке."""
    group = data.get("group_name", "Не указана")
    university = data.get("university_name", "Не указан")
    teacher = data.get("teacher_name", "Не указан")
    gradebook = data.get("gradebook", "Не указан")
    subject = data.get("subject", "Не указан")
    work_type_key = data.get("work_type", "N/A").replace("work_type_", "")
    
    work_type_str = work_type_key if work_type_key != 'other' else data.get('work_type_other_name', 'Другое')
    
    guidelines = '✅ Да' if data.get('has_guidelines') else '❌ Нет'
    task = '✅ Прикреплено' if data.get('task_file') or data.get('task_text') else '❌ Нет'
    example = '✅ Да' if data.get('has_example') else '❌ Нет'
    deadline = data.get('deadline', 'Не указана')
    comments = data.get('comments', 'Нет')
    return f"""

<b>Группа:</b> {group}
<b>ВУЗ:</b> {university}
<b>Преподаватель:</b> {teacher}
<b>Номер зачетки:</b> {gradebook}
<b>Предмет:</b> {subject}
<b>Тип работы:</b> {work_type_str}
<b>Методичка:</b> {guidelines}
<b>Задание:</b> {task}
<b>Пример:</b> {example}
<b>Дедлайн:</b> {deadline}
"""
    if comments:
        return f"{summary_text}\n<b>Комментарии:</b> {comments}"
    return summary_text

async def build_short_summary_text(data: dict) -> str:
    """Формирует короткий текст-сводку по заявке для админа/исполнителей."""
    work_type = data.get("work_type", "Тип не указан").replace("type_", "").capitalize()
    if work_type == "Other":
        work_type = data.get("work_type_other_name", "Другое")

    subject = data.get("subject", "Не указан")
    deadline = data.get("deadline", "Не указан")
    text = (f"<b>Тип работы:</b> {work_type}\n"
            f"<b>Предмет:</b> {subject}\n"
            f"<b>Срок:</b> до {deadline}")
    return text

# --- Подтверждение и сохранение заказа ---

async def save_or_update_order(order_data: dict) -> int:
    """Сохраняет новую или обновляет существующую заявку в orders.json."""
    file_path = "orders.json"
    orders = []
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                orders = json.load(f)
            except json.JSONDecodeError:
                orders = []
    
    order_id_to_process = order_data.get("order_id")
    user_id_to_process = order_data.get("user_id")
    status_to_process = order_data.get("status")

    # Если заявка подтверждается ("Рассматривается"), удаляем все черновики с этим order_id и user_id
    if status_to_process == "Рассматривается" and order_id_to_process and user_id_to_process:
        orders = [o for o in orders if not (
            o.get("order_id") == order_id_to_process and o.get("user_id") == user_id_to_process and o.get("status") == "Редактируется"
        )]

    if order_id_to_process: # Обновляем существующую
        found = False
        for i, order in enumerate(orders):
            if order.get("order_id") == order_id_to_process:
                orders[i] = order_data
                found = True
                break
        if not found: # Если по какой-то причине не нашли, добавляем как новую
             order_id_to_process = (orders[-1]['order_id'] + 1) if orders else 1
             order_data["order_id"] = order_id_to_process
             orders.append(order_data)
    else: # Создаем новую
        order_id_to_process = (orders[-1]['order_id'] + 1) if orders else 1
        order_data["order_id"] = order_id_to_process
        orders.append(order_data)
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    
    return order_id_to_process

@router.callback_query(OrderState.confirmation, F.data == "confirm_order")
async def process_confirm_order(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    data['status'] = "Рассматривается"
    data['user_id'] = callback.from_user.id
    data['username'] = callback.from_user.username or "N/A"
    data['first_name'] = callback.from_user.first_name
    data['last_name'] = callback.from_user.last_name or ""
    data['creation_date'] = datetime.now().strftime("%d.%m.%Y %H:%M")
    order_id = await save_or_update_order(data)
    # Формируем единое сообщение для админа с кнопками
    summary = await build_summary_text(data)
    full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    admin_text = f"🔥 Новая заявка {order_id} от клиента ({full_name})\n\n{summary}"
    admin_keyboard = get_admin_order_keyboard(data, show_materials_button=True)
    await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_keyboard)
    # Рассылка исполнителям (оставляем как было)
    if EXECUTOR_IDS:
        short_summary = await build_short_summary_text(data)
        notification_text = f"📢 Появился новый заказ {order_id}\n\n" + short_summary
        for executor_id in EXECUTOR_IDS:
            try:
                await bot.send_message(executor_id, notification_text, parse_mode="HTML")
            except Exception as e:
                print(f"Failed to send notification to executor {executor_id}: {e}")
    await callback.message.edit_text("✅ Ваша заявка успешно отправлена, ожидайте отклика!", reply_markup=None)
    await state.clear()
    await callback.answer()

@router.callback_query(OrderState.confirmation, F.data == "cancel_order")
async def process_cancel_order(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    user_id = callback.from_user.id
    file_path = "orders.json"
    orders = []
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                orders = json.load(f)
            except json.JSONDecodeError:
                orders = []
    # Удаляем заявку пользователя с этим order_id
    new_orders = [o for o in orders if not (str(o.get("order_id")) == str(order_id) and o.get("user_id") == user_id)]
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(new_orders, f, ensure_ascii=False, indent=4)
    await state.clear()
    await callback.message.edit_text("❌ Заявка отменена и удалена.")
    await callback.answer()

@router.callback_query(OrderState.confirmation, F.data == "contact_admin_in_order")
async def process_contact_admin_in_order(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminContact.waiting_for_message)
    await callback.message.edit_text("✍️ Напишите ваше сообщение, и я отправлю его администратору.")
    await callback.answer()


# --- Обработчик кнопки "Назад" ---
@router.callback_query(F.data == "back", StateFilter(OrderState))
async def process_back_button(callback: CallbackQuery, state: FSMContext):
    current_state_str = await state.get_state()

    async def go_to_group_name(s: FSMContext):
        await s.set_state(OrderState.group_name)
        await callback.message.edit_text("📝 Пожалуйста, укажите название вашей группы.")
    
    async def go_to_university_name(s: FSMContext):
        await s.set_state(OrderState.university_name)
        await callback.message.edit_text("🏫 Введите название вашего университета.", reply_markup=get_back_keyboard())

    async def go_to_work_type(s: FSMContext):
        await s.set_state(OrderState.work_type)
        await callback.message.edit_text("📘 Выберите тип работы:", reply_markup=get_work_type_keyboard())

    async def go_to_guidelines_choice(s: FSMContext):
        await s.set_state(OrderState.guidelines_choice)
        await callback.message.edit_text("📄 У вас есть методичка?", reply_markup=get_yes_no_keyboard("guidelines"))
    
    async def go_to_task_upload(s: FSMContext):
        await s.set_state(OrderState.task_upload)
        await callback.message.edit_text("Понял. Теперь, пожалуйста, загрузите файл с заданием (pdf, docx, png, jpeg) или просто опишите его текстом.", reply_markup=get_back_keyboard())

    async def go_to_example_choice(s: FSMContext):
        await s.set_state(OrderState.example_choice)
        await callback.message.edit_text("📑 Задание принято. У вас есть пример работы?", reply_markup=get_yes_no_keyboard("example"))

    async def go_to_deadline(s: FSMContext):
        await s.set_state(OrderState.deadline)
        await callback.message.edit_text("🗓️ Укажите дату сдачи в формате ДД.ММ.ГГГГ.", reply_markup=get_back_keyboard())

    async def go_to_comments(s: FSMContext):
        await s.set_state(OrderState.comments)
        await callback.message.edit_text("💬 Введите ваши комментарии по работе.", reply_markup=get_back_keyboard())

    back_transitions = {
        OrderState.university_name: go_to_group_name,
        OrderState.work_type: go_to_university_name,
        OrderState.work_type_other: go_to_work_type,
        OrderState.guidelines_choice: go_to_work_type,
        OrderState.guidelines_upload: go_to_guidelines_choice,
        OrderState.task_upload: go_to_guidelines_choice,
        OrderState.example_choice: go_to_task_upload,
        OrderState.example_upload: go_to_example_choice,
        OrderState.deadline: go_to_example_choice,
        OrderState.comments: go_to_deadline,
        OrderState.confirmation: go_to_comments,
    }
    
    if current_state_str in back_transitions:
        await back_transitions[current_state_str](state)
    else: # Если это первый шаг (group_name), то возвращаться некуда
        await state.clear()
        await callback.message.edit_text("❌ Заявка отменена. Вы вернулись в главное меню.")

    await callback.answer()


async def send_offer_to_admin(user, fsm_data):
    """Отправляет оффер от исполнителя админу с кнопками."""
    order_id = fsm_data['order_id']
    price = fsm_data['price']
    executor_comment = fsm_data.get('executor_comment', '')
    # Обновляем заказ в JSON
    orders = get_all_orders()
    subject = 'Не указан'
    for order in orders:
        if order.get("order_id") == order_id:
            order['status'] = "Ожидает подтверждения" # Меняем статус
            order['executor_offer'] = {
                'price': price,
                'deadline': fsm_data['deadline'],
                'executor_id': user.id,
                'executor_username': user.username,
                'executor_full_name': get_full_name(user),
                'executor_comment': executor_comment
            }
            subject = order.get('subject', 'Не указан')
            break
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    admin_notification = f"""
    ✅ Исполнитель {get_full_name(user)} (ID: {user.id}) готов взяться за заказ по предмету \"{subject}\"
    <b>Предложенные условия:</b>
    💰 <b>Цена:</b> {price} ₽
    ⏳ <b>Срок:</b> {fsm_data['deadline']}
    💬 <b>Комментарий исполнителя:</b> {executor_comment or 'Нет'}
    """
    await bot.send_message(
        ADMIN_ID, 
        admin_notification, 
        parse_mode="HTML",
        reply_markup=get_admin_final_approval_keyboard(order_id, price)
    )


@admin_router.callback_query(F.data.startswith("admin_show_materials:"))
async def admin_show_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    material_buttons = []
    if order.get('guidelines_file'):
        material_buttons.append([InlineKeyboardButton(text="Методичка", callback_data=f"admin_material_guidelines:{order_id}")])
    if order.get('task_file') or order.get('task_text'):
        material_buttons.append([InlineKeyboardButton(text="Задание", callback_data=f"admin_material_task:{order_id}")])
    if order.get('example_file'):
        material_buttons.append([InlineKeyboardButton(text="Пример работы", callback_data=f"admin_material_example:{order_id}")])
    material_buttons.append([InlineKeyboardButton(text="⬅️ Скрыть материалы", callback_data=f"admin_hide_materials:{order_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=material_buttons)
    await callback.message.edit_text("Выберите материал для просмотра:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_hide_materials:"))
async def admin_hide_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    details_text = await build_summary_text(order)
    details_text = f"<b>Детали заказа {order_id} от {get_full_name(order)}</b>\n\n" + details_text
    keyboard = get_admin_order_keyboard(order, show_materials_button=True)
    await callback.message.edit_text(details_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_material_guidelines:"))
async def admin_material_guidelines_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order or not order.get('guidelines_file'):
        await callback.answer("Методичка не найдена.", show_alert=True)
        return
    file = order['guidelines_file']
    if file['type'] == 'photo':
        await bot.send_photo(callback.from_user.id, file['id'], caption="Методичка")
    else:
        await bot.send_document(callback.from_user.id, file['id'], caption="Методичка")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_material_task:"))
async def admin_material_task_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Задание не найдено.", show_alert=True)
        return
    if order.get('task_file'):
        file = order['task_file']
        if file['type'] == 'photo':
            await bot.send_photo(callback.from_user.id, file['id'], caption="Задание")
        else:
            await bot.send_document(callback.from_user.id, file['id'], caption="Задание")
    elif order.get('task_text'):
        await bot.send_message(callback.from_user.id, f"Текст задания:\n\n{order['task_text']}")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_material_example:"))
async def admin_material_example_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order or not order.get('example_file'):
        await callback.answer("Пример работы не найден.", show_alert=True)
        return
    file = order['example_file']
    if file['type'] == 'photo':
        await bot.send_photo(callback.from_user.id, file['id'], caption="Пример работы")
    else:
        await bot.send_document(callback.from_user.id, file['id'], caption="Пример работы")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_delete_order:"))
async def admin_delete_order_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    new_orders = [o for o in orders if str(o['order_id']) != str(order_id)]
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(new_orders, f, ensure_ascii=False, indent=4)
    await callback.message.edit_text(f"❌ Заявка {order_id} удалена.")
    await callback.answer()

@admin_router.callback_query(F.data == "admin_orders_list")
async def admin_back_to_orders_list_handler(callback: CallbackQuery, state: FSMContext):
    await show_admin_orders_list(callback.message)
    await callback.answer()
# Просмотр материалов заказа для Исполнителя
@executor_router.callback_query(F.data.startswith("executor_show_materials:"))
async def executor_show_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    material_buttons = []
    if order.get('guidelines_file'):
        material_buttons.append([InlineKeyboardButton(text="Методичка", callback_data=f"executor_material_guidelines:{order_id}")])
    if order.get('task_file') or order.get('task_text'):
        material_buttons.append([InlineKeyboardButton(text="Задание", callback_data=f"executor_material_task:{order_id}")])
    if order.get('example_file'):
        material_buttons.append([InlineKeyboardButton(text="Пример работы", callback_data=f"executor_material_example:{order_id}")])
    material_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_hide_materials:{order_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=material_buttons)
    await callback.message.edit_text("Выберите материал для просмотра:", reply_markup=keyboard)
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_hide_materials:"))
async def executor_hide_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    work_type = order.get('work_type', 'N/A').replace('work_type_', '')
    subject = order.get('subject', 'Не указан')
    deadline = order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [
            InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")
        ],
    ])
    await callback.message.edit_text(executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_material_guidelines:"))
async def executor_material_guidelines_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order or not order.get('guidelines_file'):
        await callback.answer("Методичка не найдена.", show_alert=True)
        return
    file = order['guidelines_file']
    if file['type'] == 'photo':
        await bot.send_photo(callback.from_user.id, file['id'], caption="Методичка")
    else:
        await bot.send_document(callback.from_user.id, file['id'], caption="Методичка")
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_material_task:"))
async def executor_material_task_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Задание не найдено.", show_alert=True)
        return
    if order.get('task_file'):
        file = order['task_file']
        if file['type'] == 'photo':
            await bot.send_photo(callback.from_user.id, file['id'], caption="Задание")
        else:
            await bot.send_document(callback.from_user.id, file['id'], caption="Задание")
    elif order.get('task_text'):
        await bot.send_message(callback.from_user.id, f"Текст задания:\n\n{order['task_text']}")
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_material_example:"))
async def executor_material_example_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order or not order.get('example_file'):
        await callback.answer("Пример работы не найден.", show_alert=True)
        return
    file = order['example_file']
    if file['type'] == 'photo':
        await bot.send_photo(callback.from_user.id, file['id'], caption="Пример работы")
    else:
        await bot.send_document(callback.from_user.id, file['id'], caption="Пример работы")
    await callback.answer()

# --- Админ отвечает пользователю ---
@admin_router.callback_query(F.data.startswith("admin_reply_user:"))
async def admin_reply_user_handler(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split(":")[1])
    await state.clear()
    await state.update_data(reply_user_id=user_id, reply_msg_id=callback.message.message_id)
    await state.set_state(AdminContact.waiting_for_message)
    await callback.message.edit_text("✍️ Введите ваш ответ пользователю:")
    await callback.answer()

@admin_router.callback_query(F.data == "admin_delete_user_msg")
async def admin_delete_user_msg_handler(callback: CallbackQuery, state: FSMContext):
    try:
        await bot.delete_message(ADMIN_ID, callback.message.message_id)
    except:
        pass
    await callback.answer("Сообщение удалено.")

@admin_router.callback_query(F.data.startswith("admin_save_to_gsheet:"))
async def admin_save_to_gsheet_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    # Подготовка данных для таблицы
    row = [
        order.get("group_name", ""),
        order.get("university_name", ""),
        order.get("work_type", ""),
        "Да" if order.get("has_guidelines") else "Нет",
        "Есть" if order.get("task_file") or order.get("task_text") else "Нет",
        "Есть" if order.get("example_file") else "Нет",
        order.get("deadline", ""),
        order.get("comments", "")
    ]
    try:
        creds = Credentials.from_service_account_file("google-credentials.json", scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sh.sheet1
        worksheet.append_row(row, value_input_option="USER_ENTERED")
        await callback.answer("Заявка сохранена в Google таблицу!", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка при сохранении: {e}", show_alert=True)

# FSM для отказа пользователя
class UserCancelOrder(StatesGroup):
    waiting_for_confirm = State()
    waiting_for_reason = State()
    waiting_for_custom_reason = State()

USER_CANCEL_REASONS = [
    "Решил(а) сделать сам(а)",
    "Нашёл(ла) исполнителя вне сервиса",
    "Слишком дорого",
    "Другое (ввести вручную)"
]

def get_cancel_confirm_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да", callback_data=f"user_cancel_confirm:{order_id}"),
         InlineKeyboardButton(text="Нет", callback_data="user_cancel_abort")]
    ])

def get_cancel_reason_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=reason, callback_data=f"user_cancel_reason:{order_id}:{i}")]
        for i, reason in enumerate(USER_CANCEL_REASONS)
    ])

def get_admin_cancel_accept_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❇️ Принято", callback_data=f"admin_accept_cancel:{order_id}")]
    ])

@router.callback_query(F.data.startswith("user_cancel_order:"))
async def user_cancel_order_start(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    await state.set_state(UserCancelOrder.waiting_for_confirm)
    await state.update_data(cancel_order_id=order_id)
    await callback.message.edit_text(
        "❗️ Вы уверены, что хотите отказаться от заявки?",
        reply_markup=get_cancel_confirm_keyboard(order_id)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("user_cancel_confirm:"), UserCancelOrder.waiting_for_confirm)
async def user_cancel_confirm(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    await state.update_data(cancel_order_id=order_id)
    await state.set_state(UserCancelOrder.waiting_for_reason)
    await callback.message.edit_text(
        "💬 Пожалуйста, выберите причину отказа:",
        reply_markup=get_cancel_reason_keyboard(order_id)
    )
    await callback.answer()

@router.callback_query(F.data == "user_cancel_abort", UserCancelOrder.waiting_for_confirm)
async def user_cancel_abort(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data.startswith("user_cancel_reason:"), UserCancelOrder.waiting_for_reason)
async def user_cancel_reason(callback: CallbackQuery, state: FSMContext):
    _, order_id, idx = callback.data.split(":")
    idx = int(idx)
    if USER_CANCEL_REASONS[idx].startswith("Другое"):
        await state.set_state(UserCancelOrder.waiting_for_custom_reason)
        await callback.message.edit_text("✍️ Пожалуйста, введите причину отказа:")
        await callback.answer()
        return
    await finish_user_cancel_order(callback, state, order_id, USER_CANCEL_REASONS[idx])

@router.message(UserCancelOrder.waiting_for_custom_reason)
async def user_cancel_custom_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("cancel_order_id")
    await finish_user_cancel_order(message, state, order_id, message.text)

async def finish_user_cancel_order(message_or_callback, state, order_id, reason):
    user_id = message_or_callback.from_user.id
    orders = get_user_orders(user_id)
    updated = False
    target_order = None # Для получения имени
    for order in orders:
        if str(order['order_id']) == str(order_id):
            order['status'] = "Ожидает удаления"
            order['cancel_reason'] = reason
            updated = True
            target_order = order # Сохраняем заказ
            break
    # Обновляем orders.json полностью
    all_orders = []
    file_path = "orders.json"
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                all_orders = json.load(f)
            except json.JSONDecodeError:
                all_orders = []
    
    found_order = None
    for i, o in enumerate(all_orders):
        if str(o.get('order_id')) == str(order_id) and o.get('user_id') == user_id:
            all_orders[i]['status'] = "Ожидает удаления"
            all_orders[i]['cancel_reason'] = reason
            found_order = all_orders[i] # Находим заказ для получения имени
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(all_orders, f, ensure_ascii=False, indent=4)
    await state.clear()
    # Уведомляем пользователя
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer("⏳ Ваша заявка отправлена на отмену. Ожидайте решения администратора.")
    else:
        await message_or_callback.message.edit_text("⏳ Ваша заявка отправлена на отмену. Ожидайте решения администратора.")
        await message_or_callback.answer()
    
    # Уведомляем администратора, используя полное имя
    full_name = get_full_name(found_order) if found_order else f"@{getattr(message_or_callback.from_user, 'username', 'N/A')}"
    admin_text = f"""
❌ <b>Клиент</b> {full_name} (ID: {user_id}) хочет отказаться от заявки №{order_id}.

<b>Причина:</b> {reason}
"""
    await bot.send_message(
        ADMIN_ID,
        admin_text,
        parse_mode="HTML",
        reply_markup=get_admin_cancel_accept_keyboard(order_id)
    )

@admin_router.callback_query(F.data.startswith("admin_accept_cancel:"))
async def admin_accept_cancel_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    # Удаляем заявку из orders.json
    file_path = "orders.json"
    orders = []
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                orders = json.load(f)
            except json.JSONDecodeError:
                orders = []
    # Найти заявку для уведомления клиента
    target_order = next((o for o in orders if str(o.get("order_id")) == str(order_id)), None)
    user_id = target_order.get("user_id") if target_order else None
    work_type = target_order.get("work_type", "") if target_order else ""
    # Удаляем заявку
    new_orders = [o for o in orders if str(o.get("order_id")) != str(order_id)]
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(new_orders, f, ensure_ascii=False, indent=4)
    await callback.message.edit_text(f"✅ Заявка №{order_id} отменена и удалена.")
    # Уведомляем клиента
    if user_id:
        emoji = "❌"
        work_type_str = work_type.replace("work_type_", "") if work_type else ""
        await bot.send_message(user_id, f"{emoji} Администратор отменил вашу заявку на тему: <b>{work_type_str}</b>", parse_mode="HTML")
    await callback.answer()

# --- FSM для админа, когда он сам берет заказ ---
class AdminSelfTake(StatesGroup):
    waiting_for_price = State()
    waiting_for_deadline = State()
    waiting_for_comment = State()
    waiting_for_confirm = State()

# --- Клавиатуры для вариантов цены и срока ---
def get_admin_price_keyboard():
    buttons = [
        [InlineKeyboardButton(text=f"{i} ₽", callback_data=f"admin_price_{i}") for i in range(500, 2501, 500)],
        [InlineKeyboardButton(text=f"{i} ₽", callback_data=f"admin_price_{i}") for i in range(3000, 5001, 1000)],

    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_deadline_keyboard():
    buttons = [
        [InlineKeyboardButton(text="1 день", callback_data="admin_deadline_1 день"),
         InlineKeyboardButton(text="3 дня", callback_data="admin_deadline_3 дня"),
         InlineKeyboardButton(text="До дедлайна", callback_data="admin_deadline_До дедлайна")],
       
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_skip_comment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="admin_skip_comment")]
    ])

def get_admin_self_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Отправить на оплату", callback_data="admin_self_send_to_pay")]
    ])

# --- Хендлеры для кнопки 'Взять заказ' ---
@admin_router.callback_query(F.data.startswith("admin_self_take_"))
async def admin_self_take_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != int(ADMIN_ID): return
    order_id = int(callback.data.split("_")[-1])
    await state.update_data(order_id=order_id)
    await state.set_state(AdminSelfTake.waiting_for_price)
    await callback.message.edit_text("💰 Выберите или введите цену для клиента, или напишите вручную(только число):", reply_markup=get_admin_price_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_price_"), AdminSelfTake.waiting_for_price)
async def admin_self_take_price_choice(callback: CallbackQuery, state: FSMContext):
    price = callback.data.split("_")[-1]
    await state.update_data(price=price)
    await state.set_state(AdminSelfTake.waiting_for_deadline)
    await callback.message.edit_text("⏳ Выберите или введите срок выполнения, или напишите вручную(колл-во дней) :", reply_markup=get_admin_deadline_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data == "admin_price_manual", AdminSelfTake.waiting_for_price)
async def admin_self_take_price_manual(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("💰 Введите цену вручную (только число):")
    # Не обязательно снова ставить состояние, если оно уже стоит
    await state.set_state(AdminSelfTake.waiting_for_price)
    await callback.answer()

@admin_router.message(AdminSelfTake.waiting_for_price)
async def admin_self_take_price_manual_input(message: Message, state: FSMContext):
    print("Ввод вручную цены:", message.text)
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите только число.")
        return
    await state.update_data(price=message.text)
    await state.set_state(AdminSelfTake.waiting_for_deadline)
    await message.answer("⏳ Выберите или введите срок выполнения:", reply_markup=get_admin_deadline_keyboard())

@admin_router.callback_query(F.data.startswith("admin_deadline_"), AdminSelfTake.waiting_for_deadline)
async def admin_self_take_deadline_choice(callback: CallbackQuery, state: FSMContext):
    deadline = callback.data.split("_", 2)[-1]
    await state.update_data(deadline=deadline)
    await state.set_state(AdminSelfTake.waiting_for_comment)
    await callback.message.edit_text("💬 Добавьте комментарий к заказу (или пропустите этот шаг):", reply_markup=get_admin_skip_comment_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data == "admin_deadline_manual", AdminSelfTake.waiting_for_deadline)
async def admin_self_take_deadline_manual(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("⏳ Введите срок вручную:")
    await state.set_state(AdminSelfTake.waiting_for_deadline)
    await callback.answer()

@admin_router.message(AdminSelfTake.waiting_for_deadline)
async def admin_self_take_deadline_manual_input(message: Message, state: FSMContext):
    await state.update_data(deadline=message.text)
    await state.set_state(AdminSelfTake.waiting_for_comment)
    await message.answer("💬 Добавьте комментарий к заказу (или пропустите этот шаг):", reply_markup=get_admin_skip_comment_keyboard())

@admin_router.message(AdminSelfTake.waiting_for_comment)
async def admin_self_take_comment_input(message: Message, state: FSMContext):
    await state.update_data(comment=message.text)
    await state.set_state(AdminSelfTake.waiting_for_confirm)
    await show_admin_self_confirm(message, state)

@admin_router.callback_query(F.data == "admin_skip_comment", AdminSelfTake.waiting_for_comment)
async def admin_self_take_skip_comment(callback: CallbackQuery, state: FSMContext):
    await state.update_data(comment="")
    await state.set_state(AdminSelfTake.waiting_for_confirm)
    await show_admin_self_confirm(callback.message, state)
    await callback.answer()

def pluralize_days(n):
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n)
    if 11 <= n % 100 <= 14:
        return f"{n} дней"
    elif n % 10 == 1:
        return f"{n} день"
    elif 2 <= n % 10 <= 4:
        return f"{n} дня"
    else:
        return f"{n} дней"

async def show_admin_self_confirm(message_or_callback, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    price = data.get("price")
    deadline = data.get("deadline")
    comment = data.get("comment", "")
    # Добавляем склонение для дней, если это число
    deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
    text = f"\n<b>📝Заявка №{order_id}</b>\n<b>💰Стоимость:</b> {price} ₽\n<b>📌Срок выполнения:</b> {deadline_str}"
    if comment:
        text += f"\n<b>Комментарий:</b> {comment}"
    text += "\n\nПроверьте данные и отправьте клиенту на оплату."
    await message_or_callback.answer(text, parse_mode="HTML", reply_markup=get_admin_self_confirm_keyboard())

@admin_router.callback_query(F.data == "admin_self_send_to_pay", AdminSelfTake.waiting_for_confirm)
async def admin_self_take_send_to_pay(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    price = data.get("price")
    deadline = data.get("deadline")
    comment = data.get("comment", "")
    # Обновляем заказ в JSON
    orders = get_all_orders()
    target_order = None
    for order in orders:
        if order.get("order_id") == order_id:
            order['executor_offer'] = {
                'price': price,
                'deadline': deadline,
                'executor_id': int(ADMIN_ID),
                'executor_username': 'admin',
                'executor_full_name': get_full_name(callback.from_user),
                'executor_comment': comment
            }
            order['status'] = "Ожидает оплаты"
            target_order = order
            break
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    # Уведомление клиенту
    customer_id = target_order.get('user_id')
    if customer_id:
        deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
        work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')
        customer_text = f"""
✅ Исполнитель найден! Ваша заявка на тему: {work_type} готова к оплате!

<b>Итоговая стоимость:</b> {price} ₽.
<b>Срок:</b> {deadline_str}
"""
        if comment:
            customer_text += f"<b>Комментарий:</b> {comment}\n"
        customer_text += "\nНажмите кнопку ниже, чтобы перейти к оплате."
        payment_button = InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay_{order_id}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[payment_button]])
        try:
            await bot.send_message(customer_id, customer_text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            await callback.message.answer(f"⚠️ Не удалось уведомить клиента {customer_id}")
    await callback.message.edit_text(f"✅ Ваше предложение по заказу №{order_id} отправлено клиенту. Ожидаем оплату.")
    await state.clear()

@executor_router.callback_query(F.data.startswith("executor_back_to_materials:"))
async def executor_back_to_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    # Просто вызываем executor_show_materials_handler
    class DummyCallback:
        def __init__(self, data, message, from_user):
            self.data = data
            self.message = message
            self.from_user = from_user
    dummy_callback = DummyCallback(f"executor_show_materials:{order_id}", callback.message, callback.from_user)
    await executor_show_materials_handler(dummy_callback, state)
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_back_to_invite"))
async def executor_back_to_invite_handler(callback: CallbackQuery, state: FSMContext):
    # Получаем order_id только из callback.data
    if ":" in callback.data:
        order_id = callback.data.split(":", 1)[1]
    else:
        await callback.answer("Ошибка: не удалось определить заказ.", show_alert=True)
        return
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    work_type = order.get('work_type', 'N/A').replace('work_type_', '')
    subject = order.get('subject', 'Не указан')
    deadline = order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [
            InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")
        ],
    ])
    await callback.message.edit_text(executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
    await callback.answer()

# --- Клавиатура подтверждения для исполнителя ---
def get_executor_final_confirm_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data=f"executor_send_offer:{order_id}"),
         InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")]
    ])

def get_client_work_approval_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять работу", callback_data=f"client_accept_work:{order_id}")],
        [InlineKeyboardButton(text="✍️ Отправить на доработку", callback_data=f"client_request_revision:{order_id}")]
    ])

async def main():
    await dp.start_polling(bot)
   
if __name__ == "__main__":
    asyncio.run(main())

@router.callback_query(F.data.startswith("client_accept_work:"))
async def client_accept_work(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(':')[-1])
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target_order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    
    target_order['status'] = "Выполнена"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    await callback.message.edit_text("🎉 Спасибо, что приняли работу! Рады были помочь.")
    
    # Уведомления
    if target_order.get('executor_id'):
        try:
            await bot.send_message(target_order.get('executor_id'), f"🎉 Клиент принял вашу работу по заказу №{order_id}!")
        except: pass
    try:
        await bot.send_message(ADMIN_ID, f"🎉 Клиент принял работу по заказу №{order_id}.")
    except: pass
    
    await callback.answer()

@router.callback_query(F.data.startswith("client_request_revision:"))
async def client_request_revision(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(':')[-1])
    await state.set_state(ClientRevision.waiting_for_revision_comment)
    await state.update_data(revision_order_id=order_id)
    await callback.message.edit_text("✍️ Пожалуйста, подробно опишите, какие доработки требуются. Ваше сообщение будет передано исполнителю.")
    await callback.answer()

@router.message(ClientRevision.waiting_for_revision_comment)
async def process_revision_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('revision_order_id')
    comment = message.text
    
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target_order:
        await message.answer("Не удалось найти заказ для отправки на доработку.")
        await state.clear()
        return

    target_order['status'] = "На доработке"
    target_order['revision_comment'] = comment
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
        
    await message.answer("✅ Замечания отправлены исполнителю. Ожидайте исправления.")
    
    # Уведомления
    executor_id = target_order.get('executor_id')
    if executor_id:
        try:
            await bot.send_message(
                executor_id,
                f"❗️Заказ №{order_id} отправлен на доработку.\n\n<b>Комментарий клиента:</b>\n{comment}",
                parse_mode="HTML"
            )
        except: pass
    try:
        await bot.send_message(
            ADMIN_ID,
            f"❗️Клиент отправил заказ №{order_id} на доработку.\n\n<b>Комментарий:</b>\n{comment}",
            parse_mode="HTML"
        )
    except: pass
        
    await state.clear()


# --- Процесс создания нового заказа ---
# ... (остальной код main.py)

