def answer_for(game, task):
    kind, puzzle = task['station']['kind'], task['puzzle']
    if kind == 'wires':
        return list(range(4))
    if kind == 'sequence':
        return puzzle['pattern']
    if kind == 'card_swipe':
        game.test_clock[0] += 1
        return {'samples': [{'x': i / 20, 'y': 0, 't': i * 35} for i in range(21)]}
    if kind in ('upload_data', 'submit_scan') or kind == 'inspect_sample' and task['stage'] == 0:
        game.test_clock[0] = max(game.clock(), task['started'] + puzzle['duration'] + .2)
        return {'elapsed': puzzle['duration'] + .1}
    if kind in ('empty_garbage', 'empty_chute', 'fuel_engines'):
        game.test_clock[0] = max(game.clock(), task['started'] + puzzle['duration'] + .2)
        return {'held': puzzle['duration'] + .1}
    if kind == 'inspect_sample':
        game.test_clock[0] = max(game.clock(), task['ready_at'] + .1)
        return {'choice': puzzle['anomaly']}
    if kind == 'divert_power':
        return {'accepted': True} if task['stage'] else {'switch': puzzle['switch'], 'value': 1}
    if kind == 'align_engine': return {'alignment': 0}
    if kind == 'manifolds': return list(range(1, 11))
    if kind in ('clean_o2', 'clean_vent'): return {'removed': [o['id'] for o in puzzle['objects']]}
    if kind == 'steering': return {'x': .5, 'y': .5}
    if kind == 'chart_course': return {'route': list(range(5))}
    if kind == 'asteroids':
        game.test_clock[0] += 3
        return {'hits': list(range(20))}
    if kind == 'shields': return {'sectors': [1]*7}
    hits, previous = [], game.clock() - task['started']
    for rotor in puzzle['rotors']:
        hit = (360 - rotor['phase']) / rotor['speed']
        while hit <= previous + .1:
            hit += 360 / rotor['speed']
        hits.append(hit)
        previous = hit
    game.test_clock[0] = task['started'] + hits[-1] + .1
    return {'hits': hits}
