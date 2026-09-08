"""Reviewed auto-assignment suggestions are opt-in, fail-closed write preconditions."""
from copy import deepcopy

import pytest

from app import db
from tests.test_ticket_assignment_hotfix import assignment_case, snapshot


PATH = "/api/v2/employee-routing/auto-assign"


def _target(case, model="TenantTicket", *, assignee=None, frozen=True):
    ticket = {"TenantTicket": case.modern, "MunicipioTicket": case.municipal, "PymeTicket": case.business}[model]
    target = {"source_model": model, "id": ticket.id, "expected_assignee_id": None}
    if frozen:
        target["expected_suggested_assignee_id"] = assignee or case.operators[0].id
    return target


def _recommendation(target, *, suggested=None):
    return {
        "ticket": {"id": target["id"], "source_model": target["source_model"]},
        "suggested_assignee": {"id": suggested or target.get("expected_suggested_assignee_id")},
        "score": 1,
        "reasons": ["synthetic_test_recommendation"],
    }


def _routing(monkeypatch, recommendations):
    monkeypatch.setattr("routes.v2.saas.build_employee_routing_payload",
                        lambda tenant: {"recommendations": deepcopy(recommendations)})


def _apply(case, targets, **payload):
    return case.client.post(PATH, headers=case.headers(case.owner),
                            json={"dry_run": False, "tickets": targets, **payload})


def _forbid_writer(monkeypatch):
    def unexpected_write(*args, **kwargs):
        pytest.fail("An assignment writer ran before all preview preconditions were validated")
    monkeypatch.setattr("routes.v2.saas._apply_employee_assignment", unexpected_write)


@pytest.mark.parametrize("model", ["TenantTicket", "MunicipioTicket", "PymeTicket"])
def test_reviewed_target_applies_when_current_suggestion_matches(assignment_case, monkeypatch, model):
    case = assignment_case
    target = _target(case, model)
    _routing(monkeypatch, [_recommendation(target)])
    response = _apply(case, [target])
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["applied_count"] == 1
    assert snapshot(case, model)[0] == target["expected_suggested_assignee_id"]


@pytest.mark.parametrize("invalid", [None, "", True, False, 1.5, 0, -1, "1.0", "1e0", "１２", {}, [], "1" * 5000])
def test_reviewed_suggestion_requires_exact_positive_id(assignment_case, monkeypatch, invalid):
    case = assignment_case
    target = _target(case)
    target["expected_suggested_assignee_id"] = invalid
    _routing(monkeypatch, [])
    _forbid_writer(monkeypatch)
    response = _apply(case, [target])
    assert response.status_code == 400, response.get_json()
    assert response.get_json()["reason_code"] == "expected_suggested_assignee_id_invalid"
    assert snapshot(case, "TenantTicket") == (None, 0)


def test_second_changed_suggestion_rejects_entire_selection_before_writes(assignment_case, monkeypatch):
    case = assignment_case
    targets = [_target(case, "TenantTicket"), _target(case, "MunicipioTicket")]
    _routing(monkeypatch, [_recommendation(targets[0]), _recommendation(targets[1], suggested=case.operators[1].id)])
    _forbid_writer(monkeypatch)
    response = _apply(case, targets)
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["reason_code"] == "routing_preview_changed"
    assert response.get_json()["action_hint"] == "refresh_routing_preview"
    assert snapshot(case, "TenantTicket") == (None, 0)
    assert snapshot(case, "MunicipioTicket") == (None, 0)


@pytest.mark.parametrize("change", ["missing", "duplicate", "no_assignee", "different_model", "conflicting_assignee_alias"])
def test_reviewed_selection_must_match_exact_effective_recommendations(assignment_case, monkeypatch, change):
    case = assignment_case
    targets = [_target(case, "TenantTicket"), _target(case, "MunicipioTicket")]
    recommendations = [_recommendation(target) for target in targets]
    if change == "missing":
        recommendations.pop()
    elif change == "duplicate":
        recommendations.append(deepcopy(recommendations[0]))
    elif change == "no_assignee":
        recommendations[1]["suggested_assignee"] = None
    elif change == "different_model":
        recommendations[1]["ticket"]["source_model"] = "PymeTicket"
    else:
        recommendations[1]["suggested_assignee"]["employee_id"] = case.operators[1].id
    _routing(monkeypatch, recommendations)
    _forbid_writer(monkeypatch)
    response = _apply(case, targets)
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["reason_code"] == "routing_preview_changed"
    assert snapshot(case, "TenantTicket") == (None, 0)
    assert snapshot(case, "MunicipioTicket") == (None, 0)


def test_limit_cannot_silently_truncate_reviewed_selection(assignment_case, monkeypatch):
    case = assignment_case
    targets = [_target(case, "TenantTicket"), _target(case, "MunicipioTicket")]
    _routing(monkeypatch, [_recommendation(target) for target in targets])
    _forbid_writer(monkeypatch)
    response = _apply(case, targets, limit=1)
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["reason_code"] == "routing_preview_changed"


def test_unselected_queue_tickets_are_not_accidentally_applied(assignment_case, monkeypatch):
    case = assignment_case
    target, unselected = _target(case), _target(case, "MunicipioTicket")
    _routing(monkeypatch, [_recommendation(target), _recommendation(unselected)])
    response = _apply(case, [target])
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["applied_count"] == 1
    assert len(response.get_json()["items"]) == 1
    assert snapshot(case, "MunicipioTicket") == (None, 0)


