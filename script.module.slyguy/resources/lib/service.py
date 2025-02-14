import json
from time import time

from slyguy import settings
from slyguy.session import Session
from slyguy.log import log

from .proxy import Proxy
from .monitor import monitor
from .player import Player
from .util import check_updates
from .constants import *

def _check_news():
    _time = int(time())
    if _time < settings.getInt('_last_news_check', 0) + NEWS_CHECK_TIME:
        return

    settings.setInt('_last_news_check', _time)

    news = Session(timeout=15).gz_json(NEWS_URL)
    if not news:
        return

    if 'id' not in news or news['id'] == settings.get('_last_news_id'):
        return

    settings.set('_last_news_id', news['id'])
    settings.set('_news', json.dumps(news))

def start():
    log.debug('Shared Service: Started')

    player = Player()
    proxy = Proxy()

    try:
        proxy.start()
    except Exception as e:
        log.error('Failed to start proxy server')
        log.exception(e)

    ## Inital wait on boot
    monitor.waitForAbort(5)

    try:
        while not monitor.abortRequested():
            try: _check_news()
            except Exception as e: log.exception(e)

            try: check_updates()
            except Exception as e: log.exception(e)

            if monitor.waitForAbort(60):
                break
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception(e)

    try: proxy.stop()
    except: pass

    log.debug('Shared Service: Stopped')
