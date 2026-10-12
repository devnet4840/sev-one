"""Start Sev One:  python -m app   (then open http://localhost:8000)

HOST and PORT can be set in .env or the environment. Open browser tabs hold a live-update
connection, so shutdown waits at most 2 seconds for them before exiting.
"""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    port = int(os.environ.get("PORT", "8000"))
    # 127.0.0.1 keeps the demo on this computer. Set HOST=0.0.0.0 (and OPERATOR_TOKEN) to share it.
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"\n  Sev One: open http://localhost:{port}\n")
    uvicorn.run("app.main:app", host=host, port=port, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
