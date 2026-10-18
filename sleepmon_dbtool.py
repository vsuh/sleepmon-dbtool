#!/usr/bin/env python3
"""
sleepmon_dbtool.py — резервное копирование и восстановление карточек
дневника самочувствия (Obsidian, каталог 55-sleepmon) в компактную
SQLite-базу.

Ключевая идея: состав полей frontmatter не фиксируется жёстко в схеме БД.
Весь YAML-блок карточки хранится как один JSON-объект в текстовой колонке.
Поэтому появление/удаление/переименование полей между карточками (или со
временем) не ломает ни бэкап, ни восстановление — программе всё равно,
какие именно ключи лежат внутри frontmatter.

Команды:
    backup   — просканировать каталог с карточками и сохранить/обновить их в БД
    restore  — восстановить карточки из БД обратно в .md-файлы
    list     — показать, что лежит в БД
    verify   — сравнить текущее состояние каталога с БД, ничего не меняя

Пример использования см. README.md.
"""

import argparse
import fnmatch
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# If a daily-form field disappears accidentally, keep the last backed-up value.
# Explicit YAML nulls are preserved because the key is still present.
PRESERVE_IF_MISSING_FIELDS = ("alco", "well_being", "sleep_quality")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    path            TEXT PRIMARY KEY,   -- относительный путь от --root
    note_date       TEXT,               -- YYYY-MM-DD, если удалось определить
    frontmatter     TEXT NOT NULL,      -- YAML frontmatter как JSON-объект
    body            TEXT NOT NULL,      -- тело заметки (раздел "## Заметки" и т.д.)
    content_hash    TEXT NOT NULL,      -- sha256(frontmatter+body) для отслеживания изменений
    file_mtime      REAL,
    file_size       INTEGER,
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    last_run_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cards_date ON cards(note_date);
CREATE INDEX IF NOT EXISTS idx_cards_deleted ON cards(is_deleted);

CREATE TABLE IF NOT EXISTS backup_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    root_path            TEXT NOT NULL,
    files_scanned        INTEGER,
    files_new            INTEGER,
    files_updated        INTEGER,
    files_unchanged      INTEGER,
    files_marked_deleted INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ParsedNote:
    frontmatter: dict
    body: str


def parse_note(raw: str) -> ParsedNote:
    m = FRONTMATTER_RE.match(raw)
    if not m:
        # Файл без frontmatter — считаем всё тело как body, свойства пустые.
        return ParsedNote(frontmatter={}, body=raw)
    fm_text, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"не удалось разобрать YAML frontmatter: {e}")
    if not isinstance(fm, dict):
        raise ValueError("frontmatter не является отображением ключ-значение")
    return ParsedNote(frontmatter=fm, body=body)


def serialize_note(frontmatter: dict, body: str) -> str:
    fm_text = yaml.dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).rstrip("\n")
    body = body.strip("\n")
    parts = ["---", fm_text, "---"]
    if body:
        parts.append("")
        parts.append(body)
    return "\n".join(parts) + "\n"


def guess_note_date(rel_path: Path, frontmatter: dict) -> Optional[str]:
    for key in ("created", "date"):
        val = frontmatter.get(key)
        if val is None:
            continue
        if hasattr(val, "isoformat"):  # datetime.date / datetime.datetime из YAML
            return val.isoformat()
        if isinstance(val, str) and DATE_IN_NAME_RE.fullmatch(val.strip()):
            return val.strip()
    m = DATE_IN_NAME_RE.search(rel_path.name)
    if m:
        return m.group(1)
    return None


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def iter_markdown_files(root: Path):
    for p in sorted(root.rglob("*.md")):
        if p.is_file():
            yield p


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #

def cmd_backup(args):
    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.exit(f"Каталог не найден: {root}")
    conn = open_db(Path(args.db))
    cur = conn.cursor()

    run_started = now_iso()
    cur.execute(
        "INSERT INTO backup_runs (started_at, root_path) VALUES (?, ?)",
        (run_started, str(root)),
    )
    run_id = cur.lastrowid

    seen_paths = set()
    n_new = n_updated = n_unchanged = 0
    errors = []

    for f in iter_markdown_files(root):
        rel = f.relative_to(root)
        rel_str = str(rel).replace("\\", "/")
        seen_paths.add(rel_str)
        try:
            raw = f.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            errors.append((rel_str, f"кодировка: {e}"))
            continue
        try:
            parsed = parse_note(raw)
        except ValueError as e:
            errors.append((rel_str, str(e)))
            continue

        # Restore only absent fields from the previous backup. This prevents an
        # accidental omission in a Markdown file from erasing these values.
        cur.execute("SELECT frontmatter FROM cards WHERE path = ?", (rel_str,))
        previous_row = cur.fetchone()
        if previous_row is not None:
            previous_frontmatter = json.loads(previous_row[0])
            for key in PRESERVE_IF_MISSING_FIELDS:
                if key not in parsed.frontmatter and key in previous_frontmatter:
                    parsed.frontmatter[key] = previous_frontmatter[key]

        # Храним frontmatter в исходном порядке ключей (для аккуратного restore),
        # а хеш считаем по канонической (отсортированной) форме, чтобы простое
        # изменение порядка полей в файле не считалось "изменением содержимого".
        fm_json = json.dumps(
            parsed.frontmatter, ensure_ascii=False, sort_keys=False, default=str
        )
        canonical_json = json.dumps(
            parsed.frontmatter, ensure_ascii=False, sort_keys=True, default=str
        )
        content_hash = sha256_text(canonical_json + "\x00" + parsed.body)
        note_date = guess_note_date(rel, parsed.frontmatter)
        stat = f.stat()
        ts = now_iso()

        cur.execute("SELECT content_hash FROM cards WHERE path = ?", (rel_str,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """INSERT INTO cards
                   (path, note_date, frontmatter, body, content_hash,
                    file_mtime, file_size, is_deleted, first_seen_at,
                    last_seen_at, last_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (rel_str, note_date, fm_json, parsed.body, content_hash,
                 stat.st_mtime, stat.st_size, ts, ts, run_id),
            )
            n_new += 1
        elif row[0] != content_hash:
            cur.execute(
                """UPDATE cards SET note_date=?, frontmatter=?, body=?,
                   content_hash=?, file_mtime=?, file_size=?, is_deleted=0,
                   last_seen_at=?, last_run_id=? WHERE path=?""",
                (note_date, fm_json, parsed.body, content_hash,
                 stat.st_mtime, stat.st_size, ts, run_id, rel_str),
            )
            n_updated += 1
        else:
            cur.execute(
                "UPDATE cards SET is_deleted=0, last_seen_at=?, last_run_id=? WHERE path=?",
                (ts, run_id, rel_str),
            )
            n_unchanged += 1

    n_deleted = 0
    if args.prune:
        cur.execute("SELECT path FROM cards WHERE is_deleted = 0")
        stale = [r[0] for r in cur.fetchall() if r[0] not in seen_paths]
        for p in stale:
            if args.hard_prune:
                cur.execute("DELETE FROM cards WHERE path=?", (p,))
            else:
                cur.execute(
                    "UPDATE cards SET is_deleted=1, last_seen_at=? WHERE path=?",
                    (now_iso(), p),
                )
        n_deleted = len(stale)

    cur.execute(
        """UPDATE backup_runs SET finished_at=?, files_scanned=?, files_new=?,
           files_updated=?, files_unchanged=?, files_marked_deleted=?
           WHERE id=?""",
        (now_iso(), len(seen_paths), n_new, n_updated, n_unchanged, n_deleted, run_id),
    )
    conn.commit()
    conn.close()

    print(f"Каталог: {root}")
    print(f"Просканировано файлов: {len(seen_paths)}")
    print(f"  новых:         {n_new}")
    print(f"  изменённых:    {n_updated}")
    print(f"  без изменений: {n_unchanged}")
    if args.prune:
        suffix = " (удалены из БД физически)" if args.hard_prune else " (помечены is_deleted=1)"
        print(f"  пропавших на диске: {n_deleted}{suffix}")
    if errors:
        print(f"Ошибки разбора ({len(errors)}):", file=sys.stderr)
        for path, msg in errors:
            print(f"  {path}: {msg}", file=sys.stderr)
    print(f"БД: {Path(args.db).resolve()}")


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #

def cmd_restore(args):
    conn = open_db(Path(args.db))
    cur = conn.cursor()

    query = "SELECT path, frontmatter, body FROM cards WHERE 1=1"
    params = []
    if not args.include_deleted:
        query += " AND is_deleted = 0"
    if args.date_from:
        query += " AND note_date >= ?"
        params.append(args.date_from)
    if args.date_to:
        query += " AND note_date <= ?"
        params.append(args.date_to)
    query += " ORDER BY path"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    if args.path_glob:
        rows = [r for r in rows if fnmatch.fnmatch(r[0], args.path_glob)]

    out_root = Path(args.out).resolve()
    n_written = n_skipped = 0
    for path, fm_json, body in rows:
        target = out_root / path
        frontmatter = json.loads(fm_json)
        content = serialize_note(frontmatter, body)

        if args.dry_run:
            action = "перезаписать" if target.exists() else "создать"
            print(f"[dry-run] {action}: {target}")
            n_written += 1
            continue

        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if existing == content:
                n_skipped += 1
                continue
            if not args.force:
                print(f"Пропущено (файл существует и отличается, нужен --force): {target}")
                n_skipped += 1
                continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        n_written += 1

    print(f"Отобрано: {len(rows)}, записано: {n_written}, пропущено: {n_skipped}")
    print(f"Каталог назначения: {out_root}")


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #

def cmd_list(args):
    conn = open_db(Path(args.db))
    cur = conn.cursor()
    query = "SELECT path, note_date, is_deleted, last_seen_at FROM cards WHERE 1=1"
    params = []
    if not args.include_deleted:
        query += " AND is_deleted = 0"
    if args.date_from:
        query += " AND note_date >= ?"
        params.append(args.date_from)
    if args.date_to:
        query += " AND note_date <= ?"
        params.append(args.date_to)
    query += " ORDER BY note_date, path"
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    for path, note_date, is_deleted, last_seen in rows:
        flag = "  [удалён]" if is_deleted else ""
        print(f"{note_date or '??????????':10}  {path}{flag}")
    print(f"Всего показано: {len(rows)}")


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def cmd_verify(args):
    root = Path(args.root).resolve()
    conn = open_db(Path(args.db))
    cur = conn.cursor()
    cur.execute("SELECT path, content_hash, is_deleted FROM cards")
    db_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    conn.close()

    disk_paths = set()
    changed, unchanged, new_on_disk = [], [], []
    for f in iter_markdown_files(root):
        rel = str(f.relative_to(root)).replace("\\", "/")
        disk_paths.add(rel)
        raw = f.read_text(encoding="utf-8")
        try:
            parsed = parse_note(raw)
        except ValueError:
            changed.append(rel)
            continue
        canonical_json = json.dumps(
            parsed.frontmatter, ensure_ascii=False, sort_keys=True, default=str
        )
        h = sha256_text(canonical_json + "\x00" + parsed.body)
        if rel not in db_map:
            new_on_disk.append(rel)
        elif db_map[rel][0] != h:
            changed.append(rel)
        else:
            unchanged.append(rel)

    missing_on_disk = [
        p for p, (h, is_deleted) in db_map.items()
        if is_deleted == 0 and p not in disk_paths
    ]

    active_in_db = sum(1 for _, d in db_map.values() if d == 0)
    print(f"На диске: {len(disk_paths)}   Активных записей в БД: {active_in_db}")
    print(f"Без изменений:            {len(unchanged)}")
    print(f"Изменены с посл. бэкапа:  {len(changed)}")
    for p in changed:
        print(f"  ~ {p}")
    print(f"Новые (ещё не в БД):      {len(new_on_disk)}")
    for p in new_on_disk:
        print(f"  + {p}")
    print(f"Пропали на диске:         {len(missing_on_disk)}")
    for p in missing_on_disk:
        print(f"  - {p}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Бэкап/восстановление карточек самочувствия (Obsidian) в компактную SQLite-БД"
    )
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", help="Просканировать каталог и сохранить карточки в БД")
    b.add_argument("--root", required=True, help="Каталог с карточками, напр. .../55-sleepmon")
    b.add_argument("--db", required=True, help="Путь к файлу SQLite")
    b.add_argument("--prune", action="store_true",
                    help="Пометить пропавшие на диске карточки как удалённые (is_deleted=1)")
    b.add_argument("--hard-prune", action="store_true",
                    help="Вместе с --prune: удалять записи физически, а не помечать")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("restore", help="Восстановить карточки из БД в файлы")
    r.add_argument("--db", required=True)
    r.add_argument("--out", required=True,
                    help="Каталог назначения (обычно тот же путь, что был --root при бэкапе)")
    r.add_argument("--path-glob", help='Фильтр по пути, напр. "2026/08/*.md"')
    r.add_argument("--date-from", help="YYYY-MM-DD")
    r.add_argument("--date-to", help="YYYY-MM-DD")
    r.add_argument("--include-deleted", action="store_true",
                    help="Восстанавливать и карточки, помеченные как удалённые")
    r.add_argument("--force", action="store_true",
                    help="Перезаписывать существующие файлы, если содержимое отличается")
    r.add_argument("--dry-run", action="store_true",
                    help="Только показать, что было бы сделано, ничего не писать")
    r.set_defaults(func=cmd_restore)

    l = sub.add_parser("list", help="Показать карточки, хранящиеся в БД")
    l.add_argument("--db", required=True)
    l.add_argument("--date-from")
    l.add_argument("--date-to")
    l.add_argument("--include-deleted", action="store_true")
    l.add_argument("--limit", type=int)
    l.set_defaults(func=cmd_list)

    v = sub.add_parser("verify", help="Сравнить текущее состояние каталога с БД (без изменений)")
    v.add_argument("--root", required=True)
    v.add_argument("--db", required=True)
    v.set_defaults(func=cmd_verify)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
