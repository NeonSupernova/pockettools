"""Per-app JSON persistence under pockettools/data/<app>/ with atomic writes."""
import json
import os

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')


def path(app, name):
    d = os.path.join(BASE, app)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def load(app, name, default=None):
    try:
        with open(path(app, name), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(app, name, obj):
    p = path(app, name)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, p)
