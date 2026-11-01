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
import zipfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

from sarvamai import SarvamAI

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data.json"
ACCOUNTS_FILE = ROOT / "accounts.json"
USER_DATA_DIR = ROOT / "user_data"
REQUEST_CONTEXT = threading.local()
SESSIONS = {}
SESSION_LOCK = threading.Lock()
ACCOUNT_LOCK = threading.Lock()
SESSION_SECONDS = 7 * 24 * 60 * 60
KEY_FILE = ROOT / ".env"
for line in KEY_FILE.read_text(encoding="utf-8-sig").splitlines():
    name, sep, value = line.partition("=")
    if sep and name.strip() == "SARVAM_API_KEY" and value.strip():
        os.environ[name.strip()] = value.strip()
if not os.environ.get("SARVAM_API_KEY"):
    raise SystemExit("Add SARVAM_API_KEY to .env before starting the app.")

client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
DEFAULT_DATA = {"subjects": [], "topics": [], "tasks": [], "daily_log": [], "study_plan": None}


def user_data_path(user_id):
    return USER_DATA_DIR / f"{user_id}.json"


def normalize_data(data):
    for key, value in DEFAULT_DATA.items():
        data.setdefault(key, copy.deepcopy(value))
    for topic in data["topics"]:
        if not topic.get("subject"):
            topic["subject"] = "Mathematics" if topic.get("title", "").startswith("JEE Adv Maths") else "General"
        if topic["subject"] not in data["subjects"]:
            data["subjects"].append(topic["subject"])
    return data


def load_data():
    user_id = getattr(REQUEST_CONTEXT, "user_id", None)
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
        raise RuntimeError("Sign in before saving study data.")
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    user_data_path(user_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_accounts():
    if not ACCOUNTS_FILE.exists():
        return {"users": {}}
    try:
        value = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value.get("users"), dict) else {"users": {}}
    except (OSError, json.JSONDecodeError):
        return {"users": {}}


def save_accounts(accounts):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


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


def parse_image(filename, mime, encoded):
    raw = base64.b64decode(encoded)
    job = client.doc_ai.parse(file=[(filename, BytesIO(raw), mime)], language="en-IN")
    terminal = {"completed", "partially_completed", "failed", "rejected"}
    for _ in range(60):
        status = client.doc_ai.get_status(job_id=job.job_id)
        state = status.status.lower()
        if state in terminal:
            break
        time.sleep(2)
    if state not in {"completed", "partially_completed"}:
        raise ValueError("Sarvam could not read that image. Try a clearer image.")
    link = client.doc_ai.get_download_url(job_id=job.job_id)
    with urllib.request.urlopen(link.url, timeout=30) as response:
        archive = zipfile.ZipFile(BytesIO(response.read()))
    md = next((name for name in archive.namelist() if name.endswith(".md")), None)
    return archive.read(md).decode("utf-8") if md else ""


