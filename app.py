import os, re, json, html, time, hmac, hashlib, threading, datetime as dt
import requests
from flask import Flask, request, session, abort
from google.cloud import firestore
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
SECRET = hashlib.sha256(app.secret_key.encode()).hexdigest()[:32]
fs = firestore.Client.from_service_account_info(json.loads(os.environ["FIREBASE_KEY"]))
P = "bot_"  # prefix: bot data never touches the website's own collections (orders, services)
esc = lambda s: html.escape(str(s if s is not None else ""))
now = lambda: dt.datetime.now(dt.timezone.utc)
TPL = "অর্ডার রিসিভ হয়েছে\n\nঅর্ডার আইডি: #{order_id}\nপ্যাকেজ: {package}\nমূল্য: {price} টাকা\nUID: {uid}\nTransaction ID: {trx_id}\n\nযাচাই করে ডায়মন্ড পাঠানো হচ্ছে..."
STATUS = {"completed": "সম্পন্ন", "pending": "পেন্ডিং", "processing": "প্রসেসিং", "rejected": "বাতিল"}

# ---------------- storage (Firestore) ----------------
def C(n): return fs.collection(P + n)
def rows(n): return [dict(d.to_dict(), id=d.id) for d in C(n).stream()]
def one(n, i):
    d = C(n).document(str(i)).get()
    return dict(d.to_dict(), id=d.id) if d.exists else None
def save(n, i, data):
    if i:
        C(n).document(str(i)).set(data, merge=True)
        return str(i)
    ref = C(n).document()
    ref.set(data)
    return ref.id
def drop(n, i): C(n).document(str(i)).delete()

@firestore.transactional
def _inc(tx, ref):
    v = (ref.get(transaction=tx).to_dict() or {}).get("n", 0) + 1
    tx.set(ref, {"n": v})
    return v
def next_id(k): return _inc(fs.transaction(), C("meta").document(k))

_cfg = {}
def cfg(k, d=""):
    if not _cfg:
        _cfg.update(one("config", "main") or {"_": 1})
    return _cfg.get(k, d)
def cfg_set(k, v):
    cfg("x")
    _cfg[k] = v
    C("config").document("main").set({k: v}, merge=True)

def log(msg, t="info"): C("logs").add({"ts": now(), "type": t, "msg": str(msg)[:500]})

def packages(only_active=True):
    r = [p for p in rows("packages") if p.get("active") or not only_active]
    return sorted(r, key=lambda p: (p.get("sort_order", 0), p.get("name", "")))
def codes(pid): return [dict(d.to_dict(), id=d.id) for d in C("codes").where("package_id", "==", pid).stream()]

@firestore.transactional
def _take(tx, pid):
    ds = list(C("codes").where("package_id", "==", pid).stream(transaction=tx))
    if not ds:
        return None
    d = min(ds, key=lambda x: x.get("seq"))
    tx.delete(d.reference)
    return d.get("code")
def consume(pid): return _take(fs.transaction(), pid)

def get_session(chat): return one("sessions", chat) or {"step": "idle", "pkg_id": None, "uid": None}
def set_session(chat, step, pkg=None, uid=None): save("sessions", chat, {"step": step, "pkg_id": pkg, "uid": uid})

# ---------------- telegram ----------------
def tg(method, **p):
    t = cfg("bot_token")
    if not t:
        return {"ok": False, "description": "Bot token not set"}
    try:
        return requests.post("https://api.telegram.org/bot" + t + "/" + method, json=p, timeout=20).json()
    except Exception as e:
        return {"ok": False, "description": str(e)}

def send(chat, text, kb=None):
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    log("sent: " + str(chat), "out")
    return tg("sendMessage", **p)

def call_api(uid, code, name, price, oid):
    url, key = cfg("api_url"), cfg("api_key")
    if not url:
        return False
    body = {"api": key, "api_key": key, "uid": uid, "playerid": uid, "code": code, "package": name, "price": price, "order_id": oid}
    try:
        r = requests.post(url, json=body, headers={"Authorization": "Bearer " + key}, timeout=20)
    except Exception as e:
        log("API error: " + str(e), "err")
        return False
    good = 200 <= r.status_code < 300
    log("provider HTTP %s | %s" % (r.status_code, r.text[:300]), "in" if good else "err")
    try:
        d = r.json()
    except Exception:
        return False
    return bool(good and isinstance(d, dict) and (d.get("success") or d.get("status") in (True, "success")))

