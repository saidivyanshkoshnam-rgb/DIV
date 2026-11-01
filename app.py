import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import threading
import urllib.request
import urllib.error
from urllib.parse import urlencode
import zipfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

from sarvamai import SarvamAI

ROOT = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DIV_DATA_DIR", str(ROOT)))
DATA_FILE = DATA_DIR / "data.json"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
USER_DATA_DIR = DATA_DIR / "user_data"
REQUEST_CONTEXT = threading.local()
ACCOUNT_LOCK = threading.Lock()
SESSION_SECONDS = 7 * 24 * 60 * 60
KEY_FILE = ROOT / ".env"
if KEY_FILE.exists():
    for line in KEY_FILE.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip():
            os.environ.setdefault(name.strip(), value.strip())
if not os.environ.get("SARVAM_API_KEY"):
    raise SystemExit("Add SARVAM_API_KEY to .env before starting the app.")
if os.environ.get("VERCEL") and not os.environ.get("SESSION_SECRET"):
    raise SystemExit("Add SESSION_SECRET to the Vercel environment variables.")

client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
if bool(SUPABASE_URL) != bool(SUPABASE_SERVICE_ROLE_KEY):
    raise SystemExit("Set both SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, or neither.")
DEFAULT_DATA = {"subjects": [], "topics": [], "tasks": [], "daily_log": [], "plan": None}


