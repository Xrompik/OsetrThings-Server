"""
OsetrThings Server — работает 24/7 на вашем облачном сервере.

Что делает:
1. Telegram-бот @OsetrPlansBot: текст -> задача (понимает «сегодня/завтра/послезавтра»,
   даты 17.07 и время «в 15:30»); «Какие дела?» -> список на сегодня.
2. Веб-страница для iPhone (тёмная, компактная), защищена паролем.
3. API для синхронизации с Mac-приложением OsetrThings (заголовок X-Token).

Настройки берутся из переменных окружения (.env): BOT_TOKEN, WEB_PASSWORD,
API_TOKEN, SECRET, TZ.
"""

import asyncio
import hashlib
import hmac
import os
import re
import sqlite3
import time
import uuid as uuid_lib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEB_PASSWORD = os.environ["WEB_PASSWORD"]
API_TOKEN = os.environ["API_TOKEN"]
SECRET = os.environ["SECRET"]
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))
DB_PATH = os.environ.get("DB_PATH", "/data/tasks.db")

COOKIE_VALUE = hmac.new(SECRET.encode(), b"osetrthings-auth", hashlib.sha256).hexdigest()

# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS tasks (
    uuid TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    is_completed INTEGER NOT NULL DEFAULT 0,
    is_someday INTEGER NOT NULL DEFAULT 0,
    is_trashed INTEGER NOT NULL DEFAULT 0,
    due_date TEXT,
    has_time INTEGER NOT NULL DEFAULT 0,
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    updated_at REAL NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0
)""")
db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
db.commit()


def meta_get(key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key, value):
    db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, str(value)))
    db.commit()


# ----------------------------------------------------------------------------
# Даты
# ----------------------------------------------------------------------------
def now_local():
    return datetime.now(TZ)


def today_start_utc_iso():
    local = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_utc_iso(dt_local):
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def local_day(s):
    """Дата (день) задачи в местном времени."""
    dt = parse_iso(s)
    return dt.astimezone(TZ).date() if dt else None


# ----------------------------------------------------------------------------
# Разбор текста задачи: «завтра купить хлеб в 15:30»
# ----------------------------------------------------------------------------
def parse_task_text(text):
    working = text
    day = None
    hour = minute = None

    m = re.search(r"(послезавтра|завтра|сегодня)", working, re.IGNORECASE)
    if m:
        offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1).lower()]
        day = (now_local() + timedelta(days=offset)).date()
        working = working[:m.start()] + " " + working[m.end():]

    if day is None:
        m = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)", working)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            year = now_local().year
            if m.group(3):
                year = int(m.group(3))
                if year < 100:
                    year += 2000
            try:
                parsed = datetime(year, mo, d, tzinfo=TZ).date()
                if not m.group(3) and parsed < now_local().date():
                    parsed = parsed.replace(year=year + 1)
                day = parsed
                working = working[:m.start()] + " " + working[m.end():]
            except ValueError:
                pass

    m = re.search(r"(?:\bв\s*)?([01]?\d|2[0-3]):([0-5]\d)(?!\d)", working)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        working = working[:m.start()] + " " + working[m.end():]

    if hour is not None and day is None:
        day = now_local().date()

    title = " ".join(working.split()) or text
    due_iso = None
    has_time = False
    if day is not None:
        dt = datetime(day.year, day.month, day.day, tzinfo=TZ)
        if hour is not None:
            dt = dt.replace(hour=hour, minute=minute)
            has_time = True
        due_iso = to_utc_iso(dt)
    return title, due_iso, has_time


# ----------------------------------------------------------------------------
# Операции с задачами
# ----------------------------------------------------------------------------
def add_task(title, due_iso, has_time):
    task_id = str(uuid_lib.uuid4()).upper()
    db.execute(
        "INSERT INTO tasks (uuid,title,due_date,has_time,updated_at) VALUES (?,?,?,?,?)",
        (task_id, title, due_iso or today_start_utc_iso(), int(has_time), time.time()),
    )
    db.commit()
    return task_id


def rollover_overdue():
    """Невыполненные задачи прошлых дней -> на сегодня (как обычные задачи)."""
    today = now_local().date()
    rows = db.execute(
        "SELECT uuid,due_date FROM tasks WHERE deleted=0 AND is_trashed=0 "
        "AND is_completed=0 AND is_someday=0 AND due_date IS NOT NULL"
    ).fetchall()
    for row in rows:
        d = local_day(row["due_date"])
        if d and d < today:
            db.execute(
                "UPDATE tasks SET due_date=?, has_time=0, updated_at=? WHERE uuid=?",
                (today_start_utc_iso(), time.time(), row["uuid"]),
            )
    db.commit()


def today_tasks():
    rollover_overdue()
    today = now_local().date()
    rows = db.execute(
        "SELECT * FROM tasks WHERE deleted=0 AND is_trashed=0 AND is_someday=0 "
        "AND due_date IS NOT NULL ORDER BY has_time DESC, due_date"
    ).fetchall()
    return [r for r in rows if local_day(r["due_date"]) == today]


def format_today_list():
    rows = [r for r in today_tasks() if not r["is_completed"]]
    if not rows:
        return "На сегодня ничего не запланировано 🎉"
    lines = ["✅ Задачи на сегодня:"]
    for r in rows:
        line = "• "
        if r["has_time"]:
            dt = parse_iso(r["due_date"]).astimezone(TZ)
            line += dt.strftime("%H:%M") + " — "
        line += r["title"]
        lines.append(line)
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Telegram-бот
# ----------------------------------------------------------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def linked_chat():
    value = meta_get("chat_id")
    return int(value) if value else None


@dp.message(CommandStart())
async def on_start(message: Message):
    if linked_chat() is None:
        meta_set("chat_id", message.chat.id)
    if message.chat.id != linked_chat():
        return
    await message.answer(
        "Бот OsetrThings подключён (работает 24/7).\n"
        "Пиши текст — добавлю задачу. Понимаю даты: «завтра», «17.07», «в 15:30».\n"
        "«Какие дела?» — список на сегодня."
    )


@dp.message()
async def on_text(message: Message):
    if not message.text:
        return
    if linked_chat() is None:
        meta_set("chat_id", message.chat.id)
    if message.chat.id != linked_chat():
        return  # чужие сообщения игнорируем

    text = message.text.strip()
    if re.search(r"какие\s+дела", text, re.IGNORECASE) or text == "/today":
        await message.answer(format_today_list())
        return

    title, due_iso, has_time = parse_task_text(text)
    add_task(title, due_iso, has_time)

    day = local_day(due_iso) if due_iso else now_local().date()
    today = now_local().date()
    if day == today:
        day_text = "сегодня"
    elif day == today + timedelta(days=1):
        day_text = "завтра"
    else:
        day_text = day.strftime("%d.%m")
    reply = f"✅ Добавлено на {day_text}"
    if has_time:
        reply += " в " + parse_iso(due_iso).astimezone(TZ).strftime("%H:%M")
    await message.answer(reply + f": {title}")


# ----------------------------------------------------------------------------
# FastAPI
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


def api_authorized(request: Request):
    return hmac.compare_digest(request.headers.get("X-Token", ""), API_TOKEN)


def web_authorized(request: Request):
    return hmac.compare_digest(request.cookies.get("ot_auth", ""), COOKIE_VALUE)


# ---- API для Mac-приложения ----
@app.post("/api/upsert")
async def api_upsert(request: Request):
    if not api_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    t = await request.json()
    db.execute(
        """INSERT INTO tasks (uuid,title,note,is_completed,is_someday,is_trashed,
           due_date,has_time,duration_minutes,updated_at,deleted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(uuid) DO UPDATE SET
           title=excluded.title, note=excluded.note, is_completed=excluded.is_completed,
           is_someday=excluded.is_someday, is_trashed=excluded.is_trashed,
           due_date=excluded.due_date, has_time=excluded.has_time,
           duration_minutes=excluded.duration_minutes, updated_at=excluded.updated_at,
           deleted=excluded.deleted
           WHERE excluded.updated_at >= tasks.updated_at""",
        (t["uuid"], t.get("title", ""), t.get("note", ""),
         int(t.get("is_completed", False)), int(t.get("is_someday", False)),
         int(t.get("is_trashed", False)), t.get("due_date"),
         int(t.get("has_time", False)), t.get("duration_minutes", 60),
         t.get("updated_at", time.time()), int(bool(t.get("deleted")))),
    )
    db.commit()
    return {"ok": True}


@app.get("/api/changes")
async def api_changes(request: Request, since: float = 0):
    if not api_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rollover_overdue()
    rows = db.execute("SELECT * FROM tasks WHERE updated_at > ?", (since,)).fetchall()
    return [
        {
            "uuid": r["uuid"], "title": r["title"], "note": r["note"],
            "is_completed": bool(r["is_completed"]), "is_someday": bool(r["is_someday"]),
            "is_trashed": bool(r["is_trashed"]), "due_date": r["due_date"],
            "has_time": bool(r["has_time"]), "duration_minutes": r["duration_minutes"],
            "updated_at": r["updated_at"], "deleted": bool(r["deleted"]),
        }
        for r in rows
    ]


# ---- Вход по паролю ----
LOGIN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>OsetrThings</title><style>
body{background:#1c1c1e;color:#eee;font-family:-apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{display:flex;flex-direction:column;gap:12px;width:260px}
h1{text-align:center;font-size:22px}
input,button{font-size:17px;padding:12px;border-radius:10px;border:none}
input{background:#2c2c2e;color:#eee}
button{background:#0a84ff;color:#fff;font-weight:600}
.err{color:#ff453a;text-align:center;font-size:14px}
</style></head><body><form method="post" action="/login">
<h1>🐟 OsetrThings</h1>
<input type="password" name="password" placeholder="Пароль" autofocus>
<button type="submit">Войти</button>ERR</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML.replace("ERR", "")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    if hmac.compare_digest(str(form.get("password", "")), WEB_PASSWORD):
        response = RedirectResponse("/", status_code=302)
        response.set_cookie("ot_auth", COOKIE_VALUE, max_age=365 * 24 * 3600,
                            httponly=True, samesite="lax")
        return response
    return HTMLResponse(LOGIN_HTML.replace("ERR", '<div class="err">Неверный пароль</div>'))


# ---- Веб-приложение (страница для iPhone) ----
APP_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="OsetrThings">
<link rel="manifest" href="/manifest.json">
<title>OsetrThings</title><style>
:root{color-scheme:dark}
body{background:#1c1c1e;color:#eee;font-family:-apple-system,sans-serif;margin:0;
padding:env(safe-area-inset-top) 14px 40px}
h1{font-size:24px;margin:18px 4px 10px}
h2{font-size:13px;color:#8e8e93;text-transform:uppercase;margin:20px 4px 6px}
.add{display:flex;gap:8px;margin:10px 0}
.add input{flex:1;font-size:16px;padding:11px;border-radius:10px;border:none;
background:#2c2c2e;color:#eee}
.add button{font-size:20px;padding:0 16px;border-radius:10px;border:none;
background:#0a84ff;color:#fff}
.task{display:flex;align-items:flex-start;gap:10px;padding:9px 4px;
border-bottom:1px solid #2c2c2e}
.box{width:20px;height:20px;border:1.5px solid #8e8e93;border-radius:5px;
flex-shrink:0;margin-top:1px}
.done .box{background:#0a84ff;border-color:#0a84ff}
.done .title{text-decoration:line-through;color:#8e8e93}
.title{font-size:16px;line-height:1.35;word-break:break-word}
.time{color:#ff453a;font-size:14px;font-variant-numeric:tabular-nums;margin-right:2px}
.star{color:#ffd60a;font-size:12px}
.hint{color:#8e8e93;font-size:14px;padding:8px 4px}
</style></head><body>
<h1>⭐ Сегодня</h1>
<div class="add"><input id="inp" placeholder="Новая задача (завтра, 15:30…)"
enterkeyhint="done"><button onclick="add()">＋</button></div>
<div id="today"></div>
<h2>Ближайшие 7 дней</h2><div id="week"></div>
<h2>Входящие / без даты</h2><div id="inbox"></div>
<script>
async function load(){
  const r = await fetch('/web/tasks'); if(r.status===401){location='/login';return}
  const d = await r.json();
  render('today', d.today, true); render('week', d.week, false, true);
  render('inbox', d.inbox, false);
}
function esc(s){const e=document.createElement('span');e.textContent=s;return e.innerHTML}
function render(id, items, star, showDay){
  const el = document.getElementById(id);
  if(!items.length){el.innerHTML='<div class="hint">Пусто</div>';return}
  el.innerHTML = items.map(t=>`<div class="task ${t.is_completed?'done':''}"
   onclick="toggle('${t.uuid}')"><div class="box"></div><div>
   ${star&&!t.is_completed?'<span class="star">★</span> ':''}
   ${t.time?'<span class="time">'+t.time+'</span> ':''}
   ${showDay&&t.day?'<span class="time">'+t.day+'</span> ':''}
   <span class="title">${esc(t.title)}</span></div></div>`).join('');
}
async function toggle(u){await fetch('/web/toggle',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({uuid:u})});load()}
async function add(){const i=document.getElementById('inp');
 const v=i.value.trim();if(!v)return;i.value='';
 await fetch('/web/add',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({text:v})});load()}
document.getElementById('inp').addEventListener('keydown',e=>{if(e.key==='Enter')add()});
load(); setInterval(load, 60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not web_authorized(request):
        return RedirectResponse("/login")
    return APP_HTML


@app.get("/manifest.json")
async def manifest():
    return {
        "name": "OsetrThings", "short_name": "OsetrThings",
        "start_url": "/", "display": "standalone",
        "background_color": "#1c1c1e", "theme_color": "#1c1c1e",
        "icons": [],
    }


@app.get("/web/tasks")
async def web_tasks(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rollover_overdue()
    today = now_local().date()
    week_end = today + timedelta(days=7)
    rows = db.execute(
        "SELECT * FROM tasks WHERE deleted=0 AND is_trashed=0 "
        "ORDER BY has_time DESC, due_date, updated_at"
    ).fetchall()

    def dto(r, with_day=False):
        item = {"uuid": r["uuid"], "title": r["title"],
                "is_completed": bool(r["is_completed"]), "time": None, "day": None}
        if r["has_time"] and r["due_date"]:
            item["time"] = parse_iso(r["due_date"]).astimezone(TZ).strftime("%H:%M")
        if with_day and r["due_date"]:
            item["day"] = local_day(r["due_date"]).strftime("%d.%m")
        return item

    today_list, week_list, inbox_list = [], [], []
    for r in rows:
        if r["is_someday"]:
            continue
        d = local_day(r["due_date"])
        if d == today:
            today_list.append(dto(r))
        elif d and today < d <= week_end and not r["is_completed"]:
            week_list.append(dto(r, with_day=True))
        elif d is None and not r["is_completed"]:
            inbox_list.append(dto(r))

    today_list.sort(key=lambda x: (x["is_completed"], x["time"] is None, x["time"] or ""))
    return {"today": today_list, "week": week_list, "inbox": inbox_list}


@app.post("/web/add")
async def web_add(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    title, due_iso, has_time = parse_task_text(str(body.get("text", "")).strip())
    add_task(title, due_iso, has_time)
    return {"ok": True}


@app.post("/web/toggle")
async def web_toggle(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    db.execute(
        "UPDATE tasks SET is_completed = 1 - is_completed, updated_at=? WHERE uuid=?",
        (time.time(), body.get("uuid", "")),
    )
    db.commit()
    return {"ok": True}
