import asyncio, csv, hashlib, io, json, logging, os, re, secrets, time
from contextlib import asynccontextmanager
import jwt
from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .core import config
from .core.config import cfg
from .services import store
from .services.game_engine import Team, Player, engine as E, InvalidTransition, ev

AUD = {}   # audience accounts: login name -> salted password hash
logging.basicConfig(level=logging.INFO, format="%(message)s")

@asynccontextmanager
async def lifespan(_):
    try: await store.init(); await store.load_into(E); AUD.update(await store.audience_all())
    except Exception: logging.getLogger("arena").error("DATABASE UNREACHABLE at startup, running in memory only and retrying: %s | %s", store.health["error"], store.health["hint"])
    tasks = [asyncio.create_task(E.run()), asyncio.create_task(store.flusher(E))]
    yield
    for t in tasks: t.cancel()
    await store.flush(E)

app = FastAPI(title="AI Calculator Arena", lifespan=lifespan)
@app.middleware("http")
async def no_cache(request, call_next):   # the browser must never cache pages or API answers
    r = await call_next(request); r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"; r.headers["Pragma"] = "no-cache"; return r
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])

def err(msg, code): return JSONResponse({"error": msg}, status_code=code)
def mint(**c): return jwt.encode({**c, "exp": time.time() + 12 * 3600}, config.JWT_SECRET, algorithm="HS256")
def decode(tok):
    try: return jwt.decode(tok, config.JWT_SECRET, algorithms=["HS256"])
    except Exception: return None

class Reg(BaseModel):
    team_name: str = Field(min_length=1, max_length=30); name: str = Field(min_length=1, max_length=20)
    roll: str = Field(min_length=1, max_length=30); phone: str = Field("", max_length=20)
class Join(BaseModel):
    code: str = Field(max_length=8); name: str = Field(min_length=1, max_length=20)
    roll: str = Field(min_length=1, max_length=30); phone: str = Field("", max_length=20)
class Login(BaseModel): pass_: str = Field(alias="pass")

def clean_phone(p):   # India: 10-digit mobile starting 6-9 (a +91 / 91 / 0 prefix is tolerated); returns +91XXXXXXXXXX or None
    p = re.sub(r"[\s\-().]", "", p or "")
    for pre in ("+91", "0091", "91", "0"):
        if p.startswith(pre) and len(p) - len(pre) == 10: p = p[len(pre):]; break
    return "+91" + p if re.fullmatch(r"[6-9]\d{9}", p) else None
def find_roll(roll): return next(((t, p) for t in E.teams.values() for p in t.players if p.roll.lower() == roll.lower()), None)

# ---------- players ----------
@app.post("/api/register")                                       # the team LEADER creates the team and receives the team ID (code)
async def register(b: Reg):
    tn, name, roll, phone = b.team_name.strip(), b.name.strip(), b.roll.strip(), (clean_phone(b.phone) if b.phone.strip() else "")
    if not (tn and name and roll): return err("Please fill in every field", 422)
    if b.phone.strip() and not phone: return err("Enter a valid 10-digit Indian mobile number", 422)
    if any(t.name.lower() == tn.lower() for t in E.teams.values()): return err("That team name is already taken", 409)
    if find_roll(roll): return err("This roll number is already registered", 409)
    code = secrets.token_hex(3).upper()
    while any(t.code == code for t in E.teams.values()): code = secrets.token_hex(3).upper()
    t = Team("T" + secrets.token_hex(2).upper(), tn, code); p = Player("P" + secrets.token_hex(2), name, roll, phone)
    t.players.append(p); t.roles["X"] = p.id; t.leader = p.id; E.teams[t.id] = t
    ev("team_registered", team_id=t.id, player_id=p.id); await E.push(t)
    return {"token": mint(sub=p.id, team=t.id, role="PLAYER"), "team_id": t.id, "code": code}

