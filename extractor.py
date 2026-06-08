"""
CoC2 Scene Extractor v5.1
Запуск: python extractor.py [папка_с_js]

Ключи:
  --db         путь к SQLite (по умолч. coc2.db)
  --json       путь к JSON   (по умолч. output.json)
  --bitmaps    путь к папке bitmaps (сохраняется в БД для viewer)
  --min-chars  минимум символов текста в сцене (по умолч. 3000)
  --fresh      удалить старую БД и начать заново
"""
import re, os, json, sys, sqlite3, argparse
from pathlib import Path
from collections import Counter

DEFAULT_GAME_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Corruption of Champions II\resources\app"
DEFAULT_BITMAPS  = r"C:\Program Files (x86)\Steam\steamapps\common\Corruption of Champions II\resources\app\resources\bitmaps"

NSFW_SIGNALS = [
    'doSex','sexScene','startSex','pcSexStats','npcSexStats',
    '.cock','.vagina','.penis','.balls','.clit','.nipple',
    'areola','orgasm','climax','.cum(','cumshot','shaft',
    'pussy','.lewd','erect','moan(','thrust','penetrat',
    'stroke','groan','writhe','shudder','squirt','ejaculat',
]

STOPWORDS = {
    'the','you','your','she','her','him','his','they','them','this','that',
    'with','from','into','upon','over','under','for','and','but','not','all',
    'are','was','has','had','have','can','will','would','could','should',
    'been','being','champion','player','output','window','flags','scene',
    'intro','happy','angry','smug','blush','smile','frown','sad','surprised',
    'nude','clothed','bust','front','back','side','idle','talk','author',
    'combat','fight','battle','attack','defend','victory','he','it','its',
    'if','or','nor','yet','so','both','there','here','then','when','while',
    'after','before','though','just','still','even','only','each','every',
    'such','more','despite','however','perhaps','although','within',
    'between','during','without','through','beyond','against','as','in',
    'on','at','to','of','a','an','by','up',
}

SKIP_FUNCS = {
    'if','for','var','let','new','try','get','set','do','in','of',
    'on','at','to','by','or','is','as','it','be','no','up','go',
    'fn','cb','op','id','ok',
}

_BUST_STOP = {
    'happy','sad','angry','nude','clothed','blush','smug','smile','frown',
    'idle','talk','front','back','side','bust','default','normal','alt',
}

_FLAG_RE = re.compile(
    r'\[([A-Za-z_][A-Za-z0-9_.]*(?:\.[A-Za-z0-9_.]+|_[A-Za-z0-9_]+|[A-Z]{2,}[A-Za-z0-9_]*))'
    r'\s*\|\s*([^|\]]+?)(?:\s*\|[^\]]*)*\]'
)

SCENE_RE = re.compile(
    r'window\.(\w+)\s*=\s*(?:function\s*\w*\s*\([^)]*\)|\([^)]*\)\s*=>|\w+\s*=>)\s*\{'
)

# ── Утилиты ────────────────────────────────────────────────────────────────────

