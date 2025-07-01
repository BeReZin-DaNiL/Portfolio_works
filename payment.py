import io
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime, timedelta
import qrcode
from shared import get_all_orders, ADMIN_ID, bot, STATUS_EMOJI_MAP, get_full_name, pluralize_days
from aiogram.types import BufferedInputFile
import json

payment_router = Router()

# --- FSM для оплаты ---
class PaymentState(StatesGroup):
    waiting_for_payment = State()
    waiting_for_screenshot = State()

# --- FSM для отказа исполнителя ---
class ExecutorCancelOrder(StatesGroup):
    waiting_for_confirm = State()
    waiting_for_reason = State()
    waiting_for_custom_reason = State()
    waiting_for_comment = State()

# Причины отказа исполнителя
EXECUTOR_CANCEL_REASONS = [
    "Не успею до дедлайна",
    "Передумал",
    "Сложная тема",
    "Другое (ввести вручную)"
]

# --- Клавиатуры ---
def get_payment_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"payment_paid:{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить оплату", callback_data=f"payment_cancel:{order_id}")]
    ])

def get_admin_payment_check_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"admin_payment_accept:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_payment_reject:{order_id}")
        ]
    ])

def get_executor_work_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начинаю работу", callback_data=f"executor_start_work:{order_id}")],
        [InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_work:{order_id}")]
    ])

def get_executor_cancel_confirm_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data=f"executor_cancel_confirm:{order_id}"),
         InlineKeyboardButton(text="❌ Нет", callback_data="executor_cancel_abort")]
    ])

def get_executor_cancel_reason_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=reason, callback_data=f"executor_cancel_reason:{order_id}:{i}")]
        for i, reason in enumerate(EXECUTOR_CANCEL_REASONS)
    ])

def get_executor_skip_comment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="executor_skip_comment")]
    ])

