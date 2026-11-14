#!/usr/bin/env python3
"""本地预览 + 真实访客统计服务。

用法：
    python3 server.py           # http://localhost:8123
    python3 server.py 9000      # 指定端口

统计规则：
    - 每次到访写入 data/visits.db（SQLite），记录时间、设备标识、IP、UA、页面
    - 同一个设备（同名 Cookie）24 小时内只计 1 次，刷新页面不会重复计数
    - 换一个浏览器 / 设备会得到新的 Cookie，因此会被独立计入
"""

import http.cookies
import json
import os
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import date, datetime, time as dtime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
DB_PATH = os.path.join(DATA_DIR, 'visits.db')
COOKIE_NAME = 'po_visitor'
DEDUPE_SECONDS = 24 * 60 * 60
# 初始展示基数：真实访客数累加在这个值上，可按需调整
INITIAL_VISITORS = 1280
BLOCKED_PREFIXES = ('/data/', '/.git/')


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with closing(connect_db()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visitor_id TEXT NOT NULL,
                visited_at INTEGER NOT NULL,
                ip TEXT,
                user_agent TEXT,
                path TEXT,
                referer TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_visits_visitor
                ON visits(visitor_id, visited_at);
            CREATE INDEX IF NOT EXISTS idx_visits_time
                ON visits(visited_at);
            """
        )


def today_start_timestamp():
    return int(datetime.combine(date.today(), dtime(0, 0)).timestamp())


def record_visit(handler, visitor_id):
    now = int(time.time())
    ip = handler.client_address[0]
    user_agent = handler.headers.get('User-Agent', '')[:500]
    referer = handler.headers.get('Referer', '')[:500]
    path = urlparse(handler.path).path

    with closing(connect_db()) as conn, conn:
        last = conn.execute(
            'SELECT MAX(visited_at) FROM visits WHERE visitor_id = ?',
            (visitor_id,),
        ).fetchone()[0]
        counted = last is None or now - last >= DEDUPE_SECONDS

        if counted:
            conn.execute(
                'INSERT INTO visits (visitor_id, visited_at, ip, user_agent, path, referer)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                (visitor_id, now, ip, user_agent, path, referer),
            )

        visitors = conn.execute(
            'SELECT COUNT(DISTINCT visitor_id) FROM visits'
        ).fetchone()[0]
        today = conn.execute(
            'SELECT COUNT(DISTINCT visitor_id) FROM visits WHERE visited_at >= ?',
            (today_start_timestamp(),),
        ).fetchone()[0]

    return counted, INITIAL_VISITORS + visitors, today


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/api/visit':
            self.handle_visit()
            return

        if path.startswith(BLOCKED_PREFIXES) or path in ('/server.py', '/README.md'):
            self.send_error(404)
            return

        super().do_GET()

    def read_visitor_id(self):
        raw = self.headers.get('Cookie', '')
        if not raw:
            return None

        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None

        morsel = jar.get(COOKIE_NAME)
        if not morsel:
            return None

        try:
            uuid.UUID(morsel.value)
        except (ValueError, AttributeError):
            return None

        return morsel.value

    def handle_visit(self):
        visitor_id = self.read_visitor_id()
        new_cookie = None
        if visitor_id is None:
            visitor_id = str(uuid.uuid4())
            new_cookie = visitor_id

        counted, visitors, today = record_visit(self, visitor_id)

        payload = json.dumps(
            {'visitors': visitors, 'today': today, 'counted': counted}
        ).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        if new_cookie:
            self.send_header(
                'Set-Cookie',
                f'{COOKIE_NAME}={new_cookie}; Path=/; Max-Age=31536000; SameSite=Lax',
            )
        self.end_headers()
        self.wfile.write(payload)

        if counted:
            print(f'  +1 新到访 · 累计 {visitors} 位 · 今日 {today} 位')

    def log_message(self, fmt, *args):
        if urlparse(self.path).path.startswith('/api/'):
            return
        super().log_message(fmt, *args)


def main():
    port = 8123
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f'端口参数无效: {sys.argv[1]}')
            sys.exit(1)

    init_db()
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)

    print(f'Planet Observatory 已启动 → http://localhost:{port}')
    print(f'访问数据保存在 {DB_PATH}')
    print('按 Ctrl+C 停止服务')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n统计服务已停止。')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
