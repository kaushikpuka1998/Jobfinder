from __future__ import annotations

import os
import sys

# Ensure the app package is importable when running from the project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n  Job scraper UI  ->  http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)