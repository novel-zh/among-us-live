class GameError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise GameError(message)
