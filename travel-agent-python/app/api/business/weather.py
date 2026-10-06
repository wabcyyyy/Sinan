"""行程天气（C3.1）：`GET /api/itinerary/{id}/weather`——出发前逐日预报。

数据面：Open-Meteo 免 key（settings.weather_enabled 整体开关），城市坐标与
生成链路同源（city_geo 字典 → Nominatim 兜底）。读闸门 owner/editor/viewer
与行程详情一致；任何取数失败都返回 daily=None 静默降级，前端隐藏天气区。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Path

from app.agent import get_weather_forecast
from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.schemas.weather import WeatherDay, WeatherVO
from app.services import itinerary_query

router = APIRouter(
    prefix="/api/itinerary",
    tags=["weather"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/{id}/weather")
def itinerary_weather(
    id: int = Path(..., ge=1),
    user: AuthUser = Depends(enforce_business_auth),
) -> dict:
    main = itinerary_query.find_readable_main(user.id, id)
    start = main.start_date.isoformat() if isinstance(main.start_date, date) else None
    end = main.end_date.isoformat() if isinstance(main.end_date, date) else None
    rows = get_weather_forecast(main.city, start, end)
    daily = [WeatherDay(**row) for row in rows] if rows else None
    return ok(WeatherVO(city=main.city, daily=daily).model_dump(by_alias=True))
