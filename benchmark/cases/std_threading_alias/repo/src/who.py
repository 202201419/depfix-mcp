import threading


def current_name():
    return threading.currentThread().name
