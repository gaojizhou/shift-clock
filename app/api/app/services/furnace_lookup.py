from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Furnace

DEFAULT_REGION = "示例厂区A"


def get_furnace(
    db: Session, furnace_no: str, region: str | None = DEFAULT_REGION
) -> Furnace | None:
    if region:
        return db.scalar(
            select(Furnace).where(Furnace.region == region, Furnace.furnace_no == furnace_no)
        )
    return db.scalar(select(Furnace).where(Furnace.furnace_no == furnace_no))