# ---------------- bot logic ----------------
def pkg_kb(ps): return [[{"text": "%s - %g টাকা" % (p["name"], p["price"]), "callback_data": "pkg_" + p["id"]}] for p in ps]

def pay_text():
    ns = rows("numbers")
    if not ns:
        return "এডমিন এখনো কোনো পেমেন্ট নাম্বার সেট করেননি।"
    return "\n".join("• %s (%s): <b>%s</b>" % (n["method"], n["type"], esc(n["number"])) for n in ns)

def my_orders(chat):
    os_ = [o for o in rows_where("orders", "chat_id", chat)]
    return sorted(os_, key=lambda o: o.get("created_at"), reverse=True)[:10]
def rows_where(n, f, v): return [dict(d.to_dict(), id=d.id) for d in C(n).where(f, "==", v).stream()]
def order_lines(chat): return "\n".join("#%s - %s - %s" % (o["id"], esc(o["package_name"]), STATUS.get(o["status"], o["status"])) for o in my_orders(chat))

def cmd_list_text():
    lines = ["/order — নতুন অর্ডার শুরু করুন (প্যাকেজ লিস্ট দেখাবে)", "/myorders — আপনার অর্ডার হিস্টোরি দেখুন", "/help — সব কমান্ডের লিস্ট আবার দেখুন"]
    for p in packages():
        if p.get("command"):
            lines.append('/%s — সরাসরি "%s" (%g টাকা) অর্ডার করুন' % (p["command"], esc(p["name"]), p["price"]))
    desc = {"menu": "সব একটিভ প্যাকেজের মেনু দেখাবে", "orders": "আপনার অর্ডার হিস্টোরি দেখাবে", "paymentnumbers": "পেমেন্ট নাম্বার দেখাবে"}
    for c in rows("commands"):
        if not c.get("active") or c["command"] == "start":
            continue
        d = desc.get(c["type"], "একটি রিপ্লাই মেসেজ পাঠাবে")
        if c["type"] == "order":
            pk = one("packages", c.get("pkg_id")) if c.get("pkg_id") else None
            d = 'সরাসরি "%s" প্যাকেজের অর্ডার শুরু হবে' % esc(pk["name"]) if pk else "অর্ডার শুরু হবে"
        lines.append("/%s — %s" % (c["command"], d))
    return "<b>সব কমান্ড</b>\n\n" + "\n".join(lines)

def pkg_intro(p, intro=""):
    return "%sপ্যাকেজ সিলেক্ট হয়েছে: <b>%s</b> (%g টাকা)\n%s\nএখন আপনার Free Fire UID পাঠান:" % (intro, esc(p["name"]), p["price"], esc(p["description"]) + "\n" if p.get("description") else "")

def run_cmd(chat, c):
    t, msg = c["type"], c.get("message", "")
    intro = esc(msg) + "\n\n" if msg else ""
    if t == "text":
        return send(chat, msg or "ok")
    if t == "menu":
        ps = packages()
        if not ps:
            return send(chat, "দুঃখিত, এই মুহূর্তে কোনো প্যাকেজ চালু নেই।")
        set_session(chat, "choosing_package")
        return send(chat, msg or "নিচে থেকে আপনার পছন্দের প্যাকেজ সিলেক্ট করুন:", pkg_kb(ps))
    if t == "order":
        p = one("packages", c.get("pkg_id")) if c.get("pkg_id") else None
        if not p or not p.get("active"):
            return send(chat, "দুঃখিত, এই প্যাকেজটি এই মুহূর্তে পাওয়া যাচ্ছে না।")
        set_session(chat, "awaiting_uid", p["id"])
        return send(chat, pkg_intro(p, intro))
    if t == "orders":
        return send(chat, intro + (order_lines(chat) or "আপনার কোনো অর্ডার নেই।"))
    if t == "paymentnumbers":
        return send(chat, intro + pay_text())

