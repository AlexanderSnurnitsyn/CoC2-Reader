#!/usr/bin/env python3
"""
CoC2 Reader — локальный лаунчер «всё в одном».

Что делает при запуске:
  1. находит папку игры (автопоиск Steam, иначе спрашивает один раз через веб-форму);
  2. собирает coc2.db рядом с программой (обычный файл — ничего не пропадает, кэш браузера не пухнет);
  3. раздаёт viewer.html, coc2.db и картинки игры (через внутренний маршрут /bitmaps/ — без копирования);
  4. открывает браузер.

Запуск:  python server.py     (или двойной клик по run.bat / собранному .exe)
"""
import http.server, socketserver, json, os, sys, threading, time, traceback
import urllib.parse, mimetypes, webbrowser
from pathlib import Path

import extractor as ex

PORT = 8765

# ── Пути: данные (db/config) — рядом с программой; ресурсы (viewer.html) — из бандла ──
def app_dir() -> Path:
    if getattr(sys, 'frozen', False):           # собранный .exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def resource_path(name: str) -> Path:
    base = getattr(sys, '_MEIPASS', None)        # PyInstaller распаковывает сюда
    return Path(base) / name if base else app_dir() / name

APP    = app_dir()
CONFIG = APP / 'config.json'
DB     = APP / 'coc2.db'
JSONP  = APP / 'output.json'
FAVS_FILE = APP / 'favorites.json'

mimetypes.add_type('image/webp', '.webp')

# ── Состояние сборки (для прогресса в браузере) ──
STATE = {'phase': 'idle', 'msg': '', 'error': None}
CFG   = {'game': '', 'scenes': '', 'bitmaps': ''}

def load_config():
    global CFG
    try:
        CFG.update(json.loads(CONFIG.read_text(encoding='utf-8')))
    except Exception:
        pass

def save_config():
    try:
        CONFIG.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass

def ready() -> bool:
    return DB.exists() and bool(CFG.get('bitmaps')) and Path(CFG['bitmaps']).is_dir()

def read_favs():
    try:
        data = json.loads(FAVS_FILE.read_text(encoding='utf-8'))
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []

def write_favs(ids):
    try:
        FAVS_FILE.write_text(json.dumps(list(ids), ensure_ascii=False), encoding='utf-8')
        return True
    except Exception:
        return False

# ── Автопоиск папки игры ──
_STEAM_GUESSES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Corruption of Champions II",
    r"C:\Program Files\Steam\steamapps\common\Corruption of Champions II",
    r"D:\Steam\steamapps\common\Corruption of Champions II",
    r"D:\SteamLibrary\steamapps\common\Corruption of Champions II",
    r"E:\SteamLibrary\steamapps\common\Corruption of Champions II",
]

def autodetect_game():
    for g in _STEAM_GUESSES:
        if Path(g).is_dir():
            return g
    return None

# ── Поиск папок scenes (*.js) и bitmaps внутри указанного пути ──
def locate_dirs(game_path: str):
    base = Path(game_path)
    if not base.is_dir():
        return None, None
    bitmaps = None
    scores = {}      # dir -> число объявлений сцен в .js этой папки (без подпапок)
    for root, dirs, files in os.walk(base):
        rp = Path(root)
        if rp.name.lower() == 'bitmaps' and (rp / 'crops').is_dir():
            bitmaps = str(rp)
        if len(rp.relative_to(base).parts) > 8:
            dirs[:] = []
            continue
        sc = 0
        for f in files:
            if not f.endswith('.js'):
                continue
            try:
                sc += len(ex.SCENE_RE.findall((rp / f).read_text('utf-8', 'ignore')))
            except Exception:
                pass
        if sc:
            scores[rp] = sc

    if not scores:
        return None, bitmaps

    total = sum(scores.values())
    # рекурсивная сумма для каждой папки (она сама + все вложенные)
    def subtree(d):
        return sum(s for p, s in scores.items() if p == d or str(p).startswith(str(d) + os.sep))
    # берём самую ВЕРХНЮЮ папку, покрывающую ≥90% всех сцен (чтобы захватить раскиданные по подпапкам)
    candidates = [d for d in scores if subtree(d) >= 0.9 * total]
    scenes_root = min(candidates, key=lambda d: len(d.parts)) if candidates \
        else max(scores, key=scores.get)
    return str(scenes_root), bitmaps

# ── Сборка БД в фоне ──
def build_db(game_path: str):
    try:
        STATE.update(phase='locating', msg='Locating game folders…', error=None)
        scenes, bitmaps = locate_dirs(game_path)
        if not scenes:
            STATE.update(phase='error', error='No scenes (*.js) found in that folder.')
            return
        if not bitmaps:
            STATE.update(phase='error', error='No "bitmaps" folder (with "crops") found in that folder.')
            return
        CFG.update(game=game_path, scenes=scenes, bitmaps=bitmaps)
        save_config()
        STATE.update(phase='building', msg='Building database (this can take a minute)…')
        ex.extract_all(scenes, str(DB), str(JSONP),
                       bitmaps_path=bitmaps, min_chars=200, resume=False, recursive=True)
        STATE.update(phase='done', msg='Done.')
    except Exception as e:
        traceback.print_exc()
        STATE.update(phase='error', error=f'{type(e).__name__}: {e}')

def start_build(game_path: str):
    if STATE['phase'] in ('locating', 'building'):
        return
    STATE.update(phase='starting', msg='Starting…', error=None)
    threading.Thread(target=build_db, args=(game_path,), daemon=True).start()

