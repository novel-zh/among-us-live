"""The Skeld task catalogue, stages, and server-side puzzle validation.

Stage names deliberately describe panels, not floors or physical rooms.
The host places their printed QR codes anywhere on the real play area.
"""
import math
import random


TASKS = {
    'upload_data': {'name': 'Загрузить данные', 'stages': ['Скачать данные', 'Загрузить данные'], 'type': 'Короткое'},
    'empty_garbage': {'name': 'Выбросить мусор', 'stages': ['Очистить контейнер', 'Выбросить мусор'], 'type': 'Длинное'},
    'wires': {'name': 'Починить проводку', 'stages': ['Щиток 1', 'Щиток 2', 'Щиток 3'], 'type': 'Общее'},
    'empty_chute': {'name': 'Очистить мусоросборник', 'stages': ['Очистить мусоросборник', 'Выбросить мусор'], 'type': 'Длинное'},
    'inspect_sample': {'name': 'Исследовать образец', 'stages': ['Запустить анализ', 'Найти аномалию'], 'type': 'Длинное'},
    'fuel_engines': {'name': 'Заправить моторы', 'stages': ['Наполнить канистру', 'Заправить первый мотор', 'Снова наполнить канистру', 'Заправить второй мотор'], 'type': 'Длинное'},
    'divert_power': {'name': 'Подать энергию', 'stages': ['Перенаправить питание', 'Принять питание'], 'type': 'Короткое'},
    'align_engine': {'name': 'Выровнять отдачу моторов', 'stages': ['Выровнять первый мотор', 'Выровнять второй мотор'], 'type': 'Длинное'},
    'manifolds': {'name': 'Открыть коллекторы', 'stages': ['Открыть коллекторы'], 'type': 'Короткое'},
    'sequence': {'name': 'Запустить реактор', 'stages': ['Повторить 5 последовательностей'], 'type': 'Длинное'},
    'calibration': {'name': 'Откалибровать распределитель', 'stages': ['Остановить три контакта'], 'type': 'Короткое'},
    'clean_o2': {'name': 'Очистить фильтр O2', 'stages': ['Убрать листья в фильтр'], 'type': 'Короткое'},
    'steering': {'name': 'Выровнять курс', 'stages': ['Совместить прицел с центром'], 'type': 'Короткое'},
    'chart_course': {'name': 'Проложить маршрут', 'stages': ['Провести корабль по точкам'], 'type': 'Короткое'},
    'asteroids': {'name': 'Разрушить астероиды', 'stages': ['Уничтожить 20 астероидов'], 'type': 'Длинное'},
    'card_swipe': {'name': 'Провести картой', 'stages': ['Провести карту через считыватель'], 'type': 'Общее'},
    'submit_scan': {'name': 'Пройти сканирование', 'stages': ['Дождаться медицинского сканирования'], 'type': 'Длинное'},
    'shields': {'name': 'Запустить щиты', 'stages': ['Включить все красные сектора'], 'type': 'Короткое'},
    'clean_vent': {'name': 'Прочистить люк', 'stages': ['Убрать предметы из вентиляции'], 'type': 'Короткое'},
}
KINDS = {key: task['name'] for key, task in TASKS.items()} | {'tuning': 'Калибровка (старая станция)'}


def definition(kind):
    return TASKS['calibration' if kind == 'tuning' else kind]


