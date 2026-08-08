"""輸入驗證 —— 規格 US-2 驗收條件 2、3。

分兩級處理:
  errors   明顯填錯(超出合理範圍、非數值)-> 擋下不給算
  warnings 欄位互相矛盾 -> 只提醒,仍然給算

理由:填錯一個數字就整份不給算會很煩,但 300% 的分娩率算出來的級距沒有意義。
矛盾則可能是真實情況(不同統計口徑),不該替使用者決定他填錯了。
"""

from typing import Any, Dict, List, NamedTuple

from core.benchmark import get_metric, metrics_index
from core.coercion import to_number

# 年產胎數與天數的推導誤差超過此比例即提醒
LITTERS_TOLERANCE = 0.10
DAYS_PER_YEAR = 365.25


class Issue(NamedTuple):
    key: str
    message: str


class ValidationReport(NamedTuple):
    ok: bool
    errors: List[Issue]
    warnings: List[Issue]
    cleaned: Dict[str, float]


def _check_range(key: str, value: float, errors: List[Issue]) -> None:
    try:
        metric = get_metric(key)
    except KeyError:
        return
    bounds = metric.get("range")
    if not bounds:
        return
    low, high = bounds
    if not (low <= value <= high):
        errors.append(Issue(
            key=key,
            message=f"{metric['name']} 填入 {value},超出合理範圍 {low}~{high}",
        ))


def _check_cross_fields(v: Dict[str, float], warnings: List[Issue]) -> None:
    """跨欄一致性。缺任一邊就跳過,不因資料不全而報警。"""
    total = v.get("total_born_per_litter")
    live = v.get("live_born_per_litter")
    weaned = v.get("weaned_per_sow")

    if total is not None and live is not None and live > total:
        warnings.append(Issue(
            key="live_born_per_litter",
            message=f"窩均活仔數 {live} 大於窩均總仔數 {total},請確認是否填反",
        ))

    if live is not None and weaned is not None and weaned > live:
        warnings.append(Issue(
            key="weaned_per_sow",
            message=f"母豬平均離乳仔豬數 {weaned} 大於窩均活仔數 {live},請確認",
        ))

    # 年報定義:母豬年產胎數 = (365.25 - 非生產天數) / (哺乳天數 + 懷孕天數)
    # 非生產天數不可省略 —— 省略會讓每份填寫正確的表單都誤報矛盾。
    litters = v.get("litters_per_sow_year")
    lactation = v.get("lactation_days")
    gestation = v.get("gestation_days")
    npd = v.get("npd")
    if None not in (litters, lactation, gestation, npd):
        cycle = lactation + gestation
        if cycle > 0:
            implied = (DAYS_PER_YEAR - npd) / cycle
            if implied > 0 and abs(implied - litters) / implied > LITTERS_TOLERANCE:
                warnings.append(Issue(
                    key="litters_per_sow_year",
                    message=(
                        f"母豬年產胎數 {litters} 與 (365.25−非生產天數)/(哺乳+懷孕) "
                        f"推導值 {implied:.2f} 落差較大,請確認統計口徑"
                    ),
                ))


def validate(values: Dict[str, Any]) -> ValidationReport:
    """驗證使用者輸入。未知的 key 直接忽略,不視為錯誤。"""
    known = metrics_index()
    errors: List[Issue] = []
    warnings: List[Issue] = []
    cleaned: Dict[str, float] = {}

    for key, raw in values.items():
        if key not in known:
            continue
        number = to_number(raw)
        if number is None:
            errors.append(Issue(
                key=key,
                message=f"{known[key]['name']} 必須填數字,收到「{raw}」",
            ))
            continue
        _check_range(key, number, errors)
        cleaned[key] = number

    _check_cross_fields(cleaned, warnings)

    return ValidationReport(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        cleaned=cleaned,
    )