# ── Страница первичной настройки ──
def setup_html():
    guess = autodetect_game() or ''
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CoC2 Reader — Setup</title>
<style>
 body{{font:15px/1.6 system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;height:100vh;margin:0}}
 .box{{background:#171a21;border:1px solid #2a2f3a;border-radius:14px;padding:28px 32px;max-width:560px}}
 h1{{font-size:1.2rem;margin:0 0 .4rem}} p{{color:#9aa3b2}}
 input{{width:100%;box-sizing:border-box;padding:.6rem .8rem;margin:.6rem 0;border:1px solid #2a2f3a;border-radius:8px;background:#0f1115;color:#e6e6e6}}
 button{{padding:.6rem 1.1rem;border:0;border-radius:8px;background:#5b8cff;color:#fff;font-weight:600;cursor:pointer}}
 code{{background:#0f1115;padding:1px 5px;border-radius:4px}}
 #log{{margin-top:1rem;color:#9aa3b2;white-space:pre-wrap}}
</style></head><body>
<div class="box">
  <h1>First-time setup</h1>
  <p>Enter the path to your <b>Corruption of Champions II</b> game folder.
     The database is built once and saved next to this app.</p>
  <input id="game" value="{guess}" placeholder="C:\\Program Files (x86)\\Steam\\steamapps\\common\\Corruption of Champions II">
  <button onclick="go()">Build database</button>
  <div id="log"></div>
</div>
<script>
async function go(){{
  const game=document.getElementById('game').value.trim();
  if(!game) return;
  document.getElementById('log').textContent='Starting…';
  await fetch('/api/setup',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{game}})}});
  poll();
}}
async function poll(){{
  try{{
    const s=await (await fetch('/api/status')).json();
    document.getElementById('log').textContent=s.error?('Error: '+s.error):(s.msg||s.phase);
    if(s.phase==='done'){{ location.href='/'; return; }}
    if(s.phase==='error') return;
  }}catch(e){{}}
  setTimeout(poll,800);
}}
</script></body></html>"""

# ── HTTP-сервер ──
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass            # тихо

    def _send(self, code, body, ctype='text/html; charset=utf-8', headers=None):
        if isinstance(body, str): body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _send_file(self, path: Path, ctype=None):
        try:
            data = path.read_bytes()
        except Exception:
            return self._send(404, 'Not found', 'text/plain')
        if not ctype:
            ctype = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        self._send(200, data, ctype, {'Cache-Control': 'no-cache'})

    def _send_json(self, obj):
        self._send(200, json.dumps(obj), 'application/json')

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ('/', '/index.html'):
            if ready():
                return self._send_file(resource_path('viewer.html'), 'text/html; charset=utf-8')
            return self._send(200, setup_html())
        if p == '/setup':                       # принудительно открыть настройку/пересборку
            return self._send(200, setup_html())
        if p == '/api/status':
            return self._send_json(STATE)
        if p == '/api/favorites':
            return self._send_json({'ids': read_favs()})
        if p == '/coc2.db':
            return self._send_file(DB, 'application/octet-stream') if DB.exists() else self._send(404, 'no db', 'text/plain')
        if p.startswith('/bitmaps/'):
            return self._serve_bitmap(urllib.parse.unquote(p[len('/bitmaps/'):]))
        if p == '/viewer.html':
            return self._send_file(resource_path('viewer.html'), 'text/html; charset=utf-8')
        return self._send(404, 'Not found', 'text/plain')

    def do_HEAD(self): self.do_GET()

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p == '/api/favorites':
            length = int(self.headers.get('Content-Length', 0) or 0)
            body = self.rfile.read(length) if length else b'{}'
            try:
                ids = json.loads(body or b'{}').get('ids', [])
            except Exception:
                ids = []
            ok = write_favs(ids)
            return self._send_json({'ok': ok})
        if p in ('/api/setup', '/api/rebuild'):
            length = int(self.headers.get('Content-Length', 0) or 0)
            body = self.rfile.read(length) if length else b'{}'
            try:
                data = json.loads(body or b'{}')
            except Exception:
                data = {}
            game = data.get('game') or CFG.get('game') or ''
            start_build(game)
            return self._send_json({'ok': True})
        return self._send(404, 'Not found', 'text/plain')

    # картинки из папки игры; защита от выхода за пределы bitmaps
    def _serve_bitmap(self, rel):
        bmp = CFG.get('bitmaps')
        if not bmp:
            return self._send(404, 'no bitmaps', 'text/plain')
        base = Path(bmp).resolve()
        target = (base / rel).resolve()
        if base not in target.parents and target != base:
            return self._send(403, 'forbidden', 'text/plain')
        if not target.is_file():
            return self._send(404, 'not found', 'text/plain')
        return self._send_file(target)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    load_config()
    # если автодетект нашёл игру, а БД ещё нет — соберём сразу при первом заходе через setup-страницу
    print(f"CoC2 Reader — http://localhost:{PORT}/")
    if ready():
        print("  database found, serving viewer")
    else:
        g = autodetect_game()
        print("  no database yet — open the page and run setup" + (f" (detected: {g})" if g else ""))
    srv = ThreadingServer(('127.0.0.1', PORT), Handler)
    threading.Timer(0.6, lambda: webbrowser.open(f'http://localhost:{PORT}/')).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()