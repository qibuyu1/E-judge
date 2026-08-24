from web_app.config import settings

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app.main:app", host=settings.host, port=settings.port, reload=False)