def supabase_request(table, method="GET", query="", body=None, prefer=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if query:
        url += f"?{query}"
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        error.read()
        raise RuntimeError(f"DIV database request failed ({error.code}).") from None
    return json.loads(raw) if raw else None


def user_data_path(user_id):
    return USER_DATA_DIR / f"{user_id}.json"


def normalize_data(data):
    for key, value in DEFAULT_DATA.items():
        data.setdefault(key, copy.deepcopy(value))
    def ensure_node(item):
        item.setdefault("children", [])
        item.setdefault("done", False)
        for child in item["children"]:
            ensure_node(child)
    for topic in data["topics"]:
        ensure_node(topic)
        if not topic.get("subject"):
            topic["subject"] = "General"
        if topic["subject"] not in data["subjects"]:
            data["subjects"].append(topic["subject"])
    for task in data.get("tasks", []):
        task.setdefault("time", "")
        task.setdefault("done", False)
        task.setdefault("notified", False)
    return data


def load_data():
    user_id = getattr(REQUEST_CONTEXT, "user_id", None)
    if SUPABASE_URL:
        if not user_id:
            return copy.deepcopy(DEFAULT_DATA)
        rows = supabase_request(
            "div_data",
            query=urlencode({"select": "value", "user_id": f"eq.{user_id}", "limit": "1"}),
        )
        if rows:
            return normalize_data(rows[0].get("value") or copy.deepcopy(DEFAULT_DATA))
        return copy.deepcopy(DEFAULT_DATA)
    path = user_data_path(user_id) if user_id else DATA_FILE
    if not path.exists():
        return copy.deepcopy(DEFAULT_DATA)
    try:
        return normalize_data(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_DATA)


def save_data(data):
    user_id = getattr(REQUEST_CONTEXT, "user_id", None)
    if not user_id:
        raise RuntimeError("Sign in before saving data.")
    if SUPABASE_URL:
        supabase_request(
            "div_data",
            method="POST",
            query="on_conflict=user_id",
            body={"user_id": user_id, "value": data},
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    user_data_path(user_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_accounts():
    if SUPABASE_URL:
        rows = supabase_request(
            "div_accounts",
            query="select=username_key,user_id,username,salt,password_hash&limit=1000",
        )
        return {"users": {row["username_key"]: {
            "id": row["user_id"], "username": row["username"], "salt": row["salt"],
            "password_hash": row["password_hash"],
        } for row in rows}}
    if not ACCOUNTS_FILE.exists():
        return {"users": {}}
    try:
        value = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value.get("users"), dict) else {"users": {}}
    except (OSError, json.JSONDecodeError):
        return {"users": {}}


def save_accounts(accounts):
    if SUPABASE_URL:
        raise RuntimeError("Cloud accounts must be created individually.")
    ACCOUNTS_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def create_account(username_key, user):
    if SUPABASE_URL:
        rows = supabase_request(
            "div_accounts",
            method="POST",
            query="on_conflict=username_key",
            body={
                "username_key": username_key,
                "user_id": user["id"],
                "username": user["username"],
                "salt": user["salt"],
                "password_hash": user["password_hash"],
            },
            prefer="resolution=ignore-duplicates,return=representation",
        )
        return bool(rows)
    accounts = load_accounts()
    if username_key in accounts["users"]:
        return False
    accounts["users"][username_key] = user
    save_accounts(accounts)
    return True


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000).hex()


def pending_topic_titles(topics):
    pending = []
    def visit(items, parents=(), parent_done=False):
        for item in items:
            title = str(item.get("title", "")).strip()
            done = parent_done or bool(item.get("done"))
            children = item.get("children", [])
            if done:
                continue
            if children:
                visit(children, parents + ((item.get("subject", "") or title),) if not parents else parents + (title,), done)
            elif title:
                subject = item.get("subject", "")
                pending.append(" > ".join(((subject,) if subject and not parents else ()) + parents + (title,)))
    visit(topics)
    return pending


def clean_extracted_text(text):
    if not text:
        return ""
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(?:p|div|h[1-6]|li|tr|br|td|th)[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\\\\', '\n', text)
    text = re.sub(r'\\(begin|end)\{[^}]*\}', '', text)
    text = re.sub(r'\\(?:text|textbf|textit|mathrm|mathbf|emph|underline)\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\[a-zA-Z]+', ' ', text)
    text = re.sub(r'[\$\(\)\[\]\{\}]', '', text)
    text = re.sub(r'[|&]', '\n', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
    
    cleaned_lines = []
    for line in text.splitlines():
        line = re.sub(r'\s+', ' ', line).strip()
        line = re.sub(r'^[-*•\d.)\s]+', '', line).strip()
        if line and len(line) > 1 and not any(skip in line for skip in ('!important', 'margin:', 'padding:', '{', '}')):
            cleaned_lines.append(line)
    return '\n'.join(cleaned_lines)


def parse_image(filename, mime, encoded):
    raw = base64.b64decode(encoded)
    try:
        job = client.doc_ai.parse(file=[(filename, BytesIO(raw), mime)], language="en-IN", output_format="md")
    except Exception:
        job = client.doc_ai.parse(file=[(filename, BytesIO(raw), mime)], language="en-IN")
        
    terminal = {"completed", "partially_completed", "failed", "rejected"}
    state = "failed"
    for _ in range(40):
        status = client.doc_ai.get_status(job_id=job.job_id)
        state = status.status.lower()
        if state in terminal:
            break
        time.sleep(1.5)
        
    if state not in {"completed", "partially_completed"}:
        raise ValueError("Could not extract text from this image. Try pasting the text directly.")
        
    link = client.doc_ai.get_download_url(job_id=job.job_id)
    with urllib.request.urlopen(link.url, timeout=30) as response:
        archive = zipfile.ZipFile(BytesIO(response.read()))
        
    raw_text = ""
    md = next((name for name in archive.namelist() if name.endswith(".md")), None)
    if md:
        raw_text = archive.read(md).decode("utf-8", errors="ignore")
    else:
        for name in archive.namelist():
            if name.endswith((".txt", ".html")):
                raw_text = archive.read(name).decode("utf-8", errors="ignore")
                break
        if not raw_text:
            for name in archive.namelist():
                if name.endswith(".json") and not name.startswith("manifest"):
                    try:
                        data = json.loads(archive.read(name).decode("utf-8", errors="ignore"))
                        if isinstance(data, dict):
                            raw_text = data.get("text", "") or data.get("content", "")
                            if raw_text: break
                    except Exception:
                        pass

    return clean_extracted_text(raw_text)


def extract_time_and_clean_title(text):
    pattern = r'\b(?:at|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b'
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return text.strip(), ""
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        time_str = f"{hour:02d}:{minute:02d}"
        clean_title = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r'\s+', ' ', clean_title).strip(' ,.-')
        return clean_title or text.strip(), time_str
    return text.strip(), ""

MANIFEST_JSON = '''{
  "name": "DIV Study Assistant",
  "short_name": "DIV",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#f5f6fa",
  "theme_color": "#5847d8",
  "description": "AI-powered study planner with reminders",
  "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}]
}'''

SW_JS = r"""
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

let swTasks = [];
let swToday = '';

// Receive task list from the main page
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SET_TASKS') {
    swTasks = event.data.tasks || [];
    swToday  = event.data.today || '';
  }
});

function checkSwReminders() {
  let now = new Date();
  let hh = String(now.getHours()).padStart(2, '0');
  let mm = String(now.getMinutes()).padStart(2, '0');
  let currentTime = hh + ':' + mm;
  let currentDate = now.toISOString().slice(0, 10);

  swTasks.forEach((task, idx) => {
    if (!task.done && !task.swNotified && task.time && task.day === currentDate) {
      let parts = task.time.split(':').map(Number);
      let taskMins = parts[0] * 60 + parts[1];
      let nowMins  = now.getHours() * 60 + now.getMinutes();
      let diff = nowMins - taskMins;
      if (diff >= 0 && diff <= 5) {
        self.registration.showNotification('\u23f0 DIV Study Reminder', {
          body: task.title + (task.time ? ' \u2014 Scheduled for ' + task.time : '') + '\nOpen DIV to see your AI tip.',
          tag: 'div-sw-' + idx,
          requireInteraction: true,
          vibrate: [300, 100, 300, 100, 300]
        });
        task.swNotified = true;
        self.clients.matchAll().then(clients =>
          clients.forEach(c => c.postMessage({type: 'SW_NOTIFIED', idx: idx}))
        );
      }
    }
  });
}

// Check every 30 seconds — service worker runs even when tab is minimized
setInterval(checkSwReminders, 30000);

// Tap on notification → bring app to foreground
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(clients => {
      if (clients.length > 0) return clients[0].focus();
      return self.clients.openWindow('/');
    })
  );
});
"""

PAGE = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DIV</title>
<link rel="manifest" href="/manifest.json">
<style>
:root{color-scheme:light;--bg:#f5f6fa;--surface:#fff;--text:#202434;--border:#e5e7ef;--muted:#666;--soft:#eef0f8;--soft-text:#282b3b;--line:#eee;--bubble:#f2f2fa;--user-bubble:#e8e5ff;--done:#7d8190}
:root[data-theme="dark"]{color-scheme:dark;--bg:#151821;--surface:#202530;--text:#eceef6;--border:#394150;--muted:#aab0bd;--soft:#343a4a;--soft-text:#f0f1f7;--line:#353b47;--bubble:#2b3040;--user-bubble:#3b3464;--done:#a0a6b3}
body{font:16px system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0}main{max-width:980px;margin:32px auto;padding:16px}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:16px}.page-header{display:flex;align-items:center;justify-content:space-between;gap:12px}.page-header h1{margin-bottom:4px}h1{margin-bottom:4px}h2{font-size:19px;margin-top:0}button{background:#5847d8;color:white;border:0;border-radius:8px;padding:10px 14px;cursor:pointer;margin:4px}button.secondary{background:var(--soft);color:var(--soft-text)}input,textarea,select{font:inherit;padding:10px;border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:8px;width:100%;box-sizing:border-box;margin:6px 0}textarea{min-height:76px}.row{display:flex;gap:8px;align-items:center}.row input{flex:1}.item{padding:10px;border-bottom:1px solid var(--line)}.topic-node{padding:8px 4px;border-bottom:1px solid var(--line)}.topic-children{margin-left:20px;border-left:2px solid var(--border);padding-left:10px}.topic-actions button{font-size:12px;padding:6px 9px}.done{text-decoration:line-through;color:var(--done)}.msg{padding:9px 12px;border-radius:10px;background:var(--bubble);margin:8px 0;white-space:pre-wrap}.user{background:var(--user-bubble)}.hint{color:var(--muted);font-size:14px}.auth-card{max-width:440px;margin:48px auto}#authMessage{min-height:22px}#messages{max-height:360px;overflow:auto}@media(max-width:700px){.grid{grid-template-columns:1fr}.page-header{align-items:flex-start;flex-direction:column}}
.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.55);display:flex;align-items:center;justify-content:center;z-index:9999}
.modal-box{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px;width:90%;max-width:420px;box-shadow:0 12px 36px rgba(0,0,0,0.3)}
</style><main><header class="page-header"><h1>DIV</h1><button class="secondary" id="themeToggle" onclick="toggleTheme()">Switch to dark mode</button></header><section id="loginPanel" class="card auth-card" hidden><p class="hint">Sign in to open DIV, or create a new account.</p><label for="username">Username</label><input id="username" autocomplete="username" placeholder="3–32 letters, numbers, or underscores"><label for="password">Password</label><input id="password" type="password" autocomplete="current-password" placeholder="At least 8 characters"><div class="row"><button onclick="authenticate('login')">Sign in</button><button class="secondary" onclick="authenticate('register')">Create account</button></div><p id="authMessage" class="hint"></p><p class="hint">Keep your password safe; password recovery is not set up yet.</p></section><div id="dashboard" hidden><div class="row" style="justify-content:flex-end"><span id="currentUsername" class="hint"></span><button class="secondary" onclick="logout()">Sign out</button></div><p class="hint">Welcome to DIV. Add topics and tasks with timelines, then ask DIV for help.</p>

<div id="reminderBanner" style="display:none;background:#5847d8;color:white;padding:12px 18px;border-radius:12px;margin-bottom:16px;box-shadow:0 6px 18px rgba(88,71,216,0.3);justify-content:space-between;align-items:center" class="row">
  <span id="reminderBannerText" style="font-size:15px">⏰ <b>Reminder:</b></span>
  <button class="secondary" style="margin:0;padding:5px 12px;font-size:12px;background:rgba(255,255,255,0.2);color:white;border:1px solid rgba(255,255,255,0.4)" onclick="el('reminderBanner').style.display='none'">Dismiss</button>
</div>

<script>window.MathJax={tex:{inlineMath:[["\\(","\\)"],["$","$" ]],displayMath:[["\\[","\\]"],["$$","$$"]]},options:{skipHtmlTags:["script","noscript","style","textarea","pre","code"]}};</script><script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<div class="grid"><section>
<div class="card"><h2>Chat with DIV</h2><div id="messages"></div><textarea id="message" placeholder="Ask DIV anything, or try: remind me at 16:30 to revise"></textarea><div class="row"><button onclick="send()">Send</button><button class="secondary" onclick="record()" id="record">🎙 Record voice</button><label class="hint"><input id="speak" type="checkbox" style="width:auto"> Speak replies</label></div><input id="audio" type="file" accept="audio/*" hidden><audio id="player" controls hidden></audio></div>
<div class="card"><h2>Add topics or tasks from an image</h2><p class="hint">Upload a syllabus photo, or paste your topics directly in the box.</p><div class="row"><input id="newSubject" placeholder="New subject, e.g. Mathematics"><button onclick="addSubject()">Add subject</button></div><div class="row"><input id="topic" placeholder="Chapter or topic name"><button onclick="addTopic()">Add chapter</button></div><input id="image" type="file" accept="image/png,image/jpeg,.pdf"><button class="secondary" onclick="readImage()">Read image/PDF</button><div id="preview"></div></div>
</section><aside>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <h2 style="margin:0">Today's Timeline & Tasks</h2>
    <button id="notifBtn" class="secondary" style="font-size:12px;padding:4px 8px;margin:0" onclick="requestNotifPermission()">🔔 Enable Reminders</button>
  </div>
  <div class="row" style="margin-bottom:8px;flex-wrap:wrap">
    <input id="task" placeholder="Task description" style="flex:2;min-width:160px">
    <input id="taskTime" type="time" title="Set timeline reminder time" style="max-width:125px">
    <button onclick="addTask()">Add</button>
  </div>
  <div id="tasks"></div>
</div>
<div class="card"><h2>Topic checklist</h2><label for="subjectSelect">Subject</label><select id="subjectSelect" onchange="selectSubject()"></select><p id="subjectCaption" class="hint"></p><div id="topics"></div><p id="progress" class="hint"></p></div>
<div class="card"><h2>Make a plan with DIV</h2><p class="hint">Enter topics on separate lines. Leave blank to plan from your unchecked topics.</p><textarea id="planTopics" placeholder="Paste the topics you need to cover"></textarea><label for="planWeak" class="hint" style="display:block;margin-top:6px;font-weight:600">Topics you are weak in / need extra focus on (optional):</label><textarea id="planWeak" placeholder="e.g. Inverses, Eigenvalues, or 'I struggle with word problems'" style="min-height:56px"></textarea><label for="planDays">How many days do you have?</label><input id="planDays" type="number" min="1" max="365" value="7"><button onclick="makePlan()">Create my plan</button><div id="planOutput"></div></div>
<div class="card"><h2>Log today's work</h2><textarea id="work" placeholder="What did you work on today?"></textarea><button onclick="logWork()">Save daily log</button><div id="logs"></div></div>
</aside></div></div></main>

<div id="actionModal" class="modal-overlay" style="display:none">
  <div class="modal-box">
    <h3 id="modalHeading" style="margin-top:0">Edit</h3>
    <input id="modalInputValue" style="margin-bottom:14px" autofocus>
    <div class="row" style="justify-content:flex-end">
      <button class="secondary" type="button" onclick="closeInputModal()">Cancel</button>
      <button type="button" onclick="confirmInputModal()">Save</button>
    </div>
  </div>
</div>

<script>
let currentData = {subjects:[], topics:[], tasks:[], daily_log:[]};
function applyTheme(theme){document.documentElement.dataset.theme=theme;localStorage.setItem('divTheme',theme);el('themeToggle').textContent=theme==='dark'?'Switch to light mode':'Switch to dark mode'}
function toggleTheme(){applyTheme(document.documentElement.dataset.theme==='dark'?'light':'dark')}
async function authenticate(action){let username=el('username').value.trim(),password=el('password').value;try{let result=await api('/api/'+action,{username,password});el('authMessage').textContent='';el('password').value='';el('currentUsername').textContent=result.username;el('loginPanel').hidden=true;el('dashboard').hidden=false;refresh()}catch(e){el('authMessage').textContent=e.message}}
async function startApp(){try{let user=await api('/api/me');el('currentUsername').textContent=user.username;el('loginPanel').hidden=true;el('dashboard').hidden=false;updateNotifButton();refresh()}catch(e){el('dashboard').hidden=true;el('loginPanel').hidden=false}}
async function logout(){await api('/api/logout',{});el('dashboard').hidden=true;el('loginPanel').hidden=false;el('password').value='';el('authMessage').textContent='You are signed out.'}
async function api(path,body){let r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||'Something went wrong');return j}
function el(id){return document.getElementById(id)}
function addMsg(text,who='assistant'){let d=document.createElement('div');d.className='msg '+(who==='user'?'user':'');d.textContent=text;el('messages').appendChild(d);if(who!=='user'&&window.MathJax?.typesetPromise)window.MathJax.typesetPromise([d]);el('messages').scrollTop=el('messages').scrollHeight}

function playChime(){
  try{
    let ctx=new (window.AudioContext||window.webkitAudioContext)();
    let osc=ctx.createOscillator();
    let gain=ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type='sine';
    osc.frequency.setValueAtTime(587.33,ctx.currentTime);
    osc.frequency.setValueAtTime(880,ctx.currentTime+0.12);
    gain.gain.setValueAtTime(0.3,ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.6);
    osc.start();
    osc.stop(ctx.currentTime+0.6);
  }catch(e){}
}

function updateNotifButton(){
  let btn=el('notifBtn');
  if(!btn) return;
  if(!('Notification' in window)){
    btn.textContent='🔔 No browser alerts';
    btn.disabled=true;
  } else if(Notification.permission==='granted'){
    btn.textContent='🔔 Reminders active';
    btn.style.borderColor='#4caf50';
    btn.style.color='#4caf50';
  } else if(Notification.permission==='denied'){
    btn.textContent='🔕 Reminders blocked';
  } else {
    btn.textContent='🔔 Enable Reminders';
  }
}

async function requestNotifPermission(){
  if(!('Notification' in window)){
    alert('Browser notifications are not supported in this browser, but in-app banners and audio chimes will still alert you!');
    return;
  }
  let p=await Notification.requestPermission();
  updateNotifButton();
  if(p==='granted'){
    playChime();
    new Notification('DIV Reminders Active', {body: 'You will receive reminder notifications at scheduled times!'});
  }
}

function timeDiffMinutes(t1, t2){
  if(!t1 || !t2) return 999;
  let [h1, m1]=t1.split(':').map(Number);
  let [h2, m2]=t2.split(':').map(Number);
  return (h2*60 + m2) - (h1*60 + m1);
}

function triggerReminder(task, origIdx){
  task.notified=true;
  api('/api/task/notified',{index:origIdx}).catch(()=>{});
  playChime();
  if('Notification' in window && Notification.permission==='granted'){
    try{
      new Notification('⏰ DIV Study Reminder', {
        body: task.title + (task.time ? ` (Scheduled for ${task.time})` : ''),
        tag: 'div-task-'+origIdx
      });
    }catch(e){}
  }
  let banner=el('reminderBanner');
  if(banner){
    el('reminderBannerText').innerHTML=`⏰ <b>Timeline Reminder (${esc(task.time||'Now')}):</b> ${esc(task.title)}`;
    banner.style.display='flex';
  }
  if('speechSynthesis' in window && el('speak')?.checked){
    try{
      let utt=new SpeechSynthesisUtterance(`Reminder: Time to work on ${task.title}`);
      utt.lang='en-IN';
      window.speechSynthesis.speak(utt);
    }catch(e){}
  }
  refresh();
}

function checkReminders(){
  if(!currentData || !currentData.tasks) return;
  let now=new Date();
  let currentH=String(now.getHours()).padStart(2,'0');
  let currentM=String(now.getMinutes()).padStart(2,'0');
  let currentTimeStr=`${currentH}:${currentM}`;
  let todayStr=currentData.today || now.toISOString().slice(0,10);

  currentData.tasks.forEach((task, idx) => {
    if(!task.done && !task.notified && task.time && task.day===todayStr){
      let diff=timeDiffMinutes(task.time, currentTimeStr);
      if(diff>=0 && diff<=15){
        triggerReminder(task, idx);
      } else if(diff>15){
        task.notified=true;
        api('/api/task/notified',{index:idx}).catch(()=>{});
      }
    }
  });
}
setInterval(checkReminders, 10000);

let modalCallback = null;
function openInputModal(title, val, onConfirm){
  el('modalHeading').textContent = title;
  el('modalInputValue').value = val || '';
  modalCallback = onConfirm;
  el('actionModal').style.display = 'flex';
  setTimeout(() => el('modalInputValue').focus(), 50);
}
function closeInputModal(){
  el('actionModal').style.display = 'none';
  modalCallback = null;
}
async function confirmInputModal(){
  let val = el('modalInputValue').value.trim();
  if(!val) return;
  if(modalCallback) await modalCallback(val);
  closeInputModal();
}
el('modalInputValue').addEventListener('keydown', e => {
  if(e.key === 'Enter') confirmInputModal();
  if(e.key === 'Escape') closeInputModal();
});

function topicNode(t,path,ancestorDone=false){
  let done=ancestorDone||!!t.done,children=t.children||[];
  let pathJson=JSON.stringify(path);
  return `<div class="topic-node">
    <label><input style="width:auto" type="checkbox" ${done?'checked':''} onchange="toggleTopic(${pathJson},this.checked)"> <span class="${done?'done':''}">${esc(t.title)}</span></label> 
    <small class="hint">${done?'Completed':'To do'}</small>
    <div class="topic-actions">
      ${path.length<3?`<button class="secondary" type="button" onclick="addChild(${pathJson})">${path.length===1?'Add subtopic':'Add sub-sub-topic'}</button>`:''}
      <button class="secondary" type="button" onclick="editTopic(${pathJson})">Edit</button>
      <button class="secondary" type="button" onclick="removeTopic(${pathJson})">Remove</button>
    </div>
    ${children.length?`<div class="topic-children">${children.map((c,i)=>topicNode(c,[...path,i],done)).join('')}</div>`:''}
  </div>`;
}

function topicCounts(topics,ancestorDone=false){let total=0,done=0;for(const t of topics){total++;if(ancestorDone||t.done)done++;const c=topicCounts(t.children||[],ancestorDone||t.done);total+=c.total;done+=c.done}return{total,done}}

async function refresh(){
  let d=await api('/api/state');
  currentData=d;
  let selector=el('subjectSelect'),previous=localStorage.getItem('activeSubject')||'';
  selector.innerHTML='<option value="__all">All subjects</option>'+d.subjects.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');
  selector.value=d.subjects.includes(previous)?previous:(d.subjects[0]||'__all');
  localStorage.setItem('activeSubject',selector.value);
  let active=selector.value,visible=d.topics.map((t,i)=>({t,i})).filter(x=>active==='__all'||x.t.subject===active);
  el('subjectCaption').textContent=active==='__all'?'All subjects':active;
  el('topics').innerHTML=visible.map(x=>topicNode(x.t,[x.i])).join('')||'<p class="hint">No chapters for this subject yet. Add a chapter above.</p>';
  
  let todayTasks=d.tasks.map((t,origIdx)=>({...t,origIdx})).filter(t=>t.day===d.today);
  todayTasks.sort((a,b)=>{
    if(a.time && b.time) return a.time.localeCompare(b.time);
    if(a.time) return -1;
    if(b.time) return 1;
    return a.origIdx - b.origIdx;
  });

  let hasCompleted=todayTasks.some(t=>t.done);
  el('tasks').innerHTML=todayTasks.map(t=>{
    let timeBadge=t.time?`<span style="background:var(--soft);color:var(--soft-text);padding:2px 7px;border-radius:6px;font-size:12px;margin-right:6px;font-weight:600">⏰ ${esc(t.time)}</span>`:'';
    let notifiedBadge=(t.notified && !t.done)?`<span title="Reminder triggered" style="font-size:11px;color:#e67e22;margin-right:4px">🔔</span>`:'';
    return `<div class="item row" style="justify-content:space-between">
      <label style="display:flex;align-items:center;flex-wrap:wrap">
        <input style="width:auto;margin-right:8px" type="checkbox" ${t.done?'checked':''} onchange="toggleTask(${t.origIdx},this.checked)">
        ${timeBadge}${notifiedBadge}<span class="${t.done?'done':''}">${esc(t.title)}</span>
      </label>
      <button class="secondary" style="font-size:12px;padding:3px 7px;margin:0" onclick="removeTask(${t.origIdx})" title="Delete task">✕</button>
    </div>`;
  }).join('')+(hasCompleted?`<button class="secondary" style="font-size:13px;width:100%;margin-top:8px" onclick="clearCompletedTasks()">Clear completed tasks</button>`:'')||'<p class="hint">No tasks scheduled for today yet.</p>';
  
  let counts=topicCounts(visible.map(x=>x.t));
  el('progress').textContent=`${counts.done} of ${counts.total} items completed`;
  el('logs').innerHTML=d.daily_log.slice(-3).reverse().map(x=>`<div class="item"><b>${esc(x.day)}</b> ${esc(x.text)}</div>`).join('');
  el('planOutput').innerHTML=d.plan?`<p class="hint">${d.plan.days}-day plan · ${d.plan.topics.length} topics${d.plan.weak?' · Focus: <b>'+esc(d.plan.weak)+'</b>':''}</p><div class="item" style="white-space:pre-wrap">${esc(d.plan.text)}</div>`:'<p class="hint">Your saved plan will appear here.</p>';
  syncTasksToSW(d);

}

function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function selectSubject(){localStorage.setItem('activeSubject',el('subjectSelect').value);refresh()}
async function addSubject(){let name=el('newSubject').value.trim();if(!name)return;let r=await api('/api/subject',{name});el('newSubject').value='';localStorage.setItem('activeSubject',r.name);refresh()}
async function addTopic(){
  let title=el('topic').value.trim();
  let subject=el('subjectSelect').value;
  if(!title) return;
  if(subject==='__all' || !subject){
    subject = (currentData.subjects && currentData.subjects[0]) ? currentData.subjects[0] : 'General';
  }
  await api('/api/topic',{title,subject});
  el('topic').value='';
  refresh();
}
async function addTask(){
  let title=el('task').value.trim();
  if(!title) return;
  let timeVal=el('taskTime')?el('taskTime').value:'';
  await api('/api/task',{title, time:timeVal});
  el('task').value='';
  if(el('taskTime')) el('taskTime').value='';
  refresh();
  if('Notification' in window && Notification.permission==='default'){
    Notification.requestPermission().then(updateNotifButton);
  }
}
async function removeTask(index){await api('/api/task/remove',{index});refresh()}
async function clearCompletedTasks(){await api('/api/task/clear-completed',{});refresh()}

function addChild(path){
  openInputModal(path.length===1?'Add Subtopic':'Add Sub-sub-topic', '', async (title) => {
    try {
      await api('/api/topic/child', {path, title});
      refresh();
    } catch(e) {
      alert('Could not add subtopic: ' + e.message);
    }
  });
}

function editTopic(path){
  let current='';
  try{
    let item=currentData.topics[path[0]];
    for(let i=1;i<path.length;i++)item=item.children[path[i]];
    current=item.title||'';
  }catch(e){}
  openInputModal('Edit Topic Name', current, async (title) => {
    try {
      await api('/api/topic/edit', {path, title});
      refresh();
    } catch(e) {
      alert('Could not edit topic: ' + e.message);
    }
  });
}

async function removeTopic(path){
  let title='this item';
  try{
    let item=currentData.topics[path[0]];
    for(let i=1;i<path.length;i++)item=item.children[path[i]];
    title=`"${item.title}"`;
  }catch(e){}
  if(!confirm(`Remove ${title} and all its subtopics?`))return;
  try{
    await api('/api/topic/remove',{path});
    refresh();
  }catch(e){
    alert('Could not remove: '+e.message);
  }
}

async function toggleTopic(path,done){await api('/api/topic/done',{path,done});refresh()}
async function toggleTask(index,done){await api('/api/task/done',{index,done});refresh()}
async function logWork(){let text=el('work').value.trim();if(!text)return;await api('/api/log',{text});el('work').value='';refresh()}
async function makePlan(){
  let days=Number(el('planDays').value);
  if(!Number.isInteger(days)||days<1||days>365){addMsg('Enter a number of days from 1 to 365.');return}
  let topics=el('planTopics').value.split(/\n+/).map(x=>x.trim()).filter(Boolean);
  let weak=el('planWeak')?el('planWeak').value.trim():'';
  el('planOutput').textContent='Building your tailored plan with DIV…';
  try{
    let p=await api('/api/plan',{days,topics,weak});
    el('planOutput').innerHTML=`<p class="hint">${p.days}-day plan · ${p.topics.length} topics${p.weak?' · Special focus on: <b>'+esc(p.weak)+'</b>':''}</p><div class="item" style="white-space:pre-wrap">${esc(p.text)}</div>`;
    refresh();
  }catch(e){
    el('planOutput').textContent=e.message;
  }
}
async function send(){let message=el('message').value.trim();if(!message)return;el('message').value='';addMsg(message,'user');try{let r=await api('/api/chat',{message,subject:el('subjectSelect').value});addMsg(r.reply);if(el('speak').checked){let a=await api('/api/speak',{text:r.reply.slice(0,2400)});el('player').src='data:audio/wav;base64,'+a.audio;el('player').hidden=false;el('player').play()};refresh()}catch(e){addMsg(e.message)}}
el('message').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}})
let recorder, chunks=[];async function record(){if(recorder&&recorder.state==='recording'){recorder.stop();el('record').textContent='🎙 Record voice';return}try{let stream=await navigator.mediaDevices.getUserMedia({audio:true});recorder=new MediaRecorder(stream);chunks=[];recorder.ondataavailable=e=>chunks.push(e.data);recorder.onstop=async()=>{let blob=new Blob(chunks,{type:recorder.mimeType});let reader=new FileReader();reader.onload=async()=>{let base64=reader.result.split(',')[1];try{let r=await api('/api/voice',{mime:blob.type,data:base64});el('message').value=r.transcript;send()}catch(e){addMsg(e.message)}};reader.readAsDataURL(blob);stream.getTracks().forEach(t=>t.stop())};recorder.start();el('record').textContent='Stop recording'}catch(e){addMsg('Allow microphone access to use voice input.')}}

