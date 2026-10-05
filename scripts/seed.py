import asyncio

from app.config import Settings
import app.db as db
from app.db import create_tables, Game, Product, set_setting


async def main():
    settings = Settings.from_env()
    db.init_db(settings.database_url)
    await create_tables()
    async with db.SessionLocal() as session:
        if not (await session.execute(__import__('sqlalchemy').select(Game))).scalars().first():
            session.add_all([
                Game(name="Mobile Legends", aliases="MLBB,ML", auto_publish=False),
                Game(name="Free Fire", aliases="FF", auto_publish=False),
                Game(name="Genshin Impact", aliases="Genshin", auto_publish=False),
            ])
        if not (await session.execute(__import__('sqlalchemy').select(Product))).scalars().first():
            session.add_all([
                Product(game="Mobile Legends", category="game", name="100 Diamonds", price=0, currency="CUP", active=False),
                Product(game="Free Fire", category="game", name="100 Diamonds", price=0, currency="CUP", active=False),
            ])
        await set_setting(session, "service_online", "true")
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