@app.post("/api/join")                                           # teammates (and a returning leader) log in with the team ID + their own details
async def join(b: Join):
    t = next((x for x in E.teams.values() if x.code == b.code.strip().upper()), None)
    if not t: return err("Unknown team ID", 404)
    name, roll, phone = b.name.strip(), b.roll.strip(), (clean_phone(b.phone) if b.phone.strip() else "")
    if not (name and roll): return err("Please fill in every field", 422)
    if b.phone.strip() and not phone: return err("Enter a valid 10-digit Indian mobile number", 422)
    p = next((x for x in t.players if x.roll.lower() == roll.lower()), None)
    if p:                                                        # same roll number = reconnect to the same seat, but only with matching details
        if p.name.lower() != name.lower() or (p.phone and clean_phone(p.phone) != phone): return err("These details do not match the registration for this roll number" + (" (this player registered with a phone number: enter it too)" if p.phone and not phone else ""), 403)
    else:
        if find_roll(roll): return err("This roll number is already registered in a team", 409)
        if len(t.players) >= 3: return err("Team already has 3 players", 409)
        p = Player("P" + secrets.token_hex(2), name, roll, phone); t.players.append(p); t.roles["XYZ"[len(t.players) - 1]] = p.id
        ev("team_joined", team_id=t.id, player_id=p.id)
        if len(t.players) == 3 and t.phase == "LOBBY": await E.next(t)
        else: await E.push(t)
    return {"token": mint(sub=p.id, team=t.id, role="PLAYER"), "team_id": t.id}

@app.get("/api/team/{tid}/question")
async def my_question(tid: str, authorization: str = Header("")):
    c = decode(authorization.removeprefix("Bearer "))
    if not c or c.get("role") != "PLAYER": return err("unauthorized", 401)
    if c["team"] != tid: return err("forbidden", 403)           # Team A can never read Team B
    return E.view(E.teams[tid], c["sub"])["question"] if tid in E.teams else err("not found", 404)

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = ""):
    await ws.accept(); c = decode(token)
    t = c and c.get("role") == "PLAYER" and E.teams.get(c["team"]); p = t and next((x for x in t.players if x.id == c["sub"]), None)
    if not p: return await ws.close(4401)
    if p.ws:                                                       # one active session per player
        try: await p.ws.close(4401)
        except Exception: pass
    p.ws = ws; ev("player_connected", team_id=t.id, player_id=p.id); await E.push(t)   # reconnect restores role/question/time
    try:
        while True:
            try: m = json.loads(await ws.receive_text())
            except json.JSONDecodeError: continue
            if isinstance(m, dict):
                try: await E.handle(t, p, m)
                except InvalidTransition: pass
    except WebSocketDisconnect: pass
    finally:
        if p.ws is ws: p.ws = None; ev("player_disconnected", team_id=t.id, player_id=p.id); await E.push(t)

# ---------- admin (JWT role=ADMIN in x-admin header) ----------
def is_admin(tok): c = decode(tok or ""); return bool(c and c.get("role") == "ADMIN")
@app.post("/api/admin/login")
async def admin_login(b: Login):
    return {"token": mint(sub="admin", role="ADMIN")} if secrets.compare_digest(b.pass_, config.ADMIN_PASS) else err("bad password", 401)
@app.get("/api/admin/config")
async def get_cfg(x_admin: str = Header("")): return cfg if is_admin(x_admin) else err("unauthorized", 401)
@app.put("/api/admin/config")
async def put_cfg(body: dict, x_admin: str = Header("")):
    if not is_admin(x_admin): return err("unauthorized", 401)
    for k, v in body.items():
        if k in cfg: cfg[k] = {int(a): b for a, b in v.items()} if isinstance(v, dict) else v   # JSON keys arrive as strings
    ev("admin_action", action="config"); return cfg
