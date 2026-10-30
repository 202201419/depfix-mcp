import logging

log_calls = []


def notify(msg):
    logging.warn("notify: %s", msg)
    log_calls.append(msg)
    return len(log_calls)
