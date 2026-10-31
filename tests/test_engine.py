import concurrent.futures
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Engine, GameError
from puzzle_helpers import answer_for


@pytest.fixture
def game(tmp_path):
    clock = [10000.0]
    g = Engine(tmp_path / "game.db", clock=lambda: clock[0])
    g.test_clock = clock
    for uid in ("host", "1", "2", "3", "4", "5"):
        g.register(uid, "Игрок " + uid)
    code = g.create_room("host", "Тестовая сессия")["code"]
    for n in range(1, 6):
        g.join(code, str(n))
    stations = [g.station("Станция " + k, k)["code"] for k in ("wires", "sequence", "tuning")]
    g.configure(code, "host", {"stations": stations, "tasks": 2, "discussion": 10, "voting": 15})
    g.start(code, "host")
    g.test_clock[0] += 61
    g.code = code
    yield g
    g.db.close()


def roles(g):
    ps = g.room(g.code)["players"]
    return next(p for p in ps.values() if p["role"] == "impostor"), [p for p in ps.values() if p["role"] == "crew"]


def solve(g, uid, station):
    while True:
        attempt = g.task_open(g.code, uid, station)
        answer = answer_for(g, attempt)
        if attempt['stage'] + 1 >= attempt['stage_count']:
            return attempt, answer
        g.task_finish(g.code, uid, station, attempt['id'], attempt['epoch'], answer)


def arrive_all(g):
    for pid, p in list(g.room(g.code)['players'].items()):
        if p['status'] == 'alive':
            g.meeting_join(g.code, pid)



def test_assignments_and_role_secrecy(game):
    g = game
    imp, crew = roles(g)
    assert len(crew) == 4
    assert all(len(p["tasks"]) == (2 if p["role"] == "crew" else 0) for p in g.room(g.code)["players"].values())
    view = g.view(g.code, crew[0]["id"])
    assert "events" not in view
    assert all("role" not in p and "tasks" not in p and "badge" not in p for p in view["players"] if p["id"] != crew[0]["id"])
    assert all("puzzle" not in t for t in view["me"]["tasks"])
    assert g.view(g.code, "host")["admin"]


def test_completion_is_single_use_and_persistent(game, tmp_path):
    g = game
    _, crew = roles(g)
    p = crew[0]
    station = next(iter(p["tasks"]))
    attempt, answer = solve(g, p["id"], station)
    g.task_finish(g.code, p["id"], station, attempt["id"], attempt["epoch"], answer)
    assert g.progress(g.room(g.code))["done"] == 1
    with pytest.raises(GameError, match="уже выполнено"):
        g.task_finish(g.code, p["id"], station, attempt["id"], attempt["epoch"], answer)
    with pytest.raises(GameError, match="уже выполнено"):
        g.task_open(g.code, p["id"], station)
    other = Engine(g.db.execute("PRAGMA database_list").fetchone()[2])
    assert other.progress(other.room(g.code))["done"] == 1
    assert other.state["users"][p["id"]]["badge"] == g.state["users"][p["id"]]["badge"]
    other.db.close()


def test_unassigned_and_wrong_answers(game):
    g = game
    _, crew = roles(g)
    p = crew[0]
    not_assigned = next(s for s in g.room(g.code)["stations"] if s not in p["tasks"])
    with pytest.raises(GameError, match="не входит"):
        g.task_open(g.code, p["id"], not_assigned)
    station = next(iter(p["tasks"]))
    attempt, _ = solve(g, p["id"], station)
    with pytest.raises(GameError):
        g.task_finish(g.code, p["id"], station, attempt["id"], attempt["epoch"], [99])
    assert g.progress(g.room(g.code))["done"] == 0


def test_concurrent_completion_adds_one_point(game):
    g = game
    _, crew = roles(g)
    pid = crew[0]["id"]
    station = next(iter(crew[0]["tasks"]))
    attempt, answer = solve(g, pid, station)
    def finish(_):
        try:
            g.task_finish(g.code, pid, station, attempt["id"], attempt["epoch"], answer)
            return True
        except GameError:
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(finish, range(5))) == 1
    assert g.progress(g.room(g.code))["done"] == 1