def decode_js_string(s):
    return (s.replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\'))

_PLAYER_SENT = '\x00PLAYER\x00'

def _is_condition(seg):
    """Похоже ли это на игровое условие/тег, а не на отображаемый текст."""
    s = seg.strip()
    if not s:
        return False
    if re.match(r'(?i)^(pc|npc|player|flags?|party|game|gamemode|save\w*|tags?|'
                r'scene|self|target|monster|enemy|quest|time|world|crew)\b', s):
        return True
    if re.search(r'(==|!=|>=|<=|=>|>|<|&&|\|\||\.has\b|\.have\b|\.is[A-Z]\w*\b)', s):
        return True
    # короткий идентификатор с точкой без пробелов: pc.cock, flags.x
    if ' ' not in s and re.search(r'[a-z]\.[A-Za-z]', s):
        return True
    # вид "pc.heightRange 60 68 76": идентификатор с точкой + аргументы
    if re.search(r'\.', s) and re.match(r'^[A-Za-z_][\w.]*(\s+[\w.+\-]+)+$', s):
        return True
    return False

_NAME_TAG = re.compile(
    r"(?i)^(pc|npc|player)\.(name|firstname|first_name|lastname|uname|nickname|fullname|displayname)\b")

def _humanize(seg):
    """Свойство тела/состояния → читаемое слово: 'pc.lowerBody' → 'lower body'."""
    s = seg.strip().split('.')[-1]
    s = re.sub(r'\(.*$', '', s)                       # убрать (аргументы)
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', s)     # camelCase → camel Case
    return s.strip().lower()

def _resolve_tag(content):
    """Разрешает содержимое одной пары [ ... ] (без вложенных скобок)."""
    if '|' not in content:
        c = content.strip()
        if not c:
            return ''
        if c.upper() == 'PLAYER' or c.lower() in ('player', 'pc'):
            return _PLAYER_SENT
        if _NAME_TAG.match(c):                         # только обращения по ИМЕНИ → [PLAYER]
            return _PLAYER_SENT
        m = re.search(r'\(\s*"([^"]*)"', c)            # функция-выбор: mfn("man","woman",…) → первый
        if '(' in c and m:
            return m.group(1).strip()
        if re.match(r'(?i)^(pc|npc|player)\.', c):     # свойство тела персонажа → слово (arms, cock…)
            return _humanize(c)
        if _is_condition(c):                           # прочее условие/флаг → выкидываем
            return ''
        return c                                       # просто текст в скобках
    parts = content.split('|')
    if _is_condition(parts[0]):
        for p in parts[1:]:    # первый сегмент — условие → берём первый вариант текста
            if p.strip():
                return p.strip()
        return ''
    for p in parts:            # выбор вида [ед.ч.|мн.ч.] → берём первый непустой
        if p.strip():
            return p.strip()
    return ''

def _strip_tags(text):
    # схлопываем переводы строк внутри скобок
    text = re.sub(r'\[[\s\S]*?\]', lambda m: re.sub(r'\s*\n\s*', ' ', m.group(0)), text)
    inner = re.compile(r'\[([^\[\]]*)\]')
    for _ in range(25):        # разрешаем самые внутренние скобки, пока есть изменения
        new = inner.sub(lambda m: _resolve_tag(m.group(1)), text)
        if new == text:
            break
        text = new
    text = text.replace('[', '').replace(']', '')   # осиротевшие скобки
    text = text.replace(_PLAYER_SENT, '[PLAYER]')
    return text

def clean_text(raw):
    text = decode_js_string(raw)

    text = _strip_tags(text)

    text = re.sub(r'<b>(.*?)</b>',          r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<i>(.*?)</i>',          r'*\1*',   text, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>',        r'*\1*',   text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)

    text = (text.replace('&amp;','&').replace('&lt;','<').replace('&gt;','>')
                .replace('&quot;','"').replace('&#39;',"'").replace('&nbsp;',' '))

    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# ── Извлечение нарратива ────────────────────────────────────────────────────────
# Весь текст игры идёт через  output(textify( VAR || (VAR = HELPER([ "...текст..." ])) ))
# где HELPER — минифицированный тег-шаблон (S, Se, me, q, …). Якоримся на textify(
# и достаём массив строк, не угадывая имя хелпера.

def _extract_string_array(s, open_idx):
    """s[open_idx] == '[' → конкатенация строковых литералов до парной ']'
    (учитывает кавычки ' " ` и экранирование)."""
    parts, buf = [], []
    i, depth, quote = open_idx + 1, 1, None
    n = len(s)
    while i < n and depth > 0:
        ch = s[i]
        if quote:
            if ch == '\\':
                buf.append(s[i:i+2]); i += 2; continue
            if ch == quote:
                parts.append(''.join(buf)); buf = []; quote = None
            else:
                buf.append(ch)
            i += 1
        else:
            if ch in '"\'`':
                quote = ch
            elif ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
            i += 1
    return parts

def extract_texts(body):
    """Список сырых текстовых блоков сцены (в порядке появления)."""
    texts = []
    for m in re.finditer(r'\btextify\s*\(', body):
        j = body.find('[', m.end())
        if j == -1 or (j - m.end()) > 80:      # массив строк должен быть рядом
            continue
        raw = ''.join(_extract_string_array(body, j))
        if raw.strip():
            texts.append(raw)
    if texts:
        return texts
    # Резерв: нарратив без textify — массивы с «прозой» (длинные строки с пробелами)
    for m in re.finditer(r'[A-Za-z_$][\w$]*\s*\(\s*\[', body):
        j = body.find('[', m.start())
        raw = ''.join(_extract_string_array(body, j))
        if len(raw) >= 60 and ' ' in raw and re.search(r'[a-z]{3}', raw) \
           and ('.' in raw or ',' in raw or '\\n' in raw):
            texts.append(raw)
    return texts

# ── Парсер скобок ──────────────────────────────────────────────────────────────

def find_brace(s, start):
    d = 0
    for i in range(start, len(s)):
        if s[i] == '{': d += 1
        elif s[i] == '}':
            d -= 1
            if d == 0: return i
    return len(s) - 1

def find_paren(s, start):
    d = 0
    for i in range(start, len(s)):
        if s[i] == '(': d += 1
        elif s[i] == ')':
            d -= 1
            if d == 0: return i
    return len(s) - 1

def find_open_brace(s, pos):
    """Ищет { после pos. Возвращает None если до { встречается ; (однострочный if без блока)."""
    n = len(s)
    i = pos
    while i < n:
        c = s[i]
        if c == '{': return i
        if c == ';': return None
        if c in ' \t\n\r': i += 1; continue
        if c not in '()': return None
        i += 1
    return None

# ── Определение функции текста ─────────────────────────────────────────────────

def find_text_func(content, scene_matches):
    """Webpack минифицирует имя функции L() по-разному в каждом файле.
    Определяем динамически по частоте вызовов с длинными строками."""
    func_cnt = Counter()
    step = max(1, len(scene_matches) // 20)
    for m in scene_matches[::step]:
        bs = m.end() - 1
        be = find_brace(content, bs)
        body = content[bs+1:be]
        for fname in re.findall(r'(\w{1,3})\(\["[^"]{10,}"\]\)', body):
            if fname not in SKIP_FUNCS:
                func_cnt[fname] += 1
    if not func_cnt:
        return None
    return func_cnt.most_common(1)[0][0]

def make_L_re(func_name):
    return re.compile(rf'{re.escape(func_name)}\(\["([\s\S]+?)"\]\)')

# ── Парсер if/else ─────────────────────────────────────────────────────────────

def flat_texts(block, L_RE):
    return [t for t in (clean_text(r) for r in L_RE.findall(block)) if t]

def parse_segments(body, L_RE):
    segs, pos, n = [], 0, len(body)

    while pos < n:
        m = re.search(r'\bif\s*\(', body[pos:])
        if not m:
            for t in flat_texts(body[pos:], L_RE):
                segs.append({'type': 'always', 'text': t})
            break

        abs_if = pos + m.start()
        for t in flat_texts(body[pos:abs_if], L_RE):
            segs.append({'type': 'always', 'text': t})

        chain_pos = abs_if
        variants = []

        while chain_pos < n:
            chunk = body[chain_pos:]
            m_elif = re.match(r'else\s+if\s*\(', chunk)
            m_else = re.match(r'else\s*\{', chunk)
            m_if   = re.match(r'if\s*\(', chunk)

            if m_elif or m_if:
                mat = m_elif or m_if
                ps = chain_pos + mat.end() - 1
                pe = find_paren(body, ps)
                cond = body[ps+1:pe].strip()
                bp = find_open_brace(body, pe + 1)
            elif m_else:
                cond = 'else'
                bp = chain_pos + m_else.end() - 1
            else:
                break

            if bp is None:
                mat = m_elif or m_if or m_else
                chain_pos = chain_pos + mat.end()
                break

            be = find_brace(body, bp)
            texts = flat_texts(body[bp+1:be], L_RE)
            combined = '\n'.join(texts)
            if combined:
                variants.append({'condition': cond, 'text': combined})
            chain_pos = be + 1

            while chain_pos < n and body[chain_pos] in ' \t\n\r':
                chain_pos += 1

            if chain_pos >= n or not body[chain_pos:chain_pos+4].startswith('else'):
                break

        if variants:
            if len(variants) == 1:
                segs.append({'type': 'always',
                             'text': variants[0]['text'],
                             'condition': variants[0]['condition']})
            else:
                segs.append({'type': 'branch', 'variants': variants})

        pos = chain_pos

    return segs

# ── Персонажи ──────────────────────────────────────────────────────────────────

_AUTHOR_BLACKLIST = {
    'Champion','Author','Output','Window','Flags','Player',
    'Gardeford','Balak','Savin','Wsan','Tobs','Skow','Alypia','Frogapus','Scar',
}

def _split_camel(s):
    """'brienneSparring' / 'brint_intro' → ['brienne','Sparring'] и т.п."""
    s = re.sub(r'[_\-]', ' ', s)
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', s)
    return [w for w in s.split() if w]

def extract_chars(func_name, body, segments, freq, strong_out=None):
    found = Counter()
    strong = set()   # надёжные имена: явные вызовы игры (бюст-арт / объект NPC)

    for name in re.findall(r'showName\("([^"]{2,40})"\)', body):
        if re.match(r"^[A-Z][A-Za-z\-' ]+$", name):
            found[name.strip()] += 10

    # getNPC/getChar — явный объект персонажа: считаем надёжным
    for name in re.findall(r'(?:getNPC|getChar|getCharacter)\("([^"]{2,40})"\)', body):
        if re.match(r"^[A-Z][A-Za-z\-' ]+$", name):
            found[name.strip()] += 8
            strong.add(name.strip())

    # showBust — игра явно показывает арт персонажа: самый надёжный сигнал
    for bust_call in re.findall(r'showBust\(\[([^\]]+)\]\)', body):
        for raw in re.findall(r'"([^"]+)"', bust_call):
            base = raw.split('_')[0].lower()
            if base not in _BUST_STOP and len(base) >= 3:
                found[base.capitalize()] += 6
                strong.add(base.capitalize())

    for raw in re.findall(r'showBust\("([^"]+)"\)', body):
        base = raw.split('_')[0].lower()
        if base not in _BUST_STOP and len(base) >= 3:
            found[base.capitalize()] += 5
            strong.add(base.capitalize())

    # слова из НАЗВАНИЯ сцены — частый источник главного персонажа,
    # даже если по имени он в тексте почти не упоминается
    title_tokens = set()
    for w in _split_camel(func_name):
        tok = w.capitalize()
        if tok.lower() not in STOPWORDS and len(tok) >= 3:
            found[tok] += 2
            title_tokens.add(tok)

    # полный текст сцены (с вариантами ветвлений) — для подсчёта упоминаний имени
    parts = []
    for s in segments:
        if s.get('text'):
            parts.append(s['text'])
        for v in s.get('variants', []):
            if v.get('text'):
                parts.append(v['text'])
    scene_text = ' '.join(parts)

    for tok in re.findall(r'\b([A-Z][a-z]{2,})\b', scene_text):
        if tok.lower() not in STOPWORDS:
            found[tok] += 1

    def mentions(n):
        return len(re.findall(r'\b' + re.escape(n) + r'\b', scene_text, re.I))

    # Имя привязывается к сцене, если: оно есть в названии сцены, либо игра явно
    # вызывает его арт/NPC (showBust/getNPC), либо оно реально упоминается > 3 раз.
    # (Ложные имена без картинки/явного сигнала позже отсекаются прунингом.)
    result = sorted(
        n for n in found
        if n not in _AUTHOR_BLACKLIST
        and (n in strong or n in title_tokens or mentions(n) > 3)
    )
    if strong_out is not None:
        strong_out |= {n for n in strong if n not in _AUTHOR_BLACKLIST}
    return result

def infer_tags(func_name, is_nsfw):
    tags, nl = [], func_name.lower()
    if any(x in nl for x in ['sex','fuck','lewd','cum','oral','anal','nipple']):
        tags.append('nsfw'); is_nsfw = True
    if any(x in nl for x in ['combat','fight','battle','attack','lose','win']):
        tags.append('combat')
    if any(x in nl for x in ['intro','first','meet','initial']): tags.append('introduction')
    if any(x in nl for x in ['talk','chat','convo','dialog','banter']): tags.append('dialogue')
    if any(x in nl for x in ['quest','mission']): tags.append('quest')
    if any(x in nl for x in ['camp','rest','sleep']): tags.append('camp')
    if is_nsfw and 'nsfw' not in tags: tags.append('nsfw')
    if not tags: tags.append('scene')
    return tags

# ── SQLite ─────────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE scenes (
    id         TEXT PRIMARY KEY,
    file       TEXT NOT NULL,
    nsfw       INTEGER NOT NULL DEFAULT 0,
    tags       TEXT,
    char_count INTEGER DEFAULT 0
);
CREATE TABLE segments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id  TEXT NOT NULL REFERENCES scenes(id),
    pos       INTEGER NOT NULL,
    type      TEXT NOT NULL,
    condition TEXT,
    text      TEXT
);
CREATE TABLE variants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    pos        INTEGER NOT NULL,
    condition  TEXT,
    text       TEXT
);
CREATE TABLE characters (name TEXT PRIMARY KEY);
CREATE TABLE scene_characters (
    scene_id  TEXT NOT NULL REFERENCES scenes(id),
    char_name TEXT NOT NULL REFERENCES characters(name),
    PRIMARY KEY (scene_id, char_name)
);
CREATE INDEX idx_seg_scene ON segments(scene_id);
CREATE INDEX idx_var_seg   ON variants(segment_id);
CREATE INDEX idx_sc_scene  ON scene_characters(scene_id);
CREATE INDEX idx_sc_char   ON scene_characters(char_name);
CREATE TABLE character_images (
    char_name TEXT NOT NULL,
    path      TEXT NOT NULL,
    PRIMARY KEY (char_name, path)
);
CREATE INDEX idx_ci_char ON character_images(char_name);
CREATE TABLE cg_images (path TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE fts USING fts5(scene_id UNINDEXED, text, tokenize='unicode61');
"""

def open_db(db_path):
    exists = os.path.exists(db_path)
    con = sqlite3.connect(db_path)
    if not exists:
        con.executescript(DDL)
        con.commit()
    return con

def insert_scene(con, scene):
    cur = con.cursor()
    cur.execute("SELECT 1 FROM scenes WHERE id=?", (scene['id'],))
    if cur.fetchone():
        return  # дубликат из другого файла — пропускаем

    cur.execute("INSERT INTO scenes VALUES (?,?,?,?,?)",
        (scene['id'], scene['file'], int(scene['nsfw']),
         json.dumps(scene['tags'], ensure_ascii=False), scene['char_count']))

    for pos, seg in enumerate(scene.get('segments', [])):
        cur.execute(
            "INSERT INTO segments(scene_id,pos,type,condition,text) VALUES (?,?,?,?,?)",
            (scene['id'], pos, seg['type'], seg.get('condition'), seg.get('text')))
        seg_id = cur.lastrowid
        if seg.get('text'):
            try: cur.execute("INSERT INTO fts VALUES (?,?)", (scene['id'], seg['text']))
            except: pass
        for vpos, v in enumerate(seg.get('variants', [])):
            cur.execute(
                "INSERT INTO variants(segment_id,pos,condition,text) VALUES (?,?,?,?)",
                (seg_id, vpos, v.get('condition'), v.get('text')))
            if v.get('text'):
                try: cur.execute("INSERT INTO fts VALUES (?,?)", (scene['id'], v['text']))
                except: pass

    for char in scene.get('characters', []):
        cur.execute("INSERT OR IGNORE INTO characters VALUES (?)", (char,))
        cur.execute("INSERT OR IGNORE INTO scene_characters VALUES (?,?)", (scene['id'], char))

# ── Сканирование картинок (кэш путей в БД) ─────────────────────────────────────
# Картинки не имеют единой структуры суффиксов. Картинка считается принадлежащей
# персонажу, если нормализованное имя встречается как подстрока в имени файла.

_IMG_EXTS  = {'.webp', '.png', '.jpg', '.jpeg', '.gif'}
_IMG_DIRS  = ['crops', 'combatBusts']

def _norm_name(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())

def _name_keys(char):
    keys = []
    full = _norm_name(char)
    if len(full) >= 3:
        keys.append(full)
    words = sorted((_norm_name(w) for w in re.split(r'[^A-Za-z0-9]+', char)),
                   key=len, reverse=True)
    words = [w for w in words if len(w) >= 4]
    if words and words[0] not in keys:
        keys.append(words[0])
    return keys

def _base_token(stem):
    """Базовый идентификатор по имени файла: часть до первого '_' или '.'.
    'brienne_happy' -> 'brienne', 'arened.Moira.0' -> 'arened'."""
    return _norm_name(re.split(r'[._]', stem, 1)[0])

def scan_bitmaps_images(con, bitmaps_path, strong_names=None, do_prune=False):
    """Проходит crops/ и combatBusts/, при необходимости отбрасывает «персонажей»,
    не подтверждённых ни явным вызовом игры, ни реальной картинкой, и кладёт
    относительные пути картинок в таблицу character_images."""
    base = Path(bitmaps_path)
    if not base.is_dir():
        print(f"  bitmaps не найдены ({bitmaps_path}) — пропускаю скан картинок")
        return

    con.execute("""CREATE TABLE IF NOT EXISTS character_images (
        char_name TEXT NOT NULL, path TEXT NOT NULL,
        PRIMARY KEY (char_name, path))""")
    con.execute("DELETE FROM character_images")

    # (rel_path, нормализованный stem); попутно собираем базовые токены и полные stem'ы
    files = []
    base_tokens, full_stems = set(), set()
    for sub in _IMG_DIRS:
        d = base / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in _IMG_EXTS:
                files.append((f"{sub}/{p.name}", _norm_name(p.stem)))
                base_tokens.add(_base_token(p.stem))
                full_stems.add(_norm_name(p.stem))
    print(f"  Картинок в bitmaps: {len(files)}")

    strong_names = strong_names or set()

    # «Имя подтверждено картинкой», если какой-то его ключ точно равен базовому токену
    # файла или полному stem'у (строго, без подстрок — чтобы 'mom' не цеплялся к чужим файлам)
    def img_backed(name):
        for k in _name_keys(name):
            if k in base_tokens or k in full_stems:
                return True
        return False

    # ── Прунинг ложных имён ──
    if do_prune:
        all_chars = [r[0] for r in con.execute("SELECT name FROM characters")]
        drop = [n for n in all_chars if n not in strong_names and not img_backed(n)]
        cur = con.cursor()
        for n in drop:
            cur.execute("DELETE FROM scene_characters WHERE char_name=?", (n,))
            cur.execute("DELETE FROM characters WHERE name=?", (n,))
        con.commit()
        print(f"  Отброшено ложных имён: {len(drop)} (осталось: {len(all_chars) - len(drop)})")
        if drop:
            sample = ', '.join(sorted(drop)[:12])
            print(f"    напр.: {sample}{' …' if len(drop) > 12 else ''}")

    # ── Сопоставление картинок оставшимся персонажам (по подстроке) ──
    chars = [r[0] for r in con.execute("SELECT name FROM characters")]
    cur = con.cursor()
    total = 0
    for name in chars:
        keys = _name_keys(name)
        if not keys:
            continue
        matched = [(rel, stem) for (rel, stem) in files
                   if any(k in stem for k in keys)]
        matched.sort(key=lambda t: (0 if any(t[1].startswith(k) for k in keys) else 1, t[0]))
        for rel, _ in matched:
            cur.execute("INSERT OR IGNORE INTO character_images VALUES (?,?)", (name, rel))
            total += 1
    con.commit()
    matched_chars = con.execute(
        "SELECT COUNT(DISTINCT char_name) FROM character_images").fetchone()[0]
    print(f"  Сопоставлено путей: {total} (персонажей с картинками: {matched_chars})")

    # ── CG-галерея: все картинки из папки CG (без фильтрации) ──
    con.execute("CREATE TABLE IF NOT EXISTS cg_images (path TEXT PRIMARY KEY)")
    con.execute("DELETE FROM cg_images")
    cg_dir = base / 'CG'
    cg_n = 0
    if cg_dir.is_dir():
        for p in sorted(cg_dir.rglob('*')):
            if p.is_file() and p.suffix.lower() in _IMG_EXTS:
                rel = 'CG/' + p.relative_to(cg_dir).as_posix()
                con.execute("INSERT OR IGNORE INTO cg_images VALUES (?)", (rel,))
                cg_n += 1
        con.commit()
    print(f"  CG-картинок: {cg_n}")

# ── Прогресс-бар ───────────────────────────────────────────────────────────────

try:
    from tqdm import tqdm as _tqdm
    def make_progress(iterable, **kw): return _tqdm(iterable, **kw)
except ImportError:
    def make_progress(iterable, total=None, desc='', **kw):
        items = list(iterable)
        n = total or len(items)
        class _P:
            def __init__(self, it): self._it = iter(it); self._i = 0
            def __iter__(self): return self
            def __next__(self):
                item = next(self._it); self._i += 1
                pct = self._i * 100 // n
                bar = '\u2588' * (pct // 5) + '\u2591' * (20 - pct // 5)
                print(f'\r{desc} [{bar}] {self._i}/{n}', end='', flush=True)
                return item
            def __enter__(self): return self
            def __exit__(self, *a): print()
        return _P(items)

# ── Чекпоинт ───────────────────────────────────────────────────────────────────

CHECKPOINT_FILE = 'extractor_checkpoint.json'

def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE): return set()
    try:
        with open(CHECKPOINT_FILE, encoding='utf-8') as f:
            data = json.load(f)
        done = set(data.get('done_files', []))
        print(f"Чекпоинт: {len(done)} файлов уже обработано, продолжаем...")
        return done
    except Exception as e:
        print(f"Чекпоинт повреждён ({e}), начинаем заново")
        return set()

def save_checkpoint(done_files):
    with open(CHECKPOINT_FILE, 'w', encoding='utf-8') as f:
        json.dump({'done_files': sorted(done_files)}, f)

def clear_checkpoint():
    if os.path.exists(CHECKPOINT_FILE): os.remove(CHECKPOINT_FILE)

# ── Основной парсинг ───────────────────────────────────────────────────────────

def extract_all(folder, db_path, json_path, bitmaps_path='', min_chars=200, resume=True, recursive=False):
    folder = Path(folder)
    js_files = sorted(folder.rglob('*.js') if recursive else folder.glob('*.js'))
    print(f"JS-файлов найдено: {len(js_files)}" + (" (рекурсивно)" if recursive else ""))

    done_files = load_checkpoint() if resume else set()
    full_run = not done_files          # прунинг имён безопасен только при полном прогоне
    todo = [f for f in js_files if f.name not in done_files]
    if done_files:
        print(f"Пропускаем {len(done_files)} обработанных, осталось: {len(todo)}")

    freq = Counter()
    g_strong = set()                   # надёжные имена (showBust/getNPC) со всех сцен
    if done_files and os.path.exists(db_path):
        con_tmp = sqlite3.connect(db_path)
        for (name,) in con_tmp.execute("SELECT name FROM characters"):
            freq[name] += 3
        con_tmp.close()

    print("Шаг 1/2: сбор имён NPC по всем файлам...")
    for jp in make_progress(todo, desc="  Scan"):
        try: content = jp.read_text(encoding='utf-8', errors='replace')
        except: continue
        for name in re.findall(r'showName\("([^"]{2,40})"\)', content):
            if re.match(r"^[A-Z][A-Za-z\-' ]+$", name): freq[name.strip()] += 1
        for name in re.findall(r'(?:getNPC|getChar)\("([^"]{2,40})"\)', content):
            if re.match(r"^[A-Z][A-Za-z\-' ]+$", name): freq[name.strip()] += 1
        for bust_call in re.findall(r'showBust\(\[([^\]]+)\]\)', content):
            for raw in re.findall(r'"([^"]+)"', bust_call):
                base = raw.split('_')[0]
                if base.lower() not in _BUST_STOP and len(base) >= 3:
                    freq[base.capitalize()] += 1
    print(f"  Уникальных NPC: {len(freq)}")

    con = open_db(db_path)
    if bitmaps_path:
        bm = bitmaps_path.replace('\\\\', '/').replace('\\', '/').rstrip('/') + '/'
        con.execute("INSERT OR REPLACE INTO meta VALUES ('bitmaps_path',?)", (bm,))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('min_chars',?)", (str(min_chars),))
    con.commit()

    processed = 0
    stats = Counter()

    print(f"Шаг 2/2: парсинг сцен (мин. {min_chars} символов)...")
    try:
        with make_progress(todo, desc="  Parse") as pbar:
            for jp in pbar:
                try:
                    content = jp.read_text(encoding='utf-8', errors='replace')
                except Exception as e:
                    print(f"\n  Пропуск {jp.name}: {e}")
                    done_files.add(jp.name); continue

                scene_matches = list(SCENE_RE.finditer(content))
                if not scene_matches:
                    done_files.add(jp.name); continue

                for m in scene_matches:
                    fn = m.group(1)
                    bs = m.end() - 1
                    be = find_brace(content, bs)
                    body = content[bs+1:be]

                    raw_texts = extract_texts(body)
                    if not raw_texts:
                        stats['no_text'] += 1
                        continue

                    segs = []
                    for rt in raw_texts:
                        ct = clean_text(rt)
                        if ct.strip():
                            segs.append({'type': 'always', 'condition': None, 'text': ct})
                    if not segs:
                        stats['no_text'] += 1
                        continue

                    all_text = ' '.join(s['text'] for s in segs)
                    if len(all_text.strip()) < min_chars:
                        stats['too_short'] += 1
                        continue

                    is_nsfw = any(sig in body for sig in NSFW_SIGNALS)
                    scene = {
                        'id':         fn,
                        'file':       jp.name,
                        'nsfw':       is_nsfw,
                        'characters': extract_chars(fn, body, segs, freq, g_strong),
                        'tags':       infer_tags(fn, is_nsfw),
                        'char_count': len(all_text),
                        'segments':   segs,
                    }
                    insert_scene(con, scene)
                    stats['ok'] += 1

                con.commit()
                done_files.add(jp.name)
                processed += 1
                if processed % 10 == 0:
                    save_checkpoint(done_files)

    except KeyboardInterrupt:
        print("\n\nПрерывание! Сохраняем прогресс...")
        con.commit()
        save_checkpoint(done_files)
        con.close()
        print(f"Чекпоинт сохранён. Обработано: {len(done_files)}/{len(js_files)}")
        sys.exit(0)

    if bitmaps_path:
        print("Шаг 3/3: сканирование картинок и валидация имён...")
        try:
            scan_bitmaps_images(con, bitmaps_path, strong_names=g_strong, do_prune=full_run)
            if not full_run:
                print("  (прунинг имён пропущен — запусти с --fresh для чистого списка персонажей)")
        except Exception as e:
            print(f"  Скан картинок пропущен: {e}")

    con.close()
    clear_checkpoint()
    _export_json(db_path, json_path, freq, min_chars, stats)

def _export_json(db_path, json_path, freq, min_chars, stats=None):
    print("Сохраняем output.json...")
    con = sqlite3.connect(db_path)
    all_scenes = []
    for (sid, sfile, snsfw, stags, scc) in con.execute(
            "SELECT id,file,nsfw,tags,char_count FROM scenes"):
        segs = []
        for row in con.execute(
                "SELECT id,pos,type,condition,text FROM segments WHERE scene_id=? ORDER BY pos", [sid]):
            seg = {'type': row[2], 'condition': row[3], 'text': row[4]}
            if row[2] == 'branch':
                seg['variants'] = [
                    {'condition': r[0], 'text': r[1]}
                    for r in con.execute(
                        "SELECT condition,text FROM variants WHERE segment_id=? ORDER BY pos", [row[0]])
                ]
                del seg['condition'], seg['text']
            segs.append(seg)
        chars = [r[0] for r in con.execute(
            "SELECT char_name FROM scene_characters WHERE scene_id=?", [sid])]
        all_scenes.append({
            'id': sid, 'file': sfile, 'nsfw': bool(snsfw),
            'characters': chars, 'tags': json.loads(stags or '[]'),
            'char_count': scc, 'segments': segs
        })
    con.close()
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_scenes, f, ensure_ascii=False, indent=2)
    nsfw_c = sum(1 for s in all_scenes if s['nsfw'])
    print(f"\n{'='*52}")
    print(f"  Готово!")
    print(f"  Сцен всего:      {len(all_scenes)}")
    print(f"  NSFW:            {nsfw_c}")
    print(f"  Персонажей:      {len(freq)}")
    print(f"  Мин. символов:   {min_chars}")
    if stats:
        print(f"  Слишком кратких: {stats['too_short']}")
        print(f"  Ошибок парсинга: {stats['parse_err']}")
        print(f"  Без ф-ции текста:{stats['no_func']}")
    print(f"  БД:              {db_path}")
    print(f"  JSON:            {json_path}")
    print(f"{'='*52}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CoC2 Scene Extractor v5.1')
    parser.add_argument('folder',      nargs='?',   default=DEFAULT_GAME_DIR)
    parser.add_argument('--db',        default='coc2.db')
    parser.add_argument('--json',      default='output.json')
    parser.add_argument('--bitmaps',   default=DEFAULT_BITMAPS)
    parser.add_argument('--min-chars', type=int, default=1000)
    parser.add_argument('--fresh',     action='store_true',
                        help='Удалить старую БД и начать заново')
    args = parser.parse_args()

    print("CoC2 Scene Extractor v5.1")
    print(f"  Папка:         {args.folder}")
    print(f"  Bitmaps:       {args.bitmaps}")
    print(f"  Мин. символов: {args.min_chars}")
    if args.fresh:
        clear_checkpoint()
        if os.path.exists(args.db): os.remove(args.db)
        print("  Режим: --fresh")
    print()
    extract_all(args.folder, args.db, args.json, args.bitmaps,
                args.min_chars, resume=not args.fresh)