def submit_order(chat, user, p, uid, trx):
    oid = next_id("orderseq")
    save("orders", str(oid), {"chat_id": chat, "username": user, "package_id": p["id"], "package_name": p["name"], "price": p["price"], "uid": uid,
                              "trx_id": trx, "status": "processing", "code": "", "method": "", "source": "telegram", "created_at": now()})
    t = cfg("order_template") or TPL
    for k, v in (("{order_id}", oid), ("{package}", esc(p["name"])), ("{price}", "%g" % p["price"]), ("{uid}", esc(uid)), ("{trx_id}", esc(trx))):
        t = t.replace(k, str(v))
    send(chat, t)
    auto_process(str(oid), chat, p, uid, user, trx)

def auto_process(oid, chat, p, uid, user, trx):
    ok, code, method = False, "", ""
    cs = sorted(codes(p["id"]), key=lambda c: c["seq"])
    if cfg("api_url") and cs:
        c0 = cs[0]["code"]
        log("Auto Topup API call (UID: %s, code: %s)..." % (uid, c0), "out")
        if call_api(uid, c0, p["name"], p["price"], oid):
            consume(p["id"])
            ok, code, method = True, c0, "api"
            log("Auto Topup API success - diamonds sent to UID", "in")
        else:
            log("Auto Topup API failed - the same code will be tried as voucher", "err")
    if not ok and codes(p["id"]):
        code = consume(p["id"])
        if code:
            ok, method = True, "stock"
    if ok:
        save("orders", oid, {"status": "completed", "code": code, "method": method})
        if method == "api":
            m = "অর্ডার কনফার্ম হয়েছে!\n\nআপনার Free Fire UID (%s) এ ডায়মন্ড অটোমেটিক পাঠানো হয়ে গেছে।\nঅর্ডার আইডি: #%s\n\nধন্যবাদ আমাদের সাথে থাকার জন্য।" % (uid, oid)
        else:
            m = "পেমেন্ট ভেরিফাই হয়েছে!\n\nআপনার রিডিম কোড: <b>%s</b>\nUID: %s\nঅর্ডার আইডি: #%s\n\nএই কোডটি দিয়ে গেমে ডায়মন্ড রিডিম করুন।" % (esc(code), uid, oid)
        send(chat, m)
        log("order #%s completed (%s)" % (oid, method), "in")
    else:
        save("orders", oid, {"status": "pending"})
        send(chat, "আপনার পেমেন্ট যাচাই চলছে, একটু বেশি সময় লাগতে পারে। কনফার্ম হলেই ডায়মন্ড পাঠিয়ে জানানো হবে।\nঅর্ডার আইডি: #" + oid)
        if cfg("admin_id"):
            send(cfg("admin_id"), "অর্ডার ম্যানুয়াল চেক প্রয়োজন\n\nইউজার: %s\nপ্যাকেজ: %s\nমূল্য: %g টাকা\nUID: %s\nTransaction ID: %s\nঅর্ডার আইডি: #%s\n\nকারণ: Auto Topup API সেট নেই/ব্যর্থ হয়েছে এবং প্যাকেজের কোড-বক্স খালি। ড্যাশবোর্ড > Orders থেকে ম্যানুয়ালি resolve করুন।" % (esc(user), esc(p["name"]), p["price"], esc(uid), esc(trx), oid))
        log("order #%s pending - manual check needed" % oid, "err")

