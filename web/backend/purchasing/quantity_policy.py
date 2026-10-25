"""Read LEGO's published Pick a Brick default when an element has no override.

The US shop uses `element.maxOrderQuantity || constants.vM`, with vM=999.
This is verified from the scripts linked by the current US page, never assumed
from a missing API value. Unknown releases fail closed until reviewed.
No JavaScript executes and no cart operation occurs.
"""
import hashlib
import re
import threading
import time
import urllib.request

PAGE = 'https://www.lego.com/en-us/pick-and-build/pick-a-brick'
_lock = threading.Lock()
_cached = None


def parse_policy(html, scripts, *, now=None):
    links = re.findall(r'<script[^>]*src="([^"]+)"', html)
    constants = next((link for link in links if re.fullmatch(r'/_next/static/chunks/3287-[a-f0-9]+\.js', link)), None)
    consumer = next((link for link in links if re.fullmatch(r'/_next/static/chunks/6308-[a-f0-9]+\.js', link)), None)
    if not constants or not consumer or constants not in scripts or consumer not in scripts:
        raise ValueError('The published US quantity limit could not be verified')
    definition, use = scripts[constants], scripts[consumer]
    module = re.search(r'75748:\([^)]*\)=>\{(.*?)\},\d+:', definition)
    if not module:
        raise ValueError('The US quantity policy changed')
    variable = re.search(r'vM:\(\)=>\s*(\w+)', module[1])
    value = re.search(r'(?:let |,)'+re.escape(variable[1])+r'=(\d+)(?:,|;)', module[1]) if variable else None
    # Verify the consumer uses this particular exported constant, not a nearby
    # set/product limit (the general LEGO shop has a different default).
    aliases = re.findall(r'([a-zA-Z_$][\w$]*)=\w+\(75748\)', use)
    used = any(re.search(r'\.maxOrderQuantity\|\|'+re.escape(alias)+r'\.vM\b', use) for alias in aliases)
    if not value or not used or int(value[1]) != 999 or 'PAB:"11998"' not in module[1]:
        raise ValueError('The US quantity policy changed; do not infer a limit')
    return dict(max_quantity=999, checked_at=time.time() if now is None else now, source=PAGE,
                parser_version=1, scripts={link:hashlib.sha256(scripts[link].encode()).hexdigest()
                                          for link in (constants,consumer)})


def load(*, fetch=None, now=None):
    global _cached
    now = time.time() if now is None else now
    with _lock:
        if fetch is None and _cached and 0 <= now - _cached['checked_at'] < 300:
            return _cached
        def read(url):
            request=urllib.request.Request(url,headers={'User-Agent':'LDraw-Nova-private-catalogue/1.0'})
            with urllib.request.urlopen(request, timeout=30) as response:
                content=response.read(8*1024*1024+1)
            if len(content)>8*1024*1024: raise ValueError('The quantity policy response is too large')
            return content.decode()
        get = fetch or read
        html = get(PAGE)
        links = re.findall(r'<script[^>]*src="([^"]+)"',html)
        required = [link for link in links if re.fullmatch(r'/_next/static/chunks/(?:3287|6308)-[a-f0-9]+\.js',link)]
        scripts={}
        for link in required:
            if fetch is None: time.sleep(1.1)
            scripts[link]=get('https://www.lego.com'+link)
        result=parse_policy(html,scripts,now=now)
        if fetch is None: _cached=result
        return result