@app.post("/api/admin/team/{tid}/next")
async def force_next(tid: str, x_admin: str = Header("")):
    t = E.teams.get(tid)
    if not is_admin(x_admin): return err("unauthorized", 401)
    if not t or len(t.players) < 3: return err("team not ready", 400)
    try: await E.next(t)
    except InvalidTransition: return err("team finished", 409)
    ev("admin_action", action="next", team_id=tid); return Response(status_code=204)
@app.delete("/api/admin/team/{tid}")
async def delete_team(tid: str, x_admin: str = Header("")):
    if not is_admin(x_admin): return err("unauthorized", 401)
    t = E.teams.pop(tid, None)
    if not t: return err("team not found", 404)
    E.dirty.discard(tid)
    for p in t.players:                                          # kick anyone still connected; their token is now useless
        if p.ws:
            try: await p.ws.close(4401)
            except Exception: pass
    await store.delete_team(tid); ev("admin_action", action="delete_team", team_id=tid); return Response(status_code=204)
@app.delete("/api/admin/data")                                   # wipes all teams, players, scores and questions; audience accounts and settings are kept
async def wipe_data(x_admin: str = Header("")):
    if not is_admin(x_admin): return err("unauthorized", 401)
    n = len(E.teams)
    for t in list(E.teams.values()):
        for p in t.players:
            if p.ws:
                try: await p.ws.close(4401)
                except Exception: pass
    E.teams.clear(); E.dirty.clear(); E.gen.used.clear()
    try: await store.wipe()
    except Exception as e: logging.getLogger("arena").error("wipe failed: %s", store._redact(e)); return err("The server memory was cleared, but the database is not connected, so its rows were NOT deleted. Fix DATABASE_URL (see the red notice on this page)", 500)
    try:
        if os.path.exists(config.QUESTION_LOG): open(config.QUESTION_LOG, "w").close()      # the question log is part of the old data too
    except OSError: pass
    ev("admin_action", action="wipe_data", teams=n); return {"deleted_teams": n}
@app.get("/api/admin/state")
async def state(x_admin: str = Header("")):
    if not is_admin(x_admin): return err("unauthorized", 401)
    ts = list(E.teams.values())
    rows = [{"id": t.id, "name": t.name, "code": t.code, "phase": t.phase, "score": t.score, "remaining": t.remaining(), "values": t.values, "log": t.log,
             "paused": t.paused, "solved": t.solved, "minutes": t.minutes, "rank": E.ranked().index(t) + 1, "players": [{"name": p.name, "roll": p.roll, "phone": p.phone, "leader": p.id == t.leader, "online": p.ws is not None, "role": t.role_of(p.id)} for p in t.players],
             "question": t.q and {"expression": t.q.display, "target": t.q.target, "difficulty": t.q.level, "constraint": t.q.con,
                                   "solutions": len(t.q.solutions), "seed": t.q.seed, "id": t.q.id}} for t in ts]
    return {"total": len(ts), "online": sum(any(p.ws for p in t.players) for t in ts),
            "playing": sum(t.phase in ("ASSIGN", "COUNTDOWN", "PLAYING", "RESULT") for t in ts),
            "finished": sum(t.phase == "FINISHED" for t in ts), "db": store.health, "questions": len(cfg["sequence"]), "teams": sorted(rows, key=lambda r: -r["score"])}

# ---------- leaderboard (admin only), downloads, end of game ----------
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8); return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 150_000).hex()
def check_pw(pw, stored): return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)
def safe(v): v = str(v); return "'" + v if v and v[0] in "=+-@" else v          # stop spreadsheet formula injection from team names
def lb_rows():
    return [{"rank": i, "team": t.name, "id": t.id, "score": t.score, "solved": t.solved, "total": len(cfg["sequence"]), "minutes": t.minutes, "status": t.phase,
             "members": ", ".join(p.name for p in t.players), "leader": next((p.name for p in t.players if p.id == t.leader), "")} for i, t in enumerate(E.ranked(), 1)]