PAGE = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Study & Day</title>
<style>
:root{color-scheme:light;--bg:#f5f6fa;--surface:#fff;--text:#202434;--border:#e5e7ef;--muted:#666;--soft:#eef0f8;--soft-text:#282b3b;--line:#eee;--bubble:#f2f2fa;--user-bubble:#e8e5ff;--done:#7d8190}
:root[data-theme="dark"]{color-scheme:dark;--bg:#151821;--surface:#202530;--text:#eceef6;--border:#394150;--muted:#aab0bd;--soft:#343a4a;--soft-text:#f0f1f7;--line:#353b47;--bubble:#2b3040;--user-bubble:#3b3464;--done:#a0a6b3}
body{font:16px system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0}main{max-width:980px;margin:32px auto;padding:16px}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:16px}.page-header{display:flex;align-items:center;justify-content:space-between;gap:12px}.page-header h1{margin-bottom:4px}h1{margin-bottom:4px}h2{font-size:19px;margin-top:0}button{background:#5847d8;color:white;border:0;border-radius:8px;padding:10px 14px;cursor:pointer;margin:4px}button.secondary{background:var(--soft);color:var(--soft-text)}input,textarea,select{font:inherit;padding:10px;border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:8px;width:100%;box-sizing:border-box;margin:6px 0}textarea{min-height:76px}.row{display:flex;gap:8px;align-items:center}.row input{flex:1}.item{padding:10px;border-bottom:1px solid var(--line)}.topic-node{padding:8px 4px;border-bottom:1px solid var(--line)}.topic-children{margin-left:20px;border-left:2px solid var(--border);padding-left:10px}.topic-actions button{font-size:12px;padding:6px 9px}.done{text-decoration:line-through;color:var(--done)}.msg{padding:9px 12px;border-radius:10px;background:var(--bubble);margin:8px 0;white-space:pre-wrap}.user{background:var(--user-bubble)}.hint{color:var(--muted);font-size:14px}.auth-card{max-width:440px;margin:48px auto}#authMessage{min-height:22px}#messages{max-height:360px;overflow:auto}@media(max-width:700px){.grid{grid-template-columns:1fr}.page-header{align-items:flex-start;flex-direction:column}}
</style><main><header class="page-header"><h1>Study & Day</h1><button class="secondary" id="themeToggle" onclick="toggleTheme()">Switch to dark mode</button></header><section id="loginPanel" class="card auth-card" hidden><p class="hint">Sign in to open your private study space, or create a new account.</p><label for="username">Username</label><input id="username" autocomplete="username" placeholder="3–32 letters, numbers, or underscores"><label for="password">Password</label><input id="password" type="password" autocomplete="current-password" placeholder="At least 8 characters"><div class="row"><button onclick="authenticate('login')">Sign in</button><button class="secondary" onclick="authenticate('register')">Create account</button></div><p id="authMessage" class="hint"></p><p class="hint">Keep your password safe; password recovery is not set up yet.</p></section><div id="dashboard" hidden><div class="row" style="justify-content:flex-end"><span id="currentUsername" class="hint"></span><button class="secondary" onclick="logout()">Sign out</button></div><p class="hint">Your private local study and daily-work tracker. Add topics and tasks, then ask for help.</p>
<script>window.MathJax={tex:{inlineMath:[["\\(","\\)"],["$","$" ]],displayMath:[["\\[","\\]"],["$$","$$"]]},options:{skipHtmlTags:["script","noscript","style","textarea","pre","code"]}};</script><script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<div class="grid"><section>
<div class="card"><h2>Chat and subject doubts</h2><div id="messages"></div><textarea id="message" placeholder="Ask a subject question, or try: add task: revise biology"></textarea><div class="row"><button onclick="send()">Send</button><button class="secondary" onclick="record()" id="record">🎙 Record voice</button><label class="hint"><input id="speak" type="checkbox" style="width:auto"> Speak replies</label></div><input id="audio" type="file" accept="audio/*" hidden><audio id="player" controls hidden></audio></div>
<div class="card"><h2>Add syllabus or tasks from an image</h2><p class="hint">Create a subject for each syllabus. Chapters and subtopics stay grouped under the selected subject. Checking a chapter also checks its subtopics. Unchecked means “to study”; checked means “completed.”</p><div class="row"><input id="newSubject" placeholder="New subject, e.g. Physics"><button onclick="addSubject()">Add subject</button></div><div class="row"><input id="topic" placeholder="Chapter name"><button onclick="addTopic()">Add chapter</button></div><input id="image" type="file" accept="image/png,image/jpeg,.pdf"><button class="secondary" onclick="readImage()">Read image/PDF</button><div id="preview"></div></div>
</section><aside>
<div class="card"><h2>Today's tasks</h2><div class="row"><input id="task" placeholder="What do you need to do?"><button onclick="addTask()">Add</button></div><div id="tasks"></div></div>
<div class="card"><h2>Syllabus study checklist</h2><label for="subjectSelect">Subject</label><select id="subjectSelect" onchange="selectSubject()"></select><p id="subjectCaption" class="hint"></p><div id="topics"></div><p id="progress" class="hint"></p></div>
<div class="card"><h2>Make a study plan</h2><p class="hint">Enter topics on separate lines. Leave them blank to plan from your unchecked syllabus topics.</p><textarea id="planTopics" placeholder="Paste the topics you need to study"></textarea><label for="planDays">How many days do you have?</label><input id="planDays" type="number" min="1" max="365" value="7"><button onclick="makePlan()">Create my plan</button><div id="planOutput"></div></div>
<div class="card"><h2>Log today's work</h2><textarea id="work" placeholder="What did you work on today?"></textarea><button onclick="logWork()">Save daily log</button><div id="logs"></div></div>
</aside></div></div></main>
<script>
function applyTheme(theme){document.documentElement.dataset.theme=theme;localStorage.setItem('studyTheme',theme);el('themeToggle').textContent=theme==='dark'?'Switch to light mode':'Switch to dark mode'}
function toggleTheme(){applyTheme(document.documentElement.dataset.theme==='dark'?'light':'dark')}
async function authenticate(action){let username=el('username').value.trim(),password=el('password').value;try{let result=await api('/api/'+action,{username,password});el('authMessage').textContent='';el('password').value='';el('currentUsername').textContent=result.username;el('loginPanel').hidden=true;el('dashboard').hidden=false;refresh()}catch(e){el('authMessage').textContent=e.message}}
async function startApp(){try{let user=await api('/api/me');el('currentUsername').textContent=user.username;el('loginPanel').hidden=true;el('dashboard').hidden=false;refresh()}catch(e){el('dashboard').hidden=true;el('loginPanel').hidden=false}}
async function logout(){await api('/api/logout',{});el('dashboard').hidden=true;el('loginPanel').hidden=false;el('password').value='';el('authMessage').textContent='You are signed out.'}
async function api(path,body){let r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||'Something went wrong');return j}
function el(id){return document.getElementById(id)}
function addMsg(text,who='assistant'){let d=document.createElement('div');d.className='msg '+(who==='user'?'user':'');d.textContent=text;el('messages').appendChild(d);if(who!=='user'&&window.MathJax?.typesetPromise)window.MathJax.typesetPromise([d]);el('messages').scrollTop=el('messages').scrollHeight}
function topicNode(t,path,ancestorDone=false){let done=ancestorDone||!!t.done,children=t.children||[];return `<div class="topic-node" data-title="${esc(t.title)}"><label><input style="width:auto" type="checkbox" ${done?'checked':''} onchange="toggleTopic(${JSON.stringify(path)},this.checked)"> <span class="${done?'done':''}">${esc(t.title)}</span></label> <small class="hint">${done?'Completed':'To study'}</small><div class="topic-actions">${path.length<3?`<button class="secondary" onclick="addChild(${JSON.stringify(path)})">${path.length===1?'Add subtopic':'Add sub-sub-topic'}</button>`:''}<button class="secondary" onclick="editTopic(${JSON.stringify(path)},this.closest('.topic-node').dataset.title)">Edit</button><button class="secondary" onclick="removeTopic(${JSON.stringify(path)},this.closest('.topic-node').dataset.title)">Remove</button></div>${children.length?`<div class="topic-children">${children.map((c,i)=>topicNode(c,[...path,i],done)).join('')}</div>`:''}</div>`}
function topicCounts(topics,ancestorDone=false){let total=0,done=0;for(const t of topics){total++;if(ancestorDone||t.done)done++;const c=topicCounts(t.children||[],ancestorDone||t.done);total+=c.total;done+=c.done}return{total,done}}
async function refresh(){let d=await api('/api/state'),selector=el('subjectSelect'),previous=localStorage.getItem('activeSubject')||'';selector.innerHTML='<option value="__all">All subjects</option>'+d.subjects.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');selector.value=d.subjects.includes(previous)?previous:(d.subjects[0]||'__all');localStorage.setItem('activeSubject',selector.value);let active=selector.value,visible=d.topics.map((t,i)=>({t,i})).filter(x=>active==='__all'||x.t.subject===active);el('subjectCaption').textContent=active==='__all'?'All subjects':active;el('topics').innerHTML=visible.map(x=>topicNode(x.t,[x.i])).join('')||'<p class="hint">No chapters for this subject yet. Add a chapter above.</p>';el('tasks').innerHTML=d.tasks.filter(t=>t.day===d.today).map(t=>`<div class="item"><label><input style="width:auto" type="checkbox" ${t.done?'checked':''} onchange="toggleTask(${d.tasks.indexOf(t)},this.checked)"> <span class="${t.done?'done':''}">${esc(t.title)}</span></label></div>`).join('')||'<p class="hint">No tasks yet.</p>';let counts=topicCounts(visible.map(x=>x.t));el('progress').textContent=`${counts.done} of ${counts.total} syllabus items completed`;el('logs').innerHTML=d.daily_log.slice(-3).reverse().map(x=>`<div class="item"><b>${esc(x.day)}</b> ${esc(x.text)}</div>`).join('');el('planOutput').innerHTML=d.study_plan?`<p class="hint">${d.study_plan.days}-day plan ? ${d.study_plan.topics.length} topics</p><div class="item" style="white-space:pre-wrap">${esc(d.study_plan.text)}</div>`:'<p class="hint">Your saved plan will appear here.</p>'}

function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function selectSubject(){localStorage.setItem('activeSubject',el('subjectSelect').value);refresh()}
async function addSubject(){let name=el('newSubject').value.trim();if(!name)return;let r=await api('/api/subject',{name});el('newSubject').value='';localStorage.setItem('activeSubject',r.name);refresh()}
async function addTopic(){let title=el('topic').value.trim(),subject=el('subjectSelect').value;if(!title)return;if(subject==='__all'){alert('Choose a subject first, or add one above.');return}await api('/api/topic',{title,subject});el('topic').value='';refresh()}
async function addTask(){let title=el('task').value.trim();if(!title)return;await api('/api/task',{title});el('task').value='';refresh()}
async function addChild(path){let title=prompt(path.length===1?'Subtopic name':'Sub-sub-topic name');if(!title||!title.trim())return;await api('/api/topic/child',{path,title:title.trim()});refresh()}
async function editTopic(path,current){let title=prompt('Edit syllabus item',current);if(title===null||!title.trim())return;await api('/api/topic/edit',{path,title:title.trim()});refresh()}
async function removeTopic(path,title){if(!confirm(`Remove "${title}" and all its subtopics?`))return;await api('/api/topic/remove',{path});refresh()}
async function toggleTopic(path,done){await api('/api/topic/done',{path,done});refresh()}
async function toggleTask(index,done){await api('/api/task/done',{index,done});refresh()}
async function logWork(){let text=el('work').value.trim();if(!text)return;await api('/api/log',{text});el('work').value='';refresh()}
async function makePlan(){let days=Number(el('planDays').value);if(!Number.isInteger(days)||days<1||days>365){addMsg('Enter a number of days from 1 to 365.');return}let topics=el('planTopics').value.split(/\n+/).map(x=>x.trim()).filter(Boolean);el('planOutput').textContent='Building an intensive plan…';try{let p=await api('/api/plan',{days,topics});el('planOutput').innerHTML=`<p class="hint">${p.days}-day plan · ${p.topics.length} topics</p><div class="item" style="white-space:pre-wrap">${esc(p.text)}</div>`;refresh()}catch(e){el('planOutput').textContent=e.message}}
async function send(){let message=el('message').value.trim();if(!message)return;el('message').value='';addMsg(message,'user');try{let r=await api('/api/chat',{message,subject:el('subjectSelect').value});addMsg(r.reply);if(el('speak').checked){let a=await api('/api/speak',{text:r.reply.slice(0,2400)});el('player').src='data:audio/wav;base64,'+a.audio;el('player').hidden=false;el('player').play()};refresh()}catch(e){addMsg(e.message)}}
el('message').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}})
let recorder, chunks=[];async function record(){if(recorder&&recorder.state==='recording'){recorder.stop();el('record').textContent='🎙 Record voice';return}try{let stream=await navigator.mediaDevices.getUserMedia({audio:true});recorder=new MediaRecorder(stream);chunks=[];recorder.ondataavailable=e=>chunks.push(e.data);recorder.onstop=async()=>{let blob=new Blob(chunks,{type:recorder.mimeType});let data=await blob.arrayBuffer();let bytes=Array.from(new Uint8Array(data));let binary=bytes.map(b=>String.fromCharCode(b)).join('');try{let r=await api('/api/voice',{mime:blob.type,data:btoa(binary)});el('message').value=r.transcript;send()}catch(e){addMsg(e.message)}stream.getTracks().forEach(t=>t.stop())};recorder.start();el('record').textContent='Stop recording'}catch(e){addMsg('Allow microphone access to use voice input.')}}
async function readImage(){let f=el('image').files[0];if(!f)return;el('preview').textContent='Reading image…';try{let b=await f.arrayBuffer();let binary=Array.from(new Uint8Array(b)).map(x=>String.fromCharCode(x)).join('');let r=await api('/api/image',{filename:f.name,mime:f.type||'application/pdf',data:btoa(binary)});el('preview').innerHTML='<p>Check the extracted text, then choose how to save it.</p><textarea id="parsed">'+esc(r.text)+'</textarea><select id="importType"><option value="topics">Syllabus topics</option><option value="tasks">Today’s tasks</option></select><button onclick="saveParsed()">Save</button>'}catch(e){el('preview').textContent=e.message}}
async function saveParsed(){let kind=el('importType').value,subject=el('subjectSelect').value;if(kind==='topics'&&subject==='__all'){el('preview').textContent='Choose a subject before importing syllabus topics.';return}let lines=el('parsed').value.split(/\n+/).map(x=>x.replace(/^\s*(?:[-*]|\d+[.)])\s*/, '').trim()).filter(x=>x.length>2&&!/^#+\s/.test(x));await api('/api/import',{items:lines,kind,subject});el('preview').textContent='Saved. You can mark items complete below.';refresh()}

applyTheme(localStorage.getItem('studyTheme')||'light');
startApp();
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
        token = next((part.strip().partition("=")[2] for part in cookie.split(";") if part.strip().startswith("study_session=")), "")
        with SESSION_LOCK:
            session = SESSIONS.get(token)
            if not session:
                return None
            if session["expires"] <= time.time():
                SESSIONS.pop(token, None)
                return None
            return {"id": session["id"], "username": session["username"]}

    def set_session(self, user):
        token = secrets.token_urlsafe(32)
        with SESSION_LOCK:
            SESSIONS[token] = {**user, "expires": time.time() + SESSION_SECONDS}
        secure = "; Secure" if os.environ.get("PUBLIC_HTTPS") == "1" else ""
        return {"Set-Cookie": f"study_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_SECONDS}{secure}"}

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
            first_user = not accounts["users"]
            accounts["users"][key] = user
            save_accounts(accounts)
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        destination = user_data_path(user["id"])
        if first_user and DATA_FILE.exists():
            try:
                initial_data = normalize_data(json.loads(DATA_FILE.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                initial_data = copy.deepcopy(DEFAULT_DATA)
            destination.write_text(json.dumps(initial_data, ensure_ascii=False, indent=2), encoding="utf-8")
        headers = self.set_session({"id": user["id"], "username": username})
        self.send_json({"username": username}, extra_headers=headers)

    def do_GET(self):
        if self.path == "/":
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
                user = self.current_user()
                token = next((part.strip().partition("=")[2] for part in self.headers.get("Cookie", "").split(";") if part.strip().startswith("study_session=")), "")
                with SESSION_LOCK:
                    SESSIONS.pop(token, None)
                self.send_json({"ok": True}, extra_headers={"Set-Cookie": "study_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"})
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
                    raise ValueError("That syllabus item was not found.")
                items = data["topics"]
                try:
                    for index in indexes[:-1]:
                        items = items[index].setdefault("children", [])
                    if indexes[-1] >= len(items):
                        raise IndexError
                except (IndexError, TypeError):
                    raise ValueError("That syllabus item was not found.")
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
                    raise ValueError("Choose a subject before adding a chapter.")
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
                    raise ValueError("That syllabus item was not found.")
                title = str(request.get("title", "")).strip()
                if not title:
                    raise ValueError("Enter a name for this syllabus item.")
                items.append({"title": title, "done": False, "children": []})
                save_data(data); result = {"ok": True}
            elif path == "/api/topic/edit":
                items, index = topic_parent(request.get("path"))
                title = str(request.get("title", "")).strip()
                if not title:
                    raise ValueError("Enter a name for this syllabus item.")
                items[index]["title"] = title
                save_data(data); result = {"ok": True}
            elif path == "/api/topic/remove":
                items, index = topic_parent(request.get("path"))
                del items[index]
                save_data(data); result = {"ok": True}
            elif path == "/api/task":
                data["tasks"].append({"title": request["title"], "day": time.strftime("%Y-%m-%d"), "done": False})
                save_data(data); result = {"ok": True}
            elif path == "/api/topic/done":
                indexes = request.get("path", [request.get("index")])
                if not indexes or indexes[0] is None:
                    raise ValueError("That syllabus item was not found.")
                items = data["topics"]
                try:
                    for index in indexes[:-1]:
                        items = items[index].setdefault("children", [])
                    selected = items[indexes[-1]]
                except (IndexError, TypeError):
                    raise ValueError("That syllabus item was not found.")
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
                    raise ValueError("Choose a subject before importing syllabus topics.")
                if target == "topics" and subject not in data["subjects"]:
                    data["subjects"].append(subject)
                for title in request.get("items", []):
                    if title:
                        item = {"title": title, "done": False}
                        if target == "topics": item["subject"] = subject
                        if target == "tasks": item["day"] = time.strftime("%Y-%m-%d")
                        data[target].append(item)
                save_data(data); result = {"ok": True}
            elif path == "/api/plan":
                days = request.get("days")
                if not isinstance(days, int) or days < 1 or days > 365:
                    raise ValueError("Choose a number of days from 1 to 365.")
                topics = [str(item).strip() for item in request.get("topics", []) if str(item).strip()]
                if not topics:
                    topics = pending_topic_titles(data["topics"])
                if not topics:
                    raise ValueError("Paste your study topics or add syllabus topics first.")
                topic_list = "\n".join(f"- {topic}" for topic in topics)
                response = client.chat.completions(model="sarvam-105b-conversations", messages=[
                    {"role": "system", "content": "You are an expert study planner. Create an optimistic, intensive, high-productivity but plausible day-by-day plan that covers every supplied topic within the exact number of days. Group related topics and respect prerequisites. Give each day a clear focus, a concrete learning outcome, problem-solving practice, and short cumulative review. Include spaced recall and a final mixed revision or mock-test block when the day count allows. Do not mark anything as completed. No daily study-hours were provided, so do not invent a clock schedule or promise a score; state that workload can be adjusted if the student shares available hours. Keep the plan concise and actionable."},
                    {"role": "user", "content": f"Days available: {days}\nTopics to study:\n{topic_list}"},
                ])
                plan = {"days": days, "topics": topics, "text": response.choices[0].message.content}
                data["study_plan"] = plan
                save_data(data)
                result = plan
            elif path == "/api/chat":
                message = request["message"].strip()
                lower = message.casefold()
                task_prefixes = ("add task:", "add to-do:", "add todo:", "remind me to ")
                if lower.startswith(task_prefixes):
                    title = message.split(":", 1)[1].strip() if ":" in message else message[len("remind me to "):].strip()
                    data["tasks"].append({"title": title, "day": time.strftime("%Y-%m-%d"), "done": False})
                    save_data(data); result = {"reply": f"Added to today's tasks: {title}"}
                elif lower.startswith("add topic:"):
                    title = message.split(":", 1)[1].strip()
                    subject = str(request.get("subject", "General")).strip()
                    if not subject or subject == "__all": subject = "General"
                    if subject not in data["subjects"]: data["subjects"].append(subject)
                    data["topics"].append({"title": title, "subject": subject, "done": False, "children": []})
                    save_data(data); result = {"reply": f"Added syllabus topic: {title}"}
                elif lower.startswith("log work:"):
                    text = message.split(":", 1)[1].strip()
                    data["daily_log"].append({"day": time.strftime("%Y-%m-%d"), "text": text})
                    save_data(data); result = {"reply": "Saved your work log for today."}
                else:
                    context = json.dumps(data, ensure_ascii=False)
                    response = client.chat.completions(model="sarvam-105b-conversations", messages=[
                        {"role": "system", "content": "You are a friendly tutor and daily-work assistant. Answer the user's subject doubts clearly, step by step, with a simple example when useful. Use the saved syllabus topics below to tailor your explanation and say when a question seems outside that syllabus. If the subject or question is unclear, ask a short follow-up. Treat unchecked syllabus topics as still to study and checked topics as completed; when asked what to study next, recommend an unchecked topic. Also answer questions about their tasks and study progress. You can save items when the message starts with 'add task:', 'add topic:', or 'log work:'. Never claim an item was saved unless the app confirms it. Keep replies concise.\nSaved data: " + context},
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
                self.send_json({"error": "Not found"}, 404); return
            self.send_json(result)
        except Exception as error:
            self.send_json({"error": str(error)}, 500)

    def log_message(self, *_args):
        return


if __name__ == "__main__":
    port = int(os.environ.get("STUDY_DAY_PORT", "8765"))
    print(f"Study & Day is ready at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