def test_ascii_decimal_target_and_suggestion_ids_are_lossless(assignment_case, monkeypatch):
    case = assignment_case
    target = _target(case)
    recommendation = _recommendation(target)
    target["id"] = f" {target['id']} "
    target["expected_suggested_assignee_id"] = str(target["expected_suggested_assignee_id"])
    _routing(monkeypatch, [recommendation])
    response = _apply(case, [target], limit="25")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["applied_count"] == 1


@pytest.mark.parametrize("invalid", [None, True, False, 1.0, 1.5, [], {}, "oops", "1.5", "", "1" * 5000])
def test_invalid_limit_is_controlled_400_not_500(assignment_case, monkeypatch, invalid):
    case = assignment_case
    _forbid_writer(monkeypatch)
    response = _apply(case, [_target(case)], limit=invalid)
    assert response.status_code == 400, response.get_json()
    assert response.get_json()["reason_code"] == "limit_invalid"
    assert snapshot(case, "TenantTicket") == (None, 0)


def test_legacy_target_without_frozen_suggestion_keeps_existing_contract(assignment_case, monkeypatch):
    case = assignment_case
    target = _target(case, frozen=False)
    _routing(monkeypatch, [_recommendation(target, suggested=case.operators[1].id)])
    response = _apply(case, [target])
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["applied_count"] == 1
    assert snapshot(case, "TenantTicket")[0] == case.operators[1].id


def test_frozen_suggestion_does_not_bypass_current_owner_cas(assignment_case, monkeypatch):
    case = assignment_case
    case.modern.datos_extra = {**case.modern.datos_extra, "assignee_id": case.operators[1].id}
    db.session.commit()
    target = _target(case)
    _routing(monkeypatch, [_recommendation(target)])
    response = _apply(case, [target])
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["reason_code"] == "assignment_state_conflict"
    assert snapshot(case, "TenantTicket") == (case.operators[1].id, 0)


@pytest.mark.parametrize("change", ["missing_ticket", "incompatible_destination", "owner_cas"])
def test_reviewed_batch_rolls_back_earlier_assignment_if_later_target_changes(assignment_case, monkeypatch, change):
    import routes.v2.saas as saas
    case = assignment_case
    if change == "owner_cas":
        case.municipal.asignado_a_id = case.operators[1].id
        db.session.commit()
    targets = [_target(case, "TenantTicket"), _target(case, "MunicipioTicket")]
    _routing(monkeypatch, [_recommendation(target) for target in targets])
    original_find = saas.find_ticket_for_assignment
    original_compatible = saas.ticket_assignee_is_compatible
    original_apply = saas._apply_employee_assignment
    attempted = []

    def observed_apply(ticket, assignee, actor, **kwargs):
        attempted.append(type(ticket).__name__)
        return original_apply(ticket, assignee, actor, **kwargs)

    def changed_find(tenant, model, ticket_id):
        if model == "MunicipioTicket" and change == "missing_ticket":
            return None
        return original_find(tenant, model, ticket_id)

    def changed_compatibility(assignee, ticket):
        if type(ticket).__name__ == "MunicipioTicket" and change == "incompatible_destination":
            return False
        return original_compatible(assignee, ticket)

    monkeypatch.setattr(saas, "_apply_employee_assignment", observed_apply)
    monkeypatch.setattr(saas, "find_ticket_for_assignment", changed_find)
    monkeypatch.setattr(saas, "ticket_assignee_is_compatible", changed_compatibility)
    response = _apply(case, targets)
    assert response.status_code == 409, response.get_json()
    expected_reason = "assignment_state_conflict" if change == "owner_cas" else "routing_preview_changed"
    assert response.get_json()["reason_code"] == expected_reason
    assert attempted == (["TenantTicket", "MunicipioTicket"] if change == "owner_cas" else ["TenantTicket"])
    assert snapshot(case, "TenantTicket") == (None, 0)
    assert snapshot(case, "MunicipioTicket") == (case.operators[1].id if change == "owner_cas" else None, 0)


def test_frozen_preview_is_read_only_and_does_not_grant_employee_bulk_authority(assignment_case, monkeypatch):
    case = assignment_case
    target = _target(case)
    _routing(monkeypatch, [_recommendation(target)])
    _forbid_writer(monkeypatch)
    preview = _apply(case, [target], dry_run=True)
    assert preview.status_code == 200, preview.get_json()
    assert preview.get_json()["applied_count"] == 0
    actor = case.operators[0]
    actor.accesibilidad = {"employee_scope": {"categorias": ["bacheo"], "permisos": ["tickets.assign"]}}
    db.session.commit()
    forbidden = case.client.post(PATH, headers=case.headers(actor), json={"dry_run": False, "tickets": [target]})
    assert forbidden.status_code == 403, forbidden.get_json()
    assert snapshot(case, "TenantTicket") == (None, 0)


def test_real_preview_can_be_applied_with_frozen_suggestions(assignment_case):
    case = assignment_case
    preview = case.client.post(PATH, headers=case.headers(case.owner), json={"dry_run": True, "limit": 100})
    assert preview.status_code == 200, preview.get_json()
    items = preview.get_json()["items"]
    assert items
    targets = [{"source_model": item["ticket"]["source_model"], "id": item["ticket"]["id"],
                "expected_assignee_id": item["ticket"].get("assignee_id"),
                "expected_suggested_assignee_id": item["suggested_assignee"]["id"]}
               for item in items if item.get("suggested_assignee")]
    assert targets
    response = _apply(case, targets, limit=100)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["applied_count"] == len(targets)
