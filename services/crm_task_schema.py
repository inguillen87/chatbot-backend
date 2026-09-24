"""Read-only schema readiness: never creates tables or repairs a live database."""
from sqlalchemy import inspect, text
from database import db

TASK_COLUMNS = {'id','tenant_id','contact_id','title','description','assignee_id','due_at',
                'priority','status','revision','created_by','updated_by','created_at','updated_at'}
EVENT_COLUMNS = {'id','tenant_id','task_id','task_revision','actor_id','operation','reason',
                 'key_hash','request_hash','before_state','after_state','created_at'}

def task_schema_ready():
    with db.engine.connect() as connection:
        inspector = inspect(connection)
        for table, columns in [('crm_task',TASK_COLUMNS),('crm_task_event',EVENT_COLUMNS)]:
            if not inspector.has_table(table): return False
            if not columns.issubset({column['name'] for column in inspector.get_columns(table)}): return False
        constraints = {tuple(item['column_names']) for item in inspector.get_unique_constraints('crm_task_event')}
        if not {('tenant_id','actor_id','key_hash'),('tenant_id','task_id','task_revision')}.issubset(constraints):
            return False
        if connection.dialect.name == 'sqlite':
            triggers = set(connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars())
            return {'crm_task_event_no_update','crm_task_event_no_delete','crm_task_contact_scope_insert','crm_task_contact_scope_update'}.issubset(triggers)
        if connection.dialect.name == 'postgresql':
            triggers = set(connection.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgenabled IN ('O','A') AND tgrelid IN ('crm_task'::regclass,'crm_task_event'::regclass)")).scalars())
            return {'crm_task_event_immutable','crm_task_contact_scope'}.issubset(triggers)
        return False
