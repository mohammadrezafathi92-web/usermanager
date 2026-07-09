from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..panel_bridge import api, ApiError
from ..callbacks import MenuCB, TutorialCB
from ..keyboards import tutorials_kb, home_kb

router = Router(name="tutorials")


@router.callback_query(MenuCB.filter(F.action == "cust_tutorials"))
async def cb_tutorials(call: CallbackQuery) -> None:
    try:
        tutorials = await api.list_tutorials()
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    if not tutorials:
        await call.message.edit_text("در حال حاضر آموزشی ثبت نشده.", reply_markup=home_kb())
        await call.answer()
        return
    await call.message.edit_text("یک آموزش را انتخاب کنید:", reply_markup=tutorials_kb(tutorials))
    await call.answer()


@router.message(Command("tutorials"))
async def cmd_tutorials(message: Message) -> None:
    """Slash-command shortcut for "📚 آموزش"."""
    try:
        tutorials = await api.list_tutorials()
    except ApiError as exc:
        await message.answer(f"خطا: {exc}")
        return
    if not tutorials:
        await message.answer("در حال حاضر آموزشی ثبت نشده.", reply_markup=home_kb())
        return
    await message.answer("یک آموزش را انتخاب کنید:", reply_markup=tutorials_kb(tutorials))


@router.callback_query(TutorialCB.filter())
async def cb_tutorial_detail(call: CallbackQuery, callback_data: TutorialCB, bot: Bot) -> None:
    try:
        tutorials = await api.list_tutorials()
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    tutorial = next((t for t in tutorials if t["id"] == callback_data.tutorial_id), None)
    if not tutorial:
        await call.answer("این آموزش پیدا نشد", show_alert=True)
        return

    await call.answer()
    text = f"📄 <b>{tutorial['title']}</b>"
    if tutorial.get("text"):
        text += f"\n\n{tutorial['text']}"
    await call.message.edit_text(text, reply_markup=home_kb())

    try:
        media = await api.get_tutorial_media(tutorial["id"])
    except ApiError:
        media = []
    for m in media:
        try:
            file = BufferedInputFile(m["content"], filename=m["filename"])
            if m["kind"] == "video":
                await bot.send_video(call.from_user.id, file)
            else:
                await bot.send_photo(call.from_user.id, file)
        except Exception:
            pass
    if media:
        try:
            await bot.send_message(call.from_user.id, "🏠 منو:", reply_markup=home_kb())
        except Exception:
            pass
