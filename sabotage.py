"""Shared, persistent sabotage rules and exclusive repair leases."""
import copy
import random
import secrets

from game_errors import require

SABOTAGES = {
    'comms': {'name': 'Взлом связи', 'duration': None, 'blocked': ['upload_data'], 'panels': ['comms']},
    'oxygen': {'name': 'Утечка кислорода', 'duration': 45, 'blocked': ['clean_o2'], 'panels': ['oxygen_a', 'oxygen_b']},
    'reactor': {'name': 'Перегрев реактора', 'duration': 45, 'blocked': ['sequence', 'manifolds'], 'panels': ['reactor_a', 'reactor_b']},
}
INTERACTIONS = {
    'start_comms': ('comms', 'start', 'Саботаж · связь'),
    'start_oxygen': ('oxygen', 'start', 'Саботаж · кислород'),
    'start_reactor': ('reactor', 'start', 'Саботаж · реактор'),
    'comms': ('comms', 'repair', 'Восстановление связи · сервер'),
    'oxygen_a': ('oxygen', 'repair', 'Кислород · панель A'),
    'oxygen_b': ('oxygen', 'repair', 'Кислород · панель B'),
    'reactor_a': ('reactor', 'repair', 'Реактор · сенсор A'),
    'reactor_b': ('reactor', 'repair', 'Реактор · сенсор B'),
}
COOLDOWN = 90
HOLD_TTL = 1.6
OVERLAP = 2
REPAIR_TTL = 10


def route_trace(puzzle, directions):
    """Follow only the arrow's edge-sharing neighbour; never infer diagonal links."""
    size = puzzle.get('size')
    if type(size) is not int or not 2 <= size <= 8: return None
    start, end = puzzle.get('start'), puzzle.get('end')
    if any(type(c) is not int or not 0 <= c < size * size for c in (start, end)) or start == end: return None
    if not isinstance(directions, list) or len(directions) != size * size or any(type(d) is not int or not 0 <= d < 4 for d in directions): return None
    cell = start; seen = set(); path = []
    while cell not in seen:
        seen.add(cell); path.append(cell)
        if cell == end: return path
        row, col = divmod(cell, size)
        dr, dc = ((-1, 0), (0, 1), (1, 0), (0, -1))[directions[cell]]
        nr, nc = row + dr, col + dc
        if not (0 <= nr < size and 0 <= nc < size): return None
        next_cell = nr * size + nc
        # Explicitly reject row wrapping, jumps and diagonals at every transition.
        if abs(nr - row) + abs(nc - col) != 1: return None
        cell = next_cell
    return None


def route_valid(puzzle, directions):
    return route_trace(puzzle, directions) is not None


def new_route():
    rng = random.SystemRandom()
    board = {'size': 4, 'start': 12, 'end': 3, 'directions': [rng.randrange(4) for _ in range(16)]}
    while route_valid(board, board['directions']):
        board['directions'] = [rng.randrange(4) for _ in range(16)]
    return board


def route_solution(board):
    """A valid route for the explicitly host-operated test helper."""
    directions = list(board['directions']); cell = board['start']; size = board['size']
    target_row, target_col = divmod(board['end'], size)
    while cell != board['end']:
        row, col = divmod(cell, size)
        direction = 0 if row > target_row else 2 if row < target_row else 1 if col < target_col else 3
        directions[cell] = direction
        dr, dc = ((-1, 0), (0, 1), (1, 0), (0, -1))[direction]; cell = (row + dr) * size + col + dc
    return directions


