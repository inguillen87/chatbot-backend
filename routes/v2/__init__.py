from routes.v2.analytics import v2_analytics_bp
from routes.v2.auth import v2_auth_bp
from routes.v2.commerce import v2_commerce_bp
from routes.v2.demo import v2_demo_bp
from routes.v2.health import v2_health_bp
from routes.v2.offline_sync import offline_sync_bp
from routes.v2.saas import v2_saas_bp
from routes.v2.sla import v2_sla_bp
from routes.v2.surveys import v2_public_surveys_bp, v2_surveys_bp
from routes.v2.tenants import v2_tenants_bp
from routes.v2.tickets import v2_tickets_bp


def register_v2_blueprints(app):
    app.register_blueprint(v2_health_bp)
    app.register_blueprint(v2_demo_bp)
    app.register_blueprint(v2_auth_bp)
    app.register_blueprint(v2_tenants_bp)
    app.register_blueprint(v2_tickets_bp)
    app.register_blueprint(v2_sla_bp)
    app.register_blueprint(v2_surveys_bp)
    app.register_blueprint(v2_public_surveys_bp)
    app.register_blueprint(v2_analytics_bp)
    app.register_blueprint(offline_sync_bp)
    app.register_blueprint(v2_saas_bp)
    app.register_blueprint(v2_commerce_bp)
