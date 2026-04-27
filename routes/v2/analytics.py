from __future__ import annotations

from flask import Blueprint

from routes import analytics_routes as legacy_analytics

v2_analytics_bp = Blueprint("v2_analytics", __name__, url_prefix="/api/v2/analytics")


@v2_analytics_bp.route("/summary", methods=["GET"])
def summary_v2():
    return legacy_analytics.get_summary()


@v2_analytics_bp.route("/heatmap", methods=["GET"])
def heatmap_v2():
    return legacy_analytics.get_heatmap()


@v2_analytics_bp.route("/surveys/summary", methods=["GET"])
def surveys_summary_v2():
    return legacy_analytics.get_survey_summary()


@v2_analytics_bp.route("/surveys/sentiment", methods=["GET"])
def surveys_sentiment_v2():
    return legacy_analytics.get_survey_sentiment()


@v2_analytics_bp.route("/surveys/geo", methods=["GET"])
def surveys_geo_v2():
    return legacy_analytics.get_survey_geo()


@v2_analytics_bp.route("/insights", methods=["GET"])
def insights_v2():
    return legacy_analytics.get_insights()


@v2_analytics_bp.route("/sales", methods=["GET"])
def sales_v2():
    return legacy_analytics.get_sales_analytics()


@v2_analytics_bp.route("/benchmarks", methods=["GET"])
def benchmarks_v2():
    return legacy_analytics.get_benchmarks()


@v2_analytics_bp.route("/funnel", methods=["GET"])
def funnel_v2():
    return legacy_analytics.get_funnel()


@v2_analytics_bp.route("/report/latest", methods=["GET"])
def report_latest_v2():
    return legacy_analytics.get_latest_report()


@v2_analytics_bp.route("/report/generate", methods=["POST"])
def report_generate_v2():
    return legacy_analytics.trigger_generate_report()


@v2_analytics_bp.route("/generate-report", methods=["POST"])
def generate_report_v2():
    return legacy_analytics.generate_report()