@app.get("/api/admin/leaderboard")
async def lb(x_admin: str = Header("")): return lb_rows() if is_admin(x_admin) else err("unauthorized", 401)
@app.get("/api/admin/leaderboard.csv")
async def lb_csv(x_admin: str = Header("")):
    if not is_admin(x_admin): return err("unauthorized", 401)
    buf = io.StringIO(); w = csv.writer(buf); keys = ("rank", "team", "id", "score", "solved", "total", "minutes", "status", "members", "leader")
    w.writerow(["Rank", "Team", "Team ID", "Score", "Questions solved", "Total questions", "Minutes played", "Status", "Members", "Leader"])
    for r in lb_rows(): w.writerow([safe(r[k]) for k in keys])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="leaderboard.csv"'})
@app.get("/api/admin/leaderboard.pdf")
async def lb_pdf(x_admin: str = Header("")):
    if not is_admin(x_admin): return err("unauthorized", 401)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    buf, st = io.BytesIO(), getSampleStyleSheet(); doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    data = [["Rank", "Team", "Score", "Solved", "Minutes", "Status", "Members"]] + [[r["rank"], r["team"], r["score"], f'{r["solved"]}/{r["total"]}', r["minutes"], r["status"], r["members"]] for r in lb_rows()]
    tb = Table(data, repeatRows=1); tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2a52")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), .25, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2ff")]), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    doc.build([Paragraph("AI Calculator Arena: Leaderboard", st["Title"]), Paragraph(time.strftime("Generated %d %b %Y, %H:%M UTC", time.gmtime()), st["Normal"]), Spacer(1, 10), tb])
    return Response(buf.getvalue(), media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="leaderboard.pdf"'})

@app.post("/api/admin/game/end")
async def end_game(x_admin: str = Header("")):
    if not is_admin(x_admin): return err("unauthorized", 401)
    n = 0
    for t in list(E.teams.values()):
        if t.phase in ("ASSIGN", "COUNTDOWN", "PLAYING", "RESULT"):
            await E.end_team(t); n += 1
    ev("admin_action", action="end_game", teams=n); return {"ended": n}
# ---------- audience accounts ----------
class AudLogin(BaseModel): email: str = Field(max_length=80); password: str = Field(max_length=100)
class AudNew(BaseModel): email: str = Field(max_length=80); password: str = Field(min_length=6, max_length=100)
@app.post("/api/audience/login")
async def aud_login(b: AudLogin):
    e = b.email.strip().lower(); h = AUD.get(e)
    return {"token": mint(sub=e, role="AUDIENCE"), "email": e} if h and check_pw(b.password, h) else err("Wrong email or password", 401)
@app.get("/api/audience/home")                                   # event numbers only: the leaderboard is not shown to the audience
async def aud_home(authorization: str = Header("")):
    c = decode(authorization.removeprefix("Bearer "))
    if not c or c.get("role") != "AUDIENCE": return err("unauthorized", 401)
    ts = list(E.teams.values())
    return {"teams": len(ts), "players": sum(len(t.players) for t in ts), "playing": sum(t.phase in ("ASSIGN", "COUNTDOWN", "PLAYING") for t in ts), "finished": sum(t.phase == "FINISHED" for t in ts), "questions": len(cfg["sequence"])}
@app.get("/api/admin/audience")
async def aud_list(x_admin: str = Header("")): return sorted(AUD) if is_admin(x_admin) else err("unauthorized", 401)
@app.post("/api/admin/audience")
async def aud_new(b: AudNew, x_admin: str = Header("")):
    e = b.email.strip().lower()
    if not is_admin(x_admin): return err("unauthorized", 401)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e): return err("Enter a valid email address", 422)
    AUD[e] = hash_pw(b.password); await store.audience_save(e, AUD[e]); return {"ok": True}
@app.delete("/api/admin/audience/{email}")
async def aud_del(email: str, x_admin: str = Header("")):
    if not is_admin(x_admin): return err("unauthorized", 401)
    AUD.pop(email.lower(), None); await store.audience_delete(email.lower()); return Response(status_code=204)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
