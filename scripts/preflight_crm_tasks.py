"""Read-only rollout inspection. Never imports the app or executes migrations."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from services.crm_task_schema import inspect_task_schema
from services.crm_task_rollout import selected_tenants

ROOT = Path(__file__).resolve().parents[1]
BASE = '912446bf96f8330664a9dec009ae57dbf935c73c'
TASK_REVISION = '20260924_crm_tasks_v1'
PARENT = '20260820_survey_content_jurisdiction_v1'


def source_graph(root=ROOT):
    revisions = {}
    for path in (root/'migrations/versions').glob('*.py'):
        values = {}
        for node in ast.parse(path.read_text(encoding='utf-8-sig')).body:
            if not isinstance(node,ast.Assign): continue
            for target in node.targets:
                if isinstance(target,ast.Name) and target.id in ('revision','down_revision'):
                    values[target.id] = ast.literal_eval(node.value)
        if values.get('revision'):
            if values['revision'] in revisions: raise ValueError('duplicate_revision')
            revisions[values['revision']] = values.get('down_revision')
    parents = {parent for value in revisions.values() for parent in
               (value if isinstance(value,(list,tuple)) else [value] if value else [])}
    return {'active_heads':sorted(set(revisions)-parents),'missing_parents':sorted(parents-set(revisions)),
            'task_migration_active':TASK_REVISION in revisions,
            'task_parent_in_active_graph':PARENT in revisions}


def database_report(connection, expected_revision, tenant_ids):
    inspector = inspect(connection)
    installed = sorted(connection.execute(text('SELECT version_num FROM alembic_version LIMIT 16')).scalars()) if inspector.has_table('alembic_version') else []
    tables = {table: inspector.has_table(table) for table in ('crm_task','crm_task_event')}
    schema_ready = inspect_task_schema(connection) if all(tables.values()) else False
    found = []
    if tenant_ids and inspector.has_table('tenant_profile'):
        from sqlalchemy import bindparam
        query = text('SELECT id FROM tenant_profile WHERE id IN :ids').bindparams(bindparam('ids',expanding=True))
        found = list(connection.execute(query,{'ids':list(tenant_ids)}).scalars())
    return {'installed_revisions':installed,'expected_revision_matches':installed == [expected_revision],
            'task_tables':tables,'task_schema_ready':schema_ready,'requested_tenant_count':len(tenant_ids),
            'all_requested_tenants_exist':set(found)==set(tenant_ids)}


def inspect_database(dsn, expected_host, expected_revision, tenant_ids):
    url=make_url(dsn)
    if url.get_backend_name()!='postgresql' or not expected_host or url.host!=expected_host:
        raise ValueError('database_target_not_confirmed')
    engine=create_engine(dsn,pool_pre_ping=False,connect_args={'connect_timeout':5})
    try:
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                connection.execute(text('SET TRANSACTION READ ONLY'))
                connection.execute(text("SET LOCAL statement_timeout='5000ms'"))
                result=database_report(connection,expected_revision,tenant_ids)
                result['transaction_read_only']=connection.execute(text('SHOW transaction_read_only')).scalar()=='on'
                return result
    finally:engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--inspect-database',action='store_true')
    parser.add_argument('--expected-database-host')
    parser.add_argument('--expected-database-revision',default=PARENT)
    parser.add_argument('--tenant-ids',default='')
    args=parser.parse_args()
    report={'contract_version':'crm.tasks.preflight.v1','write_authorized':False,
            'activation_authorized':False,'database_checked':False,'expected_runtime_base':BASE}
    code=0
    try:
        tenants,valid=selected_tenants(args.tenant_ids)
        if not valid: raise ValueError('invalid_tenant_selection')
        if not re.fullmatch(r'[a-zA-Z0-9_]{1,128}',args.expected_database_revision):
            raise ValueError('invalid_revision')
        report['source_revision']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        report['source_descends_from_live_base']=subprocess.run(['git','merge-base','--is-ancestor',BASE,'HEAD'],cwd=ROOT,capture_output=True).returncode==0
        report['source_graph']=source_graph()
        report['source_clean']=not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()
        if not report['source_descends_from_live_base']: raise ValueError('wrong_runtime_base')
        if report['source_graph']['missing_parents'] or report['source_graph']['active_heads'] not in ([PARENT],[TASK_REVISION]):
            raise ValueError('unexpected_migration_graph')
        if args.inspect_database:
            dsn=os.getenv('CRM_TASKS_PREFLIGHT_DATABASE_URL','')
            if not dsn: raise ValueError('explicit_database_url_required')
            report['database']=inspect_database(dsn,args.expected_database_host,args.expected_database_revision,tenants)
            report['database_checked']=True
            db=report['database']
            report['next_step']='review_task_migration' if not db['task_schema_ready'] else ('review_activation' if report['source_graph']['task_migration_active'] else 'review_migration_registration')
            if not db['expected_revision_matches'] or not db['all_requested_tenants_exist']:
                report['next_step']='resolve_database_mismatch';code=1
        else:
            report['next_step']='inspect_the_verified_target_database_read_only'
    except Exception as exc:
        report['error_code']=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z_]+',str(exc)) else 'preflight_failed'
        code=1
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report));return code

if __name__=='__main__':raise SystemExit(main())