def generate_qr_code(payment_url: str) -> BufferedInputFile:
    img = qrcode.make(payment_url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return BufferedInputFile(buf.getvalue(), filename="qr_code.png")

# --- Хендлер старта оплаты ---
@payment_router.callback_query(F.data.startswith("pay_"))
async def start_payment(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[1])
    # Здесь можно получить сумму заказа из orders.json
    # Для примера:
    orders = get_all_orders()
    order = next((o for o in orders if o['order_id'] == order_id), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    price = order.get('final_price') or order.get('executor_offer', {}).get('price', '—')
    subject = order.get('subject', 'Не указан')
    work_type = order.get('work_type', 'Не указан').replace('work_type_', '')
    # Генерируем ссылку для оплаты (заглушка)
    payment_url = f"https://qr.nspk.ru/FAKE-SBP-ORDER-{order_id}-{price}"
    qr = generate_qr_code(payment_url)
    # Сохраняем время старта сессии
    await state.set_state(PaymentState.waiting_for_payment)
    await state.update_data(payment_order_id=order_id, payment_start=datetime.now().isoformat())
    await callback.message.answer(
        f"💳 Сессия оплаты длится 15 минут!\n\nОплатите заказ по предмету: <b>{subject}</b>\nСумма: <b>{price} ₽</b>\n\nСкоро будет подключён эквайринг, сейчас оплата по СБП.\n\nОтсканируйте QR-код ниже для оплаты:",
        parse_mode="HTML"
    )
    await callback.message.answer_photo(qr, caption="После оплаты нажмите кнопку ниже.", reply_markup=get_payment_keyboard(order_id))
    await callback.answer()

# --- Хендлер нажатия 'Я оплатил' ---
@payment_router.callback_query(F.data.startswith("payment_paid:"), PaymentState.waiting_for_payment)
async def payment_paid(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PaymentState.waiting_for_screenshot)
    await callback.message.answer("📃 Пожалуйста, прикрепите скриншот об оплате.")
    await callback.answer()

# --- Приём скриншота и отправка админу ---
@payment_router.message(PaymentState.waiting_for_screenshot, F.photo | F.document)
async def payment_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('payment_order_id')
    orders = get_all_orders()
    order = next((o for o in orders if o['order_id'] == order_id), None)
    if not order:
        await message.answer("Заказ не найден.")
        await state.clear()
        return
    price = order.get('final_price') or order.get('executor_offer', {}).get('price', '—')
    subject = order.get('subject', 'Не указан')
    work_type = order.get('work_type', 'Не указан').replace('work_type_', '')
    full_name = get_full_name(order)
    # Пересылаем админу
    caption = f"💸 Новый скриншот оплаты по заказу \"{work_type}\"\n" \
              f"👤 Клиент: <b>{full_name}</b>\n" \
              f"📚 Предмет: <b>{subject}</b>\n" \
              f"Сумма: <b>{price} ₽</b>\n\n" \
              "Проверьте и подтвердите оплату."
    if message.photo:
        file_id = message.photo[-1].file_id
        await bot.send_photo(ADMIN_ID, file_id, caption=caption, parse_mode="HTML", reply_markup=get_admin_payment_check_keyboard(order_id))
    elif message.document:
        await bot.send_document(ADMIN_ID, message.document.file_id, caption=caption, parse_mode="HTML", reply_markup=get_admin_payment_check_keyboard(order_id))
    await message.answer("📃 Скриншот отправлен на проверку администратору. Ожидайте подтверждения.")
    await state.clear()

# --- Админ подтверждает или отклоняет оплату ---
@payment_router.callback_query(F.data.startswith("admin_payment_accept:"))
async def admin_payment_accept(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    orders = get_all_orders()
    order = next((o for o in orders if o['order_id'] == order_id), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    order['status'] = "В работе"
    # Сохраняем orders.json
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    # Уведомляем клиента
    user_id = order.get('user_id')
    emoji = STATUS_EMOJI_MAP.get('В работе', '⏳')
    if user_id:
        deadline_executor = order.get('executor_offer', {}).get('deadline') or order.get('deadline', '')
        deadline_executor_str = pluralize_days(deadline_executor) if isinstance(deadline_executor, str) and deadline_executor.isdigit() else deadline_executor
        await bot.send_message(
            user_id,
            f"✅ Оплата подтверждена! Ваш заказ теперь {emoji} В работе.\n"
            f"⏳ Дедлайн исполнителя: <b>{deadline_executor_str}</b>",
            parse_mode="HTML"
        )
    # Уведомляем исполнителя
    executor_id = order.get('executor_offer', {}).get('executor_id')
    deadline_executor = order.get('executor_offer', {}).get('deadline') or order.get('deadline', '')
    deadline_client = order.get('deadline', 'Не указан')
    subject = order.get('subject', 'Не указан')
    work_type = order.get('work_type', 'Не указан').replace('work_type_', '')
    deadline_executor_str = pluralize_days(deadline_executor) if isinstance(deadline_executor, str) and deadline_executor.isdigit() else deadline_executor
    if executor_id:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📔 Перейти к заказу", callback_data=f"executor_view_order_{order_id}")],
            [InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_work:{order_id}")]
        ])
        await bot.send_message(
            executor_id,
            f"💸 Клиент оплатил заказ!\nСтатус: В работе.\n\n"
            f"дедлайн клиента: {deadline_client}\n"
            f"⏳ Время на работу: {deadline_executor_str}\n"
            f"📚 Предмет: {subject}\n"
            f"Тип работы: {work_type}",
            parse_mode='HTML',
            reply_markup=keyboard
        )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await bot.send_message(callback.from_user.id, "✅ Оплата успешно подтверждена, статус заказа переходит в работу ⏳")
    await callback.answer()

@payment_router.callback_query(F.data.startswith("admin_payment_reject:"))
async def admin_payment_reject(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    orders = get_all_orders()
    order = next((o for o in orders if o['order_id'] == order_id), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    user_id = order.get('user_id')
    # Меняем статус на 'Ожидает оплаты'
    order['status'] = "Ожидает оплаты"
    # Сохраняем orders.json
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    if user_id:
        await bot.send_message(user_id, "❌ Оплата не подтверждена. Пожалуйста, попробуйте ещё раз или обратитесь к администратору.")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.edit_text("Оплата отменена.")
    await callback.answer()

# --- Отмена оплаты пользователем ---
@payment_router.callback_query(F.data.startswith("payment_cancel:"), PaymentState)
async def payment_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Оплата отменена.")
    await callback.answer()

# --- Обработка кнопки 'Начинаю работу' ---
@payment_router.callback_query(F.data.startswith("executor_start_work:"))
async def executor_start_work(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Заказ успешно перешел в работу ⏳")
    await callback.answer()

# --- Обработка кнопки 'Отказаться' ---
@payment_router.callback_query(F.data.startswith("executor_refuse_work:"))
async def executor_refuse_work(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])
    await state.set_state(ExecutorCancelOrder.waiting_for_confirm)
    await state.update_data(cancel_order_id=order_id)
    await callback.message.edit_text(
        "❗️ Вы уверены что хотите отказаться от заказа?",
        reply_markup=get_executor_cancel_confirm_keyboard(order_id)
    )
    await callback.answer()

@payment_router.callback_query(F.data.startswith("executor_cancel_confirm:"), ExecutorCancelOrder.waiting_for_confirm)
async def executor_cancel_confirm(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])
    await state.set_state(ExecutorCancelOrder.waiting_for_reason)
    await callback.message.edit_text(
        "📃 Пожалуйста, выберите причину отказа:",
        reply_markup=get_executor_cancel_reason_keyboard(order_id)
    )
    await callback.answer()

@payment_router.callback_query(F.data == "executor_cancel_abort", ExecutorCancelOrder.waiting_for_confirm)
async def executor_cancel_abort(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()

@payment_router.callback_query(F.data.startswith("executor_cancel_reason:"), ExecutorCancelOrder.waiting_for_reason)
async def executor_cancel_reason(callback: CallbackQuery, state: FSMContext):
    _, order_id, idx = callback.data.split(":")
    idx = int(idx)
    if EXECUTOR_CANCEL_REASONS[idx].startswith("Другое"):
        await state.set_state(ExecutorCancelOrder.waiting_for_custom_reason)
        await callback.message.edit_text("✍️ Пожалуйста, введите причину отказа:", reply_markup=get_executor_skip_comment_keyboard())
        await callback.answer()
        return
    await finish_executor_cancel_order(callback, state, order_id, EXECUTOR_CANCEL_REASONS[idx], "")

@payment_router.message(ExecutorCancelOrder.waiting_for_custom_reason)
async def executor_cancel_custom_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("cancel_order_id")
    await finish_executor_cancel_order(message, state, order_id, "Другое", message.text)

@payment_router.callback_query(F.data == "executor_skip_comment", ExecutorCancelOrder.waiting_for_custom_reason)
async def executor_skip_comment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("cancel_order_id")
    await finish_executor_cancel_order(callback, state, order_id, "Другое", "")

async def finish_executor_cancel_order(message_or_callback, state, order_id, reason, comment):
    # Обновляем заказ
    orders = get_all_orders()
    updated = False
    for order in orders:
        if str(order['order_id']) == str(order_id):
            order['status'] = "Рассматривается"
            order['executor_cancel_reason'] = reason
            order['executor_cancel_comment'] = comment
            order.pop('executor_offer', None)
            order.pop('executor_id', None)  # Удаляем исполнителя
            updated = True
            break
    # Сохраняем orders.json
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    await state.clear()
    # Уведомляем исполнителя
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer("❎ Заказ отменен, администратор получит уведомление")
    else:
        await message_or_callback.message.edit_text("❎ Заказ отменен, администратор получит уведомление")
        await message_or_callback.answer()
    # Уведомляем администратора
    admin_text = f"""
❌ <b>Исполнитель</b> отказался от заказа №{order_id}.
<b>Причина:</b> {reason}
<b>Комментарий:</b> {comment or 'Нет'}
    """
    await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML")

@payment_router.callback_query(F.data.startswith("admin_confirm_payment:"))
async def admin_confirm_payment(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])

    all_orders = get_all_orders()
    target_order = next((o for o in all_orders if o.get("order_id") == order_id), None)

    if not target_order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    
    # Меняем статус
    target_order['status'] = "В работе"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(all_orders, f, ensure_ascii=False, indent=4)

    # Уведомления
    customer_id = target_order.get("user_id")
    executor_id = target_order.get("executor_id")
    offer = target_order.get("executor_offer", {})
    executor_deadline = offer.get("deadline", "не указан")
    deadline_str = pluralize_days(executor_deadline)
    executor_full_name = offer.get('executor_full_name', 'Не назначен')

    # Уведомление администратору
    admin_text = (
        f"✅ Оплата успешно подтверждена, статус заказа переходит в работу ⏳\n\n"
        f"<b>Исполнитель:</b> {executor_full_name}\n"
        f"<b>Срок сдачи работы:</b> {deadline_str}"
    )
    await callback.message.edit_text(admin_text, parse_mode="HTML")

    # Уведомление клиенту
    if customer_id:
        try:
            client_text = (
                f"✅ Оплата по вашей заявке подтверждена!\n\n"
                f"Исполнитель уже приступил к работе. "
                f"Ожидаемый срок сдачи: <b>{deadline_str}</b>."
            )
            await bot.send_message(customer_id, client_text, parse_mode="HTML")
        except Exception as e:
            await bot.send_message(ADMIN_ID, f"Не удалось уведомить клиента {customer_id} о подтверждении оплаты. Ошибка: {e}")

    # Уведомление исполнителю
    if executor_id:
        try:
            deadline_client = target_order.get("deadline", "не указан")
            subject = target_order.get("subject", "не указан")
            work_type = target_order.get("work_type", "").replace("work_type_", "")
            executor_text = (
                f"✅ Клиент оплатил заказ! Можно приступать к работе.\n\n"
                f"<b>Предмет:</b> {subject}\n"
                f"<b>Тип работы:</b> {work_type}\n"
                f"<b>Срок сдачи от клиента:</b> {deadline_client}\n"
                f"<b>Ваш дедлайн:</b> {deadline_str}"
            )
            await bot.send_message(executor_id, executor_text, parse_mode="HTML")
        except Exception as e:
            await bot.send_message(ADMIN_ID, f"Не удалось уведомить исполнителя {executor_id} о подтверждении оплаты. Ошибка: {e}")

    await callback.answer()

@payment_router.callback_query(F.data.startswith("admin_reject_payment:"))
async def admin_reject_payment(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])

    orders = get_all_orders()
    target_order = next((o for o in orders if o.get("order_id") == order_id), None)
    
    if not target_order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
        
    target_order["status"] = "Ожидает оплаты"
    
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
        
    customer_id = target_order.get("user_id")
    if customer_id:
        try:
            rejection_text = "❌ Администратор отклонил вашу оплату. Пожалуйста, свяжитесь с ним для уточнения деталей или попробуйте снова."
            await bot.send_message(customer_id, rejection_text)
        except Exception as e:
            await bot.send_message(ADMIN_ID, f"Не удалось уведомить клиента {customer_id} об отклонении оплаты. Ошибка: {e}")
            
    await callback.message.edit_text("Вы отклонили оплату.")
    await callback.answer() 