def puzzle(kind, stage=0):
    rng = random.SystemRandom()
    if kind == 'wires':
        return {'right': rng.sample(list(range(4)), 4)}
    if kind == 'sequence':
        return {'pattern': [rng.randrange(9) for _ in range(5)]}
    if kind == 'card_swipe':
        return {'min_ms': 450, 'max_ms': 1100}
    if kind in ('calibration', 'tuning'):
        return {'rotors': [{'phase': rng.randrange(40, 320), 'speed': rng.choice((95, 110, 125))} for _ in range(3)], 'tolerance': 24}
    if kind in ('upload_data', 'empty_garbage', 'empty_chute', 'fuel_engines', 'submit_scan'):
        return {'duration': {'upload_data': 9, 'empty_garbage': 4, 'empty_chute': 4, 'fuel_engines': 3, 'submit_scan': 10}[kind]}
    if kind == 'inspect_sample':
        return {'duration': 3} if stage == 0 else {'anomaly': rng.randrange(5)}
    if kind == 'divert_power':
        return {'switch': rng.randrange(8)} if stage == 0 else {'accept': True}
    if kind == 'align_engine':
        return {'offset': rng.choice((-1, 1)) * rng.uniform(.25, .85)}
    if kind == 'manifolds':
        return {'numbers': rng.sample(list(range(1, 11)), 10)}
    if kind in ('clean_o2', 'clean_vent'):
        return {'objects': [{'id': i, 'x': rng.uniform(.25, .85), 'y': rng.uniform(.15, .85)} for i in range(6 if kind == 'clean_o2' else 8)]}
    if kind == 'steering':
        return {'start': {'x': rng.uniform(.1, .3), 'y': rng.uniform(.1, .85)}}
    if kind == 'chart_course':
        return {'points': [{'x': .1 + i * .2, 'y': rng.uniform(.2, .8)} for i in range(5)]}
    if kind == 'asteroids':
        return {'targets': [{'id': i, 'x': rng.uniform(.15, .85), 'y': rng.uniform(.15, .85), 'size': rng.uniform(.045, .075)} for i in range(20)]}
    if kind == 'shields':
        return {'red': rng.sample(list(range(7)), rng.randint(3, 6))}
    raise ValueError('Неизвестная мини-игра')


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def check(condition, message='Решение неверное. Попробуйте ещё раз'):
    if not condition:
        raise ValueError(message)


def validate(kind, stage, task, answer, elapsed):
    """Gesture-specific validators for card and distributor live in Engine."""
    if kind in ('wires', 'sequence', 'manifolds'):
        expected = list(range(4)) if kind == 'wires' else list(range(1, 11)) if kind == 'manifolds' else task['pattern']
        check(isinstance(answer, list) and all(type(x) is int for x in answer) and answer == expected)
        return
    check(isinstance(answer, dict))
    if kind in ('upload_data', 'submit_scan') or kind == 'inspect_sample' and stage == 0:
        check(number(answer.get('elapsed')) and answer['elapsed'] >= task['duration'] and elapsed >= task['duration'], 'Дождитесь завершения процесса')
    elif kind in ('empty_garbage', 'empty_chute', 'fuel_engines'):
        check(number(answer.get('held')) and task['duration'] <= answer['held'] <= elapsed + .35, 'Удерживайте рычаг до завершения')
    elif kind == 'inspect_sample':
        check(type(answer.get('choice')) is int and answer['choice'] == task['anomaly'], 'Выберите образец другого цвета')
    elif kind == 'divert_power':
        check(answer.get('accepted') is True if stage else type(answer.get('switch')) is int and answer['switch'] == task['switch'] and number(answer.get('value')) and answer['value'] == 1)
    elif kind == 'align_engine':
        check(number(answer.get('alignment')) and abs(answer['alignment']) <= .06, 'Совместите линию двигателя с центральной отметкой')
    elif kind in ('clean_o2', 'clean_vent', 'asteroids'):
        values = answer.get('hits' if kind == 'asteroids' else 'removed')
        expected = list(range(20)) if kind == 'asteroids' else [o['id'] for o in task['objects']]
        check(isinstance(values, list) and all(type(i) is int for i in values) and sorted(values) == expected, 'Уберите все объекты')
        if kind == 'asteroids':
            check(elapsed >= 2, 'Уничтожьте все 20 астероидов')
    elif kind == 'steering':
        check(number(answer.get('x')) and number(answer.get('y')) and math.hypot(answer['x'] - .5, answer['y'] - .5) <= .06, 'Совместите прицел с центром')
    elif kind == 'chart_course':
        check(answer.get('route') == list(range(5)) and all(type(i) is int for i in answer['route']), 'Пройдите все точки маршрута по порядку')
    elif kind == 'shields':
        check(answer.get('sectors') == [1] * 7 and all(type(i) is int for i in answer['sectors']), 'Включите все сектора щита')
    else:
        raise ValueError('Неизвестная мини-игра')
