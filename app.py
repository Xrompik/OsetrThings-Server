"""
OsetrThings Server v2.1 — работает 24/7 на облачном сервере.

1. Telegram-бот @OsetrPlansBot: текст -> задача (понимает даты), «Какие дела?» -> список.
2. Веб-приложение для iPhone: вкладки Сегодня/Планы/Входящие/Когда-нибудь/Журнал/Корзина,
   добавление, редактирование, выполнение, корзина. Защищено паролем.
3. API синхронизации с Mac-приложением OsetrThings (заголовок X-Token).

Настройки из переменных окружения: BOT_TOKEN, WEB_PASSWORD, API_TOKEN, SECRET, TZ.
"""

import asyncio
import hashlib
import hmac
import json
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

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEB_PASSWORD = os.environ["WEB_PASSWORD"]
API_TOKEN = os.environ["API_TOKEN"]
SECRET = os.environ["SECRET"]
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))
DB_PATH = os.environ.get("DB_PATH", "/data/tasks.db")

COOKIE_VALUE = hmac.new(SECRET.encode(), b"osetrthings-auth", hashlib.sha256).hexdigest()

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
db.execute("""CREATE TABLE IF NOT EXISTS debts (
    uuid TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    is_paid INTEGER NOT NULL DEFAULT 0,
    is_trashed INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
)""")
db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
# Новые колонки (проекты, теги, чек-листы) — добавляем к существующей базе
for _ddl in (
    "ALTER TABLE tasks ADD COLUMN project TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN checklist TEXT NOT NULL DEFAULT '[]'",
):
    try:
        db.execute(_ddl)
    except sqlite3.OperationalError:
        pass
db.commit()


def meta_get(key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key, value):
    db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, str(value)))
    db.commit()


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
    dt = parse_iso(s)
    return dt.astimezone(TZ).date() if dt else None


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


def add_task(title, due_iso, has_time, someday=False):
    task_id = str(uuid_lib.uuid4()).upper()
    db.execute(
        "INSERT INTO tasks (uuid,title,due_date,has_time,is_someday,updated_at) VALUES (?,?,?,?,?,?)",
        (task_id, title, due_iso, int(has_time), int(someday), time.time()),
    )
    db.commit()
    return task_id


def rollover_overdue():
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


