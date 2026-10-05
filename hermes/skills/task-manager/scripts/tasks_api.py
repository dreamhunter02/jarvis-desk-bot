#!/usr/bin/env python3
"""Google Tasks CLI using the auth token from ~/.hermes/google_token.json.

Usage:
  tasks_api.py lists
  tasks_api.py list [LIST_ID]           # all tasks (default list if no id)
  tasks_api.py add TITLE [--due ISO] [--notes N] [--list LIST_ID]
  tasks_api.py done TASK_ID
  tasks_api.py update TASK_ID [--title T] [--due ISO] [--notes N] [--clear-due]
  tasks_api.py delete TASK_ID
  tasks_api.py search QUERY
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_FILE = Path.home() / '.hermes/google_token.json'
BASE = 'https://tasks.googleapis.com/tasks/v1'
USERS_ME = '/users/@me'  # note: tasks endpoints omit /users/@me; only /users/@me/lists works for list-of-lists
LIST_KEY = 'tasks.googleapis.com/last_list_id'


def autorize():
    tok = json.loads(TOKEN_FILE.read_text())
    return tok['token']


def refresh_token():
    import urllib.parse
    tok = json.loads(TOKEN_FILE.read_text())
    data = urllib.parse.urlencode({'client_id': tok['client_id'], 'client_secret': tok['client_secret'], 'refresh_token': tok['refresh_token'], 'grant_type': 'refresh_token'}).encode()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(tok['token_uri'], data=data), timeout=30).read())
    tok['token'] = r['access_token']
    tok['expiry'] = None
    TOKEN_FILE.write_text(json.dumps(tok))
    return tok['token']



# --- classification ---
KEYWORDS_FILE = Path.home() / '.hermes/task-classification.json'

def _classification():
    """~/.hermes/task-classification.json (see task-classification.example.json)."""
    return json.loads(KEYWORDS_FILE.read_text()) if KEYWORDS_FILE.exists() else {}


def classify(title, notes=''):
    # ADD YOUR PERSONAL INFO HERE: put your own work keywords (projects, colleagues,
    # customers) in ~/.hermes/task-classification.json rather than in this file.
    work_kw = _classification().get('work', [
        'paper', 'review', 'slides', 'deadline', 'draft', 'meeting', 'interview', 'conference',
    ])
    text = (title + ' ' + notes).lower()
    if any(k in text for k in work_kw):
        return 'work'
    return 'personal'


def list_for(title, notes=''):
    cfg = _classification()
    lists = call(BASE + USERS_ME + '/lists')['items']
    target = cfg.get('work_list', 'Work Todo') if classify(title, notes) == 'work' else cfg.get('personal_list', 'Life Todo')
    lid = next((l['id'] for l in lists if l['title'] == target), None)
    if not lid:
        lid = call(BASE + USERS_ME + '/lists', 'POST', {'title': target})['id']
    return lid


def call(url, method='GET', data=None, retry=True):
    try:
        req = urllib.request.Request(url, method=method,
            headers={'Authorization': 'Bearer ' + autorize(), 'Content-Type': 'application/json'},
            data=json.dumps(data).encode() if data else None)
        return json.loads(urllib.request.urlopen(req, timeout=30).read() or b'{}')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and retry:
            refresh_token()
            return call(url, method, data, retry=False)
        raise


def default_list_id():
    cfg = Path.home() / '.hermes/tasks-api.json'
    if cfg.exists():
        return json.loads(cfg.read_text()).get('last_list_id')
    lists = call(BASE + USERS_ME + '/lists')
    return lists['items'][0]['id']


def save_default_list(list_id):
    cfg = Path.home() / '.hermes/tasks-api.json'
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    data['last_list_id'] = list_id
    cfg.write_text(json.dumps(data))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('lists')
    l = sub.add_parser('list'); l.add_argument('list_id', nargs='?')
    a = sub.add_parser('add'); a.add_argument('title'); a.add_argument('--due'); a.add_argument('--notes'); a.add_argument('--list')
    d = sub.add_parser('done'); d.add_argument('task_id')
    u = sub.add_parser('update'); u.add_argument('task_id'); u.add_argument('--title'); u.add_argument('--due'); u.add_argument('--notes'); u.add_argument('--clear-due', action='store_true'); u.add_argument('--list')
    dl = sub.add_parser('delete'); dl.add_argument('task_id')
    s = sub.add_parser('search'); s.add_argument('query')
    args = p.parse_args()

    if args.cmd == 'lists':
        print(json.dumps(call(BASE + USERS_ME + '/lists')['items'], indent=1)); return
    if args.cmd == 'list':
        lid = args.list_id or default_list_id()
        items = []
        page = None
        while True:
            url = f'{BASE}/lists/{lid}/tasks?showCompleted=true&maxResults=100' + (f'&pageToken={page}' if page else '')
            r = call(url)
            items += r.get('items', [])
            page = r.get('nextPageToken')
            if not page: break
        for t in items:
            print(t['id'], '|', t.get('status'), '|', t.get('due', '-'), '|', t['title'])
        return
    if args.cmd == 'add':
        lid = args.list if args.list else list_for(args.title, args.notes or '')
        body = {'title': args.title}
        if args.due: body['due'] = args.due
        if args.notes: body['notes'] = args.notes
        r = call(f'{BASE}/lists/{lid}/tasks', 'POST', body)
        print('created:', r['id'], r['title'])
        return
    if args.cmd == 'done':
        lid = default_list_id()
        r = call(f'{BASE}/lists/{lid}/tasks/{args.task_id}', 'PATCH', {'status': 'completed'})
        print('done:', r['title']); return
    if args.cmd == 'update':
        body = {}
        if args.title: body['title'] = args.title
        if args.due: body['due'] = args.due
        if args.notes: body['notes'] = args.notes
        if args.clear_due: body['due'] = None
        lid = args.list or default_list_id()
        r = call(f'{BASE}/lists/{lid}/tasks/{args.task_id}', 'PATCH', body if body else None)
        print('updated:', r['title']); return
    if args.cmd == 'delete':
        lid = default_list_id()
        call(f'{BASE}/lists/{lid}/tasks/{args.task_id}', 'DELETE')
        print('deleted:', args.task_id); return
    if args.cmd == 'search':
        # no server-side search; fetch and filter locally
        lid = default_list_id()
        page = None; q = args.query.lower()
        while True:
            url = f'{BASE}/lists/{lid}/tasks?showCompleted=true&maxResults=100' + (f'&pageToken={page}' if page else '')
            r = call(url)
            for t in r.get('items', []):
                if q in t['title'].lower() or q in (t.get('notes') or '').lower():
                    print(t['id'], '|', t.get('status'), '|', t.get('due', '-'), '|', t['title'])
            page = r.get('nextPageToken')
            if not page: break
        return


if __name__ == '__main__':
    main()
