"""Create disposable authenticated principals for local Phase 2C HTTP checks."""

from __future__ import annotations

import json
import uuid

from app.db import SessionLocal
from app.models.estate_foundation import EstateOrganization, EstateOrganizationMember
from app.utils.survey_auth_security import (
    ensure_survey_auth_schema,
    find_or_create_survey_user,
    issue_survey_session,
)


def main() -> None:
    db = SessionLocal()
    try:
        ensure_survey_auth_schema()
        suffix = uuid.uuid4().hex[:10]
        owner_id = find_or_create_survey_user(db, email=f"phase2c-owner-{suffix}@example.test", full_name="Phase 2C Owner")
        viewer_id = find_or_create_survey_user(db, email=f"phase2c-viewer-{suffix}@example.test", full_name="Phase 2C Viewer")
        manager_id = find_or_create_survey_user(db, email=f"phase2c-manager-{suffix}@example.test", full_name="Phase 2C Manager")
        accounts_id = find_or_create_survey_user(db, email=f"phase2c-accounts-{suffix}@example.test", full_name="Phase 2C Accounts")
        sales_id = find_or_create_survey_user(db, email=f"phase2c-sales-{suffix}@example.test", full_name="Phase 2C Sales")
        outsider_id = find_or_create_survey_user(db, email=f"phase2c-outsider-{suffix}@example.test", full_name="Phase 2C Outsider")
        org = EstateOrganization(name=f"Phase 2C {suffix}", slug=f"phase2c-{suffix}")
        db.add(org)
        db.flush()
        for user_id, role in ((owner_id, "owner"), (viewer_id, "viewer"), (manager_id, "manager"), (accounts_id, "accounts"), (sales_id, "sales")):
            db.add(EstateOrganizationMember(organization_id=org.id, subject_type="survey_user", subject_id=str(user_id), role_key=role))
        db.commit()
        print(json.dumps({
            "organization_id": org.id,
            "owner_token": issue_survey_session(db, user_id=owner_id)["access_token"],
            "viewer_token": issue_survey_session(db, user_id=viewer_id)["access_token"],
            "manager_token": issue_survey_session(db, user_id=manager_id)["access_token"],
            "accounts_token": issue_survey_session(db, user_id=accounts_id)["access_token"],
            "sales_token": issue_survey_session(db, user_id=sales_id)["access_token"],
            "outsider_token": issue_survey_session(db, user_id=outsider_id)["access_token"],
        }))
    finally:
        db.close()


if __name__ == "__main__":
    main()
