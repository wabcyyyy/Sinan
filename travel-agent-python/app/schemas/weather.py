"""Weather contracts（C3.1）：行程页出发前逐日预报；daily=None 表示暂不可用（静默降级）。"""

from app.schemas.common import WireModel


class WeatherDay(WireModel):
    date: str
    #: WMO weather code 原值；text 为后端翻译的中文短语
    code: int | None = None
    text: str
    t_max: float | None = None
    t_min: float | None = None
    #: 降水概率百分比（0-100）；Open-Meteo 缺失时为 None
    precip_prob: int | None = None


class WeatherVO(WireModel):
    city: str
    source: str = "open-meteo"
    #: None = 行程窗口取不到预报（超出 16 天能力窗/城市无坐标/上游失败），前端静默隐藏
    daily: list[WeatherDay] | None = None