def on_message(m):
    chat, text = m["chat"]["id"], (m.get("text") or "").strip()
    fr = m.get("from", {})
    user = "@" + fr["username"] if fr.get("username") else fr.get("first_name", "User")
    log("msg (%s): %s" % (user, text), "in")
    s, ps = get_session(chat), packages()
    name = text[1:].split(" ")[0].split("@")[0].lower() if text.startswith("/") else ""
    if name:
        for c in rows("commands"):
            if c.get("active") and c["command"] == name:
                return run_cmd(chat, c)
    if text == "/start":
        set_session(chat, "idle")
        send(chat, "স্বাগতম" + (", " + esc(user) if user != "User" else "") + "!\nএখান থেকে ফ্রি ফায়ার ডায়মন্ড টপআপ করতে পারবেন।")
        send(chat, cmd_list_text())
        if ps:
            set_session(chat, "choosing_package")
            return send(chat, "নিচে থেকে আপনার পছন্দের প্যাকেজ সিলেক্ট করুন:", pkg_kb(ps))
        return send(chat, "দুঃখিত, এই মুহূর্তে কোনো প্যাকেজ চালু নেই। পরে আবার চেষ্টা করুন।")
    if text == "/order":
        if not ps:
            return send(chat, "দুঃখিত, এই মুহূর্তে কোনো প্যাকেজ চালু নেই।")
        set_session(chat, "choosing_package")
        return send(chat, "নিচে থেকে আপনার পছন্দের প্যাকেজ সিলেক্ট করুন:", pkg_kb(ps))
    if text == "/help":
        return send(chat, cmd_list_text())
    if text == "/myorders":
        return send(chat, order_lines(chat) or "আপনার কোনো অর্ডার নেই।")
    if name:
        for p in ps:
            if p.get("command", "").lower() == name:
                set_session(chat, "awaiting_uid", p["id"])
                return send(chat, pkg_intro(p))
    if s["step"] == "awaiting_uid":
        p = one("packages", s["pkg_id"]) if s.get("pkg_id") else None
        if not p:
            set_session(chat, "idle")
            return send(chat, "সমস্যা হয়েছে, আবার /order দিন।")
        if p.get("uid_check") and not re.fullmatch(r"[0-9]{6,12}", text):
            return send(chat, "সঠিক UID দিন (শুধু সংখ্যা, ৬-১২ ডিজিট):")
        set_session(chat, "awaiting_trxid", p["id"], text)
        return send(chat, "নিচের যেকোনো নাম্বারে <b>%g টাকা Send Money</b> করুন:\n\n%s\n\nপেমেন্ট করার পর আপনার <b>Transaction ID (TrxID)</b> এখানে টাইপ করে পাঠান।\n(bKash/Nagad মেসেজে যেটা দেওয়া থাকে, যেমন: 9EI7XXXXXX)" % (p["price"], pay_text()))
    if s["step"] == "awaiting_trxid":
        p = one("packages", s["pkg_id"]) if s.get("pkg_id") else None
        if not p or not s.get("uid"):
            set_session(chat, "idle")
            return send(chat, "সেশন এক্সপায়ার হয়েছে, আবার /order দিন।")
        if not re.fullmatch(r"[A-Za-z0-9]{6,15}", text):
            return send(chat, "সঠিক Transaction ID দিন (৬-১৫ ডিজিট/অক্ষর, স্পেস ছাড়া):")
        set_session(chat, "idle")
        submit_order(chat, user, p, s["uid"], text)

def on_cb(q):
    chat, data = q["message"]["chat"]["id"], q.get("data", "")
    log("button: " + data, "in")
    if data.startswith("pkg_"):
        p = one("packages", data[4:])
        if not p:
            return tg("answerCallbackQuery", callback_query_id=q["id"], text="প্যাকেজ পাওয়া যায়নি")
        set_session(chat, "awaiting_uid", p["id"])
        tg("answerCallbackQuery", callback_query_id=q["id"], text="সিলেক্ট হয়েছে")
        send(chat, pkg_intro(p))

