import sys
import os
import html
from unittest.mock import MagicMock, patch

# Add parent path to import app modules
sys.path.insert(0, os.path.abspath('.'))

# We mock database operations
mock_db = MagicMock()

# Mock planting query results
planting_row = {
    "sponsor_id": 42,
    "sponsor_name": "Alice Green",
    "sponsor_email": "alice@example.com",
    "unit_uid": "UNT-12345",
    "project_name": "Forest Restoration Project",
    "project_location_text": "Sector 4B, Nairobi",
    "species": "Acacia",
    "project_tree_no": 105,
    "tree_id": 999
}

# Mock maintenance query results
maintenance_rows = [
    {
        "sponsor_id": 42,
        "sponsor_name": "Alice Green",
        "sponsor_email": "alice@example.com",
        "unit_uid": "UNT-12345",
        "project_name": "Forest Restoration Project",
        "project_location_text": "Sector 4B, Nairobi",
        "species": "Acacia",
        "project_tree_no": 105
    }
]

# Configure mock execute responses
def mock_execute(query, params=None):
    query_str = str(query).lower()
    mock_result = MagicMock()
    if "green_sponsorship_units" in query_str:
        if "tree_id" in query_str:
            # maintenance query
            mock_result.mappings().all.return_value = maintenance_rows
        else:
            # planting query
            mock_result.mappings().first.return_value = planting_row
    elif "green_sponsor_push_tokens" in query_str:
        mock_result.scalars().all.return_value = ["ExponentPushToken[xxxxxxxxxxxx]"]
    return mock_result

mock_db.execute = mock_execute

# Mocks for email/push sending functions
sent_push_messages = []
sent_emails = []

def dummy_send_push(messages):
    sent_push_messages.extend(messages)

def dummy_send_email(*, to_email, subject, text_body, html_body):
    sent_emails.append({
        "to": to_email,
        "subject": subject,
        "text": text_body,
        "html": html_body
    })

# Patch the routing file helper functions
from app.routers.green import _notify_sponsor_tree_planted, _notify_sponsor_tree_maintenance

@patch('app.routers.green._send_expo_push_messages', side_effect=dummy_send_push)
@patch('app.routers.green._send_html_email', side_effect=dummy_send_email)
def run_tests(mock_email, mock_push):
    print("--- Running Planting Notification Test ---")
    _notify_sponsor_tree_planted(mock_db, unit_id=100)
    
    print(f"Push messages count: {len(sent_push_messages)}")
    for push in sent_push_messages:
        print(f"  Push To: {push['to']}")
        print(f"  Push Title: {push['title']}")
        print(f"  Push Body: {push['body']}")
        print(f"  Push Data: {push['data']}")
        
    print(f"\nEmails count: {len(sent_emails)}")
    for email in sent_emails:
        print(f"  Email To: {email['to']}")
        print(f"  Email Subject: {email['subject']}")
        print("  Email Text Body snippet:")
        print("\n".join("    " + l for l in email['text'].split("\n")[:12]))
        print("  Checking key phrases in Email HTML:")
        has_tab = "My Trees" in email['html']
        has_cert = "certificate" in email['html'].lower()
        has_share = "share" in email['html'].lower()
        print(f"    Has 'My Trees' tab info: {has_tab}")
        print(f"    Has certificate info: {has_cert}")
        print(f"    Has share info: {has_share}")
        
    # Clear logs
    sent_push_messages.clear()
    sent_emails.clear()
    
    print("\n--- Running Maintenance Notification Test ---")
    _notify_sponsor_tree_maintenance(mock_db, tree_id=999, task_row={
        "task_type": "weeding_and_mulching",
        "completed_at": "2026-06-07 14:00 UTC",
        "review_notes": "The weeding was completed successfully around the base."
    })
    
    print(f"Push messages count: {len(sent_push_messages)}")
    for push in sent_push_messages:
        print(f"  Push To: {push['to']}")
        print(f"  Push Title: {push['title']}")
        print(f"  Push Body: {push['body']}")
        print(f"  Push Data: {push['data']}")
        
    print(f"\nEmails count: {len(sent_emails)}")
    for email in sent_emails:
        print(f"  Email To: {email['to']}")
        print(f"  Email Subject: {email['subject']}")
        print("  Email Text Body snippet:")
        print("\n".join("    " + l for l in email['text'].split("\n")[:14]))
        print("  Checking key phrases in Email HTML:")
        has_tab = "My Trees" in email['html']
        has_cert = "certificate" in email['html'].lower()
        has_share = "share" in email['html'].lower()
        print(f"    Has 'My Trees' tab info: {has_tab}")
        print(f"    Has certificate info: {has_cert}")
        print(f"    Has share info: {has_share}")

if __name__ == "__main__":
    run_tests()