def test_impostor_has_no_tasks_and_cannot_use_crew_endpoints(game):
    g = game
    imp, crew = roles(g)
    assert imp['tasks'] == {}
    station = next(iter(crew[0]['tasks']))
    attempt, answer = solve(g, crew[0]['id'], station)
    for fn in (lambda: g.task_open(g.code, imp['id'], station),
               lambda: g.inspect_scan(g.code, imp['id'], 's_' + station),
               lambda: g.task_finish(g.code, imp['id'], station, attempt['id'], attempt['epoch'], answer)):
        with pytest.raises(GameError, match='Предатели'):
            fn()
    assert g.view(g.code, imp['id'])['me']['tasks'] == []
    assert g.progress(g.room(g.code))['done'] == 0


def test_kill_permissions_cooldown_and_hidden_body(game):
    g = game
    imp, crew = roles(g)
    badge = g.state["users"][crew[0]["id"]]["badge"]
    with pytest.raises(GameError):
        g.kill(g.code, crew[1]["id"], badge)
    g.kill(g.code, imp["id"], badge)
    victim = g.room(g.code)["players"][crew[0]["id"]]
    assert victim["status"] == "dead"
    view = g.view(g.code, crew[1]["id"])
    assert next(p for p in view["players"] if p["id"] == victim["id"])["status"] == "unknown"
    with pytest.raises(GameError, match="Перезарядка"):
        g.kill(g.code, imp["id"], g.state["users"][crew[2]["id"]]["badge"])
    g.report(g.code, crew[1]["id"], badge)
    assert g.room(g.code)['phase'] == 'gathering'
    arrive_all(g)
    assert g.room(g.code)["phase"] == "discussion"
    assert g.room(g.code)["players"][victim["id"]]["known_dead"]
    with pytest.raises(GameError):
        g.kill(g.code, imp["id"], badge)
    assert g.db.execute("SELECT count(*) FROM outbox WHERE uid=? AND text LIKE '%Вас убили%'", (victim["id"],)).fetchone()[0] == 1


def test_cannot_report_alive_or_act_as_ghost(game):
    g = game
    imp, crew = roles(g)
    badge = g.state["users"][crew[0]["id"]]["badge"]
    with pytest.raises(GameError, match="нет доступного"):
        g.report(g.code, crew[1]["id"], badge)
    g.kill(g.code, imp["id"], badge)
    with pytest.raises(GameError, match="Погибшие"):
        g.emergency(g.code, crew[0]["id"])
    # Ghost tasks remain possible and still count for crew victory.
    station = next(iter(g.room(g.code)["players"][crew[0]["id"]]["tasks"]))
    attempt, answer = solve(g, crew[0]["id"], station)
    g.task_finish(g.code, crew[0]["id"], station, attempt["id"], attempt["epoch"], answer)
    assert g.progress(g.room(g.code))["done"] == 1


def test_pause_freezes_meeting_and_cooldown(game):
    g = game
    imp, crew = roles(g)
    g.emergency(g.code, crew[0]["id"])
    arrive_all(g)
    r = g.room(g.code)
    remaining = r["meeting"]["deadline"] - g.clock()
    g.control(g.code, "host", "pause")
    g.test_clock[0] += 1000
    g.tick()
    assert g.room(g.code)["phase"] == "discussion"
    with pytest.raises(GameError):
        g.vote(g.code, imp["id"], r["meeting"]["id"], "skip")
    g.control(g.code, "host", "resume")
    assert g.room(g.code)["meeting"]["deadline"] - g.clock() == remaining
    g.test_clock[0] += remaining + 1
    g.tick()
    assert g.room(g.code)["phase"] == "voting"
    g.test_clock[0] += 16
    g.tick()
    assert g.room(g.code)["phase"] == "running"


def test_voting_tie_skip_single_vote_and_crew_win(game):
    g = game
    imp, crew = roles(g)
    g.emergency(g.code, crew[0]["id"])
    arrive_all(g)
    g.control(g.code, "host", "next_phase")
    mid = g.room(g.code)["meeting"]["id"]
    g.vote(g.code, crew[0]["id"], mid, imp["id"])
    with pytest.raises(GameError, match="уже принят"):
        g.vote(g.code, crew[0]["id"], mid, "skip")
    for p in crew[1:] + [imp]:
        g.vote(g.code, p["id"], mid, imp["id"])
    assert g.room(g.code)["winner"] == "crew"
    assert g.room(g.code)["phase"] == "ended"


