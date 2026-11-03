import uvicorn
from backend.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "backend.app:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
    )

