"""Run the Figure Planner API with secrets loaded first."""
from __future__ import annotations

import os

import uvicorn

from .secret_loader import load_secrets


def main() -> int:
    load_secrets()
    if not os.getenv("OPENAI_API_KEY"):
        print("WARNING: OPENAI_API_KEY is not loaded. Check key.env.enc.")
    uvicorn.run(
        "figure_planner.server:app",
        host=os.getenv("HOST", "0.0.0.0"),   # 0.0.0.0 = 클라우드/컨테이너 필수
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
