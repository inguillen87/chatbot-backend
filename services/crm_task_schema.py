"""Read-only schema readiness; names alone do not prove a guard is effective."""
from sqlalchemy import inspect, text

TASK_COLUMNS = {'id','tenant_id','contact_id','title','description','assignee_id','due_at',
                'priority','status','revision','created_by','updated_by','created_at','updated_at'}
EVENT_COLUMNS = {'id','tenant_id','task_id','task_revision','actor_id','operation','reason',
                 'key_hash','request_hash','before_state','after_state','created_at'}
IMMUTABLE_BODY = "BEGIN RAISE EXCEPTION 'crm_task_event_append_only'; END;"
SCOPE_BODY = """BEGIN IF NOT EXISTS (SELECT 1 FROM contact WHERE id=NEW.contact_id AND tenant_id=NEW.tenant_id)
 THEN RAISE EXCEPTION 'crm_task_contact_scope'; END IF; RETURN NEW; END;"""


def _normalized(value):
    return ''.join(str(value).lower().split()).rstrip(';')


def _sqlite_guards(connection):
    actual = {row.name: row.sql for row in connection.execute(text(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger'"))}
    expected = {}
    for operation in ('UPDATE','DELETE'):
        name = 'crm_task_event_no_'+operation.lower()
        expected[name] = f"CREATE TRIGGER {name} BEFORE {operation} ON crm_task_event BEGIN SELECT RAISE(ABORT, 'crm_task_event_append_only'); END"
    for operation in ('INSERT','UPDATE'):
        name = 'crm_task_contact_scope_'+operation.lower()
        expected[name] = f"CREATE TRIGGER {name} BEFORE {operation} ON crm_task WHEN NOT EXISTS (SELECT 1 FROM contact WHERE id=NEW.contact_id AND tenant_id=NEW.tenant_id) BEGIN SELECT RAISE(ABORT, 'crm_task_contact_scope'); END"
    return all(name in actual and _normalized(actual[name]) == _normalized(sql) for name,sql in expected.items())


def _postgres_guards(connection):
    query = text("""SELECT t.tgname,t.tgtype,t.tgenabled,t.tgnargs,t.tgattr::text AS columns,
        t.tgqual IS NULL AS unconditional,c.relname,p.proname,p.prosrc,p.prosecdef,l.lanname,
        p.pronamespace=c.relnamespace AS same_namespace
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
        WHERE NOT t.tgisinternal AND t.tgrelid IN ('crm_task'::regclass,'crm_task_event'::regclass)""")
    rows = {row.tgname: row for row in connection.execute(query)}
    # pg_trigger.h: ROW=1, BEFORE=2, INSERT=4, DELETE=8, UPDATE=16.
    expected = {'crm_task_event_immutable': ('crm_task_event',27,IMMUTABLE_BODY),
                'crm_task_contact_scope': ('crm_task',23,SCOPE_BODY)}
    for name,(table,event_mask,body) in expected.items():
        row = rows.get(name)
        if row is None or row.relname != table or row.tgtype != event_mask:
            return False
        if row.tgenabled not in ('O','A') or row.tgnargs != 0 or row.columns or not row.unconditional:
            return False
        if row.proname != name or row.lanname != 'plpgsql' or row.prosecdef or not row.same_namespace:
            return False
        if _normalized(row.prosrc) != _normalized(body):
            return False
    # Replica sessions skip ordinary ('O') triggers, so never certify that session.
    return connection.execute(text('SHOW session_replication_role')).scalar() in ('origin','local')


def inspect_task_schema(connection):
    inspector = inspect(connection)
    for table,required,optional in [('crm_task',TASK_COLUMNS,{'assignee_id','due_at'}),
                                    ('crm_task_event',EVENT_COLUMNS,{'before_state'})]:
        if not inspector.has_table(table):
            return False
        columns = {column['name']: column for column in inspector.get_columns(table)}
        if not required.issubset(columns):
            return False
        if any(columns[name]['nullable'] for name in required-optional-{'id'}):
            return False
    constraints = {tuple(item['column_names']) for item in inspector.get_unique_constraints('crm_task_event')}
    if not {('tenant_id','actor_id','key_hash'),('tenant_id','task_id','task_revision')}.issubset(constraints):
        return False
    if connection.dialect.name == 'sqlite':
        return _sqlite_guards(connection)
    if connection.dialect.name == 'postgresql':
        return _postgres_guards(connection)
    return False


def task_schema_ready():
    from database import db
    with db.engine.connect() as connection:
        return inspect_task_schema(connection)
