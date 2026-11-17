"""
Exam Seat Plan Management System
Barpeta Polytechnic - Examination Cell
Flask + SQLite | ES5 frontend | Gunicorn/Nginx deployment
"""
import os
import re
import csv
import io
import sqlite3
import secrets
import datetime
from collections import deque, OrderedDict
from functools import wraps

from flask import (Flask, g, render_template, request, redirect, url_for,
                   flash, session, Response, abort)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('SEATPLAN_DB', os.path.join(BASE_DIR, 'seatplan.db'))


def _secret_key():
    if os.environ.get('SECRET_KEY'):
        return os.environ['SECRET_KEY']
    path = os.path.join(BASE_DIR, '.secret_key')
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_secret_key(),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS rooms(
    id INTEGER PRIMARY KEY,
    room_no TEXT UNIQUE NOT NULL,
    building TEXT DEFAULT '',
    floor TEXT DEFAULT '',
    rows INTEGER NOT NULL,
    cols INTEGER NOT NULL,
    capacity INTEGER NOT NULL,
    accessible INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    remarks TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS students(
    id INTEGER PRIMARY KEY,
    roll_no TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    branch TEXT NOT NULL,
    semester TEXT DEFAULT '',
    pwd INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS exams(
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    exam_date TEXT DEFAULT '',
    session TEXT DEFAULT '',
    time_slot TEXT DEFAULT '',
    mode TEXT DEFAULT 'column',
    gap INTEGER DEFAULT 0,
    exclude_rolls TEXT DEFAULT '',
    instructions TEXT DEFAULT '',
    created_at TEXT,
    generated_at TEXT,
    unallocated INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS exam_groups(
    id INTEGER PRIMARY KEY,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    branch TEXT NOT NULL,
    semester TEXT DEFAULT '',
    subject TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exam_rooms(
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    sort_order INTEGER DEFAULT 0,
    invigilators TEXT DEFAULT '',
    PRIMARY KEY(exam_id, room_id)
);
CREATE TABLE IF NOT EXISTS allocations(
    id INTEGER PRIMARY KEY,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    seat_no INTEGER,
    r INTEGER,
    c INTEGER,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES exam_groups(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_alloc_exam ON allocations(exam_id, room_id);
CREATE INDEX IF NOT EXISTS idx_alloc_student ON allocations(student_id);
"""

DEFAULT_SETTINGS = OrderedDict([
    ('institute_name', 'Barpeta Polytechnic'),
    ('institute_name_as', 'বৰপেটা পলিটেকনিক'),
    ('exam_cell', 'Examination Cell'),
    ('address', 'Barpeta, Assam'),
    ('signatory', 'Officer-in-Charge, Examination'),
])
DEFAULT_BRANCHES = ['CE', 'ME', 'BME']
DEFAULT_SEMESTERS = ['1', '2', '3', '4', '5', '6']
SESSIONS = ['Morning', 'Afternoon', 'Evening']
MODES = OrderedDict([
    ('column', 'Column-wise alternate — branch changes every column (recommended)'),
    ('checker', 'Checkerboard — no same group side-by-side or front-back'),
    ('sequential', 'Sequential — strict roll order, no mixing'),
])
GROUP_COLORS = ['#dbeafe', '#fde68a', '#bbf7d0', '#fbcfe8', '#ddd6fe',
                '#fed7aa', '#a5f3fc', '#e2e8f0', '#fecaca', '#d9f99d']


# ---------------------------------------------------------------- database
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        db.execute('INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)', (k, v))
    if not db.execute('SELECT 1 FROM users').fetchone():
        pw = os.environ.get('ADMIN_PASSWORD', 'admin@123')
        db.execute('INSERT INTO users(username, password_hash) VALUES(?, ?)',
                   ('admin', generate_password_hash(pw)))
    db.commit()
    db.close()


init_db()


def get_settings():
    if 'settings' not in g:
        g.settings = dict(DEFAULT_SETTINGS)
        for row in get_db().execute('SELECT key, value FROM settings'):
            g.settings[row['key']] = row['value']
    return g.settings


# ---------------------------------------------------------------- helpers
def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s or '')]


def norm_roll(s):
    return (s or '').strip().upper()


def parse_rolls(text):
    return set(norm_roll(t) for t in re.split(r'[\s,;]+', text or '') if t.strip())


def to_int(v, default=0):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return default


def compress_rolls(rolls):
    """['BP01','BP02','BP03','BP07'] -> ['BP01 – BP03', 'BP07']"""
    out, cur = [], None
    for r in rolls:
        m = re.match(r'^(.*?)(\d+)(\D*)$', r)
        pre, num, width, suf = (m.group(1), int(m.group(2)), len(m.group(2)), m.group(3)) if m else (r, None, 0, '')
        if (cur and num is not None and cur['num'] is not None and cur['pre'] == pre
                and cur['suf'] == suf and cur['width'] == width and cur['num'] + 1 == num):
            cur['num'] = num
            cur['end'] = r
            cur['count'] += 1
        else:
            if cur:
                out.append(cur)
            cur = {'pre': pre, 'suf': suf, 'num': num, 'width': width, 'start': r, 'end': r, 'count': 1}
    if cur:
        out.append(cur)
    return [c['start'] if c['count'] == 1 else '%s – %s' % (c['start'], c['end']) for c in out]


def seat_label(r, c):
    row = chr(65 + r) if r < 26 else 'R%d' % (r + 1)
    return '%s%d' % (row, c + 1)


def room_positions(room, gap):
    pos = [(r, c) for r in range(room['rows']) for c in range(room['cols'])][:room['capacity']]
    if gap:
        pos = [p for p in pos if p[1] % 2 == 0]
    return pos


def all_branches():
    found = [r['branch'] for r in get_db().execute('SELECT DISTINCT branch FROM students ORDER BY branch')]
    return sorted(set(found + DEFAULT_BRANCHES))


def all_semesters():
    found = [r['semester'] for r in get_db().execute(
        "SELECT DISTINCT semester FROM students WHERE semester<>''")]
    return sorted(set(found + DEFAULT_SEMESTERS), key=natural_key)


# ---------------------------------------------------------------- auth / csrf
def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


@app.before_request
def csrf_protect():
    if request.method == 'POST':
        token = request.form.get('_csrf')
        if not token or token != session.get('_csrf'):
            abort(400, 'Invalid or expired form token. Please reload the page.')


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        return fn(*a, **kw)
    return wrapper


@app.context_processor
def inject():
    return {'S': get_settings(), 'csrf_token': csrf_token, 'MODES': MODES,
            'logged_in': bool(session.get('user_id')),
            'today': datetime.date.today().strftime('%d-%m-%Y')}


@app.template_filter('dmy')
def dmy(value):
    try:
        return datetime.datetime.strptime(value, '%Y-%m-%d').strftime('%d-%m-%Y')
    except (ValueError, TypeError):
        return value or ''


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = get_db().execute('SELECT * FROM users WHERE username=?',
                             (request.form.get('username', '').strip(),)).fetchone()
        if u and check_password_hash(u['password_hash'], request.form.get('password', '')):
            session.clear()
            session['user_id'] = u['id']
            session['username'] = u['username']
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('lookup'))


# ---------------------------------------------------------------- dashboard
@app.route('/')
@login_required
def dashboard():
    db = get_db()
    stats = {
        'rooms': db.execute('SELECT COUNT(*) FROM rooms WHERE active=1').fetchone()[0],
        'seats': db.execute('SELECT COALESCE(SUM(capacity),0) FROM rooms WHERE active=1').fetchone()[0],
        'students': db.execute('SELECT COUNT(*) FROM students').fetchone()[0],
        'exams': db.execute('SELECT COUNT(*) FROM exams').fetchone()[0],
    }
    by_branch = db.execute('SELECT branch, semester, COUNT(*) n FROM students '
                           'GROUP BY branch, semester ORDER BY branch, semester').fetchall()
    exams = db.execute('SELECT * FROM exams ORDER BY exam_date DESC, id DESC LIMIT 8').fetchall()
    return render_template('dashboard.html', stats=stats, by_branch=by_branch, exams=exams)


# ---------------------------------------------------------------- rooms
@app.route('/rooms')
@login_required
def rooms():
    rows = get_db().execute('SELECT * FROM rooms ORDER BY active DESC, building, room_no').fetchall()
    rows = sorted(rows, key=lambda r: (-r['active'], r['building'].lower(), natural_key(r['room_no'])))
    total = sum(r['capacity'] for r in rows if r['active'])
    return render_template('rooms.html', rooms=rows, total=total)


@app.route('/rooms/new', methods=['GET', 'POST'])
@app.route('/rooms/<int:rid>/edit', methods=['GET', 'POST'])
@login_required
def room_form(rid=None):
    db = get_db()
    room = db.execute('SELECT * FROM rooms WHERE id=?', (rid,)).fetchone() if rid else None
    if rid and not room:
        abort(404)
    if request.method == 'POST':
        f = request.form
        rows_, cols_ = to_int(f.get('rows')), to_int(f.get('cols'))
        cap = to_int(f.get('capacity')) or rows_ * cols_
        room_no = f.get('room_no', '').strip()
        if not room_no or rows_ < 1 or cols_ < 1:
            flash('Room number, rows and columns are required.', 'error')
        elif cap > rows_ * cols_:
            flash('Capacity cannot exceed rows × columns (%d).' % (rows_ * cols_), 'error')
        else:
            data = (room_no, f.get('building', '').strip(), f.get('floor', '').strip(), rows_, cols_, cap,
                    1 if f.get('accessible') else 0, 1 if f.get('active') else 0, f.get('remarks', '').strip())
            try:
                if room:
                    db.execute('UPDATE rooms SET room_no=?, building=?, floor=?, rows=?, cols=?, capacity=?, '
                               'accessible=?, active=?, remarks=? WHERE id=?', data + (rid,))
                else:
                    db.execute('INSERT INTO rooms(room_no, building, floor, rows, cols, capacity, accessible, '
                               'active, remarks) VALUES(?,?,?,?,?,?,?,?,?)', data)
                db.commit()
                flash('Room %s saved (capacity %d).' % (room_no, cap), 'success')
                return redirect(url_for('rooms'))
            except sqlite3.IntegrityError:
                flash('Room number %s already exists.' % room_no, 'error')
    return render_template('room_form.html', room=room)


@app.route('/rooms/<int:rid>/delete', methods=['POST'])
@login_required
def room_delete(rid):
    db = get_db()
    db.execute('DELETE FROM rooms WHERE id=?', (rid,))
    db.commit()
    flash('Room deleted.', 'success')
    return redirect(url_for('rooms'))


# ---------------------------------------------------------------- students
@app.route('/students')
@login_required
def students():
    db = get_db()
    branch, sem, q = request.args.get('branch', ''), request.args.get('semester', ''), request.args.get('q', '').strip()
    sql, p = 'SELECT * FROM students WHERE 1=1', []
    if branch:
        sql += ' AND branch=?'; p.append(branch)
    if sem:
        sql += ' AND semester=?'; p.append(sem)
    if q:
        sql += ' AND (roll_no LIKE ? OR name LIKE ?)'; p += ['%' + q + '%', '%' + q + '%']
    rows = sorted(db.execute(sql, p).fetchall(), key=lambda s: (s['branch'], natural_key(s['semester']), natural_key(s['roll_no'])))
    return render_template('students.html', students=rows, branches=all_branches(), semesters=all_semesters(),
                           f={'branch': branch, 'semester': sem, 'q': q})


@app.route('/students/add', methods=['POST'])
@login_required
def student_add():
    f = request.form
    roll, branch = norm_roll(f.get('roll_no')), f.get('branch', '').strip().upper()
    if not roll or not branch:
        flash('Roll number and branch are required.', 'error')
    else:
        try:
            get_db().execute('INSERT INTO students(roll_no, name, branch, semester, pwd) VALUES(?,?,?,?,?)',
                             (roll, f.get('name', '').strip(), branch, f.get('semester', '').strip(),
                              1 if f.get('pwd') else 0))
            get_db().commit()
            flash('Student %s added.' % roll, 'success')
        except sqlite3.IntegrityError:
            flash('Roll number %s already exists.' % roll, 'error')
    return redirect(url_for('students', branch=branch))


@app.route('/students/bulk', methods=['POST'])
@login_required
def student_bulk():
    f = request.form
    prefix, suffix = norm_roll(f.get('prefix')), norm_roll(f.get('suffix'))
    start, end, pad = to_int(f.get('start'), -1), to_int(f.get('end'), -1), to_int(f.get('pad'))
    branch, sem = f.get('branch', '').strip().upper(), f.get('semester', '').strip()
    if start < 0 or end < start or not branch:
        flash('Enter a valid start/end number and branch.', 'error')
        return redirect(url_for('students'))
    if end - start > 5000:
        flash('Range too large (max 5000 at a time).', 'error')
        return redirect(url_for('students'))
    db, added = get_db(), 0
    for n in range(start, end + 1):
        roll = '%s%s%s' % (prefix, str(n).zfill(pad), suffix)
        cur = db.execute('INSERT OR IGNORE INTO students(roll_no, branch, semester) VALUES(?,?,?)', (roll, branch, sem))
        added += cur.rowcount
    db.commit()
    skipped = (end - start + 1) - added
    flash('%d roll numbers added for %s Sem %s.%s' % (added, branch, sem or '-',
          (' %d already existed and were skipped.' % skipped) if skipped else ''), 'success')
    return redirect(url_for('students', branch=branch, semester=sem))


@app.route('/students/import', methods=['POST'])
@login_required
def student_import():
    file = request.files.get('csvfile')
    if not file or not file.filename:
        flash('Choose a CSV file.', 'error')
        return redirect(url_for('students'))
    try:
        text = file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        flash('File must be UTF-8 encoded CSV (Excel: Save As → CSV UTF-8).', 'error')
        return redirect(url_for('students'))
    update = bool(request.form.get('update'))
    reader = csv.DictReader(io.StringIO(text))
    alias = {'roll': 'roll_no', 'roll no': 'roll_no', 'rollno': 'roll_no', 'roll_number': 'roll_no',
             'roll number': 'roll_no', 'student name': 'name', 'sem': 'semester', 'department': 'branch',
             'dept': 'branch', 'divyang': 'pwd', 'pwd': 'pwd'}
    db, added, updated, bad = get_db(), 0, 0, 0
    for raw in reader:
        row = {}
        for k, v in raw.items():
            if k is None:
                continue
            key = k.strip().lower()
            row[alias.get(key, key)] = (v or '').strip()
        roll, branch = norm_roll(row.get('roll_no')), row.get('branch', '').upper()
        if not roll or not branch:
            bad += 1
            continue
        pwd = 1 if row.get('pwd', '').lower() in ('1', 'y', 'yes', 'true') else 0
        vals = (row.get('name', ''), branch, row.get('semester', ''), pwd)
        if db.execute('SELECT 1 FROM students WHERE roll_no=?', (roll,)).fetchone():
            if update:
                db.execute('UPDATE students SET name=?, branch=?, semester=?, pwd=? WHERE roll_no=?', vals + (roll,))
                updated += 1
        else:
            db.execute('INSERT INTO students(name, branch, semester, pwd, roll_no) VALUES(?,?,?,?,?)', vals + (roll,))
            added += 1
    db.commit()
    flash('Import complete: %d added, %d updated, %d rows skipped (missing roll/branch).' % (added, updated, bad), 'success')
    return redirect(url_for('students'))


@app.route('/students/template.csv')
@login_required
def student_template():
    body = 'roll_no,name,branch,semester,pwd\nBP23CE001,Student Name,CE,3,0\nBP23ME001,Another Name,ME,3,1\n'
    return Response(body, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=students_template.csv'})


@app.route('/students/<int:sid>/edit', methods=['GET', 'POST'])
@login_required
def student_edit(sid):
    db = get_db()
    s = db.execute('SELECT * FROM students WHERE id=?', (sid,)).fetchone()
    if not s:
        abort(404)
    if request.method == 'POST':
        f = request.form
        try:
            db.execute('UPDATE students SET roll_no=?, name=?, branch=?, semester=?, pwd=? WHERE id=?',
                       (norm_roll(f.get('roll_no')), f.get('name', '').strip(), f.get('branch', '').strip().upper(),
                        f.get('semester', '').strip(), 1 if f.get('pwd') else 0, sid))
            db.commit()
            flash('Student updated.', 'success')
            return redirect(url_for('students', branch=f.get('branch', '').strip().upper()))
        except sqlite3.IntegrityError:
            flash('That roll number already exists.', 'error')
    return render_template('student_form.html', s=s, branches=all_branches(), semesters=all_semesters())


@app.route('/students/delete', methods=['POST'])
@login_required
def student_delete():
    db = get_db()
    ids = [to_int(i) for i in request.form.getlist('ids') if to_int(i)]
    if request.form.get('scope') == 'filter':
        branch, sem = request.form.get('branch', ''), request.form.get('semester', '')
        if not branch:
            flash('Select a branch filter before bulk delete.', 'error')
            return redirect(url_for('students'))
        sql, p = 'DELETE FROM students WHERE branch=?', [branch]
        if sem:
            sql += ' AND semester=?'; p.append(sem)
        n = db.execute(sql, p).rowcount
    elif ids:
        n = db.execute('DELETE FROM students WHERE id IN (%s)' % ','.join('?' * len(ids)), ids).rowcount
    else:
        n = 0
    db.commit()
    flash('%d student(s) deleted.' % n, 'success')
    return redirect(request.referrer or url_for('students'))


# ---------------------------------------------------------------- exams
def exam_or_404(eid):
    e = get_db().execute('SELECT * FROM exams WHERE id=?', (eid,)).fetchone()
    if not e:
        abort(404)
    return e


def exam_rooms_rows(eid):
    rows = get_db().execute('SELECT r.*, er.sort_order, er.invigilators FROM exam_rooms er '
                            'JOIN rooms r ON r.id=er.room_id WHERE er.exam_id=?', (eid,)).fetchall()
    return sorted(rows, key=lambda r: (r['sort_order'], natural_key(r['room_no'])))


def exam_groups_rows(eid):
    return get_db().execute('SELECT * FROM exam_groups WHERE exam_id=? ORDER BY id', (eid,)).fetchall()


def exam_queues(exam):
    db = get_db()
    excl = parse_rolls(exam['exclude_rolls'])
    seen, queues = set(), []
    for gr in exam_groups_rows(exam['id']):
        sql, p = 'SELECT * FROM students WHERE branch=?', [gr['branch']]
        if gr['semester']:
            sql += ' AND semester=?'; p.append(gr['semester'])
        studs = [s for s in db.execute(sql, p).fetchall() if s['roll_no'] not in excl and s['id'] not in seen]
        studs.sort(key=lambda s: natural_key(s['roll_no']))
        seen.update(s['id'] for s in studs)
        queues.append({'group': gr, 'students': studs})
    return queues


def group_label(gr):
    lbl = gr['branch'] + ((' Sem ' + gr['semester']) if gr['semester'] else '')
    return lbl + ((' · ' + gr['subject']) if gr['subject'] else '')


def generate_plan(exam):
    """Allocate seats. Returns (allocated_count, unallocated_count)."""
    db = get_db()
    queues = exam_queues(exam)
    rooms_ = list(exam_rooms_rows(exam['id']))
    gap, mode, k = bool(exam['gap']), exam['mode'], len(queues)
    step = 2 if gap else 1

    # PwD candidates go to accessible rooms first, front seats
    pwd = deque((s, qi) for qi, q in enumerate(queues) for s in q['students'] if s['pwd'])
    dq = [deque(s for s in q['students'] if not s['pwd']) for q in queues]
    if pwd:
        rooms_.sort(key=lambda r: 0 if r['accessible'] else 1)

    alloc = []
    for room in rooms_:
        pos = room_positions(room, gap)
        seatno = dict((p, i + 1) for i, p in enumerate(pos))
        grid = {}
        for p in pos:  # row-major = front rows first
            if not pwd:
                break
            s, qi = pwd.popleft()
            grid[p] = qi
            alloc.append((exam['id'], room['id'], seatno[p], p[0], p[1], s['id'], queues[qi]['group']['id']))
        order = sorted(pos, key=lambda p: (p[1], p[0])) if mode == 'column' else pos
        for p in order:
            if p in grid:
                continue
            if not any(dq):
                break
            r, c = p
            if mode == 'sequential':
                qi = next(i for i in range(k) if dq[i])
            else:
                if mode == 'column':
                    pref = (c // step) % k
                    nbrs = [(r, c - step), (r, c + step)]
                else:
                    pref = (r + c // step) % k
                    nbrs = [(r, c - step), (r, c + step), (r - 1, c), (r + 1, c)]
                taken = set(grid.get(x) for x in nbrs)
                cand = [pref] + sorted([i for i in range(k) if i != pref], key=lambda i: -len(dq[i]))
                qi = next((i for i in cand if dq[i] and i not in taken), None)
                if qi is None:
                    qi = next(i for i in cand if dq[i])
            s = dq[qi].popleft()
            grid[p] = qi
            alloc.append((exam['id'], room['id'], seatno[p], r, c, s['id'], queues[qi]['group']['id']))

    unallocated = sum(len(d) for d in dq) + len(pwd)
    db.execute('DELETE FROM allocations WHERE exam_id=?', (exam['id'],))
    db.executemany('INSERT INTO allocations(exam_id, room_id, seat_no, r, c, student_id, group_id) '
                   'VALUES(?,?,?,?,?,?,?)', alloc)
    db.execute('UPDATE exams SET generated_at=?, unallocated=? WHERE id=?',
               (datetime.datetime.now().strftime('%d-%m-%Y %H:%M'), unallocated, exam['id']))
    db.commit()
    return len(alloc), unallocated


@app.route('/exams')
@login_required
def exams():
    rows = get_db().execute(
        'SELECT e.*, (SELECT COUNT(*) FROM allocations a WHERE a.exam_id=e.id) allocated, '
        '(SELECT COUNT(*) FROM exam_rooms er WHERE er.exam_id=e.id) nrooms '
        'FROM exams e ORDER BY e.exam_date DESC, e.id DESC').fetchall()
    return render_template('exams.html', exams=rows)


@app.route('/exams/new', methods=['GET', 'POST'])
@app.route('/exams/<int:eid>/edit', methods=['GET', 'POST'])
@login_required
def exam_form(eid=None):
    db = get_db()
    exam = exam_or_404(eid) if eid else None
    if request.method == 'POST':
        f = request.form
        title = f.get('title', '').strip()
        groups = []
        for b, s, sub in zip(f.getlist('group_branch'), f.getlist('group_sem'), f.getlist('group_subject')):
            if b.strip():
                groups.append((b.strip().upper(), s.strip(), sub.strip()))
        room_ids = [to_int(r) for r in f.getlist('room_ids') if to_int(r)]
        if not title or not groups or not room_ids:
            flash('Title, at least one branch group and at least one room are required.', 'error')
        else:
            mode = f.get('mode') if f.get('mode') in MODES else 'column'
            vals = (title, f.get('exam_date', ''), f.get('session', ''), f.get('time_slot', '').strip(), mode,
                    1 if f.get('gap') else 0, f.get('exclude_rolls', '').strip(), f.get('instructions', '').strip())
            if exam:
                db.execute('UPDATE exams SET title=?, exam_date=?, session=?, time_slot=?, mode=?, gap=?, '
                           'exclude_rolls=?, instructions=?, generated_at=NULL, unallocated=0 WHERE id=?', vals + (eid,))
                db.execute('DELETE FROM allocations WHERE exam_id=?', (eid,))
                db.execute('DELETE FROM exam_groups WHERE exam_id=?', (eid,))
                db.execute('DELETE FROM exam_rooms WHERE exam_id=?', (eid,))
            else:
                cur = db.execute('INSERT INTO exams(title, exam_date, session, time_slot, mode, gap, exclude_rolls, '
                                 'instructions, created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                 vals + (datetime.datetime.now().isoformat(),))
                eid = cur.lastrowid
            db.executemany('INSERT INTO exam_groups(exam_id, branch, semester, subject) VALUES(?,?,?,?)',
                           [(eid,) + gr for gr in groups])
            db.executemany('INSERT INTO exam_rooms(exam_id, room_id, sort_order, invigilators) VALUES(?,?,?,?)',
                           [(eid, rid, to_int(f.get('order_%d' % rid), 99), f.get('invig_%d' % rid, '').strip())
                            for rid in room_ids])
            db.commit()
            flash('Exam saved. Review capacity and click Generate Seat Plan.', 'success')
            return redirect(url_for('exam_view', eid=eid))
    rooms_ = sorted(db.execute('SELECT * FROM rooms WHERE active=1').fetchall(),
                    key=lambda r: (r['building'].lower(), natural_key(r['room_no'])))
    sel = {}
    groups = []
    if exam:
        sel = dict((r['id'], r) for r in exam_rooms_rows(eid))
        groups = exam_groups_rows(eid)
    return render_template('exam_form.html', exam=exam, rooms=rooms_, sel=sel, groups=groups,
                           branches=all_branches(), semesters=all_semesters(), sessions=SESSIONS)


@app.route('/exams/<int:eid>')
@login_required
def exam_view(eid):
    db = get_db()
    exam = exam_or_404(eid)
    queues = exam_queues(exam)
    rooms_ = exam_rooms_rows(eid)
    need = sum(len(q['students']) for q in queues)
    room_info = []
    for r in rooms_:
        used = db.execute('SELECT COUNT(*) FROM allocations WHERE exam_id=? AND room_id=?', (eid, r['id'])).fetchone()[0]
        room_info.append({'room': r, 'seats': len(room_positions(r, exam['gap'])), 'used': used})
    have = sum(x['seats'] for x in room_info)
    pwd_count = sum(1 for q in queues for s in q['students'] if s['pwd'])
    conflicts = []
    if exam['exam_date'] and exam['session']:
        conflicts = db.execute(
            'SELECT DISTINCT e.id, e.title, r.room_no FROM exams e JOIN exam_rooms er ON er.exam_id=e.id '
            'JOIN rooms r ON r.id=er.room_id WHERE e.id<>? AND e.exam_date=? AND e.session=? '
            'AND er.room_id IN (SELECT room_id FROM exam_rooms WHERE exam_id=?)',
            (eid, exam['exam_date'], exam['session'], eid)).fetchall()
    excl = parse_rolls(exam['exclude_rolls'])
    return render_template('exam_view.html', exam=exam, queues=queues, room_info=room_info, need=need,
                           have=have, pwd_count=pwd_count, conflicts=conflicts, excl_count=len(excl),
                           group_label=group_label, colors=GROUP_COLORS)


@app.route('/exams/<int:eid>/generate', methods=['POST'])
@login_required
def exam_generate(eid):
    exam = exam_or_404(eid)
    done, left = generate_plan(exam)
    if left:
        flash('%d students seated. WARNING: %d students could not be seated — add more rooms or disable gap seating.'
              % (done, left), 'error')
    else:
        flash('Seat plan generated: %d students seated.' % done, 'success')
    return redirect(url_for('exam_view', eid=eid))


@app.route('/exams/<int:eid>/clear', methods=['POST'])
@login_required
def exam_clear(eid):
    db = get_db()
    db.execute('DELETE FROM allocations WHERE exam_id=?', (eid,))
    db.execute('UPDATE exams SET generated_at=NULL, unallocated=0 WHERE id=?', (eid,))
    db.commit()
    flash('Seat plan cleared.', 'success')
    return redirect(url_for('exam_view', eid=eid))


@app.route('/exams/<int:eid>/duplicate', methods=['POST'])
@login_required
def exam_duplicate(eid):
    db = get_db()
    e = exam_or_404(eid)
    cur = db.execute('INSERT INTO exams(title, exam_date, session, time_slot, mode, gap, exclude_rolls, instructions, '
                     'created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (e['title'] + ' (copy)', e['exam_date'], e['session'], e['time_slot'], e['mode'], e['gap'],
                      e['exclude_rolls'], e['instructions'], datetime.datetime.now().isoformat()))
    nid = cur.lastrowid
    db.execute('INSERT INTO exam_groups(exam_id, branch, semester, subject) '
               'SELECT ?, branch, semester, subject FROM exam_groups WHERE exam_id=?', (nid, eid))
    db.execute('INSERT INTO exam_rooms(exam_id, room_id, sort_order, invigilators) '
               'SELECT ?, room_id, sort_order, invigilators FROM exam_rooms WHERE exam_id=?', (nid, eid))
    db.commit()
    flash('Exam duplicated. Update the date/session and subjects.', 'success')
    return redirect(url_for('exam_form', eid=nid))


@app.route('/exams/<int:eid>/delete', methods=['POST'])
@login_required
def exam_delete(eid):
    db = get_db()
    db.execute('DELETE FROM exams WHERE id=?', (eid,))
    db.commit()
    flash('Exam deleted.', 'success')
    return redirect(url_for('exams'))


# ---------------------------------------------------------------- outputs
def plan_data(exam, room_filter=None):
    db = get_db()
    groups = exam_groups_rows(exam['id'])
    gidx = dict((gr['id'], i) for i, gr in enumerate(groups))
    rows = db.execute('SELECT a.*, s.roll_no, s.name, s.branch, s.semester, s.pwd FROM allocations a '
                      'JOIN students s ON s.id=a.student_id WHERE a.exam_id=? ORDER BY a.seat_no',
                      (exam['id'],)).fetchall()
    by_room = {}
    for a in rows:
        by_room.setdefault(a['room_id'], []).append(a)
    out = []
    for room in exam_rooms_rows(exam['id']):
        if room_filter and room['id'] != room_filter:
            continue
        allocs = by_room.get(room['id'], [])
        usable = set(room_positions(room, exam['gap']))
        cellmap = dict(((a['r'], a['c']), a) for a in allocs)
        grid = []
        for r in range(room['rows']):
            line = []
            for c in range(room['cols']):
                a = cellmap.get((r, c))
                if a:
                    gi = gidx.get(a['group_id'], 0)
                    line.append({'state': 'seat', 'a': a, 'label': seat_label(r, c),
                                 'color': GROUP_COLORS[gi % len(GROUP_COLORS)]})
                else:
                    line.append({'state': 'empty' if (r, c) in usable else 'off', 'label': seat_label(r, c)})
            grid.append(line)
        summary = []
        for gr in groups:
            rolls = sorted([a['roll_no'] for a in allocs if a['group_id'] == gr['id']], key=natural_key)
            if rolls:
                summary.append({'group': gr, 'label': group_label(gr), 'count': len(rolls),
                                'ranges': compress_rolls(rolls),
                                'color': GROUP_COLORS[gidx[gr['id']] % len(GROUP_COLORS)]})
        out.append({'room': room, 'allocs': allocs, 'grid': grid, 'summary': summary})
    return groups, out


@app.route('/exams/<int:eid>/plan')
@login_required
def exam_plan(eid):
    exam = exam_or_404(eid)
    groups, data = plan_data(exam, to_int(request.args.get('room')) or None)
    return render_template('plan.html', exam=exam, data=data, groups=groups, group_label=group_label,
                           colors=GROUP_COLORS, seat_label=seat_label)


@app.route('/exams/<int:eid>/notice')
@login_required
def exam_notice(eid):
    exam = exam_or_404(eid)
    groups, data = plan_data(exam)
    branch_wise = []
    for gr in groups:
        items = []
        for d in data:
            for sm in d['summary']:
                if sm['group']['id'] == gr['id']:
                    items.append({'room': d['room'], 'count': sm['count'], 'ranges': sm['ranges']})
        if items:
            branch_wise.append({'label': group_label(gr), 'entries': items, 'total': sum(i['count'] for i in items)})
    return render_template('notice.html', exam=exam, data=data, branch_wise=branch_wise)


@app.route('/exams/<int:eid>/attendance')
@login_required
def exam_attendance(eid):
    exam = exam_or_404(eid)
    groups, data = plan_data(exam, to_int(request.args.get('room')) or None)
    return render_template('attendance.html', exam=exam, data=data, seat_label=seat_label)


@app.route('/exams/<int:eid>/stickers')
@login_required
def exam_stickers(eid):
    exam = exam_or_404(eid)
    groups, data = plan_data(exam, to_int(request.args.get('room')) or None)
    return render_template('stickers.html', exam=exam, data=data, seat_label=seat_label)


@app.route('/exams/<int:eid>/export.csv')
@login_required
def exam_export(eid):
    exam = exam_or_404(eid)
    groups = dict((gr['id'], gr) for gr in exam_groups_rows(eid))
    rows = get_db().execute(
        'SELECT a.*, s.roll_no, s.name, s.branch, s.semester, s.pwd, r.room_no, r.building FROM allocations a '
        'JOIN students s ON s.id=a.student_id JOIN rooms r ON r.id=a.room_id WHERE a.exam_id=?', (eid,)).fetchall()
    rows = sorted(rows, key=lambda a: (natural_key(a['room_no']), a['seat_no']))
    buf = io.StringIO()
    buf.write('﻿')
    w = csv.writer(buf)
    w.writerow(['Exam', 'Date', 'Session', 'Room', 'Building', 'Seat No', 'Seat Position', 'Roll No', 'Name',
                'Branch', 'Semester', 'Subject', 'PwD'])
    for a in rows:
        gr = groups.get(a['group_id'])
        w.writerow([exam['title'], dmy(exam['exam_date']), exam['session'], a['room_no'], a['building'], a['seat_no'],
                    seat_label(a['r'], a['c']), a['roll_no'], a['name'], a['branch'], a['semester'],
                    gr['subject'] if gr else '', 'Yes' if a['pwd'] else ''])
    fname = re.sub(r'[^A-Za-z0-9]+', '_', exam['title']).strip('_') or 'seatplan'
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=%s_seatplan.csv' % fname})


# ---------------------------------------------------------------- public lookup
@app.route('/find')
def lookup():
    roll = norm_roll(request.args.get('roll'))
    results = []
    if roll:
        results = get_db().execute(
            'SELECT e.title, e.exam_date, e.session, e.time_slot, r.room_no, r.building, r.floor, a.seat_no, a.r, a.c, '
            's.roll_no, s.name, s.branch, s.semester FROM allocations a JOIN exams e ON e.id=a.exam_id '
            'JOIN rooms r ON r.id=a.room_id JOIN students s ON s.id=a.student_id WHERE s.roll_no=? '
            'ORDER BY e.exam_date DESC, e.id DESC LIMIT 20', (roll,)).fetchall()
    return render_template('lookup.html', roll=roll, results=results, seat_label=seat_label)


# ---------------------------------------------------------------- settings
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    db = get_db()
    if request.method == 'POST':
        if request.form.get('action') == 'password':
            u = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
            new = request.form.get('new_password', '')
            if not check_password_hash(u['password_hash'], request.form.get('old_password', '')):
                flash('Current password is incorrect.', 'error')
            elif len(new) < 8 or new != request.form.get('confirm_password'):
                flash('New password must be at least 8 characters and match confirmation.', 'error')
            else:
                db.execute('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(new), u['id']))
                db.commit()
                flash('Password changed.', 'success')
        else:
            for k in DEFAULT_SETTINGS:
                db.execute('INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)',
                           (k, request.form.get(k, '').strip()))
            db.commit()
            flash('Settings saved.', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html', keys=DEFAULT_SETTINGS)


@app.errorhandler(400)
@app.errorhandler(404)
def http_error(e):
    return render_template('error.html', error=e), e.code


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
