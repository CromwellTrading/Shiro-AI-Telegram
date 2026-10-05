from app.main import settings, run_all
import asyncio

if __name__ == "__main__":
    settings.validate()
    asyncio.run(run_all())
