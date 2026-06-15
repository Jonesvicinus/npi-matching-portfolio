import os
import sys

# review_site/ on the path (for app.py, review_assistant.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# pipeline/ on the path (for scoring.py, match_medical_centers.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pipeline"))