def test_undo_restores_whole_state_and_invalidates_attempt(game):
    g = game
    imp, crew = roles(g)
    pid = crew[0]["id"]
    station = next(iter(crew[0]["tasks"]))
    attempt, answer = solve(g, pid, station)
    g.kill(g.code, imp["id"], g.state["users"][pid]["badge"])
    g.undo(g.code, "host")
    r = g.room(g.code)
    assert r["paused_at"] is not None and r["players"][pid]["status"] == "alive"
    g.control(g.code, "host", "resume")
    with pytest.raises(GameError, match="устарело"):
        g.task_finish(g.code, pid, station, attempt["id"], attempt["epoch"], answer)


def test_admin_rights_and_session_isolation(game):
    g = game
    _, crew = roles(g)
    pid = crew[0]["id"]
    with pytest.raises(GameError):
        g.control(g.code, pid, "pause")
    g.control(g.code, "host", "grant_admin", {"player": pid})
    assert g.view(g.code, pid)["admin"]
    g.control(g.code, pid, "pause")
    with pytest.raises(GameError, match="создатель"):
        g.control(g.code, pid, "grant_admin", {"player": crew[1]["id"]})
    other = g.create_room("host", "Вторая")["code"]
    with pytest.raises(GameError, match="другой"):
        g.join(other, pid)
    with pytest.raises(GameError):
        g.view(other, pid)


def test_paused_player_controls_and_task_correction(game):
    g = game
    _, crew = roles(g)
    pid = crew[0]["id"]
    station = next(iter(crew[0]["tasks"]))
    with pytest.raises(GameError, match="паузу"):
        g.control(g.code, "host", "eject", {"player": pid})
    g.control(g.code, "host", "pause")
    g.control(g.code, "host", "eject", {"player": pid})
    g.control(g.code, "host", "revive", {"player": pid})
    g.control(g.code, "host", "task_reset", {"player": pid, "station": station, "done": True})
    assert g.progress(g.room(g.code))["done"] == 1
    g.control(g.code, "host", "task_reset", {"player": pid, "station": station, "done": False})
    assert g.progress(g.room(g.code))["done"] == 0


def test_stations_and_badges_survive_new_session(game):
    g = game
    uid = "1"
    badge = g.state["users"][uid]["badge"]
    stations = copy.deepcopy(g.state["stations"])
    g.control(g.code, "host", "end")
    code = g.create_room("host", "Следующая")["code"]
    g.join(code, uid)
    assert g.state["users"][uid]["badge"] == badge
    assert g.state["stations"] == stations


def test_invalid_config_rolls_back(game):
    g = game
    r = g.create_room("host", "Лобби")
    before = copy.deepcopy(g.room(r["code"]))
    with pytest.raises(GameError):
        g.configure(r["code"], "host", {"name": "Изменено", "tasks": -1})
    assert g.room(r["code"]) == before


def test_task_victory_includes_ghost_tasks(game):
    g = game
    _, crew = roles(g)
    for p in crew:
        for station in p["tasks"]:
            attempt, answer = solve(g, p["id"], station)
            g.task_finish(g.code, p["id"], station, attempt["id"], attempt["epoch"], answer)
    assert g.room(g.code)["winner"] == "crew"


def test_impostor_parity_victory(game):
    g = game
    imp, crew = roles(g)
    for p in crew[:3]:
        g.kill(g.code, imp["id"], g.state["users"][p["id"]]["badge"])
        g.test_clock[0] += 61
    assert g.room(g.code)["phase"] == "ended"
    assert g.room(g.code)["winner"] == "impostor"


def test_repeated_join_is_noop_and_does_not_mask_undo(game):
    g = game
    imp, crew = roles(g)
    pid = crew[0]["id"]
    g.kill(g.code, imp["id"], g.state["users"][pid]["badge"])
    before_count = g.db.execute("SELECT count(*) FROM events").fetchone()[0]
    g.join(g.code, pid)
    assert g.db.execute("SELECT count(*) FROM events").fetchone()[0] == before_count
    g.undo(g.code, "host")
    assert g.room(g.code)["players"][pid]["status"] == "alive"


def test_concurrent_kills_only_one_target(game):
    g = game
    imp, crew = roles(g)
    def kill(p):
        try:
            g.kill(g.code, imp["id"], g.state["users"][p["id"]]["badge"])
            return True
        except GameError:
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(kill, crew)) == 1
