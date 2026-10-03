"""Готовые туры: отдых и шопинг по Азии. Позже перенесём в PostgreSQL."""

from dataclasses import dataclass


TOUR_TYPE_LABELS = {
    "leisure": "Отдых",
    "shopping": "Шопинг-тур",
}


@dataclass(frozen=True)
class Tour:
    id: int
    title: str
    description: str
    destination: str
    tour_type: str
    duration_days: int
    price: int


TOURS: tuple[Tour, ...] = (
    Tour(
        id=1,
        title="Пхукет: море и спокойный отдых",
        description=(
            "Отель у пляжа Патонг, завтраки, трансфер из аэропорта. "
            "Свободное время для моря, массажа и вечерних прогулок. "
            "Экскурсии по острову — по желанию."
        ),
        destination="Таиланд",
        tour_type="leisure",
        duration_days=8,
        price=89000,
    ),
    Tour(
        id=2,
        title="Токио: шопинг в Гиндзе и Сибуе",
        description=(
            "Отель в Синдзюку, карта универмагов и outlet. "
            "Сопровождение в дни шопинга, помощь с tax free. "
            "Свободный вечер в Сибуе и вечерняя подсветка города."
        ),
        destination="Япония",
        tour_type="shopping",
        duration_days=6,
        price=145000,
    ),
    Tour(
        id=3,
        title="Дананг и Хойан: пляж и старый город",
        description=(
            "Проживание у моря в Дананге, день в Хойане. "
            "Море, кухня Вьетнама, вечерние фонарики. "
            "Подходит для пар и семей без насыщенной экскурсионной программы."
        ),
        destination="Вьетнам",
        tour_type="leisure",
        duration_days=10,
        price=72000,
    ),
    Tour(
        id=4,
        title="Сеул: шопинг в Мёндоне и Outlet",
        description=(
            "Отель в центре, дни в Мёндоне, Дондэмуне и премиум-outlet. "
            "Косметика, одежда, электроника. Помощь с возвратом налога. "
            "Вечером — квартал Каннам или ночной рынок."
        ),
        destination="Южная Корея",
        tour_type="shopping",
        duration_days=5,
        price=98000,
    ),
    Tour(
        id=5,
        title="Бали: вилла, океан и закаты",
        description=(
            "Вилла с бассейном в районе Чангу или Убуд (на выбор при бронировании). "
            "Встреча в аэропорту, дни без жёсткого графика. "
            "Море, рисовые террасы, ужины на закате."
        ),
        destination="Индонезия",
        tour_type="leisure",
        duration_days=9,
        price=110000,
    ),
)

TOURS_BY_ID = {tour.id: tour for tour in TOURS}


def get_tour(tour_id: int) -> Tour | None:
    return TOURS_BY_ID.get(tour_id)


def format_price(amount: int) -> str:
    return f"{amount:,} ₽".replace(",", " ")
