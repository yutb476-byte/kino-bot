"""
Kino bot (aiogram 3.x + SQLite)
Imkoniyatlar: referal, majburiy obuna, adminlar, statistika,
reklama, kino boshqaruvi, VIP, ID qidirish.

Ishga tushirish:
    pip install -r requirements.txt
    BOT_TOKEN=123:ABC OWNER_ID=123456789 python bot.py
"""
import asyncio
import html
import logging
import os
import re
import sqlite3
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, ChatJoinRequest, ChatMemberUpdated,
                           InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YANGI_TOKENNI_SHU_YERGA_YOZING")
OWNER_ID = int(os.getenv("OWNER_ID", "7883264888"))
REF_BONUS = 60  # har bir referal uchun so'm
VIP_PRICE = "5 000 so'm / oy"
ADMIN_CONTACT = "@uzb_wib"   # VIP sotib olish uchun

START = time.time()
db = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "kino.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, username TEXT, joined INTEGER,
  ref_by INTEGER, refs INTEGER DEFAULT 0, vip_until INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS movies(
  code TEXT PRIMARY KEY, title TEXT, file_id TEXT, ftype TEXT,
  vip INTEGER DEFAULT 0, views INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels(
  chat_id INTEGER PRIMARY KEY, title TEXT, link TEXT);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
""")
db.commit()
try:
    db.execute("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass
db.execute("CREATE TABLE IF NOT EXISTS join_reqs(user_id INTEGER, chat_id INTEGER, PRIMARY KEY(user_id,chat_id))")
db.execute("CREATE TABLE IF NOT EXISTS stat_adj(key TEXT PRIMARY KEY, val INTEGER DEFAULT 0)")
db.execute("CREATE TABLE IF NOT EXISTS ch_joins(user_id INTEGER, chat_id INTEGER, ts INTEGER, PRIMARY KEY(user_id,chat_id))")
try:
    db.execute("ALTER TABLE channels ADD COLUMN req INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
try:
    db.execute("ALTER TABLE channels ADD COLUMN auto INTEGER DEFAULT 0")  # 0 = so'rov kutib turadi
except sqlite3.OperationalError:
    pass
try:
    db.execute("ALTER TABLE join_reqs ADD COLUMN done INTEGER DEFAULT 0")
    db.execute("UPDATE join_reqs SET done=1")  # eski so'rovlar allaqachon qabul qilingan
except sqlite3.OperationalError:
    pass
try:
    db.execute("ALTER TABLE movies ADD COLUMN trailer TEXT")
except sqlite3.OperationalError:
    pass
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, val TEXT)")
db.commit()


def q(sql, args=(), one=False, commit=False):
    cur = db.execute(sql, args)
    if commit:
        db.commit()
    return (cur.fetchone() if one else cur.fetchall()) if not commit else cur


def esc(t) -> str:
    return html.escape(str(t or ""))


def is_admin(uid: int) -> bool:
    return uid == OWNER_ID or q("SELECT 1 FROM admins WHERE id=?", (uid,), one=True) is not None


def is_vip(uid: int) -> bool:
    r = q("SELECT vip_until FROM users WHERE id=?", (uid,), one=True)
    return bool(r and r["vip_until"] > time.time())


def add_vip_days(uid: int, days: int):
    r = q("SELECT vip_until FROM users WHERE id=?", (uid,), one=True)
    base = max(int(time.time()), r["vip_until"] if r else 0)
    q("UPDATE users SET vip_until=? WHERE id=?", (base + days * 86400, uid), commit=True)


def kb(*rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True)


USER_MENU = kb(["🎯 Referal", "💎 VIP"], ["ℹ️ Yordam"])
ADMIN_MENU = kb(
    ["📊 Statistika", "🎬 Kino boshqaruvi"],
    ["📢 Majburiy obuna", "🎯 Referal boshqaruv"],
    ["👥 Foydalanuvchilar", "👮 Adminlar"],
    ["📤 Reklama", "💎 VIP boshqaruv"],
    ["🔍 ID qidirish", "🎯 Referal"],
    ["🏠 Bosh menyu"])
CANCEL = kb(["❌ Bekor qilish"])


def menu_for(uid):
    return ADMIN_MENU if is_admin(uid) else USER_MENU


def ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) if not d.startswith("http")
         else InlineKeyboardButton(text=t, url=d) for t, d in row] for row in rows])


class S(StatesGroup):
    movie_file = State()
    movie_code = State()
    movie_title = State()
    movie_vip = State()
    movie_del = State()
    chan_add = State()
    admin_add = State()
    broadcast = State()
    vip_give = State()
    ref_adjust = State()
    find_id = State()
    stat_add = State()
    movie_trailer = State()
    post_chan = State()


router = Router()
top = Router()
admin_r = Router()
admin_r.message.filter(lambda m: is_admin(m.from_user.id))
admin_r.callback_query.filter(lambda c: is_admin(c.from_user.id))


# ---------------------------------------------------------- majburiy obuna
async def not_subscribed(bot: Bot, uid: int):
    """Obuna bo'lmagan kanallar ro'yxatini qaytaradi."""
    bad = []
    for ch in q("SELECT * FROM channels"):
        try:
            m = await bot.get_chat_member(ch["chat_id"], uid)
            if m.status in ("left", "kicked"):
                if not q("SELECT 1 FROM join_reqs WHERE user_id=? AND chat_id=?",
                         (uid, ch["chat_id"]), one=True):
                    bad.append(ch)
        except Exception:
            continue  # bot kanalda admin emas - o'tkazib yuboramiz
    return bad


def sub_kb(chs):
    rows = [[(f"📢 {c['title']}", c["link"])] for c in chs]
    rows.append([("✅ Tekshirish", "check_sub")])
    return ikb(rows)


# ------------------------------------------------------------------- start
def register(user, ref_by=None):
    if q("SELECT 1 FROM users WHERE id=?", (user.id,), one=True):
        return False
    q("INSERT INTO users(id,name,username,joined,ref_by) VALUES(?,?,?,?,?)",
      (user.id, user.full_name, user.username, int(time.time()), ref_by), commit=True)
    return True


@top.message(CommandStart())
async def start(m: Message, command: CommandObject, bot: Bot):
    ref = None
    if command.args and command.args.startswith("ref"):
        try:
            ref = int(command.args[3:])
        except ValueError:
            pass
    if ref == m.from_user.id or not q("SELECT 1 FROM users WHERE id=?", (ref,), one=True):
        ref = None
    if register(m.from_user, ref) and ref:
        q("UPDATE users SET refs=refs+1, balance=balance+? WHERE id=?",
          (REF_BONUS, ref), commit=True)
        u = q("SELECT refs,balance FROM users WHERE id=?", (ref,), one=True)
        try:
            await bot.send_message(
                ref,
                f"🎉 Yangi referal! +{REF_BONUS} so'm\n"
                f"👥 Jami: <b>{u['refs']}</b>\n"
                f"💰 Balans: <b>{u['balance']} so'm</b>")
        except Exception:
            pass
    bad = await not_subscribed(bot, m.from_user.id)
    if bad:
        return await m.answer("Botdan foydalanish uchun kanallarga obuna bo'ling:",
                              reply_markup=sub_kb(bad))
    await m.answer("🎬 Xush kelibsiz!\nKino kodini yuboring.", reply_markup=menu_for(m.from_user.id))
    if command.args and command.args.startswith("kino"):
        await deliver_movie(m, bot, command.args[4:])


@router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    bad = await not_subscribed(bot, c.from_user.id)
    if bad:
        return await c.answer("Hali hamma kanalga obuna bo'lmadingiz!", show_alert=True)
    await c.message.delete()
    register(c.from_user)
    await c.message.answer("✅ Rahmat! Endi kino kodini yuboring.",
                           reply_markup=menu_for(c.from_user.id))


# ------------------------------------------------------- foydalanuvchi menyu
@top.message(F.text == "❌ Bekor qilish")
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=menu_for(m.from_user.id))


@top.message(F.text == "🏠 Bosh menyu")
async def home(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🏠 Bosh menyu", reply_markup=menu_for(m.from_user.id))


@router.message(F.text == "🎯 Referal")
async def referal(m: Message, bot: Bot):
    me = await bot.get_me()
    u = q("SELECT refs,balance FROM users WHERE id=?", (m.from_user.id,), one=True)
    refs = u["refs"] if u else 0
    bal = u["balance"] if u else 0
    await m.answer(
        f"🎯 <b>Referal bo'limi</b>\n\n"
        f"Har bir taklif qilingan do'st uchun <b>{REF_BONUS} so'm</b> olasiz!\n\n"
        f"👥 Taklif qilinganlar: <b>{refs}</b>\n"
        f"💰 Balansingiz: <b>{bal} so'm</b>\n\n"
        f"🔗 Havolangiz:\nhttps://t.me/{me.username}?start=ref{m.from_user.id}")


@router.message(F.text == "💎 VIP")
async def vip(m: Message):
    r = q("SELECT vip_until FROM users WHERE id=?", (m.from_user.id,), one=True)
    if r and r["vip_until"] > time.time():
        left = int((r["vip_until"] - time.time()) // 86400)
        status = f"✅ VIP faol, <b>{left}</b> kun qoldi"
    else:
        status = "❌ VIP faol emas"
    await m.answer(f"💎 <b>VIP</b>\n{status}\n\nNarx: {VIP_PRICE}\nSotib olish: {ADMIN_CONTACT}")


@router.message(F.text == "ℹ️ Yordam")
async def help_(m: Message):
    await m.answer("Kino kodini raqam ko'rinishida yuboring, bot kinoni jo'natadi.")


# --------------------------------------------------------- kino kodi yuborish
@router.message(F.text.regexp(r"^\d{1,10}$"))
async def get_movie(m: Message, bot: Bot):
    register(m.from_user)
    bad = await not_subscribed(bot, m.from_user.id)
    if bad:
        return await m.answer("Avval kanallarga obuna bo'ling:", reply_markup=sub_kb(bad))
    await deliver_movie(m, bot, m.text)


# ===================================================================== ADMIN
@admin_r.message(F.text == "/admin")
async def admin_cmd(m: Message):
    await m.answer("👮 Admin panel", reply_markup=ADMIN_MENU)


# ---- statistika
STAT_NAMES = {
    "users": "👥 Foydalanuvchilar",
    "today": "➕ Bugun",
    "movies": "🎬 Kinolar",
    "views": "👁 Ko'rishlar",
    "vips": "💎 Faol VIP",
}


def stat_line(key: str, real: int) -> str:
    """Haqiqiy son; qo'lda qo'shilgan bo'lsa, alohida ko'rsatiladi."""
    r = q("SELECT val FROM stat_adj WHERE key=?", (key,), one=True)
    a = r["val"] if r else 0
    if a:
        return f"{STAT_NAMES[key]}: <b>{real + a}</b> (haqiqiy {real}, qo'lda {a:+d})"
    return f"{STAT_NAMES[key]}: <b>{real}</b>"


def stats_view(uid: int):
    now = int(time.time())
    total = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    today = q("SELECT COUNT(*) c FROM users WHERE joined>?", (now - 86400,), one=True)["c"]
    movies = q("SELECT COUNT(*) c FROM movies", one=True)["c"]
    vips = q("SELECT COUNT(*) c FROM users WHERE vip_until>?", (now,), one=True)["c"]
    views = q("SELECT COALESCE(SUM(views),0) c FROM movies", one=True)["c"]
    chans = q("SELECT COUNT(*) c FROM channels", one=True)["c"]
    up = int(now - START)
    text = (f"📊 <b>Statistika</b>\n\n{stat_line('users', total)}\n{stat_line('today', today)}\n"
            f"{stat_line('movies', movies)}\n{stat_line('views', views)}\n"
            f"{stat_line('vips', vips)}\n📢 Majburiy kanallar: <b>{chans}</b>\n\n"
            f"⏰ Uptime: {up // 86400} kun, {up % 86400 // 3600} soat, {up % 3600 // 60} daqiqa")
    markup = ikb([[("➕ Statistikaga qo'shish", "st_add")]]) if uid == OWNER_ID else None
    return text, markup


@admin_r.message(F.text == "📊 Statistika")
async def stats(m: Message):
    text, markup = stats_view(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@admin_r.callback_query(F.data == "st_add")
async def st_add(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Faqat bot egasi", show_alert=True)
    rows = [[(name, f"st_k:{key}")] for key, name in STAT_NAMES.items()]
    rows.append([("♻️ Qo'lda qo'shilganlarni tozalash", "st_clear")])
    await c.message.answer("Qaysi ko'rsatkichga qo'shamiz?", reply_markup=ikb(rows))
    await c.answer()


@admin_r.callback_query(F.data.startswith("st_k:"))
async def st_key(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Faqat bot egasi", show_alert=True)
    key = c.data.split(":")[1]
    await state.set_state(S.stat_add)
    await state.update_data(key=key)
    await c.message.answer(f"{STAT_NAMES[key]}: qancha qo'shamiz? Raqam yozing "
                           f"(ayirish uchun minus bilan, masalan -10).", reply_markup=CANCEL)
    await c.answer()


@admin_r.message(S.stat_add, F.text.regexp(r"^-?\d{1,9}$"))
async def st_add_do(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    key = (await state.get_data()).get("key")
    q("INSERT INTO stat_adj(key,val) VALUES(?,?) "
      "ON CONFLICT(key) DO UPDATE SET val=val+excluded.val", (key, int(m.text)), commit=True)
    await state.clear()
    text, markup = stats_view(m.from_user.id)
    await m.answer("✅ Qo'shildi.", reply_markup=ADMIN_MENU)
    await m.answer(text, reply_markup=markup)


@admin_r.callback_query(F.data == "st_clear")
async def st_clear(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Faqat bot egasi", show_alert=True)
    q("DELETE FROM stat_adj", commit=True)
    await c.answer("Tozalandi")
    text, markup = stats_view(c.from_user.id)
    await c.message.answer(text, reply_markup=markup)


# ---- kino boshqaruvi
@admin_r.message(F.text == "🎬 Kino boshqaruvi")
async def movie_panel(m: Message):
    n = q("SELECT COUNT(*) c FROM movies", one=True)["c"]
    await m.answer(f"🎬 Kinolar soni: <b>{n}</b>", reply_markup=ikb([
        [("➕ Kino qo'shish", "mv_add"), ("🗑 Kino o'chirish", "mv_del")],
        [("📃 Oxirgi 10 ta", "mv_list"), ("📣 Post kanal", "mv_post")]]))


@admin_r.callback_query(F.data == "mv_add")
async def mv_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.movie_file)
    await c.message.answer("🎞 Kinoni (video yoki fayl) yuboring:", reply_markup=CANCEL)
    await c.answer()


@admin_r.message(S.movie_file, F.video | F.document)
async def mv_file(m: Message, state: FSMContext):
    if m.video:
        await state.update_data(file_id=m.video.file_id, ftype="video")
    else:
        await state.update_data(file_id=m.document.file_id, ftype="document")
    await state.set_state(S.movie_trailer)
    await m.answer("🎥 Endi kinoning <b>qisqa videosini</b> (treyler) yuboring. "
                   "Bot uni avtomatik kanalga joylaydi.",
                   reply_markup=kb(["⏭ O'tkazib yuborish"], ["❌ Bekor qilish"]))


@admin_r.message(S.movie_code, F.text.regexp(r"^\d{1,10}$"))
async def mv_code(m: Message, state: FSMContext):
    if q("SELECT 1 FROM movies WHERE code=?", (m.text,), one=True):
        return await m.answer("⚠️ Bu kod band, boshqa kod yozing:")
    await state.update_data(code=m.text)
    await state.set_state(S.movie_title)
    await m.answer("📝 Kino nomini yozing:")


@admin_r.message(S.movie_title, F.text)
async def mv_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    await state.set_state(S.movie_vip)
    await m.answer("💎 VIP kinomi?", reply_markup=kb(["Ha", "Yo'q"], ["❌ Bekor qilish"]))


@admin_r.message(S.movie_vip, F.text.in_({"Ha", "Yo'q"}))
async def mv_vip(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    vip = m.text == "Ha"
    q("INSERT INTO movies(code,title,file_id,ftype,vip,trailer) VALUES(?,?,?,?,?,?)",
      (d["code"], d["title"], d["file_id"], d["ftype"], int(vip), d.get("trailer")), commit=True)
    await state.clear()
    text = f"✅ Qo'shildi: {d['title']} (kod {d['code']})"
    if d.get("trailer"):
        text += "\n" + await post_trailer(bot, d["code"], d["title"], d["trailer"], vip)
    await m.answer(text, reply_markup=ADMIN_MENU)


# ---- treyler (qisqa video) va kanalga joylash
@admin_r.message(S.movie_trailer, F.video)
async def mv_trailer(m: Message, state: FSMContext):
    await state.update_data(trailer=m.video.file_id)
    await state.set_state(S.movie_code)
    await m.answer("🔑 Kino kodini yozing (faqat raqam):", reply_markup=CANCEL)


@admin_r.message(S.movie_trailer, F.text == "⏭ O'tkazib yuborish")
async def mv_trailer_skip(m: Message, state: FSMContext):
    await state.update_data(trailer=None)
    await state.set_state(S.movie_code)
    await m.answer("🔑 Kino kodini yozing (faqat raqam):", reply_markup=CANCEL)


async def post_trailer(bot: Bot, code: str, title: str, file_id: str, vip: bool) -> str:
    r = q("SELECT val FROM settings WHERE key='post_chan'", one=True)
    if not r:
        return "⚠️ Post kanal sozlanmagan, treyler joylanmadi (🎬 Kino boshqaruvi → 📣 Post kanal)."
    me = await bot.get_me()
    cap = (f"🎬 <b>{esc(title)}</b>\n🔑 Kod: <code>{code}</code>\n"
           f"{'💎 VIP kino' + chr(10) if vip else ''}\n👉 Kinoni olish: @{me.username}")
    try:
        await bot.send_video(
            int(r["val"]), file_id, caption=cap,
            reply_markup=ikb([[("🎬 Kinoni ko'rish", f"https://t.me/{me.username}?start=kino{code}")]]))
        return "📣 Treyler kanalga joylandi."
    except Exception as e:
        return f"⚠️ Kanalga joylab bo'lmadi: {esc(e)}"


async def deliver_movie(m: Message, bot: Bot, code: str):
    mv = q("SELECT * FROM movies WHERE code=?", (code,), one=True)
    if not mv:
        return await m.answer("❌ Bunday kodli kino topilmadi.")
    if mv["vip"] and not is_vip(m.from_user.id) and not is_admin(m.from_user.id):
        return await m.answer("💎 Bu kino faqat VIP a'zolar uchun.\nVIP olish: «💎 VIP» tugmasi "
                              "yoki do'stlarni taklif qiling («🎯 Referal»).")
    cap = f"🎬 <b>{esc(mv['title'])}</b>\n🔑 Kod: {mv['code']}"
    send = bot.send_video if mv["ftype"] == "video" else bot.send_document
    await send(m.chat.id, mv["file_id"], caption=cap)
    q("UPDATE movies SET views=views+1 WHERE code=?", (code,), commit=True)


@admin_r.callback_query(F.data == "mv_post")
async def mv_post(c: CallbackQuery, state: FSMContext):
    r = q("SELECT val FROM settings WHERE key='post_chan'", one=True)
    cur = f"Hozirgi: <code>{r['val']}</code>" if r else "Hozircha sozlanmagan."
    await state.set_state(S.post_chan)
    await c.message.answer(
        f"📣 <b>Post kanal</b>\n{cur}\n\nTreylerlar shu kanalga joylanadi. Botni kanalga admin qiling "
        f"(xabar yuborish huquqi bilan), so'ng kanal ID si (<code>-1001234567890</code>), @username "
        f"yoki kanaldan forward xabar yuboring.", reply_markup=CANCEL)
    await c.answer()


@admin_r.message(S.post_chan)
async def mv_post_do(m: Message, state: FSMContext, bot: Bot):
    try:
        if m.forward_origin and getattr(m.forward_origin, "chat", None):
            chat = await bot.get_chat(m.forward_origin.chat.id)
        else:
            t = (m.text or "").strip()
            if re.fullmatch(r"-?\d+", t):
                chat = await bot.get_chat(int(t) if t.startswith("-100") else int("-100" + t.lstrip("-")))
            else:
                chat = await bot.get_chat(t)
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
        if me.status not in ("administrator", "creator"):
            return await m.answer("⚠️ Bot bu kanalda admin emas. Admin qilib qayta yuboring.")
        if getattr(me, "can_post_messages", True) is False:
            return await m.answer("⚠️ Botga «xabar joylash» huquqini bering va qayta yuboring.")
    except Exception as e:
        return await m.answer(f"❌ Xatolik: {esc(e)}")
    q("INSERT OR REPLACE INTO settings(key,val) VALUES('post_chan',?)", (str(chat.id),), commit=True)
    await state.clear()
    await m.answer(f"✅ Post kanal: {esc(chat.title)}", reply_markup=ADMIN_MENU)


@admin_r.callback_query(F.data == "mv_del")
async def mv_del(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.movie_del)
    await c.message.answer("O'chiriladigan kino kodini yozing:", reply_markup=CANCEL)
    await c.answer()


@admin_r.message(S.movie_del, F.text)
async def mv_del_do(m: Message, state: FSMContext):
    cur = q("DELETE FROM movies WHERE code=?", (m.text,), commit=True)
    await state.clear()
    await m.answer("🗑 O'chirildi." if cur.rowcount else "❌ Topilmadi.", reply_markup=ADMIN_MENU)


@admin_r.callback_query(F.data == "mv_list")
async def mv_list(c: CallbackQuery):
    rows = q("SELECT * FROM movies ORDER BY rowid DESC LIMIT 10")
    text = "\n".join(f"{'💎' if r['vip'] else '🎬'} {r['code']} — {r['title']} ({r['views']}👁)"
                     for r in rows) or "Kinolar yo'q"
    await c.message.answer(text)
    await c.answer()


@top.chat_join_request()
async def on_join_request(req: ChatJoinRequest, bot: Bot):
    """So'rovni yozib qo'yadi. Avto-qabul yoqilmagan bo'lsa, so'rov kanalda kutib turadi."""
    ch = q("SELECT auto FROM channels WHERE chat_id=?", (req.chat.id,), one=True)
    if not ch:
        return
    q("INSERT OR IGNORE INTO join_reqs(user_id,chat_id,done) VALUES(?,?,0)",
      (req.from_user.id, req.chat.id), commit=True)
    q("INSERT OR IGNORE INTO ch_joins VALUES(?,?,?)",
      (req.from_user.id, req.chat.id, int(time.time())), commit=True)
    if ch["auto"]:
        try:
            await bot.approve_chat_join_request(req.chat.id, req.from_user.id)
            q("UPDATE join_reqs SET done=1 WHERE user_id=? AND chat_id=?",
              (req.from_user.id, req.chat.id), commit=True)
        except Exception:
            pass


@top.chat_member()
async def on_chat_member(ev: ChatMemberUpdated):
    """Oddiy kanalga kirganlarni hisoblash."""
    if not q("SELECT 1 FROM channels WHERE chat_id=?", (ev.chat.id,), one=True):
        return
    old, new = ev.old_chat_member.status, ev.new_chat_member.status
    if old in ("left", "kicked") and new in ("member", "administrator", "creator"):
        q("INSERT OR IGNORE INTO ch_joins VALUES(?,?,?)",
          (ev.new_chat_member.user.id, ev.chat.id, int(time.time())), commit=True)


# ---- majburiy obuna (kanallar)
async def channels_view(target: Message):
    rows = q("SELECT * FROM channels")
    btns = []
    for r in rows:
        cnt = q("SELECT COUNT(*) c FROM ch_joins WHERE chat_id=?", (r["chat_id"],), one=True)["c"]
        btns.append([(f"📢 {r['title']} — {cnt} ta", f"ch_info:{r['chat_id']}")])
    btns.append([("➕ Kanal ulash", "ch_add")])
    btns.append([("📨 Zayafkali kanal ulash", "ch_add_req")])
    await target.answer(
        f"📢 Majburiy obuna kanallari: <b>{len(rows)}</b>\nKanal ustiga bossangiz, statistikasi chiqadi.",
        reply_markup=ikb(btns))


@admin_r.message(F.text == "📢 Majburiy obuna")
async def ch_panel(m: Message):
    await channels_view(m)


@admin_r.callback_query(F.data == "ch_add_req")
async def ch_add_req(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.chan_add)
    await state.update_data(req=True)
    await c.message.answer(
        "📨 <b>Zayafkali kanal</b>\n"
        "Botni kanalga <b>admin</b> qiling, so'ng quyidagilardan birini yuboring:\n• kanal <b>ID</b> si (maxfiy kanal uchun), masalan <code>-1001234567890</code>\n• yoki ochiq kanal @username (masalan @kanalim)\n• yoki kanaldan istalgan xabarni forward qiling\n\nID ni bilish: kanaldan biror xabarni @getidsbot ga forward qiling.",
        reply_markup=CANCEL)
    await c.answer()


@admin_r.callback_query(F.data == "ch_add")
async def ch_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.chan_add)
    await state.update_data(req=False)
    await c.message.answer(
        "📢 <b>Oddiy kanal</b>\n"
        "Botni kanalga <b>admin</b> qiling, so'ng quyidagilardan birini yuboring:\n• kanal <b>ID</b> si (maxfiy kanal uchun), masalan <code>-1001234567890</code>\n• yoki ochiq kanal @username (masalan @kanalim)\n• yoki kanaldan istalgan xabarni forward qiling\n\nID ni bilish: kanaldan biror xabarni @getidsbot ga forward qiling.",
        reply_markup=CANCEL)
    await c.answer()


@admin_r.message(S.chan_add)
async def ch_add_do(m: Message, state: FSMContext, bot: Bot):
    try:
        if m.forward_origin and getattr(m.forward_origin, "chat", None):
            chat = await bot.get_chat(m.forward_origin.chat.id)
        else:
            t = (m.text or "").strip()
            if re.fullmatch(r"-?\d+", t):
                # -100 bilan yoki -100siz yozilgan ID ni to'g'rilaymiz
                cid = int(t) if t.startswith("-100") else int("-100" + t.lstrip("-"))
                chat = await bot.get_chat(cid)
            else:
                chat = await bot.get_chat(t)
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
        if me.status not in ("administrator", "creator"):
            return await m.answer("⚠️ Bot bu kanalda admin emas. Admin qilib qayta yuboring.")
        req = (await state.get_data()).get("req", False)
        if req:
            link = (await bot.create_chat_invite_link(chat.id, creates_join_request=True)).invite_link
        else:
            link = f"https://t.me/{chat.username}" if chat.username else \
                (await bot.create_chat_invite_link(chat.id)).invite_link
    except Exception as e:
        return await m.answer(
            f"❌ Xatolik: {esc(e)}\n\nBot kanalda admin ekanini va ID to'g'riligini tekshiring.")
    q("INSERT OR REPLACE INTO channels(chat_id,title,link,req) VALUES(?,?,?,?)",
      (chat.id, chat.title, link, int(bool(req))), commit=True)
    await state.clear()
    await m.answer(f"✅ Ulandi: {chat.title}", reply_markup=ADMIN_MENU)


async def ch_card(bot: Bot, cid: int):
    ch = q("SELECT * FROM channels WHERE chat_id=?", (cid,), one=True)
    if not ch:
        return None
    now = int(time.time())
    total = q("SELECT COUNT(*) c FROM ch_joins WHERE chat_id=?", (cid,), one=True)["c"]
    today = q("SELECT COUNT(*) c FROM ch_joins WHERE chat_id=? AND ts>?", (cid, now - 86400), one=True)["c"]
    week = q("SELECT COUNT(*) c FROM ch_joins WHERE chat_id=? AND ts>?", (cid, now - 7 * 86400), one=True)["c"]
    pending = q("SELECT COUNT(*) c FROM join_reqs WHERE chat_id=? AND done=0", (cid,), one=True)["c"]
    try:
        members = await bot.get_chat_member_count(cid)
    except Exception:
        members = "—"
    auto = bool(ch["auto"])
    text = (f"📢 <b>{esc(ch['title'])}</b>\n"
            f"Turi: {'📨 Zayafkali' if ch['req'] else '📢 Oddiy'}\n"
            f"🆔 <code>{cid}</code>\n\n"
            f"👥 Bot orqali kirganlar: <b>{total}</b>\n"
            f"📅 Bugun: <b>{today}</b>\n"
            f"🗓 7 kunda: <b>{week}</b>\n"
            f"👤 Kanaldagi jami a'zolar: <b>{members}</b>\n")
    btns = []
    if ch["req"]:
        text += (f"⏳ Kutayotgan so'rovlar: <b>{pending}</b>\n"
                 f"🔄 Avto-qabul: <b>{'yoqiq' if auto else 'oʻchiq (so‘rovlar kanalda kutadi)'}</b>\n")
        btns.append([(f"✅ Hammasini qabul qilish ({pending})", f"ch_ap:{cid}")])
        btns.append([(f"🔄 Avto-qabul: {'✅ yoqiq' if auto else '❌ oʻchiq'}", f"ch_auto:{cid}")])
    btns.append([("🗑 O'chirish", f"ch_del:{cid}")])
    text += f"\n🔗 {esc(ch['link'])}"
    return text, ikb(btns)


@admin_r.callback_query(F.data.startswith("ch_info:"))
async def ch_info(c: CallbackQuery, bot: Bot):
    card = await ch_card(bot, int(c.data.split(":")[1]))
    if not card:
        return await c.answer("Kanal topilmadi", show_alert=True)
    await c.message.answer(card[0], reply_markup=card[1])
    await c.answer()


@admin_r.callback_query(F.data.startswith("ch_auto:"))
async def ch_auto(c: CallbackQuery, bot: Bot):
    cid = int(c.data.split(":")[1])
    q("UPDATE channels SET auto = 1 - COALESCE(auto,0) WHERE chat_id=?", (cid,), commit=True)
    card = await ch_card(bot, cid)
    if card:
        await safe_edit(c, card[0], card[1])
    await c.answer("O'zgartirildi")


@admin_r.callback_query(F.data.startswith("ch_ap:"))
async def ch_approve_all(c: CallbackQuery, bot: Bot):
    cid = int(c.data.split(":")[1])
    rows = q("SELECT user_id FROM join_reqs WHERE chat_id=? AND done=0", (cid,))
    if not rows:
        return await c.answer("Kutayotgan so'rov yo'q", show_alert=True)
    await c.answer("Qabul qilinmoqda...")
    msg = await c.message.answer(f"⏳ {len(rows)} ta so'rov qabul qilinmoqda...")
    ok = fail = 0
    for r in rows:
        done = False
        for _ in range(3):
            try:
                await bot.approve_chat_join_request(cid, r["user_id"])
                ok += 1
                done = True
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                break
        if not done:
            fail += 1
        q("UPDATE join_reqs SET done=1 WHERE user_id=? AND chat_id=?", (r["user_id"], cid), commit=True)
        await asyncio.sleep(0.1)
    await msg.edit_text(f"✅ Qabul qilindi: {ok}\n⚠️ O'tkazib yuborildi: {fail}")
    card = await ch_card(bot, cid)
    if card:
        await safe_edit(c, card[0], card[1])


@admin_r.callback_query(F.data.startswith("ch_del:"))
async def ch_del(c: CallbackQuery):
    q("DELETE FROM channels WHERE chat_id=?", (int(c.data.split(":")[1]),), commit=True)
    await c.message.delete()
    await c.answer("O'chirildi")
    await channels_view(c.message)


# ---- adminlar
@admin_r.message(F.text == "👮 Adminlar")
async def admins_panel(m: Message):
    rows = q("SELECT id FROM admins")
    text = f"👑 Egasi: <code>{OWNER_ID}</code>\n" + "\n".join(f"👮 <code>{r['id']}</code>" for r in rows)
    btns = [[(f"🗑 {r['id']}", f"adm_del:{r['id']}")] for r in rows] if m.from_user.id == OWNER_ID else []
    btns.append([("➕ Admin qo'shish", "adm_add")])
    await m.answer(text, reply_markup=ikb(btns))


@admin_r.callback_query(F.data == "adm_add")
async def adm_add(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Faqat bot egasi admin qo'sha oladi", show_alert=True)
    await state.set_state(S.admin_add)
    await c.message.answer("Yangi admin Telegram ID sini yuboring:", reply_markup=CANCEL)
    await c.answer()


@admin_r.message(S.admin_add, F.text.regexp(r"^\d+$"))
async def adm_add_do(m: Message, state: FSMContext):
    q("INSERT OR IGNORE INTO admins VALUES(?)", (int(m.text),), commit=True)
    await state.clear()
    await m.answer("✅ Admin qo'shildi.", reply_markup=ADMIN_MENU)


@admin_r.callback_query(F.data.startswith("adm_del:"))
async def adm_del(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Faqat bot egasi", show_alert=True)
    q("DELETE FROM admins WHERE id=?", (int(c.data.split(":")[1]),), commit=True)
    await c.message.delete()
    await c.answer("O'chirildi")


# ---- foydalanuvchilar va referallar
PAGE = 10


def users_view(page: int):
    total = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    n = q("SELECT COUNT(*) c FROM users WHERE refs>0", one=True)["c"]
    tr = q("SELECT COALESCE(SUM(refs),0) c FROM users", one=True)["c"]
    tb = q("SELECT COALESCE(SUM(balance),0) c FROM users", one=True)["c"]
    pages = max(1, (n + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    rows = q("SELECT id,name,refs,balance FROM users WHERE refs>0 "
             "ORDER BY refs DESC, id LIMIT ? OFFSET ?", (PAGE, page * PAGE))
    text = (f"👥 Jami foydalanuvchi: <b>{total}</b>\n"
            f"🎯 Referal olganlar: <b>{n}</b>\n"
            f"📊 Jami referal: <b>{tr}</b>\n"
            f"💰 Jami balans: <b>{tb} so'm</b>\n\n"
            f"<b>Referallar ro'yxati</b> ({page + 1}/{pages}):")
    if not rows:
        text += "\nHali referal yo'q."
    for i, r in enumerate(rows, page * PAGE + 1):
        text += f"\n{i}. {esc(r['name'])} (<code>{r['id']}</code>) — {r['refs']} ta, {r['balance']} so'm"
    btns = [[(f"👤 {(r['name'] or str(r['id']))[:22]} — {r['refs']}", f"us_v:{r['id']}:{page}")]
            for r in rows]
    nav = []
    if page > 0:
        nav.append(("⬅️ Oldingi", f"us_l:{page - 1}"))
    if page < pages - 1:
        nav.append(("Keyingi ➡️", f"us_l:{page + 1}"))
    if nav:
        btns.append(nav)
    return text, ikb(btns)


async def safe_edit(c: CallbackQuery, text: str, markup):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


@admin_r.message(F.text == "👥 Foydalanuvchilar")
async def users_panel(m: Message):
    text, kbd = users_view(0)
    await m.answer(text, reply_markup=kbd)


@admin_r.callback_query(F.data.startswith("us_l:"))
async def us_list(c: CallbackQuery):
    text, kbd = users_view(int(c.data.split(":")[1]))
    await safe_edit(c, text, kbd)
    await c.answer()


@admin_r.callback_query(F.data.startswith("us_v:"))
async def us_view(c: CallbackQuery):
    _, uid, page = c.data.split(":")
    u = q("SELECT * FROM users WHERE id=?", (int(uid),), one=True)
    if not u:
        return await c.answer("Topilmadi", show_alert=True)
    text = (f"👤 <b>{esc(u['name'])}</b>\n🆔 <code>{u['id']}</code>\n"
            f"🎯 Referal: <b>{u['refs']}</b> ta\n💰 Balans: <b>{u['balance']} so'm</b>")
    await safe_edit(c, text, ikb([
        [("0️⃣ Referal va balansni 0 qilish", f"us_z:{uid}:{page}")],
        [("⬅️ Orqaga", f"us_l:{page}")]]))
    await c.answer()


@admin_r.callback_query(F.data.startswith("us_z:"))
async def us_zero(c: CallbackQuery):
    _, uid, page = c.data.split(":")
    u = q("SELECT name,refs,balance FROM users WHERE id=?", (int(uid),), one=True)
    if not u:
        return await c.answer("Topilmadi", show_alert=True)
    await safe_edit(
        c,
        f"⚠️ <b>{esc(u['name'])}</b> ning referali ({u['refs']} ta) va balansi ({u['balance']} so'm) "
        f"0 qilinsinmi?",
        ikb([[("✅ Ha, 0 qilish", f"us_zok:{uid}:{page}"), ("❌ Yo'q", f"us_v:{uid}:{page}")]]))
    await c.answer()


@admin_r.callback_query(F.data.startswith("us_zok:"))
async def us_zero_ok(c: CallbackQuery):
    _, uid, page = c.data.split(":")
    q("UPDATE users SET refs=0, balance=0 WHERE id=?", (int(uid),), commit=True)
    text, kbd = users_view(int(page))
    await safe_edit(c, text, kbd)
    await c.answer("✅ 0 qilindi")


# ---- reklama
@admin_r.message(F.text == "📤 Reklama")
async def ad_start(m: Message, state: FSMContext):
    await state.set_state(S.broadcast)
    await m.answer("Reklama xabarini yuboring (matn, rasm, video — hammasi bo'ladi):",
                   reply_markup=CANCEL)


@admin_r.message(S.broadcast)
async def ad_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ids = [r["id"] for r in q("SELECT id FROM users")]
    await m.answer(f"⏳ Yuborilmoqda: {len(ids)} ta...", reply_markup=ADMIN_MENU)
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, m.chat.id, m.message_id)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Yuborildi: {ok}\n❌ Yetmadi: {fail}")


# ---- VIP boshqaruv
@admin_r.message(F.text == "💎 VIP boshqaruv")
async def vip_panel(m: Message, state: FSMContext):
    n = q("SELECT COUNT(*) c FROM users WHERE vip_until>?", (int(time.time()),), one=True)["c"]
    await state.set_state(S.vip_give)
    await m.answer(f"💎 Faol VIP: <b>{n}</b>\n\nVIP berish: <code>ID KUN</code> (masalan <code>123456 30</code>)\n"
                   f"VIP olib tashlash: <code>ID 0</code>", reply_markup=CANCEL)


@admin_r.message(S.vip_give, F.text.regexp(r"^\d+ \d+$"))
async def vip_give(m: Message, state: FSMContext, bot: Bot):
    uid, days = map(int, m.text.split())
    if not q("SELECT 1 FROM users WHERE id=?", (uid,), one=True):
        return await m.answer("❌ Foydalanuvchi topilmadi.")
    if days == 0:
        q("UPDATE users SET vip_until=0 WHERE id=?", (uid,), commit=True)
        msg = "VIP olib tashlandi."
    else:
        add_vip_days(uid, days)
        msg = f"✅ {days} kun VIP berildi."
        try:
            await bot.send_message(uid, f"💎 Sizga {days} kun VIP berildi!")
        except Exception:
            pass
    await state.clear()
    await m.answer(msg, reply_markup=ADMIN_MENU)


# ---- referal boshqaruv (berish / olish)
@admin_r.message(F.text == "🎯 Referal boshqaruv")
async def ref_panel(m: Message, state: FSMContext):
    await state.set_state(S.ref_adjust)
    await m.answer("Referal berish: <code>ID 5</code>\nReferal olish: <code>ID -5</code>",
                   reply_markup=CANCEL)


@admin_r.message(S.ref_adjust, F.text.regexp(r"^\d+ -?\d+$"))
async def ref_do(m: Message, state: FSMContext):
    uid, n = map(int, m.text.split())
    cur = q("UPDATE users SET refs=MAX(refs+?,0), balance=MAX(balance+?,0) WHERE id=?",
            (n, n * REF_BONUS, uid), commit=True)
    await state.clear()
    await m.answer("✅ Bajarildi." if cur.rowcount else "❌ Foydalanuvchi topilmadi.",
                   reply_markup=ADMIN_MENU)


# ---- ID qidirish
@admin_r.message(F.text == "🔍 ID qidirish")
async def find_start(m: Message, state: FSMContext):
    await state.set_state(S.find_id)
    await m.answer("Foydalanuvchi ID sini yuboring:", reply_markup=CANCEL)


@admin_r.message(S.find_id, F.text.regexp(r"^\d+$"))
async def find_do(m: Message, state: FSMContext):
    u = q("SELECT * FROM users WHERE id=?", (int(m.text),), one=True)
    await state.clear()
    if not u:
        return await m.answer("❌ Topilmadi.", reply_markup=ADMIN_MENU)
    vip_txt = time.strftime("%Y-%m-%d", time.localtime(u["vip_until"])) if u["vip_until"] > time.time() else "yo'q"
    await m.answer(
        f"👤 {u['name']} (@{u['username']})\n🆔 <code>{u['id']}</code>\n"
        f"🎯 Referallar: {u['refs']}\n💎 VIP: {vip_txt}\n"
        f"📅 Qo'shilgan: {time.strftime('%Y-%m-%d', time.localtime(u['joined']))}",
        reply_markup=ADMIN_MENU)


# ==================================================================== main
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN or BOT_TOKEN.startswith("YANGI_TOKEN"):
        raise SystemExit("BOT_TOKEN o'rnatilmagan! Masalan: export BOT_TOKEN=123:ABC")

    # PythonAnywhere BEPUL akkauntda tashqi saytlarga faqat proxy orqali chiqiladi.
    # PythonAnywhere'da bo'lsak, proxy avtomatik yoqiladi. O'chirish: NO_PROXY=1
    proxy = os.getenv("PROXY")
    if not proxy and os.getenv("PYTHONANYWHERE_DOMAIN") and not os.getenv("NO_PROXY"):
        proxy = "http://proxy.server:3128"
    if proxy:
        logging.info("Proxy ishlatilmoqda: %s", proxy)
    session = AiohttpSession(proxy=proxy, timeout=60) if proxy else AiohttpSession(timeout=60)

    bot = Bot(BOT_TOKEN, session=session,
              default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    # tartib muhim: top -> admin (holatlar) -> umumiy
    dp.include_router(top)
    dp.include_router(admin_r)
    dp.include_router(router)
    try:
        # tarmoq vaqtincha uzilsa, bot to'xtab qolmasin
        while True:
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                await dp.start_polling(bot, handle_signals=False)
                break
            except (TelegramNetworkError, OSError) as e:
                logging.warning("Tarmoq xatosi: %s. 10 soniyadan keyin qayta uriniladi...", e)
                await asyncio.sleep(10)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
