import asyncio, os, tempfile
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
from app.services import store
from app.services.game_engine import Engine, Team, Player

def test_team_members_and_state_survive_restart():
    async def go():
        await store.init(); e = Engine(); t = Team("T1", "Ninjas", "ABC123")
        t.players = [Player("P1", "Alice", "R1", "9000000001"), Player("P2", "Bob", "R2", "9000000002"), Player("P3", "Cara", "R3", "9000000003")]; t.leader = "P1"; t.roles = {"X": "P1", "Y": "P2", "Z": "P3"}
        t.score, t.phase, t.n = 150, "ASSIGN", 1; t.q = e.gen.generate_question("T1", 1, 1, t.history); e.teams["T1"] = t
        await store.save(t); e2 = Engine(); await store.load_into(e2); r = e2.teams["T1"]
        assert [p.name for p in r.players] == ["Alice", "Bob", "Cara"] and r.score == 150 and r.leader == "P1" and r.players[0].roll == "R1" and r.players[2].phone == "9000000003" and r.q.id == t.q.id and r.q.solutions and r.history
    asyncio.run(go())

def test_wipe_removes_everything_from_the_database():
    async def go():
        await store.init(); e = Engine(); t = Team("T9", "Old", "ZZZ999"); t.players = [Player("P9", "Zed", "R9", "")]; t.leader = "P9"; t.roles = {"X": "P9"}
        t.q = e.gen.generate_question("T9", 1, 1, t.history); e.teams["T9"] = t
        await store.save(t); await store.wipe(); e2 = Engine(); await store.load_into(e2)
        assert "T9" not in e2.teams
    asyncio.run(go())

def test_database_errors_are_redacted_and_explained():
    bad = Exception('database "arena_db_bfscpostgresql://user:SECRET@host/arena_db_bfsc" does not exist')
    store._mark(False, bad)
    assert "SECRET" not in store.health["error"] and "two URLs" in store.health["hint"] and store.health["ok"] is False
    store._mark(True); assert store.health == {"ok": True, "error": "", "hint": ""}

def test_clean_url_repairs_pasted_values():
    one = "postgresql://u:p@host/arena_db"
    assert store.clean_url(one + one) == one and store.clean_url("DATABASE_URL=" + one) == one and store.clean_url(f'"{one}"') == one and store.clean_url(one + "\n" + one) == one
    assert store.clean_url("sqlite+aiosqlite:///data/arena.db") == "sqlite+aiosqlite:///data/arena.db"
