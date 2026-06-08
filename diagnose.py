#!/usr/bin/env python3
"""
Диагностика структуры сцен CoC2.
Показывает, как объявляются сцены, какие функции вывода текста используются и
СКОЛЬКО сцен теряет текущий extractor и ПОЧЕМУ.

Запуск:
    python diagnose.py "C:\\...\\resources\\app\\resources\\scenes"
или без аргумента — возьмёт DEFAULT_GAME_DIR из extractor.py.

Ничего не меняет, только читает и печатает отчёт.
"""
import re, sys, collections
from pathlib import Path

import extractor as ex  # переиспользуем регэкспы и парсер

# Широкий поиск любых window.X = ... присваиваний (чтобы сравнить с узким SCENE_RE)
ANY_WINDOW = re.compile(r'window\.(\w+)\s*=\s*(.{0,40})', re.S)
RHS_FUNC_NOARG  = re.compile(r'^function\s*\(\s*\)')
RHS_FUNC_ARG    = re.compile(r'^function\s*\w*\s*\([^)]+\)')
RHS_ARROW_NOARG = re.compile(r'^\(\s*\)\s*=>')
RHS_ARROW_ARG   = re.compile(r'^(\([^)]*\)|\w+)\s*=>')

CALL_ARR = re.compile(r'([A-Za-z_$][\w$]*)\s*\(\s*\[')      # ident(["..."])
CALL_STR = re.compile(r'([A-Za-z_$][\w$]*)\s*\(\s*["\'`]')  # ident("...")


def classify_rhs(rhs):
    rhs = rhs.lstrip()
    if RHS_FUNC_NOARG.match(rhs):  return 'function()'
    if RHS_FUNC_ARG.match(rhs):    return 'function(args)'
    if RHS_ARROW_NOARG.match(rhs): return 'arrow()'
    if RHS_ARROW_ARG.match(rhs):   return 'arrow(args)'
    return 'other (не функция)'


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else ex.DEFAULT_GAME_DIR)
    js_files = sorted(folder.glob('*.js'))
    print(f"Папка: {folder}")
    print(f"JS-файлов: {len(js_files)}\n")
    if not js_files:
        print("!! Файлы не найдены. Укажи папку аргументом.")
        return

    rhs_kinds = collections.Counter()
    n_any = 0
    n_scene_re = 0
    n_has_textfunc = 0
    n_missing_marker = 0
    n_no_textfunc_file = 0
    call_arr_global = collections.Counter()
    call_str_global = collections.Counter()
    missed_samples = []        # сцены с window-def, но без маркера text_func(["
    big_missed = []            # из них — крупные (тело > 2000)
    rhs_other_samples = []
    body_sizes_ok = []
    body_sizes_missed = []

    for jp in js_files:
        try:
            content = jp.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue

        for m in ANY_WINDOW.finditer(content):
            n_any += 1
            rhs_kinds[classify_rhs(m.group(2))] += 1
            if classify_rhs(m.group(2)) == 'other (не функция)' and len(rhs_other_samples) < 8:
                rhs_other_samples.append((jp.name, m.group(1), m.group(2).strip()[:40]))

        scene_matches = list(ex.SCENE_RE.finditer(content))
        n_scene_re += len(scene_matches)
        if not scene_matches:
            continue

        text_func = ex.find_text_func(content, scene_matches)
        if not text_func:
            n_no_textfunc_file += len(scene_matches)
            continue
        marker = f'{text_func}(["'

        for sm in scene_matches:
            fn = sm.group(1)
            bs = sm.end() - 1
            be = ex.find_brace(content, bs)
            body = content[bs + 1:be]
            size = len(body)

            # какие функции реально зовут с массивом/строкой внутри тела
            for c in CALL_ARR.findall(body): call_arr_global[c] += 1
            for c in CALL_STR.findall(body): call_str_global[c] += 1

            if marker in body:
                n_has_textfunc += 1
                body_sizes_ok.append(size)
            else:
                n_missing_marker += 1
                body_sizes_missed.append(size)
                # чем этот сцен-блок выводит текст?
                arr_calls = collections.Counter(CALL_ARR.findall(body))
                str_calls = collections.Counter(CALL_STR.findall(body))
                top = (arr_calls + str_calls).most_common(4)
                if len(missed_samples) < 12:
                    snippet = re.sub(r'\s+', ' ', body[:160])
                    missed_samples.append((jp.name, fn, size, text_func, top, snippet))
                if size > 2000 and len(big_missed) < 12:
                    snippet = re.sub(r'\s+', ' ', body[:200])
                    big_missed.append((jp.name, fn, size, text_func, top, snippet))

    print("=" * 70)
    print("ОБЪЯВЛЕНИЯ window.X = ...")
    print(f"  всего найдено:                 {n_any}")
    for k, v in rhs_kinds.most_common():
        print(f"    {k:24} {v}")
    print(f"  ловит текущий SCENE_RE:        {n_scene_re}")
    print(f"  → теряется на этом этапе ~       {n_any - n_scene_re} "
          f"(в основном 'other', их в сцены и не надо)")

    print("\n" + "=" * 70)
    print("РАЗБОР СЦЕН, пойманных SCENE_RE:")
    print(f"  с маркером text_func([\"  (парсятся): {n_has_textfunc}")
    print(f"  БЕЗ маркера (теряются):              {n_missing_marker}")
    print(f"  в файлах без распознанной text-функции: {n_no_textfunc_file}")

    def dist(name, arr):
        if not arr: 
            print(f"  {name}: нет"); return
        arr = sorted(arr)
        n = len(arr)
        print(f"  {name}: n={n}, медиана={arr[n//2]}, "
              f">2000симв={sum(1 for x in arr if x>2000)}, "
              f">5000симв={sum(1 for x in arr if x>5000)}")
    print("\nРазмеры тел сцен:")
    dist("  пойманные", body_sizes_ok)
    dist("  потерянные", body_sizes_missed)

    print("\n" + "=" * 70)
    print("ТОП функций, вызываемых с массивом  ident([\"...\"]  :")
    for c, v in call_arr_global.most_common(12):
        print(f"    {c:24} {v}")
    print("\nТОП функций, вызываемых со строкой  ident(\"...\"   :")
    for c, v in call_str_global.most_common(12):
        print(f"    {c:24} {v}")

    if rhs_other_samples:
        print("\n" + "=" * 70)
        print("Примеры window.X = (не функция) — обычно НЕ сцены:")
        for f, name, rhs in rhs_other_samples:
            print(f"    [{f}] {name} = {rhs}…")

    if missed_samples:
        print("\n" + "=" * 70)
        print("ПОТЕРЯННЫЕ сцены (есть window-def, но нет маркера). Чем выводят текст:")
        for f, fn, size, tf, top, snip in missed_samples:
            print(f"  • {fn}  ({size} симв, файл {f}; ожидался '{tf}')")
            print(f"      вызовы: {top}")
            print(f"      нач.: {snip}…")

    if big_missed:
        print("\n" + "=" * 70)
        print("КРУПНЫЕ потерянные сцены (>2000 симв) — то, чего тебе не хватает:")
        for f, fn, size, tf, top, snip in big_missed:
            print(f"  • {fn}  ({size} симв, файл {f})")
            print(f"      вызовы: {top}")
            print(f"      нач.: {snip}…")

    print("\n" + "=" * 70)
    print("ИТОГ: пришли этот вывод — по нему видно, какие функции вывода добавить")
    print("в парсер и какие формы объявления сцен он сейчас не ловит.")


if __name__ == '__main__':
    main()