function fileToBase64(file){return new Promise((resolve,reject)=>{let r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file)})}

function cleanHtmlAndLatex(t){
  if(!t) return '';
  t = t.replace(/<style[\s\S]*?<\/style>/gi, '');
  t = t.replace(/<script[\s\S]*?<\/script>/gi, '');
  t = t.replace(/<\/?(?:p|div|h[1-6]|li|tr|br|td|th)[^>]*>/gi, '\n');
  t = t.replace(/<[^>]+>/g, ' ');
  t = t.replace(/\\\\/g, '\n')
       .replace(/\\(?:text|textbf|textit|mathrm|mathbf|emph|underline)\{([^}]*)\}/g, '$1')
       .replace(/\\(begin|end)\{[^}]*\}/g, '')
       .replace(/\\[a-zA-Z]+/g, ' ')
       .replace(/[\$\(\)\[\]\{\}]/g, '');
  t = t.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  return t.split('\n')
          .map(l => l.replace(/^[-*•\d.)\s]+/, '').trim())
          .filter(l => l.length > 1 && !l.includes('{') && !l.includes('!important') && !l.includes('margin:'))
          .join('\n');
}

async function readImage(){
  let f=el('image').files[0];
  if(!f){alert('Please select an image or PDF first.');return}
  el('preview').innerHTML='<p class="hint">Reading image with Sarvam AI… (takes ~10–15s)</p>';
  try{
    let base64=await fileToBase64(f);
    let r=await api('/api/image',{filename:f.name,mime:f.type||'image/png',data:base64});
    let text=cleanHtmlAndLatex(r.text||'');
    el('preview').innerHTML='<p>Review or edit your extracted topics below:</p><textarea id="parsed" style="min-height:140px">'+esc(text)+'</textarea><div class="row"><button class="secondary" type="button" onclick="el(\'parsed\').value=cleanHtmlAndLatex(el(\'parsed\').value)">Clean up formatting</button></div><select id="importType"><option value="topics">Topics</option><option value="tasks">Today’s tasks</option></select><button onclick="saveParsed()">Save to list</button>';
  }catch(e){
    el('preview').innerHTML='<p class="hint" style="color:#d9534f">Scan failed ('+esc(e.message)+'). You can paste your topics directly below:</p><textarea id="parsed" style="min-height:140px" placeholder="Art and Culture&#10;Indian History&#10;World History"></textarea><select id="importType"><option value="topics">Topics</option><option value="tasks">Today’s tasks</option></select><button onclick="saveParsed()">Save to list</button>';
  }
}