def format_today_list():
    """Нумерованный список на сегодня; порядок запоминается,
    чтобы работала команда «Задача 3 выполнена»."""
    rollover_overdue()
    today = now_local().date()
    rows = db.execute(
        "SELECT * FROM tasks WHERE deleted=0 AND is_trashed=0 AND is_someday=0 "
        "AND is_completed=0 AND due_date IS NOT NULL ORDER BY has_time DESC, due_date"
    ).fetchall()
    rows = [r for r in rows if local_day(r["due_date"]) == today]
    if not rows:
        meta_set("tg_list", "[]")
        return "На сегодня ничего не запланировано 🎉"
    meta_set("tg_list", json.dumps([r["uuid"] for r in rows]))
    lines = ["✅ Задачи на сегодня:"]
    for i, r in enumerate(rows, start=1):
        line = f"{i}. "
        if r["has_time"]:
            line += parse_iso(r["due_date"]).astimezone(TZ).strftime("%H:%M") + " — "
        line += r["title"]
        if r["project"]:
            line += f" [{r['project']}]"
        lines.append(line)
    lines.append("")
    lines.append("Напиши «Задача N выполнена» — отмечу.")
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
        return

    text = message.text.strip()
    if re.search(r"какие\s+дела", text, re.IGNORECASE) or text == "/today":
        await message.answer(format_today_list())
        return

    # «Задача 3 выполнена» — отметить по номеру из последнего списка
    m = re.search(r"задач[аиу]\s*№?\s*(\d+)\s*[-—:]?\s*(выполнен|сделан|готов|заверш)",
                  text, re.IGNORECASE)
    if m:
        index = int(m.group(1)) - 1
        uuids = json.loads(meta_get("tg_list") or "[]")
        if 0 <= index < len(uuids):
            row = db.execute("SELECT title FROM tasks WHERE uuid=?", (uuids[index],)).fetchone()
            db.execute("UPDATE tasks SET is_completed=1, updated_at=? WHERE uuid=?",
                       (time.time(), uuids[index]))
            db.commit()
            title = row["title"] if row else "задача"
            await message.answer(f"✅ «{title}» отмечена выполненной")
        else:
            await message.answer("Не нашёл задачу с таким номером — спроси «Какие дела?» ещё раз")
        return

    title, due_iso, has_time = parse_task_text(text)
    add_task(title, due_iso or today_start_utc_iso(), has_time)

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
           due_date,has_time,duration_minutes,updated_at,deleted,project,tags,checklist)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(uuid) DO UPDATE SET
           title=excluded.title, note=excluded.note, is_completed=excluded.is_completed,
           is_someday=excluded.is_someday, is_trashed=excluded.is_trashed,
           due_date=excluded.due_date, has_time=excluded.has_time,
           duration_minutes=excluded.duration_minutes, updated_at=excluded.updated_at,
           deleted=excluded.deleted, project=excluded.project, tags=excluded.tags,
           checklist=excluded.checklist
           WHERE excluded.updated_at >= tasks.updated_at""",
        (t["uuid"], t.get("title", ""), t.get("note", ""),
         int(t.get("is_completed", False)), int(t.get("is_someday", False)),
         int(t.get("is_trashed", False)), t.get("due_date"),
         int(t.get("has_time", False)), t.get("duration_minutes", 60),
         t.get("updated_at", time.time()), int(bool(t.get("deleted"))),
         str(t.get("project") or ""),
         json.dumps(t.get("tags") or [], ensure_ascii=False),
         json.dumps(t.get("checklist") or [], ensure_ascii=False)),
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
            "project": r["project"] or None,
            "tags": json.loads(r["tags"] or "[]"),
            "checklist": json.loads(r["checklist"] or "[]"),
        }
        for r in rows
    ]


# ---- API: долги ----
@app.post("/api/debts/upsert")
async def api_debts_upsert(request: Request):
    if not api_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    d = await request.json()
    db.execute(
        """INSERT INTO debts (uuid,title,amount,note,is_paid,is_trashed,updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(uuid) DO UPDATE SET
           title=excluded.title, amount=excluded.amount, note=excluded.note,
           is_paid=excluded.is_paid, is_trashed=excluded.is_trashed,
           updated_at=excluded.updated_at
           WHERE excluded.updated_at >= debts.updated_at""",
        (d["uuid"], d.get("title", ""), float(d.get("amount", 0)), d.get("note", ""),
         int(d.get("is_paid", False)), int(d.get("is_trashed", False)),
         d.get("updated_at", time.time())),
    )
    db.commit()
    return {"ok": True}


@app.get("/api/debts/changes")
async def api_debts_changes(request: Request, since: float = 0):
    if not api_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = db.execute("SELECT * FROM debts WHERE updated_at > ?", (since,)).fetchall()
    return [
        {
            "uuid": r["uuid"], "title": r["title"], "amount": r["amount"],
            "note": r["note"], "is_paid": bool(r["is_paid"]),
            "is_trashed": bool(r["is_trashed"]), "updated_at": r["updated_at"],
        }
        for r in rows
    ]


# ---- Вход ----
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


# ---- Веб-приложение ----
APP_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="OsetrThings">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.png">
<title>OsetrThings</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
html,body{height:100%}
/* Колонка: шапка фиксирована, список прокручивается независимо (важно для iPhone) */
body{background:#1c1c1e;color:#eee;font-family:-apple-system,sans-serif;margin:0;
display:flex;flex-direction:column;overflow:hidden;
padding:env(safe-area-inset-top) 12px 0}
.head{flex:0 0 auto}
#list{flex:1 1 auto;overflow-y:auto;-webkit-overflow-scrolling:touch;
padding-bottom:calc(40px + env(safe-area-inset-bottom))}
.tabs{display:flex;gap:4px;overflow-x:auto;padding:12px 0 8px;-webkit-overflow-scrolling:touch}
.tabs button{white-space:nowrap;font-size:15px;padding:8px 13px;border-radius:16px;
border:none;background:#2c2c2e;color:#aaa}
.tabs button.on{background:#0a84ff;color:#fff}
.add{display:flex;gap:8px;margin:6px 0 10px}
.add input{flex:1;font-size:17px;padding:12px;border-radius:10px;border:none;
background:#2c2c2e;color:#eee}
.add button{font-size:22px;padding:0 18px;border-radius:10px;border:none;
background:#0a84ff;color:#fff}
h2{font-size:14px;color:#8e8e93;margin:16px 4px 4px;font-weight:600}
.task{display:flex;align-items:flex-start;gap:11px;padding:12px 4px;
border-bottom:1px solid #2c2c2e}
.box{width:22px;height:22px;border:1.5px solid #8e8e93;border-radius:5px;
flex-shrink:0;margin-top:1px}
.done .box{background:#0a84ff;border-color:#0a84ff}
.done .tt{text-decoration:line-through;color:#8e8e93}
.body{flex:1;min-width:0}
.tt{font-size:18px;line-height:1.35;word-break:break-word}
.doc{margin-left:6px;color:#8e8e93;font-size:15px}
.time{color:#ff453a;font-variant-numeric:tabular-nums;margin-right:4px;font-size:15px}
.star{color:#ffd60a;font-size:13px}
.hint{color:#8e8e93;font-size:15px;padding:10px 4px}
.badge{font-size:11px;color:#8e8e93;background:#2c2c2e;border-radius:8px;
padding:1px 7px;margin-left:6px;white-space:nowrap}
.tagchip{font-size:11px;border-radius:8px;padding:1px 7px;margin-left:4px;white-space:nowrap}
.ci{display:flex;gap:10px;align-items:center;padding:7px 2px;font-size:15px}
.ci .cbox{width:18px;height:18px;border:1.5px solid #8e8e93;border-radius:50%;flex-shrink:0}
.ci.don .cbox{background:#0a84ff;border-color:#0a84ff}
.ci.don .ct{text-decoration:line-through;color:#8e8e93}
.ci .del{margin-left:auto;color:#8e8e93;padding:0 6px}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;
align-items:flex-end;justify-content:center;z-index:9}
.sheet{background:#242426;border-radius:16px 16px 0 0;width:100%;max-width:560px;
padding:16px 16px calc(16px + env(safe-area-inset-bottom));display:flex;
flex-direction:column;gap:10px}
.sheet textarea{width:100%;background:#2c2c2e;color:#eee;border:none;border-radius:10px;
padding:10px;font-size:16px;font-family:inherit;resize:vertical}
.row{display:flex;align-items:center;justify-content:space-between;font-size:15px}
.row input{background:#2c2c2e;color:#eee;border:none;border-radius:8px;padding:8px;
font-size:15px;color-scheme:dark}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.btns button{flex:1;min-width:96px;font-size:15px;padding:11px;border-radius:10px;
border:none;background:#3a3a3c;color:#eee}
.btns .primary{background:#0a84ff;color:#fff;font-weight:600}
.btns .danger{background:#3a3a3c;color:#ff453a}
</style></head><body>
<div class="head">
<div class="tabs" id="tabs">
<button data-t="today" class="on">★ Сегодня</button>
<button data-t="upcoming">Планы</button>
<button data-t="projects">Проекты</button>
<button data-t="inbox">Входящие</button>
<button data-t="someday">Когда-нибудь</button>
<button data-t="debts">₽ Долги</button>
<button data-t="logbook">Журнал</button>
<button data-t="trash">Корзина</button>
</div>
<div class="add"><input id="inp" placeholder="Новая задача (завтра, 15:30…)"
enterkeyhint="done"><button onclick="add()">＋</button></div>
</div>
<div id="list"></div>

<div id="modal" class="overlay" style="display:none" onclick="if(event.target===this)closeModal()">
<div class="sheet">
<textarea id="m_title" rows="2" placeholder="Название"></textarea>
<textarea id="m_note" rows="5" placeholder="Заметки" style="max-height:45vh"></textarea>
<div id="m_check"></div>
<div class="row" style="gap:8px">
 <input id="m_newitem" placeholder="＋ пункт чек-листа" style="flex:1"
  onkeydown="if(event.key==='Enter')addCheck()">
</div>
<div class="row"><span>Проект</span>
 <input id="m_project" list="projlist" placeholder="без проекта"><datalist id="projlist"></datalist></div>
<div class="row"><span>Теги</span>
 <input id="m_tags" placeholder="через запятую" style="min-width:55%"></div>
<div class="row"><span>Дата</span><input type="date" id="m_date"></div>
<div class="row"><span>Время</span><input type="time" id="m_time"></div>
<div class="row"><span>Когда-нибудь</span><input type="checkbox" id="m_someday"></div>
<div class="btns">
<button class="primary" onclick="saveModal()">Сохранить</button>
<button class="danger" id="m_trash" onclick="trashModal()">В корзину</button>
<button id="m_restore" onclick="restoreModal()">Вернуть</button>
<button class="danger" id="m_delete" onclick="deleteForever()">Удалить навсегда</button>
</div>
</div></div>

<script>
let TODAY='', tasks=[], tab='today', cur=null, PROJECTS=[], DEBTS=[];

document.getElementById('tabs').addEventListener('click', e=>{
  const b=e.target.closest('button'); if(!b)return;
  tab=b.dataset.t;
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x===b));
  document.getElementById('inp').placeholder = tab==='debts'
    ? 'Долг: 5000 Иван за обед' : 'Новая задача (завтра, 15:30…)';
  render();
});
document.getElementById('inp').addEventListener('keydown',e=>{if(e.key==='Enter')add()});

async function load(){
  const r=await fetch('/web/all'); if(r.status===401){location='/login';return}
  const d=await r.json(); TODAY=d.today; tasks=d.tasks; PROJECTS=d.projects||[];
  DEBTS=d.debts||[]; render();
}
function money(v){return (Math.round(v*100)/100).toLocaleString('ru-RU')+' ₽'}
function esc(s){const e=document.createElement('span');e.textContent=s||'';return e.innerHTML}
function bucket(t){
  if(t.deleted)return null;
  if(t.is_trashed)return 'trash';
  if(t.is_completed)return 'logbook';
  if(t.is_someday)return 'someday';
  if(!t.day)return 'inbox';
  return t.day<=TODAY?'today':'upcoming';
}
function row(t,showDay){
  const cl=(t.checklist||[]);
  const done=cl.filter(x=>x.done).length;
  return `<div class="task ${t.is_completed?'done':''}">
  <div class="box" onclick="toggle('${t.uuid}');event.stopPropagation()"></div>
  <div class="body" onclick="openModal('${t.uuid}')">
   <div class="tt">${tab==='today'&&!t.is_completed?'<span class=star>★</span> ':''}
    ${t.time?'<span class=time>'+t.time+'</span>':''}
    ${showDay&&t.day?'<span class=time>'+t.day.slice(8,10)+'.'+t.day.slice(5,7)+'</span>':''}
    ${esc(t.title)}
    ${t.note?'<span class=doc title="есть заметка">📄</span>':''}
    ${cl.length?'<span class=badge>'+done+'/'+cl.length+'</span>':''}
    ${t.project?'<span class=badge>'+esc(t.project)+'</span>':''}
    ${(t.tags||[]).map(x=>'<span class=tagchip style="color:#'+x.color+';background:#'+x.color+'22">'+esc(x.name)+'</span>').join('')}
   </div>
  </div></div>`;
}
function debtRow(d){
  return `<div class="task ${d.is_paid?'done':''}">
   <div class="box" style="border-radius:50%" onclick="debtToggle('${d.uuid}');event.stopPropagation()"></div>
   <div class="body">
     <div class="tt">${esc(d.title)}${d.note?'<span class=doc>📄</span>':''}</div>
     ${d.note?'<div class="sub" style="white-space:normal;color:#8e8e93;font-size:14px">'+esc(d.note)+'</div>':''}
   </div>
   <div style="font-weight:600;white-space:nowrap">${money(d.amount)}</div>
   <div class="del" style="color:#8e8e93;padding:0 4px" onclick="debtDelete('${d.uuid}')">✕</div>
  </div>`;
}
function render(){
  const el=document.getElementById('list');
  if(tab==='debts'){
    const live=DEBTS.filter(d=>!d.is_trashed);
    const unpaid=live.filter(d=>!d.is_paid), paid=live.filter(d=>d.is_paid);
    const total=unpaid.reduce((s,d)=>s+d.amount,0);
    let html='<h2 style="font-size:16px;color:#eee">Всего должны: '+money(total)+'</h2>';
    html+='<h2>Не погашено</h2>';
    html+= unpaid.length?unpaid.map(debtRow).join(''):'<div class="hint">Нет активных долгов</div>';
    if(paid.length){html+='<h2>Погашено</h2>'+paid.map(debtRow).join('')}
    el.innerHTML=html;
    return;
  }
  if(tab==='projects'){
    // Все открытые задачи, сгруппированные по проектам (спискам)
    const open=tasks.filter(t=>!t.deleted&&!t.is_trashed&&!t.is_completed);
    const groups={};
    for(const t of open){const k=t.project||'Без проекта';(groups[k]=groups[k]||[]).push(t)}
    const keys=Object.keys(groups).sort((a,b)=>
      (a==='Без проекта')-(b==='Без проекта')||a.localeCompare(b,'ru'));
    if(!keys.length){el.innerHTML='<div class="hint">Пусто. Проект создаётся в задаче: открой задачу → поле «Проект» → впиши название.</div>';return}
    let html='';
    for(const k of keys){
      html+='<h2>'+esc(k)+' ('+groups[k].length+')</h2>';
      html+=groups[k].map(t=>row(t,true)).join('');
    }
    el.innerHTML=html;
    return;
  }
  let items=tasks.filter(t=>bucket(t)===tab);
  if(!items.length){el.innerHTML='<div class="hint">Пусто</div>';return}
  if(tab==='today'){
    items.sort((a,b)=>(a.time===null)-(b.time===null)||String(a.time).localeCompare(String(b.time)));
    el.innerHTML=items.map(t=>row(t)).join('');
  }else if(tab==='upcoming'){
    items.sort((a,b)=>a.day.localeCompare(b.day)||String(a.time).localeCompare(String(b.time)));
    let html='',day='';
    for(const t of items){
      if(t.day!==day){day=t.day;html+='<h2>'+day.slice(8,10)+'.'+day.slice(5,7)+'.'+day.slice(0,4)+'</h2>'}
      html+=row(t);
    }
    el.innerHTML=html;
  }else if(tab==='logbook'){
    el.innerHTML=items.reverse().map(t=>row(t,true)).join('');
  }else{
    el.innerHTML=items.map(t=>row(t,tab==='trash')).join('');
  }
}
async function post(url,body){
  await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify(body)});
  await load();
}
async function add(){
  const i=document.getElementById('inp');const v=i.value.trim();if(!v)return;i.value='';
  if(tab==='debts'){
    // «5000 Иван за обед» → сумма + название
    const m=v.match(/^\s*([0-9]+(?:[.,][0-9]+)?)\s+(.+)$/);
    const amount=m?parseFloat(m[1].replace(',','.')):0;
    const title=m?m[2]:v;
    await post('/web/debt_add',{title:title,amount:amount});
    return;
  }
  await post('/web/add',{text:v,target:tab});
}
async function toggle(u){await post('/web/toggle',{uuid:u})}
async function debtToggle(u){await post('/web/debt_toggle',{uuid:u})}
async function debtDelete(u){await post('/web/debt_delete',{uuid:u})}
let editCheck=[];
function renderCheck(){
  m_check.innerHTML=editCheck.map((c,i)=>`<div class="ci ${c.done?'don':''}">
   <div class="cbox" onclick="editCheck[${i}].done=!editCheck[${i}].done;renderCheck()"></div>
   <span class="ct">${esc(c.text)}</span>
   <span class="del" onclick="editCheck.splice(${i},1);renderCheck()">✕</span></div>`).join('');
}
function addCheck(){
  const v=m_newitem.value.trim();if(!v)return;
  editCheck.push({text:v,done:false});m_newitem.value='';renderCheck();
}
function openModal(u){
  cur=tasks.find(t=>t.uuid===u);if(!cur)return;
  m_title.value=cur.title;m_note.value=cur.note||'';
  m_date.value=cur.day||'';m_time.value=cur.time||'';
  m_someday.checked=!!cur.is_someday;
  m_project.value=cur.project||'';
  m_tags.value=(cur.tags||[]).map(x=>x.name).join(', ');
  editCheck=(cur.checklist||[]).map(x=>({text:x.text,done:!!x.done}));
  renderCheck();
  projlist.innerHTML=PROJECTS.map(p=>'<option value="'+esc(p)+'">').join('');
  m_trash.style.display=cur.is_trashed?'none':'';
  m_restore.style.display=cur.is_trashed?'':'none';
  m_delete.style.display=cur.is_trashed?'':'none';
  modal.style.display='flex';
}
function closeModal(){modal.style.display='none';cur=null}
async function saveModal(){
  if(!cur)return;
  await post('/web/update',{uuid:cur.uuid,title:m_title.value.trim(),
   note:m_note.value.trim(),date:m_date.value||null,time:m_time.value||null,
   is_someday:m_someday.checked,
   project:m_project.value.trim(),
   tags:m_tags.value.split(',').map(s=>s.trim()).filter(Boolean),
   checklist:editCheck});
  closeModal();
}
async function trashModal(){if(!cur)return;
  await post('/web/update',{uuid:cur.uuid,is_trashed:true});closeModal()}
async function restoreModal(){if(!cur)return;
  await post('/web/update',{uuid:cur.uuid,is_trashed:false});closeModal()}
async function deleteForever(){if(!cur)return;
  await post('/web/update',{uuid:cur.uuid,deleted:true});closeModal()}
load();setInterval(load,60000);
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
        "icons": [{"src": "/icon.png", "sizes": "1024x1024", "type": "image/png"}],
    }


def task_dto(r):
    day = time_str = None
    if r["due_date"]:
        dt = parse_iso(r["due_date"]).astimezone(TZ)
        day = dt.strftime("%Y-%m-%d")
        if r["has_time"]:
            time_str = dt.strftime("%H:%M")
    return {
        "uuid": r["uuid"], "title": r["title"], "note": r["note"],
        "is_completed": bool(r["is_completed"]), "is_someday": bool(r["is_someday"]),
        "is_trashed": bool(r["is_trashed"]), "deleted": bool(r["deleted"]),
        "day": day, "time": time_str,
        "project": r["project"] or None,
        "tags": json.loads(r["tags"] or "[]"),
        "checklist": json.loads(r["checklist"] or "[]"),
    }


@app.get("/web/all")
async def web_all(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rollover_overdue()
    rows = db.execute("SELECT * FROM tasks WHERE deleted=0 ORDER BY updated_at").fetchall()
    projects = sorted({r["project"] for r in rows if r["project"]})
    debt_rows = db.execute(
        "SELECT * FROM debts WHERE is_trashed=0 ORDER BY updated_at DESC").fetchall()
    debts = [{"uuid": d["uuid"], "title": d["title"], "amount": d["amount"],
              "note": d["note"], "is_paid": bool(d["is_paid"]),
              "is_trashed": bool(d["is_trashed"])} for d in debt_rows]
    return {"today": now_local().strftime("%Y-%m-%d"),
            "tasks": [task_dto(r) for r in rows],
            "projects": projects,
            "debts": debts}


@app.post("/web/debt_add")
async def web_debt_add(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    db.execute(
        "INSERT INTO debts (uuid,title,amount,note,updated_at) VALUES (?,?,?,?,?)",
        (str(uuid_lib.uuid4()).upper(), str(body.get("title", "")).strip(),
         float(body.get("amount", 0) or 0), "", time.time()),
    )
    db.commit()
    return {"ok": True}


@app.post("/web/debt_toggle")
async def web_debt_toggle(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    db.execute("UPDATE debts SET is_paid = 1 - is_paid, updated_at=? WHERE uuid=?",
               (time.time(), body.get("uuid", "")))
    db.commit()
    return {"ok": True}


@app.post("/web/debt_delete")
async def web_debt_delete(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    db.execute("UPDATE debts SET is_trashed=1, updated_at=? WHERE uuid=?",
               (time.time(), body.get("uuid", "")))
    db.commit()
    return {"ok": True}


@app.get("/icon.png")
async def icon():
    if os.path.exists("icon.png"):
        with open("icon.png", "rb") as f:
            return Response(content=f.read(), media_type="image/png")
    return Response(status_code=404)


@app.post("/web/add")
async def web_add(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    text = str(body.get("text", "")).strip()
    target = body.get("target", "today")
    title, due_iso, has_time = parse_task_text(text)
    if target == "someday":
        add_task(title, None, False, someday=True)
    elif target == "inbox":
        add_task(title, due_iso, has_time)  # без даты, если не указана в тексте
    else:
        add_task(title, due_iso or today_start_utc_iso(), has_time)
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


@app.post("/web/update")
async def web_update(request: Request):
    if not web_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    uid = body.get("uuid", "")
    row = db.execute("SELECT * FROM tasks WHERE uuid=?", (uid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)

    fields = {}
    if "title" in body:
        fields["title"] = str(body["title"]) or row["title"]
    if "note" in body:
        fields["note"] = str(body["note"])
    if "is_someday" in body:
        fields["is_someday"] = int(bool(body["is_someday"]))
    if "is_trashed" in body:
        fields["is_trashed"] = int(bool(body["is_trashed"]))
    if "is_completed" in body:
        fields["is_completed"] = int(bool(body["is_completed"]))
    if "deleted" in body:
        fields["deleted"] = int(bool(body["deleted"]))
    if "project" in body:
        fields["project"] = str(body.get("project") or "").strip()
    if "tags" in body:
        names = [str(x).strip() for x in (body["tags"] or []) if str(x).strip()]
        # цвета берём из уже известных тегов
        color_map = {}
        for r2 in db.execute("SELECT tags FROM tasks").fetchall():
            for tg in json.loads(r2["tags"] or "[]"):
                color_map[tg.get("name", "").lower()] = tg.get("color", "5E8D5A")
        fields["tags"] = json.dumps(
            [{"name": n, "color": color_map.get(n.lower(), "5E8D5A")} for n in names],
            ensure_ascii=False)
    if "checklist" in body:
        fields["checklist"] = json.dumps(
            [{"text": str(i.get("text", "")), "done": bool(i.get("done"))}
             for i in (body["checklist"] or [])],
            ensure_ascii=False)

    # Дата и время: date "YYYY-MM-DD" | null, time "HH:MM" | null
    if "date" in body:
        date_str = body.get("date")
        time_str = body.get("time")
        if date_str:
            y, mo, d = (int(x) for x in date_str.split("-"))
            dt = datetime(y, mo, d, tzinfo=TZ)
            has_time = False
            if time_str:
                h, mi = (int(x) for x in time_str.split(":"))
                dt = dt.replace(hour=h, minute=mi)
                has_time = True
            fields["due_date"] = to_utc_iso(dt)
            fields["has_time"] = int(has_time)
            fields["is_someday"] = 0
        else:
            fields["due_date"] = None
            fields["has_time"] = 0

    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE tasks SET {sets} WHERE uuid=?", (*fields.values(), uid))
    db.commit()
    return {"ok": True}
