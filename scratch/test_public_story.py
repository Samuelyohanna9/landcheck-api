import sys
import os

# Add parent path to import app modules
sys.path.insert(0, os.path.abspath('.'))

from app.routers.green import _render_public_sponsor_tree_story_html

item = {
    "sponsor_name": "John Doe",
    "sponsor_organization_name": "Green Earth Foundation",
    "project_name": "Lagos Reforestation",
    "species": "Mahogany",
    "project_tree_no": 452,
    "location_text": "Lagos, Nigeria",
    "unit_uid": "UNT-LAG-9821",
    "tree_id": 105,
    "tree_status": "healthy",
    "planting_date": "2026-05-15",
    "photo_url": "https://images.unsplash.com/photo-1542273917363-3b1817f69a2d?auto=format&fit=crop&q=80&w=800",
    "photo_urls": [
        "https://images.unsplash.com/photo-1542273917363-3b1817f69a2d?auto=format&fit=crop&q=80&w=800"
    ],
    "carbon": {
        "current_co2_kg": 15.42,
        "annual_co2_kg": 22.50,
        "lifetime_co2_kg": 900.00
    },
    "timeline": [
        {
            "task_type": "watering",
            "status": "completed",
            "reviewed_at": "2026-06-01",
            "notes": "Verified tree was watered and mulching added."
        }
    ],
    "dedication_type": "birthday_gift",
    "dedication_name": "Jane Doe",
    "dedication_message": "Happy Birthday! Grow strong like this tree.",
    "lat": 6.5244,
    "lng": 3.3792,
    "public_story_url": "https://api.landcheck.online/green/sponsor/public/trees/UNT-LAG-9821",
    "public_certificate_url": "https://api.landcheck.online/green/sponsor/public/trees/UNT-LAG-9821/certificate.pdf"
}

html_output = _render_public_sponsor_tree_story_html(item)

# Write to scratch
output_path = "scratch/test_tree_story.html"
with open(output_path, "w", encoding="utf-8") as f:
    f.write(html_output)

print(f"Successfully generated public story html at: {output_path}")
