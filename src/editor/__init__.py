def main() -> None:
    """Local dev server. Loads .env if present; login is skipped unless EDITOR_LOCAL=0."""
    import os

    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    os.environ.setdefault("EDITOR_LOCAL", "1")
    uvicorn.run("editor.app:app", host="127.0.0.1", port=8000, reload=True)
