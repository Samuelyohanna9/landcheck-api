import sys
import os
import html
from unittest.mock import MagicMock, patch

# Add parent path to import app modules
sys.path.insert(0, os.path.abspath('.'))

# We mock database operations
mock_db = MagicMock()

# Mock project row
project_row = {
    "id": 101,
    "name": "Acacia Reforestation Project",
    "location_text": "Lagos, Nigeria",
    "sponsor": "Green Climate Fund"
}

# Mock user row
user_row = {
    "full_name": "Bob Agent",
    "email": "bob@example.com",
    "work_username": "bob_agent"
}

# Configure mock execute responses
def mock_execute(query, params=None):
    query_str = str(query).lower()
    mock_result = MagicMock()
    if "tree_projects" in query_str:
        mock_result.mappings().first.return_value = project_row
    elif "green_users" in query_str:
        mock_result.mappings().first.return_value = user_row
    return mock_result

mock_db.execute = mock_execute

# Mocks for email sending function
sent_emails = []

def dummy_send_email(*, to_email, subject, text_body, html_body):
    sent_emails.append({
        "to": to_email,
        "subject": subject,
        "text": text_body,
        "html": html_body
    })

# Patch the routing file helper functions
from app.routers.green import _notify_agent_added_to_project, _notify_agent_first_planting_assignment

@patch('app.routers.green._send_html_email', side_effect=dummy_send_email)
def run_tests(mock_email):
    print("--- Running Agent Welcome Email Test ---")
    _notify_agent_added_to_project(mock_db, project_id=101, agent_user_ids=[5])
    
    print(f"Emails count: {len(sent_emails)}")
    for email in sent_emails:
        print(f"  Email To: {email['to']}")
        print(f"  Email Subject: {email['subject']}")
        print("  Email Text Body snippet:")
        print("\n".join("    " + l for l in email['text'].split("\n")[:14]))
        print("  Checking details in HTML:")
        print(f"    Has project name: {'Acacia Reforestation Project' in email['html']}")
        print(f"    Has dropdown select info: {'dropdown' in email['html'].lower()}")
        
    sent_emails.clear()
    
    print("\n--- Running Agent First Planting Assignment Email Test ---")
    _notify_agent_first_planting_assignment(mock_db, project_id=101, assignee_name="bob_agent", target_trees=250)
    
    print(f"Emails count: {len(sent_emails)}")
    for email in sent_emails:
        print(f"  Email To: {email['to']}")
        print(f"  Email Subject: {email['subject']}")
        print("  Email Text Body snippet:")
        print("\n".join("    " + l for l in email['text'].split("\n")[:16]))
        print("  Checking details in HTML:")
        print(f"    Has target trees (250): {'250' in email['html']}")
        print(f"    Has dropdown select info: {'dropdown' in email['html'].lower()}")

if __name__ == "__main__":
    run_tests()
