import datetime
import logging
import threading


def run():
    logging.warn("a")
    n = threading.activeCount()
    return n, datetime.datetime.utcnow()