async function saveParsed(){let kind=el('importType').value,subject=el('subjectSelect').value;if(kind==='topics'&&subject==='__all'){alert('Choose a specific subject above first.');return}let lines=el('parsed').value.split(/\n+/).map(x=>x.replace(/^\s*(?:[-*•]|\d+[.)])\s*/, '').trim()).filter(x=>x.length>1&&!/^#+\s/.test(x));await api('/api/import',{items:lines,kind,subject});el('preview').innerHTML='<p class="hint" style="color:#4caf50">✓ Saved successfully! Look at your checklist below.</p>';refresh()}

applyTheme(localStorage.getItem('divTheme')||'light');
startApp();

if('serviceWorker' in navigator){
  navigator.serviceWorker.register('/sw.js').then(reg => {
    console.log('DIV SW registered');
  }).catch(err => console.warn('SW registration failed:', err));

  navigator.serviceWorker.addEventListener('message', async event => {
    if(event.data && event.data.type === 'SW_NOTIFIED'){
      let idx = event.data.idx;
      if(currentData && currentData.tasks && currentData.tasks[idx]){
        let task = currentData.tasks[idx];
        await api('/api/task/notified', {index: idx}).catch(()=>{});
        playChime();
        let aiMsg = '';
        try{
          let r = await api('/api/reminder-msg', {title: task.title, time: task.time||''});
          aiMsg = r.message || '';
        }catch(e){}
        let banner = el('reminderBanner');
        if(banner){
          el('reminderBannerText').innerHTML =
            `⏰ <b>Reminder (${esc(task.time||'Now')}):</b> ${esc(task.title)}`
            + (aiMsg ? `<br><span style="font-size:13px;opacity:0.92;margin-top:4px;display:block">${esc(aiMsg)}</span>` : '');
          banner.style.display = 'flex';
        }
        refresh();
      }
    }
  });
}

function syncTasksToSW(data){
  if('serviceWorker' in navigator && navigator.serviceWorker.controller){
    navigator.serviceWorker.controller.postMessage({
      type: 'SET_TASKS',
      tasks: (data.tasks||[]).map(t => ({...t, swNotified: t.notified||false})),
      today: data.today || new Date().toISOString().slice(0,10)
    });
  }
}
</script></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200, extra_headers=None):
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def current_user(self):
        cookie = self.headers.get("Cookie", "")
        token = next((part.strip().partition("=")[2] for part in cookie.split(";") if part.strip().startswith("div_session=")), "")
        try:
            encoded, signature = token.split(".", 1)
            expected = base64.urlsafe_b64encode(
                hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
            ).decode().rstrip("=")
            if not hmac.compare_digest(signature, expected):
                return None
            padded = encoded + "=" * (-len(encoded) % 4)
            session = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if session["expires"] <= time.time():
                return None
            return {"id": session["id"], "username": session["username"]}
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def set_session(self, user):
        payload = json.dumps({**user, "expires": time.time() + SESSION_SECONDS}, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
        token = encoded + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")
        is_https = os.environ.get("PUBLIC_HTTPS") == "1" or self.headers.get("x-forwarded-proto") == "https"
        secure = "; Secure" if is_https else ""
        return {"Set-Cookie": f"div_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_SECONDS}{secure}"}

    def handle_login(self, request):
        username = str(request.get("username", "")).strip()
        password = str(request.get("password", ""))
        accounts = load_accounts()
        user = accounts["users"].get(username.casefold())
        if not user or not hmac.compare_digest(password_hash(password, bytes.fromhex(user["salt"])), user["password_hash"]):
            self.send_json({"error": "Username or password is incorrect."}, 401)
            return
        headers = self.set_session({"id": user["id"], "username": user["username"]})
        self.send_json({"username": user["username"]}, extra_headers=headers)

    def handle_register(self, request):
        username = str(request.get("username", "")).strip()
        password = str(request.get("password", ""))
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
            raise ValueError("Username must be 3–32 letters, numbers, or underscores.")
        if len(password) < 8:
            raise ValueError("Choose a password with at least 8 characters.")
        key = username.casefold()
        with ACCOUNT_LOCK:
            accounts = load_accounts()
            if key in accounts["users"]:
                self.send_json({"error": "That username is already taken."}, 409)
                return
            user = {"id": uuid.uuid4().hex, "username": username, "salt": secrets.token_bytes(16).hex()}
            user["password_hash"] = password_hash(password, bytes.fromhex(user["salt"]))
            if not create_account(key, user):
                self.send_json({"error": "That username is already taken."}, 409)
                return
        if not SUPABASE_URL:
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        headers = self.set_session({"id": user["id"], "username": username})
        self.send_json({"username": username}, extra_headers=headers)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json({"ok": True})
        elif self.path == "/":
            payload = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/api/me":
            user = self.current_user()
            if user:
                self.send_json(user)
            else:
                self.send_json({"error": "Sign in to continue."}, 401)
        elif self.path == "/api/state":
            user = self.current_user()
            if not user:
                self.send_json({"error": "Sign in to continue."}, 401)
                return
            REQUEST_CONTEXT.user_id = user["id"]
            data = load_data()
            self.send_json({**data, "today": time.strftime("%Y-%m-%d")})
        elif self.path == "/sw.js":
            payload = SW_JS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/manifest.json":
            payload = MANIFEST_JSON.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            if self.path == "/api/login":
                self.handle_login(request)
                return
            if self.path == "/api/register":
                self.handle_register(request)
                return
            if self.path == "/api/logout":
                self.send_json({"ok": True}, extra_headers={"Set-Cookie": "div_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"})
                return
            user = self.current_user()
            if not user:
                self.send_json({"error": "Sign in to continue."}, 401)
                return
            REQUEST_CONTEXT.user_id = user["id"]
            data = load_data()
            path = self.path
            def topic_parent(indexes):
                if not isinstance(indexes, list) or not 1 <= len(indexes) <= 3 or any(type(i) is not int or i < 0 for i in indexes):
                    raise ValueError("That item was not found.")
                items = data["topics"]
                try:
                    for index in indexes[:-1]:
                        items = items[index].setdefault("children", [])
                    if indexes[-1] >= len(items):
                        raise IndexError
                except (IndexError, TypeError):
                    raise ValueError("That item was not found.")
                return items, indexes[-1]
            if path == "/api/subject":
                name = str(request.get("name", "")).strip()
                if not name:
                    raise ValueError("Enter a subject name.")
                existing = next((subject for subject in data["subjects"] if subject.casefold() == name.casefold()), None)
                name = existing or name
                if not existing:
                    data["subjects"].append(name)
                save_data(data); result = {"name": name, "subjects": data["subjects"]}
            elif path == "/api/topic":
                subject = str(request.get("subject", "General")).strip()
                if not subject or subject == "__all":
                    subject = "General"
                if subject not in data["subjects"]:
                    data["subjects"].append(subject)
                data["topics"].append({"title": request["title"], "subject": subject, "done": False, "children": []})
                save_data(data); result = {"ok": True}
            elif path == "/api/topic/child":
                indexes = request.get("path", [])
                if not indexes or len(indexes) > 2 or not isinstance(indexes, list):
                    raise ValueError("Choose a chapter or subtopic first.")
                items = data["topics"]
                try:
                    for index in indexes:
                        items = items[index].setdefault("children", [])
                except (IndexError, TypeError):
                    raise ValueError("That item was not found.")
                title = str(request.get("title", "")).strip()
                if not title:
                    raise ValueError("Enter a name for this item.")
                items.append({"title": title, "done": False, "children": []})
                save_data(data); result = {"ok": True}
            elif path == "/api/topic/edit":
                items, index = topic_parent(request.get("path"))
                title = str(request.get("title", "")).strip()
                if not title:
                    raise ValueError("Enter a name for this item.")
                items[index]["title"] = title
                save_data(data); result = {"ok": True}
            elif path == "/api/topic/remove":
                items, index = topic_parent(request.get("path"))
                del items[index]
                save_data(data); result = {"ok": True}
            elif path == "/api/task":
                title = str(request.get("title", "")).strip()
                time_val = str(request.get("time", "")).strip()
                if not time_val:
                    title, time_val = extract_time_and_clean_title(title)
                data["tasks"].append({
                    "title": title,
                    "time": time_val,
                    "day": time.strftime("%Y-%m-%d"),
                    "done": False,
                    "notified": False
                })
                save_data(data); result = {"ok": True}
            elif path == "/api/task/remove":
                index = request.get("index")
                if isinstance(index, int) and 0 <= index < len(data["tasks"]):
                    del data["tasks"][index]
                save_data(data); result = {"ok": True}
            elif path == "/api/task/clear-completed":
                data["tasks"] = [t for t in data["tasks"] if not t.get("done")]
                save_data(data); result = {"ok": True}
            elif path == "/api/task/notified":
                index = request.get("index")
                if isinstance(index, int) and 0 <= index < len(data["tasks"]):
                    data["tasks"][index]["notified"] = True
                save_data(data); result = {"ok": True}
            elif path == "/api/topic/done":
                indexes = request.get("path", [request.get("index")])
                if not indexes or indexes[0] is None:
                    raise ValueError("That item was not found.")
                items = data["topics"]
                try:
                    for index in indexes[:-1]:
                        items = items[index].setdefault("children", [])
                    selected = items[indexes[-1]]
                except (IndexError, TypeError):
                    raise ValueError("That item was not found.")
                def set_branch(item, done):
                    item["done"] = done
                    for child in item.get("children", []):
                        set_branch(child, done)
                set_branch(selected, bool(request["done"]))
                save_data(data); result = {"ok": True}
            elif path == "/api/task/done":
                data["tasks"][request["index"]]["done"] = request["done"]
                save_data(data); result = {"ok": True}
            elif path == "/api/log":
                data["daily_log"].append({"day": time.strftime("%Y-%m-%d"), "text": request["text"]})
                save_data(data); result = {"ok": True}
            elif path == "/api/import":
                kind = request.get("kind", "topics")
                target = "tasks" if kind == "tasks" else "topics"
                subject = str(request.get("subject", "")).strip()
                if target == "topics" and (not subject or subject == "__all"):
                    subject = "General"
                if target == "topics" and subject not in data["subjects"]:
                    data["subjects"].append(subject)
                for title in request.get("items", []):
                    if title:
                        item = {"title": title, "done": False, "children": []}
                        if target == "topics": item["subject"] = subject
                        if target == "tasks": item["day"] = time.strftime("%Y-%m-%d")
                        data[target].append(item)
                save_data(data); result = {"ok": True}
            elif path == "/api/plan":
                days = request.get("days")
                if not isinstance(days, int) or days < 1 or days > 365:
                    raise ValueError("Choose a number of days from 1 to 365.")
                topics = [str(item).strip() for item in request.get("topics", []) if str(item).strip()]
                weak = str(request.get("weak", "")).strip()
                if not topics:
                    topics = pending_topic_titles(data["topics"])
                if not topics:
                    raise ValueError("Paste your topics or add chapters first.")
                topic_list = "\n".join(f"- {topic}" for topic in topics)
                
                weak_prompt = ""
                if weak:
                    weak_prompt = (
                        f"\nSTUDENT'S SPECIFIC WEAK AREAS:\n"
                        f"The student explicitly flagged: \"{weak}\".\n"
                        f"- YOU MUST prioritize these weak areas: allocate extra time/days, breakdown core fundamentals step-by-step, assign double practice problems, and schedule repeated spaced review specifically for them.\n"
                        f"- Explicitly indicate in the daily focus/outcome that special remedial attention is being given to these weak areas.\n"
                    )
                
                system_content = (
                    "You are DIV, an expert academic planner and tutor. Create a structured, rigorous day-by-day study schedule covering the topics provided.\n"
                    "CRITICAL RULES:\n"
                    "1. Phase titles and Day titles must ACCURATELY match the actual subject/topics taught on those days. E.g. If teaching Matrices, the Phase title MUST be 'Matrices' or 'Linear Algebra'. NEVER use the word 'Integration' unless the topic is actual Calculus Integration.\n"
                    "2. Do NOT use the word 'integration' as a generic buzzword for combining or reviewing topics.\n"
                    "3. Do NOT output orientation summaries, meta-rules, or prompt disclaimers. Begin immediately with '### Phase 1: [Accurate Phase Name] (Days 1–X)'.\n"
                    "4. For each day, include:\n"
                    "   **Day X — Focus:** [Topic]\n"
                    "   **Timeline:** [Suggested study blocks, e.g. 09:00-11:00 Foundations, 15:00-16:30 Practice, 19:30-20:00 Recall]\n"
                    "   **Outcome:** [Specific learning goal]\n"
                    "   **Practice:** [Concrete exercises or problem counts]\n"
                    "   **Review:** [Spaced recall / key points to remember]\n"
                    "5. Respect logical prerequisites."
                    + weak_prompt
                )
                
                user_content = f"Days available: {days}\nTopics to cover:\n{topic_list}"
                if weak:
                    user_content += f"\n\nStudent weakness note: {weak}"

                response = client.chat.completions(model="sarvam-105b-conversations", messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ])
                plan = {"days": days, "topics": topics, "weak": weak, "text": response.choices[0].message.content}
                data["plan"] = plan
                save_data(data)
                result = plan
            elif path == "/api/chat":
                message = request["message"].strip()
                lower = message.casefold()
                task_prefixes = ("add task:", "add to-do:", "add todo:", "remind me to ")
                if lower.startswith(task_prefixes):
                    raw_title = message.split(":", 1)[1].strip() if ":" in message else message[len("remind me to "):].strip()
                    title, time_val = extract_time_and_clean_title(raw_title)
                    data["tasks"].append({
                        "title": title,
                        "time": time_val,
                        "day": time.strftime("%Y-%m-%d"),
                        "done": False,
                        "notified": False
                    })
                    time_info = f" with reminder at {time_val} ⏰" if time_val else ""
                    save_data(data); result = {"reply": f"Added to today's timeline: {title}{time_info}"}
                elif lower.startswith("add topic:"):
                    title = message.split(":", 1)[1].strip()
                    subject = str(request.get("subject", "General")).strip()
                    if not subject or subject == "__all": subject = "General"
                    if subject not in data["subjects"]: data["subjects"].append(subject)
                    data["topics"].append({"title": title, "subject": subject, "done": False, "children": []})
                    save_data(data); result = {"reply": f"Added topic: {title}"}
                elif lower.startswith("log work:"):
                    text = message.split(":", 1)[1].strip()
                    data["daily_log"].append({"day": time.strftime("%Y-%m-%d"), "text": text})
                    save_data(data); result = {"reply": "Saved your work log for today."}
                else:
                    context = json.dumps(data, ensure_ascii=False)
                    response = client.chat.completions(model="sarvam-105b-conversations", messages=[
                        {"role": "system", "content": "You are DIV, a friendly AI tutor and daily-work assistant. Answer questions clearly, step by step, with a simple example when useful. Use the saved topics and timeline tasks below to tailor your explanation. If the subject or question is unclear, ask a short follow-up. Treat unchecked topics as still to do and checked topics as completed; when asked what to work on next, recommend an unchecked topic. Also answer questions about tasks and progress. You can save items when the message starts with 'add task:', 'add topic:', or 'log work:'. Never claim an item was saved unless the app confirms it. Keep replies concise.\nSaved data: " + context},
                        {"role": "user", "content": message},
                    ])
                    result = {"reply": response.choices[0].message.content}
            elif path == "/api/image":
                extracted = parse_image(request["filename"], request["mime"], request["data"])
                result = {"text": extracted}
            elif path == "/api/voice":
                audio = base64.b64decode(request["data"])
                mime = request.get("mime", "audio/wav").split(";", 1)[0]
                extension = mime.split("/")[-1].split(";")[0]
                transcript = client.speech_to_text.transcribe(file=(f"voice.{extension}", BytesIO(audio), mime), model="saaras:v4", language_code="unknown")
                result = {"transcript": transcript.transcript}
            elif path == "/api/speak":
                audio = client.text_to_speech.convert(text=request["text"], language_code="en-IN", model="bulbul:v4-flash")
                result = {"audio": audio.audios[0]}
            else:
                self.send_json({"error": "Not found"}, 404)
                return
            self.send_json(result)
        except Exception as error:
            self.send_json({"error": str(error)}, 500)

    def log_message(self, *_args):
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("DIV_PORT", "8765")))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"DIV is ready on {host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
