"""Stałe integracji Narwal Flow 2."""

from homeassistant.const import Platform

from .narwal_client import (
    AmbientLightCtrlType,
    CleaningRoute,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
)

DOMAIN = "narwal"
DEFAULT_PORT = 9002
MANUFACTURER = "Narwal"
MODEL = "Flow 2"

# Klucze produktu, pod którymi zgłaszają się roboty Flow 2 (trzy znane warianty
# sprzętowe/regionalne). Pierwszy jest domyślnym prefiksem tematów.
DEFAULT_PRODUCT_KEY = "QxMSPG6VSO"
PRODUCT_KEYS: frozenset[str] = frozenset({DEFAULT_PRODUCT_KEY, "iSuVlI1If2", "mkbqaprvrb"})

CONF_DEVICE_ID = "device_id"
CONF_PRODUCT_KEY = "product_key"
# Klucz z czasów integracji autora, zostaje w danych wpisu, nieużywany.
CONF_MODEL = "model"

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.SWITCH,
    Platform.LIGHT,
]

SERVICE_CLEAN_ROOMS = "clean_rooms"
SERVICE_GET_CURRENT_TASK = "get_current_task"
SERVICE_RESET_CONSUMABLE = "reset_consumable"
SERVICE_SEND_RAW = "send_raw"

DOCK_LIGHT_MODES: dict[str, AmbientLightCtrlType] = {
    "Off": AmbientLightCtrlType.OFF,
    "Fireplace": AmbientLightCtrlType.WINTER_WARMTH,
    "Nightlight": AmbientLightCtrlType.NIGHT_LIGHT,
    "Purple": AmbientLightCtrlType.PURPLE_LIGHT,
}
DOCK_LIGHT_MODE_NAMES: dict[int, str] = {
    int(value): key for key, value in DOCK_LIGHT_MODES.items()
}

# Cztery poziomy ssania Flow 2, w kolejności jak w aplikacji (Cicha, Standardowa,
# Silna, Super mocna). Klucze są wartościami `fan_speed` encji vacuum i opcji
# `suction` usługi clean_rooms, nazwy po polsku daje tłumaczenie.
FAN_SPEEDS: dict[str, FanLevel] = {
    "quiet": FanLevel.MUTE,
    "standard": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "super_powerful": FanLevel.DEEP,
}
FAN_SPEED_LIST: list[str] = list(FAN_SPEEDS)
FAN_SPEED_KEYS: dict[FanLevel, str] = {level: key for key, level in FAN_SPEEDS.items()}

WORK_MODE_MAP: dict[str, WorkMode] = {
    "vacuum": WorkMode.VACUUM,
    "mop": WorkMode.MOP,
    "vacuum_then_mop": WorkMode.VACUUM_THEN_MOP,
    "vacuum_and_mop": WorkMode.VACUUM_AND_MOP,
}
WATER_MAP: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
MOP_STRENGTH_MAP: dict[str, MopStrengthLevel] = {
    "normal": MopStrengthLevel.NORMAL,
    "high": MopStrengthLevel.HIGH,
}
ROUTE_MAP: dict[str, CleaningRoute] = {
    "standard": CleaningRoute.STANDARD,
    "meticulous": CleaningRoute.METICULOUS,
}

PASSES_MIN = 1
PASSES_MAX = 3

# base_status pole 15 (terminateReason) → klucz opcji HA. Z enumu TaskResult.
TASK_RESULT_OPTIONS: dict[int, str] = {
    1: "normal_end",
    2: "user_force_end",
    3: "shutdown_force_end",
    4: "low_battery_force_end",
    5: "overflow_force_end",
    6: "pause_too_long_force_end",
    7: "system_error_force_end",
    8: "user_force_end_on_station",
    9: "user_force_end_on_app",
    10: "recall_end",
    11: "normal_end_replenish_map_fail",
    12: "map_unmatched_force_end",
    13: "relocation_fail_with_station_force_end",
    14: "relocation_fail_without_station_force_end",
    15: "pretask_error_force_end",
    16: "schedule_task_force_end",
    17: "unexecuted",
    18: "not_find_pet",
}

# Enumy materiałów eksploatacyjnych (ConsumableMaintainItem / ConsumableReplaceItem).
CONSUMABLE_MAINTAIN_ITEMS: dict[int, str] = {
    1: "dust box", 2: "dust filter", 4: "wash ribs", 6: "universal wheel",
    7: "cliff sensor", 8: "side distance sensor", 9: "water tank sponge",
    10: "anti-winding brush", 11: "smart module sponge", 20: "dust container",
}
CONSUMABLE_REPLACE_ITEMS: dict[int, str] = {
    1: "dust filter", 2: "mop", 3: "side brush", 4: "clear water filter",
    5: "roller brush", 6: "detergent", 7: "smart module filter", 8: "dust bag",
    20: "station bag", 21: "silver ions", 22: "curing agent", 23: "heavy detergent",
    24: "inner dust box",
}

# Polskie nazwy i co zrobić, dla atrybutu `pozycje` encji konserwacji i wymiany.
CONSUMABLE_MAINTAIN_PL: dict[int, tuple[str, str]] = {
    1: ("Pojemnik na kurz w robocie", "opróżnić i przepłukać"),
    2: ("Filtr kurzu", "wytrzepać, przepłukać i wysuszyć"),
    4: ("Żeberka tacki myjącej w stacji", "wyjąć tackę ze stacji i umyć"),
    6: ("Kółko skrętne", "usunąć włosy i brud z przedniego kółka"),
    7: ("Czujniki upadku", "przetrzeć suchą szmatką od spodu robota"),
    8: ("Boczny czujnik odległości", "przetrzeć suchą szmatką z boku robota"),
    9: ("Gąbka zbiornika wody", "wypłukać"),
    10: ("Szczotka główna (przeciw plątaniu)", "wyjąć i usunąć włosy"),
    11: ("Gąbka modułu dozowania", "wypłukać"),
    20: ("Pojemnik na kurz w stacji", "opróżnić"),
}
CONSUMABLE_REPLACE_PL: dict[int, tuple[str, str]] = {
    1: ("Filtr kurzu", "wymienić na nowy"),
    2: ("Mopy", "wymienić na nowe"),
    3: ("Szczotka boczna", "wymienić na nową"),
    4: ("Filtr czystej wody", "wymienić na nowy"),
    5: ("Szczotka główna", "wymienić na nową"),
    6: ("Detergent", "uzupełnić"),
    7: ("Filtr modułu dozowania", "wymienić na nowy"),
    8: ("Worek na kurz", "wymienić na nowy"),
    20: ("Worek na kurz w stacji", "wymienić na nowy"),
    21: ("Moduł jonów srebra", "wymienić na nowy"),
    22: ("Środek pielęgnacyjny (curing agent)", "uzupełnić"),
    23: ("Detergent do silnych zabrudzeń", "uzupełnić"),
    24: ("Wewnętrzny pojemnik na kurz", "wymienić na nowy"),
}

# Odnośnik do centrum pomocy Narwala dla kodu błędu (wzór z aplikacji).
ERROR_HELP_URL_TEMPLATE = (
    "https://help.narwal.com/helpcenter/vall/#/p2/question/all?eType=1&code={code}&lang=en-US"
)