@app.route("/tg", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    u = request.get_json(force=True, silent=True) or {}
    try:
        if "message" in u:
            on_message(u["message"])
        elif "callback_query" in u:
            on_cb(u["callback_query"])
    except Exception as e:
        log("handler error: " + str(e), "err")
    return "ok"

@app.route("/")
@app.route("/ping")
def ping(): return "FF Topup Bot is running"

# ---------------- website order watcher (replaces cron_watcher.php) ----------------
def watch_once():
    if not cfg("api_url"):
        return
    q = fs.collection("orders").where("needsApiDelivery", "==", True).where("status", "==", "pending")
    for o in q.stream():
        d, oid = o.to_dict(), o.id
        g, pid = d.get("gameId"), d.get("productId")
        pref = fs.collection("services").document(g).collection("products").document(pid) if g and pid else None
        stock = list(pref.collection("stock").where("status", "==", "available").limit(1).stream()) if pref else []
        if not stock:
            log("website order #%s - gameId/productId or stock code missing" % oid, "err")
            o.reference.update({"needsApiDelivery": False, "apiDeliveryFailed": True})
            continue
        code = stock[0].to_dict().get("code", "")
        log("website order #%s processing (UID: %s, code: %s)..." % (oid, d.get("playerId"), code), "out")
        ok = call_api(d.get("playerId", ""), code, d.get("productName", ""), d.get("price", 0), oid)
        stock[0].reference.delete()
        pref.update({"stockCount": firestore.Increment(-1)})
        if ok:
            o.reference.update({"status": "success", "deliveredVia": "api", "needsApiDelivery": False})
            log("website order #%s completed via API" % oid, "in")
        else:
            o.reference.update({"status": "success", "redeemCode": code, "deliveredVia": "stock", "needsApiDelivery": False})
            log("website order #%s - API failed, delivered voucher code" % oid, "in")

def loop():
    n = 0
    while True:
        try:
            watch_once()
            n += 1
            if n % 60 == 0:
                for d in C("logs").order_by("ts", direction=firestore.Query.DESCENDING).offset(500).stream():
                    d.reference.delete()
        except Exception as e:
            log("watcher error: " + str(e), "err")
        time.sleep(60)

if os.environ.get("RUN_WATCHER", "1") == "1":
    threading.Thread(target=loop, daemon=True).start()

# ---------------- admin panel ----------------
CSS = """*{box-sizing:border-box}body{margin:0;background:#070a12;color:#e6ecf5;font-family:system-ui,'Noto Sans Bengali',sans-serif}
nav{display:flex;gap:6px;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid #1c2433;background:#0b1019;position:sticky;top:0}
nav a{color:#8b97a8;text-decoration:none;padding:8px 14px;border-radius:10px;transition:.2s}nav a:hover,nav a.on{background:#00e5ff22;color:#00e5ff}
main{max-width:980px;margin:auto;padding:18px}h2{margin:.3em 0}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:#0f1623;border:1px solid #1c2433;border-radius:14px;padding:16px;margin:12px 0;animation:u .4s}@keyframes u{from{opacity:0;transform:translateY(10px)}}
label{display:block;font-size:12px;color:#8b97a8;margin:8px 0}input,select,textarea{width:100%;margin-top:4px;padding:10px;border-radius:10px;border:1px solid #243049;background:#0a0f1a;color:#e6ecf5;font:inherit}
.ck{display:flex;gap:8px;align-items:center}.ck input{width:auto;margin:0}.b,.b2,.b3{border:0;border-radius:10px;padding:9px 16px;cursor:pointer;font:inherit;font-weight:600;background:#00e5ff;color:#001018;text-decoration:none;display:inline-block}
.b2{background:#1c2638;color:#e6ecf5}.b3{background:#3b1219;color:#fca5a5}.t{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:9px;border-bottom:1px solid #1c2433;text-align:left;vertical-align:top}th{color:#8b97a8;font-weight:500}.m{background:#00e5ff22;border:1px solid #00e5ff55;padding:10px 14px;border-radius:10px;margin:12px 0}
small{color:#8b97a8}form.i{display:inline}"""
NAV = [("dashboard", "Dashboard"), ("packages", "Packages"), ("numbers", "Numbers"), ("commands", "Commands"), ("orders", "Orders"), ("settings", "Settings"), ("console", "Console")]

def layout(cur, body, msg=""):
    nav = "".join('<a href="/admin/%s" class="%s">%s</a>' % (k, "on" if k == cur else "", v) for k, v in NAV) + '<a href="/admin/logout" style="margin-left:auto">Logout</a>'
    return "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Admin</title><style>%s</style><nav>%s</nav><main>%s%s</main>" % (CSS, nav, '<div class=m>%s</div>' % esc(msg) if msg else "", body)

def fld(n, l, v="", t="text", ph=""): return '<label>%s<input name="%s" type="%s" value="%s" placeholder="%s"></label>' % (l, n, t, esc(v), ph)
def area(n, l, v="", r=4): return '<label>%s<textarea name="%s" rows="%d">%s</textarea></label>' % (l, n, r, esc(v))
def sel(n, l, opts, v=""): return '<label>%s<select name="%s">%s</select></label>' % (l, n, "".join('<option value="%s"%s>%s</option>' % (esc(a), " selected" if str(a) == str(v) else "", esc(b)) for a, b in opts))
def chk(n, l, v): return '<label class=ck><input type=checkbox name="%s"%s> %s</label>' % (n, " checked" if v else "", l)
def hid(**kw): return "".join('<input type=hidden name="%s" value="%s">' % (k, esc(v)) for k, v in kw.items())
def act(action, label, cls="b2", conf="", **kw):
    oc = ' onsubmit="return confirm(\'%s\')"' % conf if conf else ""
    return '<form method=post class=i%s>%s<button class=%s>%s</button></form>' % (oc, hid(action=action, **kw), cls, label)
def box(body, btn="Save"): return '<form method=post class=card>%s<button class=b>%s</button></form>' % (body, btn)
def tbl(head, body): return "<div class=t><table><tr>%s</tr>%s</table></div>" % ("".join("<th>%s</th>" % h for h in head), "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r) for r in body))
def edit(page, i): return '<a class=b2 href="/admin/%s?edit=%s">Edit</a>' % (page, i)

def v_dashboard():
    o = rows("orders")
    st = [("Orders", len(o)), ("Completed", sum(1 for x in o if x.get("status") == "completed")), ("Pending", sum(1 for x in o if x.get("status") == "pending")), ("Packages", len(rows("packages")))]
    return "<h2>Dashboard</h2><div class=g>" + "".join("<div class=card><small>%s</small><h2>%s</h2></div>" % s for s in st) + "</div>"

def v_packages():
    e = one("packages", request.args["edit"]) if request.args.get("edit") else None
    e = e or {}
    form = box(hid(action="save_package", pkg_id=e.get("id", "")) + "<div class=g>" + fld("name", "Package Name", e.get("name", "")) + fld("command", "Command (without /)", e.get("command", "")) +
               fld("price", "Price", e.get("price", ""), "number") + fld("old_price", "Old Price", e.get("old_price", ""), "number") + "</div>" + fld("description", "Description", e.get("description", "")) +
               chk("uid_check", "UID validation (6-12 digits)", e.get("uid_check", True)) + chk("active", "Active", e.get("active", True)) + area("codes", "Add codes (one per line)"))
    cs = ""
    if e:
        cs = "<div class=card><b>Codes in stock</b>" + tbl(["Code", ""], [[esc(c["code"]), act("delete_code", "Delete", "b3", "Delete this code?", code_id=c["id"])] for c in sorted(codes(e["id"]), key=lambda c: c["seq"])]) + "</div>"
    rs = [[esc(p["name"]), esc(p.get("command", "")), "%g" % p["price"], len(codes(p["id"])), "Active" if p.get("active") else "Off",
           edit("packages", p["id"]) + act("toggle_package", "On/Off", pkg_id=p["id"]) + act("delete_package", "Delete", "b3", "Delete package and all its codes?", pkg_id=p["id"])] for p in packages(False)]
    return "<h2>Packages</h2>" + form + cs + tbl(["Name", "Command", "Price", "Codes", "Status", ""], rs)

def h_packages(f):
    a = f.get("action")
    if a == "save_package":
        d = {"name": f["name"].strip(), "price": float(f.get("price") or 0), "old_price": float(f.get("old_price") or 0), "command": f.get("command", "").strip().lstrip("/"),
             "description": f.get("description", "").strip(), "uid_check": "uid_check" in f, "active": "active" in f}
        pid = save("packages", f.get("pkg_id") or None, d)
        for line in f.get("codes", "").splitlines():
            if line.strip():
                save("codes", None, {"package_id": pid, "code": line.strip(), "seq": next_id("codeseq")})
        return "Package saved."
    if a == "delete_package":
        for c in codes(f["pkg_id"]):
            drop("codes", c["id"])
        drop("packages", f["pkg_id"])
        return "Package deleted."
    if a == "toggle_package":
        p = one("packages", f["pkg_id"])
        save("packages", p["id"], {"active": not p.get("active")})
    if a == "delete_code":
        drop("codes", f["code_id"])

def v_numbers():
    e = (one("numbers", request.args["edit"]) if request.args.get("edit") else None) or {}
    form = box(hid(action="save_number", num_id=e.get("id", "")) + "<div class=g>" + sel("method", "Method", [(x, x) for x in ("bKash", "Nagad", "Rocket")], e.get("method")) +
               sel("type", "Type", [(x, x) for x in ("Personal", "Agent", "Merchant")], e.get("type")) + "</div>" + fld("number", "Number", e.get("number", ""), ph="01XXXXXXXXX"))
    return "<h2>Payment Numbers</h2>" + form + tbl(["Method", "Type", "Number", ""], [[n["method"], n["type"], esc(n["number"]), edit("numbers", n["id"]) + act("delete_number", "Delete", "b3", "Delete?", num_id=n["id"])] for n in rows("numbers")])

def h_numbers(f):
    if f.get("action") == "save_number":
        save("numbers", f.get("num_id") or None, {"method": f["method"], "type": f["type"], "number": f["number"].strip()})
        return "Number saved."
    if f.get("action") == "delete_number":
        drop("numbers", f["num_id"])

def v_commands():
    e = (one("commands", request.args["edit"]) if request.args.get("edit") else None) or {}
    types = [("text", "Text Reply"), ("menu", "Show Package Menu"), ("order", "Direct Order (specific package)"), ("orders", "My Orders History"), ("paymentnumbers", "Payment Numbers")]
    form = box(hid(action="save_command", cmd_id=e.get("id", "")) + "<div class=g>" + fld("command", "Command (without /)", e.get("command", ""), ph="support") + sel("type", "Type", types, e.get("type")) +
               sel("pkg_id", "Package (for Direct Order)", [(p["id"], p["name"]) for p in packages(False)], e.get("pkg_id")) + "</div>" +
               area("message", "Message (reply text, or intro line for other types)", e.get("message", "")) + chk("active", "Active", e.get("active", True)))
    return "<h2>Commands</h2>" + form + tbl(["Command", "Type", "Message", "Status", ""], [["/" + esc(c["command"]), c["type"], esc(c.get("message", ""))[:60], "Active" if c.get("active") else "Off",
                                                                                      edit("commands", c["id"]) + act("delete_command", "Delete", "b3", "Delete?", cmd_id=c["id"])] for c in rows("commands")])

def h_commands(f):
    if f.get("action") == "save_command":
        t = f["type"]
        save("commands", f.get("cmd_id") or None, {"command": f["command"].strip().lstrip("/").lower(), "type": t, "message": f.get("message", "").strip(),
                                                    "pkg_id": f.get("pkg_id") if t == "order" else None, "active": "active" in f})
        return "Command saved."
    if f.get("action") == "delete_command":
        drop("commands", f["cmd_id"])

def v_orders():
    flt = request.args.get("status", "all")
    os_ = sorted(rows("orders"), key=lambda o: int(o["id"]), reverse=True)
    os_ = [o for o in os_ if flt == "all" or o.get("status") == flt][:100]
    links = " ".join('<a class=b2 href="/admin/orders?status=%s">%s</a>' % (s, s) for s in ("all", "pending", "processing", "completed", "rejected"))
    rs = []
    for o in os_:
        ac = ""
        if o.get("status") in ("pending", "processing"):
            ac = '<form method=post class=i>%s<input name=manual_code placeholder="code (optional)" style="width:130px"> <button class=b>Approve</button></form>' % hid(action="approve_order", order_id=o["id"]) + act("reject_order", "Reject", "b3", order_id=o["id"])
        rs.append(["#" + o["id"], esc(o.get("username")), esc(o.get("package_name")), "%g" % o.get("price", 0), esc(o.get("uid")), esc(o.get("trx_id")), STATUS.get(o.get("status"), o.get("status")), esc(o.get("code")), ac])
    return "<h2>Orders</h2><p>" + links + "</p>" + tbl(["ID", "User", "Package", "Price", "UID", "TrxID", "Status", "Code", ""], rs)

def h_orders(f):
    o = one("orders", f.get("order_id", "x"))
    if not o:
        return
    if f["action"] == "approve_order":
        code = f.get("manual_code", "").strip() or "ম্যানুয়াল ডেলিভারি সম্পন্ন"
        save("orders", o["id"], {"status": "completed", "code": code, "method": "manual"})
        if o.get("chat_id"):
            send(o["chat_id"], "আপনার অর্ডার #%s ম্যানুয়ালি সম্পন্ন করা হয়েছে।\n\nকোড/রেফারেন্স: %s" % (o["id"], esc(code)))
        return "Order #%s approved." % o["id"]
    if f["action"] == "reject_order":
        save("orders", o["id"], {"status": "rejected"})
        if o.get("chat_id"):
            send(o["chat_id"], "দুঃখিত, আপনার অর্ডার #%s বাতিল করা হয়েছে। সাহায্যের জন্য এডমিনের সাথে যোগাযোগ করুন।" % o["id"])
        return "Order #%s rejected." % o["id"]

def v_settings():
    return "<h2>Settings</h2>" + box(fld("bot_token", "Bot Token", cfg("bot_token")) + fld("admin_id", "Admin Telegram ID", cfg("admin_id")) + fld("api_url", "Auto Topup API URL", cfg("api_url")) +
                                     fld("api_key", "Auto Topup API Key", cfg("api_key")) + area("order_template", "Order received message (use {order_id} {package} {price} {uid} {trx_id})", cfg("order_template") or TPL, 7) +
                                     fld("new_password", "New admin password (empty = unchanged)", "", "password") + fld("new_password_confirm", "Confirm password", "", "password")) + \
        '<div class=card><b>Telegram webhook</b><p><small>Save the bot token first, then press the button once (and again if your Render URL changes).</small></p>%s</div>' % act("webhook", "Set Webhook", "b")

def h_settings(f):
    if f.get("action") == "webhook":
        base = os.environ.get("RENDER_EXTERNAL_URL") or request.url_root.rstrip("/").replace("http://", "https://")
        r = tg("setWebhook", url=base + "/tg", secret_token=SECRET)
        return "Webhook: " + str(r.get("description") or r.get("ok"))
    for k in ("bot_token", "admin_id", "api_url", "api_key", "order_template"):
        cfg_set(k, f.get(k, "").strip())
    if f.get("new_password"):
        if f["new_password"] != f.get("new_password_confirm"):
            return "Settings saved, but the passwords did not match."
        cfg_set("admin_password_hash", generate_password_hash(f["new_password"]))
    return "Settings saved."

def v_console():
    ls = C("logs").order_by("ts", direction=firestore.Query.DESCENDING).limit(100).stream()
    return "<h2>Bot Console</h2>" + tbl(["Time (UTC)", "Type", "Message"], [[d.get("ts").strftime("%m-%d %H:%M:%S"), d.get("type"), esc(d.get("msg"))] for d in ls])

PAGES = {"dashboard": (v_dashboard, None), "packages": (v_packages, h_packages), "numbers": (v_numbers, h_numbers), "commands": (v_commands, h_commands),
         "orders": (v_orders, h_orders), "settings": (v_settings, h_settings), "console": (v_console, None)}

@app.route("/admin", methods=["GET", "POST"])
@app.route("/admin/<page>", methods=["GET", "POST"])
def admin(page="dashboard"):
    if page == "logout":
        session.clear()
        return "", 302, {"Location": "/admin"}
    if not session.get("ok"):
        err = ""
        if request.method == "POST":
            pw, h = request.form.get("password", ""), cfg("admin_password_hash")
            if check_password_hash(h, pw) if h else hmac.compare_digest(pw.encode(), ADMIN_PASS.encode()):
                session["ok"] = True
                return "", 302, {"Location": "/admin"}
            err = "Wrong password."
        return "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><style>%s</style><main style='max-width:340px;margin-top:18vh'><form method=post class=card><h2>Admin Login</h2>%s<button class=b style='width:100%%'>Sign in</button><p style=color:#f87171>%s</p></form></main>" % (CSS, fld("password", "Password", "", "password"), err)
    if page not in PAGES:
        abort(404)
    msg = ""
    if request.method == "POST" and PAGES[page][1]:
        msg = PAGES[page][1](request.form) or ""
    return layout(page, PAGES[page][0](), msg)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
