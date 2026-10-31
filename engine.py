"""Authoritative, persistent game rules. No Telegram or web dependencies."""
from __future__ import annotations

import copy
import json
import math
import random
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from task_rules import KINDS, TASKS, definition, puzzle, validate
from game_errors import GameError, require
from sabotage import SabotageMixin, SABOTAGES
from test_players import TestPlayersMixin
PHASES = {"lobby", "running", "gathering", "discussion", "voting", "ended"}


def clean_name(value, limit=60):
    value = str(value).strip()
    require(1 <= len(value) <= limit, f"Название должно содержать от 1 до {limit} символов")
    return value


def integer(value, low, high):
    require(type(value) is int and low <= value <= high, f"Нужно целое число от {low} до {high}")
    return value


class Engine(SabotageMixin, TestPlayersMixin):
    def __init__(self, path, clock=time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, room TEXT, at REAL, actor TEXT, action TEXT, before_room TEXT, undone INTEGER DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, uid TEXT, text TEXT, tries INTEGER DEFAULT 0, next_try REAL DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()
        row = self.db.execute("SELECT data FROM state WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {"stations": {}, "users": {}, "rooms": {}}
        if "meeting_code" not in self.state:
            self.state["meeting_code"] = secrets.token_urlsafe(16)
            self._save()
            self.db.commit()
        self.lock = threading.RLock()
        self.clock = clock
        if self.state.get('schema_version', 1) < 2:
            for r in self.state['rooms'].values():
                if r['settings']['cooldown'] == 30:
                    r['settings']['cooldown'] = 60
                for p in r['players'].values():
                    p.setdefault('ghost_ready', 0)
                    p.setdefault('death_id', 'legacy' if p['status'] == 'dead' else None)
                    for s, t in p['tasks'].items():
                        t.setdefault('stage', 0)
                        # An existing active round keeps its original one-panel tasks.
                        t.setdefault('stage_count', 1)
            self.state['schema_version'] = 2
            self._save()
            self.db.commit()

        self.init_sabotages()

    def ensure_catalog(self):
        """Add missing Skeld templates without changing user-created stations."""
        existing = {s['kind'] for s in self.state['stations'].values()}
        for kind, task in TASKS.items():
            if kind not in existing:
                self.station(task['name'], kind)

    def station_view(self, station):
        return copy.deepcopy(station) | {'stages': definition(station['kind'])['stages'], 'type': definition(station['kind'])['type']}

    def station_target(self, target):
        require(isinstance(target, str), 'Станция не найдена')
        if target in self.state['stations']:
            return target, None
        match = re.fullmatch(r'(.+)_p([1-4])', target)
        require(match and match[1] in self.state['stations'], 'Станция не найдена')
        station, stage = match[1], int(match[2]) - 1
        require(stage < len(definition(self.state['stations'][station]['kind'])['stages']), 'Этап станции не найден')
        return station, stage

    def new_task(self, station):
        kind = self.state['stations'][station]['kind']
        return {'id': secrets.token_urlsafe(12), 'done': False, 'puzzle': self._puzzle(kind),
                'stage': 0, 'stage_count': len(definition(kind)['stages']), 'started': None, 'ready_at': 0}

    def reset_attempt(self, station, task):
        if not task['done']:
            task.update(id=secrets.token_urlsafe(12), started=None,
                        puzzle=puzzle(self.state['stations'][station]['kind'], task.get('stage', 0)))

    def _save(self):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(self.state, ensure_ascii=False),))

    def _notifications(self, before):
        for code, r in self.state["rooms"].items():
            old = before["rooms"].get(code)
            if not old:
                continue
            broadcast = []
            old_sabotage, new_sabotage = old.get('sabotage'), r.get('sabotage')
            if new_sabotage and (not old_sabotage or new_sabotage['id'] != old_sabotage['id']):
                spec = SABOTAGES[new_sabotage['kind']]
                broadcast.append('САБОТАЖ: ' + spec['name'] + ('. На устранение 45 секунд!' if spec['duration'] else '. Система связи взломана. Отбейте атаку у серверной панели.'))
            elif old_sabotage and not new_sabotage and r['phase'] != 'ended':
                broadcast.append('Саботаж устранён. Системы восстановлены.')
            if old["phase"] != r["phase"]:
                if r["phase"] == "gathering":
                    broadcast.append(('Экстренное совещание!' if r['meeting']['reason'] == 'emergency' else 'Обнаружено тело!') + ' Пройдите в главную комнату и отсканируйте QR для совещаний. Все игровые действия заблокированы.')
                elif r["phase"] == "discussion":
                    broadcast.append("Все живые игроки собрались. Началось обсуждение. Убийства и задания приостановлены.")
                elif r["phase"] == "voting":
                    broadcast.append("Началось голосование. Откройте приложение и выберите игрока или пропуск.")
                elif r["phase"] == "running" and old["phase"] in ("discussion", "voting"):
                    result = (r["meeting"] or {}).get("result")
                    target = result and result.get("ejected")
                    broadcast.append((f"Изгнан: {r['players'][target]['name']}. " if target else "Никто не изгнан. ") + "Игра продолжается.")
                elif r["phase"] == "ended":
                    broadcast.append({"crew": "Победа экипажа!", "impostor": "Победа предателей!", "stopped": "Ведущий завершил партию."}.get(r["winner"], "Партия завершена."))
            if old["paused_at"] != r["paused_at"] and r["phase"] != "ended":
                broadcast.append("Ведущий поставил игру на паузу." if r["paused_at"] is not None else "Ведущий снял паузу.")
            for pid, p in r["players"].items():
                if not pid.isdigit():
                    continue
                prev = old["players"].get(pid, {})
                messages = list(broadcast)
                if p["role"] and prev.get("role") != p["role"]:
                    messages.append("Ваша роль: " + ("ПРЕДАТЕЛЬ. Устраняйте экипаж и избегайте разоблачения. Выполнять задания нельзя." if p["role"] == "impostor" else "ЭКИПАЖ. Выполняйте личные задания и найдите предателей."))
                if prev.get("status") == "alive" and p["status"] == "dead":
                    messages.append("Вас убили. Оставайтесь на своём месте, не раскрывайте предателя и выполняйте задания в приложении без QR. Между выполненными заданиями — 60 секунд.")
                elif prev.get("status") != p["status"] and p["status"] == "alive" and prev:
                    messages.append("Ведущий восстановил вас в игре.")
                for msg in messages:
                    self.db.execute("INSERT INTO outbox(uid,text) VALUES(?,?)", (pid, f"{r['name']} · {code}\n{msg}"))

    def _change(self, fn, room=None, actor="system", action=""):
        with self.lock:
            before = copy.deepcopy(self.state)
            try:
                result = fn()
                if self.state == before:
                    return copy.deepcopy(result)
                self._notifications(before)
                if room and action:
                    self.db.execute("INSERT INTO events(room,at,actor,action,before_room) VALUES(?,?,?,?,?)",
                                    (room, self.clock(), str(actor), action, json.dumps(before["rooms"].get(room), ensure_ascii=False)))
                    # Keep 150 events per room; snapshots contain no other rooms or credentials.
                    self.db.execute("DELETE FROM events WHERE room=? AND id NOT IN (SELECT id FROM events WHERE room=? ORDER BY id DESC LIMIT 150)", (room, room))
                self._save()
                self.db.commit()
                return copy.deepcopy(result)
            except Exception:
                self.state = before
                self.db.rollback()
                raise

    def register(self, uid, name):
        uid = str(uid)
        def change():
            u = self.state["users"].setdefault(uid, {"id": uid, "badge": secrets.token_urlsafe(12)})
            u["name"] = clean_name(name)
            return u
        return self._change(change)

    def station(self, name, kind):
        require(kind in KINDS, "Неизвестный тип активности")
        def change():
            code = secrets.token_urlsafe(12)
            self.state["stations"][code] = {"code": code, "name": clean_name(name), "kind": kind}
            return self.state["stations"][code]
        return self._change(change)

    def create_room(self, uid, name):
        uid = str(uid)
        def change():
            code = secrets.token_hex(3).upper()
            while code in self.state["rooms"]:
                code = secrets.token_hex(3).upper()
            self.state["rooms"][code] = {
                "code": code, "name": clean_name(name), "owner": uid, "admins": [uid],
                "phase": "lobby", "paused_at": None, "stations": [], "players": {},
                "settings": {"impostors": 1, "tasks": 3, "cooldown": 60, "discussion": 60, "voting": 45, "emergencies": 1},
                "meeting": None, "winner": None, "epoch": 0, "created": self.clock(), "round": 0,
                "sabotage": None, "sabotage_ready": 0, "test_mode": False,
            }
            return self.state["rooms"][code]
        return self._change(change)

    def room(self, code):
        code = str(code).upper()
        require(code in self.state["rooms"], "Комната не найдена")
        return self.state["rooms"][code]

    def admin(self, r, uid, host=False):
        require(host or str(uid) in r["admins"], "Нужны права администратора этой сессии")

    def member(self, r, uid):
        p = r["players"].get(str(uid))
        require(p is not None, "Вы не участвуете в этой сессии")
        return p

    def active(self, r):
        require(r["phase"] == "running", "Сейчас действие недоступно: игра не идёт или началось собрание")
        require(r["paused_at"] is None, "Игра на паузе")

    def current_room(self, uid):
        rooms = [r for r in self.state["rooms"].values() if str(uid) in r["players"] and r["phase"] != "ended"]
        return rooms[-1] if rooms else None

    def join(self, code, uid):
        uid = str(uid)
        def change():
            r = self.room(code)
            if uid in r["players"]:
                return r
            require(r["phase"] == "lobby", "Присоединиться можно только до начала партии")
            require(uid in self.state["users"], "Сначала войдите в приложение")
            other = self.current_room(uid)
            require(not other or other["code"] == r["code"], "Вы уже в другой активной сессии. Выйдите из её лобби или завершите её")
            require(len(r["players"]) < 40, "В первой версии максимум 40 игроков")
            r["players"][uid] = {"id": uid, "name": self.state["users"][uid]["name"], "role": None,
                "status": "alive", "known_dead": False, "reported": False, "tasks": {}, "kill_ready": 0, "emergencies": 0,
                "ghost_ready": 0, "death_id": None}
            return r
        return self._change(change, str(code).upper(), uid, "Игрок присоединился")

    def leave(self, code, uid):
        def change():
            r = self.room(code)
            require(r["phase"] == "lobby", "Выход во время партии выполняет администратор через исключение")
            r["players"].pop(str(uid), None)
        return self._change(change, str(code).upper(), uid, "Выход из лобби")

    def configure(self, code, uid, data, host=False):
        def change():
            r = self.room(code)
            self.admin(r, uid, host)
            require(r["phase"] == "lobby", "Настройки доступны до старта. Для новой партии создайте новую сессию")
            if "name" in data:
                r["name"] = clean_name(data["name"])
            if "stations" in data:
                stations = data["stations"]
                require(isinstance(stations, list) and len(stations) <= 100, "Некорректный список станций")
                require(all(isinstance(s, str) and s in self.state["stations"] for s in stations), "Станция не найдена")
                r["stations"] = list(dict.fromkeys(stations))
            limits = {"impostors": (1, 10), "tasks": (1, 30), "cooldown": (5, 600), "discussion": (0, 600), "voting": (10, 600), "emergencies": (0, 5)}
            for key, bounds in limits.items():
                if key in data:
                    r["settings"][key] = integer(data[key], *bounds)
            return r
        return self._change(change, str(code).upper(), uid, "Изменены настройки сессии")

    def _puzzle(self, kind):
        return puzzle(kind)

    def start(self, code, uid, host=False):
        def change():
            r = self.room(code)
            self.admin(r, uid, host)
            require(r["phase"] == "lobby", "Партия уже запущена")
            ps, settings = r["players"], r["settings"]
            require(len(ps) >= 3 and len(ps) > 2 * settings["impostors"], "Нужно минимум три игрока; предателей должно быть меньше половины")
            require(len(r["stations"]) >= settings["tasks"], "Добавьте достаточно разных станций для заданий игрока")
            rng = random.SystemRandom()
            impostors = set(rng.sample(list(ps), settings["impostors"]))
            r["round"] += 1
            used_lists = set()
            for pid, p in ps.items():
                p.update(role="impostor" if pid in impostors else "crew", status="alive", known_dead=False,
                         reported=False, kill_ready=self.clock() + settings["cooldown"], emergencies=0, tasks={}, ghost_ready=0, death_id=None)
                if pid in impostors:
                    continue
                assigned = rng.sample(r["stations"], settings["tasks"])
                # Prefer distinct routes where the chosen station count permits it.
                for _ in range(100):
                    if tuple(sorted(assigned)) not in used_lists:
                        break
                    assigned = rng.sample(r["stations"], settings["tasks"])
                used_lists.add(tuple(sorted(assigned)))
                for s in assigned:
                    p["tasks"][s] = self.new_task(s)
            r["phase"], r["winner"] = "running", None
            r.update(sabotage=None, sabotage_ready=self.clock())
            return r
        return self._change(change, str(code).upper(), uid, "Старт партии и выдача ролей")

    def progress(self, r):
        tasks = [t for p in r["players"].values() if p["role"] == "crew" for t in p["tasks"].values()]
        return {"done": sum(t["done"] for t in tasks), "total": len(tasks)}

    def _win(self, r):
        s = r.get('sabotage')
        if s and s['deadline'] is not None and self.clock() >= s['deadline']:
            r.update(phase='ended', winner='impostor', paused_at=None, end_reason='sabotage_' + s['kind'])
            self.clear_sabotage(r, 'timeout')
            return
        alive = [p for p in r["players"].values() if p["status"] == "alive"]
        imps = sum(p["role"] == "impostor" for p in alive)
        crew = len(alive) - imps
        prog = self.progress(r)
        winner = "crew" if imps == 0 else "impostor" if imps >= crew else "crew" if prog["total"] and prog["done"] == prog["total"] else None
        if winner:
            r["phase"], r["winner"], r["paused_at"] = "ended", winner, None
            self.clear_sabotage(r, "game_ended")

    def task_open(self, code, uid, station, ghost=False):
        def change():
            r = self.room(code)
            self.active(r)
            p = self.member(r, uid)
            require(p['role'] == 'crew', 'Предатели не могут выполнять задания')
            if ghost:
                require(p['status'] == 'dead', 'Без QR задания доступны только убитым игрокам')
            if p['status'] == 'dead':
                require(self.clock() >= p.get('ghost_ready', 0), 'Следующее задание станет доступно через минуту после предыдущего')
            target, requested_stage = self.station_target(station)
            self.task_available(r, target)
            require(target in p['tasks'], 'Эта станция не входит в ваш список заданий')
            t = p['tasks'][target]
            require(not t['done'], 'Задание уже выполнено в этой партии')
            stage = t.get('stage', 0)
            require(requested_stage is None or requested_stage == stage, f'Сначала выполните этап {stage + 1} этого задания')
            for other, attempt in p['tasks'].items():
                if other != target and attempt.get('started') is not None:
                    self.reset_attempt(other, attempt)
            self.reset_attempt(target, t)
            t['started'] = self.clock()
            labels = definition(self.state['stations'][target]['kind'])['stages']
            return {'station': self.station_view(self.state['stations'][target]), 'station_code': target,
                    'id': t['id'], 'epoch': r['epoch'], 'puzzle': t['puzzle'], 'started': t['started'],
                    'stage': stage, 'stage_count': t.get('stage_count', len(labels)), 'stage_label': labels[stage],
                    'ready_at': t.get('ready_at', 0), 'time': self.clock()}
        return self._change(change)

    def inspect_scan(self, code, uid, payload):
        """Resolve a QR in the caller's room; never grant actions from a client label."""
        with self.lock:
            r = self.room(code)
            p = self.member(r, uid)
            require(isinstance(payload, str) and len(payload) <= 100, "Неизвестный QR-код")
            kind, target = payload[:2], payload[2:]
            if r['phase'] == 'gathering':
                require(r['paused_at'] is None, 'Игра на паузе')
                require(kind == 'm_' and target == self.state['meeting_code'], 'Во время сбора отсканируйте QR совещания в главной комнате')
                require(p['status'] == 'alive', 'Погибшие наблюдают за совещанием без отметки прибытия')
                return {'kind': 'arrival', 'target': target, 'name': 'Прибытие на совещание', 'actions': ['meeting_join']}
            self.active(r)
            if kind == "x_":
                return self.inspect_sabotage(r, uid, target)
            if kind == "s_":
                require(p["role"] == "crew", "Предатели не могут выполнять задания")
                base, stage = self.station_target(target)
                self.task_available(r, base)
                require(base in p['tasks'], 'Эта станция не входит в ваши задания')
                t = p['tasks'][base]
                require(not t['done'], 'Задание уже выполнено')
                require(stage is None or stage == t.get('stage', 0), f"Сначала выполните этап {t.get('stage', 0) + 1} этого задания")
                return {'kind': 'task', 'target': target, 'name': self.state['stations'][base]['name'], 'actions': ['task_open']}
            require(p["status"] == "alive", "Призраки могут сканировать только задания")
            if kind == "m_":
                require(not r.get("sabotage"), "Во время саботажа экстренное совещание недоступно. Можно сообщить о найденном теле")
                require(target == self.state["meeting_code"], "Это не станция совещания")
                require(p["emergencies"] < r["settings"]["emergencies"], "Вы уже использовали доступные совещания")
                return {"kind": "meeting", "target": target, "name": "Экстренное совещание", "actions": ["emergency"]}
            require(kind == "b_", "Неизвестный QR-код")
            victim = self._victim(r, target)
            require(victim["id"] != str(uid), "Это ваш собственный бейдж")
            if victim["status"] == "dead" and not victim["reported"]:
                return {"kind": "body", "target": target, "name": victim["name"], "actions": ["report"]}
            if victim["status"] == "alive":
                if p["role"] == "impostor" and victim["role"] == "crew":
                    require(self.clock() >= p["kill_ready"], "Убийство ещё на перезарядке")
                    return {"kind": "player", "target": target, "name": victim["name"], "actions": ["kill"]}
                return {"kind": "player", "target": target, "name": victim["name"], "actions": []}
            raise GameError("Этот игрок уже выбыл; обнаружить тело нельзя")

    def task_finish(self, code, uid, station, attempt, epoch, answer):
        def change():
            r = self.room(code)
            self.active(r)
            p = self.member(r, uid)
            require(p['role'] == 'crew', 'Предатели не могут выполнять задания')
            target, requested_stage = self.station_target(station)
            self.task_available(r, target)
            require(target in p['tasks'], 'Задание вам не назначено')
            t = p['tasks'][target]
            require(not t['done'], 'Задание уже выполнено')
            require(epoch == r['epoch'] and attempt == t['id'], 'Задание устарело. Откройте текущий этап заново')
            require(t['started'] is not None, 'Сначала откройте задание')
            require(p['status'] != 'dead' or self.clock() >= p.get('ghost_ready', 0), 'Дождитесь минутной перезарядки заданий')
            require(self.clock() >= t.get('ready_at', 0), 'Образец ещё исследуется. Дождитесь окончания анализа')
            kind, task_puzzle = self.state['stations'][target]['kind'], t['puzzle']
            elapsed = self.clock() - t['started']
            if kind == 'card_swipe':
                self._validate_swipe(answer, task_puzzle, elapsed)
            elif kind in ('calibration', 'tuning'):
                self._validate_calibration(answer, task_puzzle, elapsed)
            else:
                try:
                    validate(kind, t.get('stage', 0), task_puzzle, answer, elapsed)
                except ValueError as exc:
                    raise GameError(str(exc)) from None
            t['stage'] = t.get('stage', 0) + 1
            t['done'] = t['stage'] >= t.get('stage_count', len(definition(kind)['stages']))
            t['started'] = None
            t['ready_at'] = self.clock() + 60 if kind == 'inspect_sample' and not t['done'] else 0
            t['id'] = secrets.token_urlsafe(12)
            if not t['done']:
                t['puzzle'] = puzzle(kind, t['stage'])
            if p['status'] == 'dead' and t['done']:
                p['ghost_ready'] = self.clock() + 60
            self._win(r)
            return {'ok': True, 'done': t['done'], 'stage': t['stage'], 'stage_count': t.get('stage_count', 1),
                    'ghost_ready': p.get('ghost_ready', 0), 'progress': self.progress(r)}
        return self._change(change, str(code).upper(), uid, 'Пройден этап задания')

    def _validate_swipe(self, answer, puzzle, elapsed):
        require(isinstance(answer, dict), "Нужно провести карту через считыватель")
        samples = answer.get("samples")
        require(isinstance(samples, list) and 8 <= len(samples) <= 300, "Проведите карту плавно до конца")
        def number(x):
            return type(x) in (int, float) and math.isfinite(x)
        require(all(isinstance(s, dict) and all(number(s.get(k)) for k in ('x', 'y', 't')) for s in samples), "Некорректный жест")
        require(0 <= samples[0]['x'] <= .08 and .92 <= samples[-1]['x'] <= 1.08, "Проведите карту от начала до конца")
        start_cross = next(s['t'] for s in samples if s['x'] >= .1)
        end_cross = next(s['t'] for s in samples if s['x'] >= .9)
        duration = (end_cross - start_cross) / .8
        require(puzzle['min_ms'] <= duration, "Слишком быстро. Проведите карту медленнее")
        require(duration <= puzzle['max_ms'], "Слишком медленно. Проведите карту быстрее")
        require(elapsed * 1000 + 200 >= duration, "Дождитесь окончания прохода карты")
        require(all(-.08 <= s['x'] <= 1.08 and abs(s['y']) <= .45 and 0 <= s['t'] <= 5000 for s in samples), "Держите карту в направляющей")
        require(all(b['t'] > a['t'] and b['x'] >= a['x'] - .07 for a, b in zip(samples, samples[1:])), "Проведите карту одним движением вправо")

    def _validate_calibration(self, answer, puzzle, elapsed):
        require(isinstance(answer, dict) and isinstance(answer.get('hits'), list) and len(answer['hits']) == 3, "Остановите все три контакта")
        previous = -1
        for hit, rotor in zip(answer['hits'], puzzle['rotors']):
            require(type(hit) in (int, float) and math.isfinite(hit) and previous < hit <= elapsed + .35, "Некорректное время остановки")
            angle = (rotor['phase'] + hit * rotor['speed']) % 360
            require(min(angle, 360 - angle) <= puzzle['tolerance'], "Контакт не попал в сектор. Начните калибровку заново")
            previous = hit

    def _victim(self, r, badge):
        user = next((u for u in self.state["users"].values() if u["badge"] == badge), None)
        require(user is not None and user["id"] in r["players"], "Бейдж не относится к участнику этой сессии")
        return r["players"][user["id"]]

    def kill(self, code, uid, badge):
        def change():
            r = self.room(code)
            self.active(r)
            p = self.member(r, uid)
            require(p["role"] == "impostor" and p["status"] == "alive", "Убийство доступно только живому предателю")
            require(self.clock() >= p["kill_ready"], "Перезарядка убийства ещё не закончилась")
            v = self._victim(r, badge)
            require(v["id"] != str(uid) and v["status"] == "alive" and v["role"] == "crew", "Эту цель нельзя убить")
            self.drop_repair(r, v['id'])
            v.update(status='dead', reported=False, death_id=secrets.token_urlsafe(8), ghost_ready=0)
            for station, task in v['tasks'].items():
                self.reset_attempt(station, task)
            p["kill_ready"] = self.clock() + r["settings"]["cooldown"]
            self._win(r)
            return {"victim": v["id"]}
        return self._change(change, str(code).upper(), uid, "Убийство")

    def _meeting(self, r, caller, reason, body=None):
        if reason == 'body' and r.get('sabotage') and r['sabotage']['kind'] in ('oxygen', 'reactor'):
            self.clear_sabotage(r, 'body_report')
        # Current-panel input is invalidated, but completed stages stay committed.
        for p in r['players'].values():
            for station, task in p['tasks'].items():
                self.reset_attempt(station, task)
        if r.get('sabotage'): r['sabotage'].update(access={}, holds={}, overlap_since=None)
        r['phase'] = 'gathering'
        r['meeting'] = {'id': secrets.token_urlsafe(8), 'caller': str(caller), 'reason': reason, 'body': body,
                        'deadline': None, 'arrived': [], 'votes': {}, 'result': None, 'started': self.clock()}
        r['epoch'] += 1

    def _begin_discussion(self, r, force=False):
        if force:
            r['meeting']['arrived'] = [pid for pid, p in r['players'].items() if p['status'] == 'alive']
            r['meeting']['forced'] = True
        for p in r['players'].values():
            if p['status'] != 'alive':
                p['known_dead'] = True
                if p['status'] == 'dead':
                    p['reported'] = True
        r['phase'] = 'discussion' if r['settings']['discussion'] else 'voting'
        r['meeting']['deadline'] = self.clock() + r['settings'][r['phase']]

    def meeting_join(self, code, uid):
        def change():
            r = self.room(code)
            require(r['phase'] == 'gathering' and r['paused_at'] is None, 'Сбор на совещание сейчас недоступен')
            require(self.member(r, uid)['status'] == 'alive', 'Погибшие не отмечаются среди живых')
            m = r['meeting']
            if str(uid) not in m['arrived']:
                m['arrived'].append(str(uid))
            if all(pid in m['arrived'] for pid, p in r['players'].items() if p['status'] == 'alive'):
                self._begin_discussion(r)
        return self._change(change, str(code).upper(), uid, 'Прибытие по QR на совещание')

    def report(self, code, uid, badge):
        def change():
            r = self.room(code)
            self.active(r)
            require(self.member(r, uid)["status"] == "alive", "Погибшие не могут сообщать о теле")
            v = self._victim(r, badge)
            require(v["status"] == "dead" and not v["reported"], "Здесь нет доступного для обнаружения тела")
            self._meeting(r, uid, "body", v["id"])
        return self._change(change, str(code).upper(), uid, "Обнаружено тело — собрание")

    def emergency(self, code, uid):
        def change():
            r = self.room(code)
            self.active(r)
            require(not r.get("sabotage"), "Во время саботажа экстренное совещание недоступно")
            p = self.member(r, uid)
            require(p["status"] == "alive", "Погибшие не могут вызывать собрание")
            require(p["emergencies"] < r["settings"]["emergencies"], "Лимит экстренных собраний исчерпан")
            p["emergencies"] += 1
            self._meeting(r, uid, "emergency")
        return self._change(change, str(code).upper(), uid, "Экстренное собрание")

    def _finish_vote(self, r):
        m = r["meeting"]
        counts = {}
        for target in m["votes"].values():
            counts[target] = counts.get(target, 0) + 1
        leaders = [target for target, n in counts.items() if n == max(counts.values(), default=0)]
        ejected = leaders[0] if len(leaders) == 1 and leaders[0] != "skip" else None
        if ejected and ejected in r["players"]:
            r["players"][ejected].update(status="ejected", known_dead=True, reported=True)
        m["result"] = {"counts": counts, "ejected": ejected}
        r["phase"] = "running"
        r["epoch"] += 1
        for p in r["players"].values():
            p["kill_ready"] = self.clock() + r["settings"]["cooldown"]
        self._win(r)

    def vote(self, code, uid, meeting, target):
        def change():
            r = self.room(code)
            require(r["phase"] == "voting" and r["paused_at"] is None, "Голосование сейчас недоступно")
            p = self.member(r, uid)
            require(p["status"] == "alive", "Погибшие не голосуют")
            m = r["meeting"]
            require(m["id"] == meeting, "Это голосование уже закончилось")
            require('arrived' not in m or str(uid) in m['arrived'], 'Сначала отметьте прибытие по QR совещания')
            require(str(uid) not in m["votes"], "Ваш голос уже принят")
            require(target == "skip" or target in r["players"] and r["players"][target]["status"] == "alive", "Недопустимая цель")
            m["votes"][str(uid)] = target
            if len(m["votes"]) == sum(p["status"] == "alive" for p in r["players"].values()):
                self._finish_vote(r)
        return self._change(change, str(code).upper(), uid, "Принят голос")

    def tick(self):
        with self.lock:
            for code in list(self.state['rooms']):
                self.tick_sabotage(code)
                self.tick_bots(code)
            for code, r in list(self.state["rooms"].items()):
                if r["paused_at"] is not None or r["phase"] not in ("discussion", "voting") or self.clock() < r["meeting"]["deadline"]:
                    continue
                def change():
                    if r["phase"] == "discussion":
                        r["phase"] = "voting"
                        r["meeting"]["deadline"] = self.clock() + r["settings"]["voting"]
                    else:
                        self._finish_vote(r)
                self._change(change, code, "system", "Истёк таймер собрания")

    def control(self, code, uid, action, data=None, host=False):
        data = data or {}
        def change():
            r = self.room(code)
            self.admin(r, uid, host)
            if action == "pause":
                require(r["phase"] in ("running", "gathering", "discussion", "voting") and r["paused_at"] is None, "Нельзя поставить на паузу")
                r["paused_at"] = self.clock()
                if r.get("sabotage"): r["sabotage"].update(holds={}, access={}, overlap_since=None)
            elif action == "resume":
                require(r["paused_at"] is not None, "Игра не на паузе")
                delta = self.clock() - r["paused_at"]
                r['sabotage_ready'] = r.get('sabotage_ready', 0) + delta
                if r.get('sabotage') and r['sabotage']['deadline'] is not None:
                    r['sabotage']['deadline'] += delta
                for p in r["players"].values():
                    p["kill_ready"] += delta
                    p['ghost_ready'] = p.get('ghost_ready', 0) + delta
                    for task in p['tasks'].values():
                        if task.get('ready_at'):
                            task['ready_at'] += delta
                        if task.get('started') is not None:
                            task['started'] += delta
                if r["phase"] in ("discussion", "voting"):
                    r["meeting"]["deadline"] += delta
                r["paused_at"] = None
                self._win(r)
            elif action == "end":
                self.clear_sabotage(r, "admin_end")
                r.update(phase="ended", winner="stopped", paused_at=None)
                r["epoch"] += 1
            elif action == 'clear_sabotage':
                require(r.get('sabotage'), 'Сейчас нет саботажа')
                self.clear_sabotage(r, 'admin')
            elif action == "cancel_meeting":
                require(r["phase"] in ("gathering", "discussion", "voting"), "Собрание не идёт")
                r["phase"], r["meeting"] = "running", None
                r["epoch"] += 1
            elif action == "next_phase":
                require(r["paused_at"] is None, "Сначала снимите паузу")
                require(r["phase"] in ("gathering", "discussion", "voting"), "Собрание не идёт")
                if r['phase'] == 'gathering':
                    self._begin_discussion(r, force=True)
                elif r["phase"] == "discussion":
                    r["phase"] = "voting"
                    r["meeting"]["deadline"] = self.clock() + r["settings"]["voting"]
                else:
                    self._finish_vote(r)
            elif action == "restart_vote":
                require(r["phase"] in ("discussion", "voting"), "Для нового собрания используйте кнопку вызова")
                r["phase"] = "voting"
                now = r["paused_at"] if r["paused_at"] is not None else self.clock()
                r["meeting"].update(id=secrets.token_urlsafe(8), votes={}, result=None, deadline=now + r["settings"]["voting"])
            elif action in ("revive", "eject", "kick", "grant_admin", "revoke_admin", "role"):
                pid = str(data.get("player", ""))
                p = self.member(r, pid)
                if action == "kick":
                    require(r["phase"] == "lobby", "После старта используйте исключение из партии")
                    del r["players"][pid]
                elif action in ("grant_admin", "revoke_admin"):
                    require(host or str(uid) == r["owner"], "Назначать администраторов может создатель сессии или владелец сервера")
                    require(pid != r["owner"], "Нельзя снять права создателя сессии")
                    require(not p.get("bot"), "Тестовый бот не может быть администратором")
                    r["admins"] = list(set(r["admins"] + [pid])) if action == "grant_admin" else [a for a in r["admins"] if a != pid]
                else:
                    require(r["phase"] == "running" and r["paused_at"] is not None, "Сначала поставьте обычную игру на паузу")
                    if action == "role":
                        require(data.get("role") in ("crew", "impostor"), "Неизвестная роль")
                        if p["role"] == "impostor" and data["role"] == "crew":
                            p['tasks'] = {s: self.new_task(s) for s in random.SystemRandom().sample(r['stations'], r['settings']['tasks'])}
                        p["role"] = data["role"]
                        if p["role"] == "impostor":
                            p["tasks"] = {}
                    else:
                        p.update(status="alive" if action == "revive" else "ejected", known_dead=action != "revive", reported=action != "revive")
                    r["epoch"] += 1
            elif action == "task_reset":
                require(r["phase"] == "running" and r["paused_at"] is not None, "Сначала поставьте обычную игру на паузу")
                p = self.member(r, str(data.get("player", "")))
                require(p["role"] == "crew", "У предателя нет заданий")
                s = data.get("station")
                require(s in p["tasks"], "Задание не найдено")
                p['tasks'][s] = self.new_task(s)
                p['tasks'][s].update(done=bool(data.get('done')))
                if p['tasks'][s]['done']:
                    p['tasks'][s]['stage'] = p['tasks'][s]['stage_count']
                r["epoch"] += 1
            else:
                raise GameError("Неизвестное действие администратора")
            return r
        labels = {"pause": "Пауза", "resume": "Продолжение игры", "end": "Завершение партии", "cancel_meeting": "Отмена собрания", "next_phase": "Переход к следующей фазе собрания", "restart_vote": "Перезапуск голосования", "revive": "Восстановление игрока", "eject": "Исключение игрока", "kick": "Удаление из лобби", "grant_admin": "Назначение администратора", "revoke_admin": "Снятие прав администратора", "role": "Изменение роли", "task_reset": "Исправление задания"}
        return self._change(change, str(code).upper(), uid, "Администратор: " + labels.get(action, str(action)))

    def undo(self, code, uid, host=False):
        def change():
            r = self.room(code)
            self.admin(r, uid, host)
            row = self.db.execute("SELECT id,before_room,at FROM events WHERE room=? AND undone=0 ORDER BY id DESC LIMIT 1", (r["code"],)).fetchone()
            require(row is not None and row[1] != "null", "Нет доступного действия для отмены")
            old = json.loads(row[1])
            require(old["phase"] != "lobby", "Для изменений лобби используйте настройки; запущенную партию нельзя вернуть в лобби")
            # An undo may not silently put a participant back into a different active room.
            for pid in old["players"]:
                other = self.current_room(pid)
                require(not other or other["code"] == r["code"], "Участник уже перешёл в другую сессию; откат невозможен")
            old["epoch"] = r["epoch"] + 1
            now = self.clock()
            old["phase"] = old["phase"] if old["phase"] != "ended" else "running"
            # Restore remaining times as they were at the event, then leave the game paused.
            anchor = old["paused_at"] if old["paused_at"] is not None else row[2]
            for p in old["players"].values():
                p["kill_ready"] = now + max(0, p["kill_ready"] - anchor)
                p['ghost_ready'] = now + max(0, p.get('ghost_ready', 0) - anchor)
                for station, task in p['tasks'].items():
                    if task.get('ready_at'):
                        task['ready_at'] = now + max(0, task['ready_at'] - anchor)
                    self.reset_attempt(station, task)
            old['sabotage_ready'] = now + max(0, old.get('sabotage_ready', 0) - anchor)
            if old.get('sabotage'):
                old['sabotage'].update(holds={}, access={}, overlap_since=None)
                if old['sabotage']['deadline'] is not None:
                    old['sabotage']['deadline'] = now + max(0, old['sabotage']['deadline'] - anchor)
            if old["phase"] in ("discussion", "voting"):
                old["meeting"]["deadline"] = now + max(0, old["meeting"]["deadline"] - anchor)
            old["paused_at"] = now
            self.normalize_sabotage(old)
            self.state["rooms"][r["code"]] = old
            self.db.execute("UPDATE events SET undone=1 WHERE id=?", (row[0],))
        return self._change(change)

    def view(self, code, uid, host=False):
        with self.lock:
            r = self.room(code)
            is_admin = host or str(uid) in r["admins"]
            require(is_admin or str(uid) in r["players"], "Вы не участник этой комнаты")
            out = {k: copy.deepcopy(r[k]) for k in ("code", "name", "phase", "paused_at", "settings", "winner", "epoch", "owner", "admins")}
            out["admin"] = is_admin
            out['test_mode'] = r.get('test_mode', False)
            out['sabotage'] = self.sabotage_view(r, str(uid))
            out['sabotage_ready'] = r.get('sabotage_ready', 0)
            out['end_reason'] = r.get('end_reason')
            out["progress"] = self.progress(r)
            out['stations'] = [self.station_view(self.state['stations'][s]) for s in r['stations']]
            out["players"] = []
            for pid, p in r["players"].items():
                item = {"id": pid, "name": p["name"], "status": p["status"] if is_admin or pid == str(uid) or p["known_dead"] or r["phase"] == "ended" else "unknown"}
                item['bot'] = p.get('bot', False)
                if is_admin and p.get('bot'): item['auto'] = p.get('auto', True)
                if is_admin or pid == str(uid) or r["phase"] == "ended":
                    item["role"] = p["role"]
                if is_admin:
                    item["badge"] = self.state["users"][pid]["badge"]
                    item["tasks"] = [{"station": s, "name": self.state["stations"][s]["name"], "done": t["done"]} for s, t in p["tasks"].items()] if p["role"] == "crew" else []
                out["players"].append(item)
            p = r["players"].get(str(uid))
            out["me"] = None if not p else {"id": p["id"], "role": p["role"], "status": p["status"], "kill_ready": p["kill_ready"], "emergencies": p["emergencies"],
                "badge": self.state["users"][str(uid)]["badge"], 'death_id': p.get('death_id'), 'ghost_ready': p.get('ghost_ready', 0),
                "tasks": [{"station": s, "name": self.state["stations"][s]["name"], "kind": self.state["stations"][s]["kind"], "done": t["done"],
                           'stage': t.get('stage', 0), 'stage_count': t.get('stage_count', 1), 'ready_at': t.get('ready_at', 0),
                           'stage_label': definition(self.state['stations'][s]['kind'])['stages'][min(t.get('stage', 0), len(definition(self.state['stations'][s]['kind'])['stages']) - 1)]}
                          for s, t in p["tasks"].items()] if p["role"] == "crew" else []}
            m = copy.deepcopy(r["meeting"])
            if m:
                m["voted"] = str(uid) in m["votes"]
                m["vote_count"] = len(m["votes"])
                m['arrived_me'] = str(uid) in m.get('arrived', r['players'])
                m['required'] = sum(p['status'] == 'alive' for p in r['players'].values())
                m['arrived_count'] = sum(pid in m.get('arrived', []) for pid, p in r['players'].items() if p['status'] == 'alive')
                m['participants'] = [copy.deepcopy(item) for item in out['players']
                                     if r['phase'] != 'gathering' or item['id'] in m.get('arrived', [])]
                del m["votes"]  # Even admins do not see choices while the ballot is open.
            out["meeting"] = m
            if is_admin:
                out["meeting_code"] = self.state["meeting_code"]
                out["sabotage_qrs"] = self.sabotage_qrs()
                rows = self.db.execute("SELECT id,at,actor,action,undone FROM events WHERE room=? ORDER BY id DESC LIMIT 50", (r["code"],)).fetchall()
                out["events"] = [dict(zip(("id", "at", "actor", "action", "undone"), row)) for row in rows]
            return out

    def listing(self, uid, host=False):
        with self.lock:
            return [{"code": r["code"], "name": r["name"], "phase": r["phase"], "paused_at": r["paused_at"], "count": len(r["players"]), "admin": host or str(uid) in r["admins"]}
                    for r in reversed(list(self.state["rooms"].values())) if host or str(uid) in r["admins"] or str(uid) in r["players"]]