class SabotageMixin:
    def normalize_sabotage(self, r):
        s = r.get('sabotage')
        if s and s['kind'] == 'lights':
            s.update(kind='comms', route=new_route(), access={}, holds={}, overlap_since=None)
            s.pop('switches', None)
        if r.get('last_sabotage', {}).get('kind') == 'lights': r['last_sabotage']['kind'] = 'comms'
        if s and s['kind'] == 'oxygen' and s.get('fixed'):
            self.clear_sabotage(r, 'repair')

    def init_sabotages(self):
        codes = self.state.setdefault('sabotage_codes', {})
        # Keep the exact opaque payloads of already printed light QR codes.
        for old, new in (('start_lights', 'start_comms'), ('lights', 'comms')):
            if old in codes: codes.setdefault(new, codes.pop(old))
        for key in INTERACTIONS: codes.setdefault(key, secrets.token_urlsafe(12))
        for r in self.state['rooms'].values():
            r.setdefault('sabotage', None); r.setdefault('sabotage_ready', 0)
            self.normalize_sabotage(r)
            if r['sabotage']: r['sabotage'].update(holds={}, overlap_since=None, access={})
        self._save(); self.db.commit()

    def interaction(self, target):
        key = next((k for k, value in self.state['sabotage_codes'].items() if value == target), None)
        require(key in INTERACTIONS, 'Неизвестный QR саботажа')
        return key, INTERACTIONS[key]

    def sabotage_qrs(self):
        return [{'key': key, 'kind': kind, 'use': use, 'name': name, 'payload': 'x_' + self.state['sabotage_codes'][key]}
                for key, (kind, use, name) in INTERACTIONS.items() if use == 'repair']

    def task_available(self, r, station):
        s = r.get('sabotage')
        require(not s or self.state['stations'][station]['kind'] not in SABOTAGES[s['kind']]['blocked'],
                'Это задание связано с текущим саботажем. Сначала устраните аварию')

    def live_repairs(self, r):
        s = r.get('sabotage')
        return {} if not s or r['phase'] != 'running' or r['paused_at'] is not None else {
            uid: grant for uid, grant in s['access'].items()
            if grant['expires'] > self.clock() and r['players'].get(uid, {}).get('status') == 'alive'}

    def repair_free(self, r, uid, panel):
        s = r['sabotage']
        return not any(actor != str(uid) and (s['kind'] != 'reactor' or grant['panel'] == panel)
                       for actor, grant in self.live_repairs(r).items())

    def inspect_sabotage(self, r, uid, target):
        self.active(r); p = self.member(r, uid)
        key, (kind, use, name) = self.interaction(target)
        require(use == 'repair', 'Запускайте саботажи кнопками в интерфейсе предателя. Этот QR запуска больше не используется')
        require(p['status'] == 'alive', 'Призраки не могут устранять саботажи')
        s = r.get('sabotage')
        require(s and s['kind'] == kind, 'Сейчас эта система исправна')
        require(s['deadline'] is None or self.clock() < s['deadline'], 'Время устранения истекло')
        require(key not in s['fixed'], 'Эта панель уже восстановлена')
        require(self.repair_free(r, uid, key), 'Ремонт уже выполняет другой игрок. Дождитесь освобождения панели')
        actions = ['sabotage_open']
        return {'kind': 'sabotage', 'target': target, 'name': name, 'system': kind, 'actions': actions}

    def sabotage_start(self, code, uid, kind):
        def change():
            r = self.room(code); self.active(r); p = self.member(r, uid)
            require(isinstance(kind, str) and kind in SABOTAGES, 'Неизвестный саботаж')
            require(p['role'] == 'impostor', 'Саботаж доступен только предателю')
            require(not r.get('sabotage'), 'В комнате уже активен саботаж')
            require(self.clock() >= r.get('sabotage_ready', 0), 'Саботаж ещё на перезарядке')
            rng = random.SystemRandom(); duration = SABOTAGES[kind]['duration']
            r['sabotage'] = {'id': secrets.token_urlsafe(12), 'kind': kind, 'started': self.clock(),
                             'deadline': self.clock() + duration if duration else None, 'fixed': [],
                             'codes': {panel: f'{rng.randrange(100000):05d}' for panel in SABOTAGES[kind]['panels']},
                             'holds': {}, 'overlap_since': None, 'access': {}}
            if kind == 'comms': r['sabotage']['route'] = new_route()
            r['sabotage_ready'] = self.clock() + COOLDOWN
            for p in r['players'].values():
                for station, task in p['tasks'].items():
                    if self.state['stations'][station]['kind'] in SABOTAGES[kind]['blocked']: self.reset_attempt(station, task)
            return self.sabotage_view(r, str(uid))
        return self._change(change, str(code).upper(), uid, 'Предатель запустил саботаж кнопкой')

    def clear_sabotage(self, r, reason='repair'):
        if r.get('sabotage'):
            r['last_sabotage'] = {'id': r['sabotage']['id'], 'kind': r['sabotage']['kind'], 'reason': reason}
            r['sabotage'] = None
            r['sabotage_ready'] = (r['paused_at'] if r['paused_at'] is not None else self.clock()) + COOLDOWN

    def drop_repair(self, r, uid):
        s = r.get('sabotage')
        if not s: return
        s['access'].pop(str(uid), None)
        for panel, hold in list(s['holds'].items()):
            if hold['uid'] == str(uid):
                del s['holds'][panel]; s['overlap_since'] = None

    def sabotage_open(self, code, uid, target):
        def change():
            r = self.room(code); result = self.inspect_sabotage(r, uid, target)
            require(result['actions'] == ['sabotage_open'], 'Отсканируйте QR панели ремонта')
            key, _ = self.interaction(target); s = r['sabotage']; self.drop_repair(r, uid)
            grant = {'ticket': secrets.token_urlsafe(24), 'panel': key, 'expires': self.clock() + REPAIR_TTL}
            s['access'][str(uid)] = grant
            puzzle = {'code': s['codes'][key]} if s['kind'] == 'oxygen' else copy.deepcopy(s['route']) if s['kind'] == 'comms' else {}
            return {'id': s['id'], 'kind': s['kind'], 'name': result['name'], 'panel': key,
                    'ticket': grant['ticket'], 'deadline': s['deadline'], 'time': self.clock(), 'puzzle': puzzle}
        return self._change(change)

    def repair_access(self, r, uid, data):
        self.active(r)
        require(self.member(r, uid)['status'] == 'alive', 'Призраки не могут устранять саботажи')
        s = r.get('sabotage')
        require(s and data.get('id') == s['id'], 'Этот саботаж уже завершён')
        require(s['deadline'] is None or self.clock() < s['deadline'], 'Время устранения истекло')
        grant = self.live_repairs(r).get(str(uid))
        require(grant and grant['ticket'] == data.get('ticket'), 'Панель освобождена. Отсканируйте её QR ещё раз')
        return s, grant['panel']

    def repair_heartbeat(self, code, uid, data):
        def change():
            r = self.room(code); s, _ = self.repair_access(r, uid, data)
            s['access'][str(uid)]['expires'] = self.clock() + REPAIR_TTL
            return {'state': self.sabotage_view(r, str(uid))}
        return self._change(change)

    def repair_release(self, code, uid, data):
        def change():
            r = self.room(code); self.member(r, uid); s = r.get('sabotage')
            grant = s and s['access'].get(str(uid))
            if s and s['id'] == data.get('id') and grant and grant['ticket'] == data.get('ticket'):
                self.drop_repair(r, uid)
            return {'ok': True}
        return self._change(change)

    def sabotage_fix(self, code, uid, data):
        def change():
            r = self.room(code); s, panel = self.repair_access(r, uid, data)
            require(s['kind'] in ('comms', 'oxygen'), 'У реактора нужно удерживать сенсор')
            if s['kind'] == 'oxygen':
                require(panel not in s['fixed'], 'Эта панель уже восстановлена')
                require(isinstance(data.get('code'), str) and data['code'] == s['codes'][panel], 'Код не совпал с запиской на панели')
                s['fixed'].append(panel); resolved = True
            else:
                path = route_trace(s['route'], data.get('directions'))
                require(path is not None, 'Маршрут обрывается. Соединяйте А и Б только по горизонтали или вертикали, без петель и выхода за поле')
                if 'path' in data:
                    submitted = data['path']
                    require(isinstance(submitted, list) and all(type(c) is int for c in submitted) and submitted == path,
                            'Отображённый путь не совпал со стрелками. Диагональные переходы не засчитываются')
                resolved = True
            result = {'resolved': resolved, 'panel_fixed': s['kind'] == 'oxygen'}
            self.drop_repair(r, uid)
            if resolved: self.clear_sabotage(r)
            return result
        return self._change(change, str(code).upper(), uid, 'Ремонт саботажа')

    def reactor_hold(self, code, uid, data):
        def change():
            r = self.room(code); s, panel = self.repair_access(r, uid, data)
            require(s['kind'] == 'reactor', 'Это не сенсор реактора')
            require(type(data.get('pressed')) is bool, 'Некорректное состояние сенсора')
            self._reactor_progress(r)
            if r.get('sabotage') is None: return {'resolved': True, 'state': None}
            s['access'][str(uid)]['expires'] = self.clock() + REPAIR_TTL
            if data['pressed']:
                for other, hold in list(s['holds'].items()):
                    if hold['uid'] == str(uid) and other != panel: del s['holds'][other]
                s['holds'][panel] = {'uid': str(uid), 'until': self.clock() + HOLD_TTL}
            elif s['holds'].get(panel, {}).get('uid') == str(uid): del s['holds'][panel]
            self._reactor_progress(r)
            return {'resolved': r['sabotage'] is None, 'state': self.sabotage_view(r, str(uid))}
        return self._change(change)

    def _reactor_progress(self, r):
        s = r.get('sabotage')
        if not s or s['kind'] != 'reactor': return
        for panel, hold in list(s['holds'].items()):
            if hold['until'] <= self.clock() or hold['uid'] not in self.live_repairs(r): del s['holds'][panel]
        holds = list(s['holds'].values())
        if len(holds) == 2 and len({hold['uid'] for hold in holds}) == 2:
            if s['overlap_since'] is None: s['overlap_since'] = self.clock()
            if self.clock() - s['overlap_since'] >= OVERLAP: self.clear_sabotage(r)
        else: s['overlap_since'] = None

    def tick_sabotage(self, code):
        def change():
            r = self.room(code); s = r.get('sabotage')
            if not s or r['phase'] != 'running' or r['paused_at'] is not None: return
            if s['deadline'] is not None and self.clock() >= s['deadline']:
                r.update(phase='ended', winner='impostor', paused_at=None, end_reason='sabotage_' + s['kind'])
                self.clear_sabotage(r, 'timeout'); return
            live = self.live_repairs(r)
            for uid in list(s['access']):
                if uid not in live: self.drop_repair(r, uid)
            self._reactor_progress(r)
        return self._change(change)

    def sabotage_view(self, r, uid):
        s = r.get('sabotage')
        if not s or r['phase'] == 'ended': return None
        spec = SABOTAGES[s['kind']]; live = self.live_repairs(r)
        out = {key: copy.deepcopy(s[key]) for key in ('id', 'kind', 'started', 'deadline', 'fixed')}
        out.update(name=spec['name'], blocked=spec['blocked'], panels=spec['panels'],
                   busy={panel: any(actor != str(uid) and (s['kind'] != 'reactor' or grant['panel'] == panel) for actor, grant in live.items()) for panel in spec['panels']},
                   held={panel: hold['until'] > self.clock() and hold['uid'] in live for panel, hold in s['holds'].items()})
        if str(uid) in live: out['mine'] = {'panel': live[str(uid)]['panel']}
        return out
