"""Единый формат объявления для всех парсеров."""

from dataclasses import asdict, dataclass, field, fields


@dataclass
class Listing:
    company: str                    # "HOWOGE", "degewo", ...
    external_id: str                # стабильный ID объекта компании (родной вид, нормализованный)
    link: str
    title: str | None = None
    address: str | None = None
    postcode: str | None = None
    district: str | None = None
    rooms: float | None = None
    area: float | None = None
    kalt: float | None = None
    neben: float | None = None      # холодные Nebenkosten (или общие, если heiz_in_neben)
    heiz: float | None = None
    warm: float | None = None
    wbs: str | None = None          # "да" / "нет" / None
    lat: float | None = None
    lon: float | None = None
    published: str | None = None
    heiz_in_neben: bool = False     # HOWOGE/WBM: отопление входит в Nebenkosten, отдельно не указано
    wbs_source: str | None = None   # откуда WBS: «фильтр сайта», «поле API», «подробная страница», «заголовок»
    prices: dict = field(default_factory=dict)   # ВСЕ ценовые поля как есть: метка -> строка с сайта
    tags: str = ""                  # заголовок + метки — текст для фильтра по типу жилья
    detail_loaded: bool = False     # подробная страница уже прочитана
    extra: dict = field(default_factory=dict)   # служебное: расстояние, приоритет, ошибки и т.п.

    @property
    def sheet_id(self) -> str:
        """Значение колонки «ID» в таблице, напр. 'HOWOGE:1770-24035-55'."""
        return f"{self.company}:{self.external_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
