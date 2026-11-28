"""
Dashboard application: serves src/web/index.html at / and mounts the shared
/api/v1 router (health + triage) from src/api/routes.py.
"""

import logging
import sys
from pathlib import Path
from dotenv import load_dotenv

# Read .env from root directory and populate os.environ
load_dotenv()

# Surface engine/analyzer warnings (unparseable lines, degraded analysis) in dev runs.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Ensures the project root is ALWAYS in Python's search path, so the imports below
# resolve whether uvicorn or a plain interpreter starts this module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

# Standard absolute imports using 'src.'
from src.api.routes import router as api_router  # noqa: E402
from src.config import settings  # noqa: E402

app = FastAPI(title=settings.PROJECT_NAME, version=settings.VERSION)

# 1. Register API routes from routes.py (includes /api/v1/health)
app.include_router(api_router)

# 2. Path to the web frontend directory
WEB_DIR = Path(__file__).parent / "web"

# 3. Mount static files directory if there are local CSS/JSS assets
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# 4. Serve index.html at the root URL
# The badge shows Settings.VERSION rather than a copy of it, so a release bump cannot
# leave the dashboard claiming an older version than /docs reports.
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html.replace("{{VERSION}}", settings.VERSION))


if __name__ == "__main__":
    import uvicorn

    # 8001 by default: the Compose stack owns 8000, and Docker relays loopback there,
    # so a dev server on the same port would answer some requests and the container
    # others. Override with TRIAGE_PORT, or pass --port when starting uvicorn directly.
    uvicorn.run(
        "src.main.app", host="127.0.0.1", port=settings.TRIAGE_PORT, reload=True
    )
