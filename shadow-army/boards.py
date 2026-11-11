"""Read-only Kanban projection with conservative display filtering."""
from contextlib import contextmanager
from contextvars import copy_context
import os
import json
from pathlib import Path
import re
import sqlite3

def clean(value, limit=240):
    """Drop sensitive display lines; this is not a credential boundary."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value or ""))
    suspicious = (
        r"(?i)(?:bearer|password|passwd|secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|authorization)\s*[:= ]",
        r"(?i)(?:https?://[^/\s]*@|[\w.+-]+@[\w.-]+\.[a-z]{2,}|/Users/|/home/|~[/\\])",
        r"(?i)\b(?:sk-|gh[pousr]_|github_pat_|AKIA)[A-Za-z0-9_-]+",
    )
    return "\n".join(line for line in text.splitlines()
                     if not any(re.search(pattern, line) for pattern in suspicious))[:limit]


def root():
    override = os.environ.get('HERMES_KANBAN_HOME', '').strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def databases():
    home = root()
    candidates = [("default", home / "kanban.db")]
    candidates += [(p.parent.name, p) for p in sorted((home / "kanban" / "boards").glob("*/kanban.db"))]
    pinned = os.environ.get('HERMES_KANBAN_DB', '').strip()
    if pinned:
        path = Path(pinned).expanduser().resolve()
        candidates.append(('path:' + str(path), path))
    seen = set()
    for board, path in candidates:
        if path.is_file() and path.resolve() not in seen:
            seen.add(path.resolve())
            yield board, path.resolve()


@contextmanager
def connect(path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def cards():
    result, errors = [], []
    for board, path in databases():
        try:
            with connect(path) as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
                workspace = 'workspace_path' if 'workspace_path' in columns else 'NULL AS workspace_path'
                kind = 'workspace_kind' if 'workspace_kind' in columns else 'NULL AS workspace_kind'
                query = "SELECT id,title,assignee,status,result," + workspace + ',' + kind + " FROM tasks WHERE status != 'archived'"
                board_cards = [dict(row, board=board) for row in db.execute(query)]
            result.extend(board_cards)
        except (sqlite3.Error, OSError):
            errors.append('Board ' + clean(board, 128) + ': read failed')
    return result, errors


def key_for(card, board=None, dispatch=None):
    from .policy import GateKey
    card = str(card or '').strip()
    if not card or len(card) > 128:
        raise ValueError('Specify card as a nonempty card id.')
    if dispatch:
        key = completion_key(dict(task_id=card, **({'board': board} if board is not None else {})), dispatch)
        if key:
            return key
        if board is not None:
            raise ValueError('Card not found in the host board context; check its board and card id.')
    if board is None:
        matches = [r['board'] for r in cards()[0] if r['id'] == card]
        if len(matches) > 1:
            raise ValueError('Ambiguous card id; specify board.')
        if dispatch and not matches:
            raise ValueError('Card not found; check its board and card id.')
        board = matches[0] if matches else os.environ.get('HERMES_KANBAN_BOARD', 'default')
    board = str(board or 'default').strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', board):
        raise ValueError('Specify a valid board slug.')
    return GateKey(board, card)


def path_for(board):
    if board.startswith('path:'):
        return Path(board[5:])
    return root() / ('kanban.db' if board == 'default' else 'kanban/boards/' + board + '/kanban.db')


def card_row(key):
    path = path_for(key.board).resolve()
    if path.is_file():
        with connect(path) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (key.card_id,)).fetchone()
            return dict(row) if row else None
    return None


def in_work_tree(workspace):
    try:
        path = Path(workspace).expanduser().resolve()
        home = Path.home().resolve()
        if not path.is_dir():
            return False
        device = path.stat().st_dev
        while True:
            if path == home or path in home.parents or path.stat().st_dev != device:
                return False
            try:
                (path / '.git').lstat()
                from .policy import git
                try:
                    return git(workspace, 'rev-parse', '--is-inside-work-tree').strip() == b'true'
                except ValueError:
                    return False
            except FileNotFoundError:
                pass
            if path.parent == path:
                return False
            path = path.parent
    except (OSError, ValueError, RuntimeError):
        return False


def repository(key, settings):
    row = card_row(key) or {}
    kind, workspace = row.get('workspace_kind'), row.get('workspace_path')
    if kind == 'worktree':
        if not workspace or not in_work_tree(workspace):
            raise ValueError('The card worktree is missing or is not a git work tree; restore its workspace or update the card to an existing worktree.')
        return workspace
    if kind == 'dir' and workspace and in_work_tree(workspace):
        return workspace
    return settings.get('repo')


def ungated_reason(key):
    row = card_row(key) or {}
    return ('Scratch cards are never gated.' if row.get('workspace_kind') in ('scratch', None)
            else "This card's folder is not a git work tree.")


def worker_sessions(key):
    sessions = set()
    path = path_for(key.board).resolve()
    if path.is_file():
        with connect(path) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='task_runs'").fetchone():
                for run in db.execute('SELECT metadata FROM task_runs WHERE task_id=?', (key.card_id,)):
                    metadata = json.loads(run[0] or '{}')
                    if isinstance(metadata, dict) and metadata.get('worker_session_id'):
                        sessions.add(metadata['worker_session_id'])
    return sessions


def selected_board(board=None):
    context = {var.name: value for var, value in copy_context().items()}
    explicit = board if board is not None else context.get('hermes_kanban_current_board_override')
    explicit = str(explicit or '').strip().lower() or None
    pin = os.environ.get('HERMES_KANBAN_DB', '').strip()
    fenced = (context.get('hermes_kanban_pin_first_board_resolution') or
              context.get('hermes_delegated_child_context') or
              os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT') or os.environ.get('HERMES_KANBAN_TASK', '').strip())
    if pin and (not explicit or fenced):
        path = Path(pin).expanduser().resolve()
        return next((slug for slug, candidate in databases() if candidate == path), 'path:' + str(path))
    if explicit:
        return explicit
    candidates = [context.get('hermes_kanban_current_board_override'), os.environ.get('HERMES_KANBAN_BOARD')]
    for candidate in candidates:
        slug = str(candidate or '').strip().lower()
        metadata = root() / 'kanban/boards' / slug / 'board.json'
        if slug == 'default':
            return slug
        if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', slug) and metadata.is_file():
            try:
                archived = json.loads(metadata.read_text()).get('archived')
            except (OSError, ValueError):
                archived = False
            if not archived:
                return slug
    try:
        slug = (root() / 'kanban/current').read_text(encoding='utf-8-sig').strip().lower()
        if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', slug) and (slug == 'default' or
                (root() / 'kanban/boards' / slug / 'board.json').is_file()):
            return slug
    except OSError:
        pass
    return 'default'


def completion_key(args, dispatch):
    from .policy import GateKey
    try:
        result = json.loads(dispatch('kanban_show', {k: args[k] for k in ('task_id', 'board') if k in args}))
        if result.get('error') or not result.get('task'):
            return None
        key = GateKey(selected_board(args.get('board')), result['task']['id'])
        row = card_row(key)
        if row and all(row[field] == result['task'][field] for field in
                ('title', 'workspace_path', 'created_at', 'assignee', 'status', 'result') if field in row and field in result['task']):
            return key
    except (ValueError, sqlite3.Error, OSError):
        pass
    return None
