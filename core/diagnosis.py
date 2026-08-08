"""弱項排序 —— 規格 US-4。

**排序依據是可驗證的統計距離,不是人為指定的權重。**

初版用了手工指定的 3/2/1 影響權重,但那是無法驗證的猜測 ——
而這份排序會直接影響牧場主把錢花在哪裡。改為兩項皆可追溯到來源的資訊:

  落後程度  = 距離全國平均幾個標準差(常模表本來就有 mean 與 sd,純計算)
  連鎖影響  = 依年報自身的因果敘述,這項改善後會帶動哪些同樣落後的指標

排序只用第一項(客觀),第二項作為脈絡呈現,不混進分數裡。
把兩者相乘會產生一個看似精確、實則無依據的數字,那正是要避免的事。
"""

from typing import Dict, List, NamedTuple

from core.benchmark import get_metric, upstream_of
from core.grading import GradeResult, is_higher_better

GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}

# 弱項的定義:低於全國中位數,也就是 D 級以下(D=50~75%, E=75~90%, F=後10%)。
#
# 初版把 C 級也算進來,但 C 級是「前 25~50%」——高於中位數。
# 結果清單裡出現「本場比全國平均好」的項目,牧場主看了會質疑整份報告的可信度。
WEAKNESS_THRESHOLD = GRADE_RANK["D"]


class Weakness(NamedTuple):
    key: str
    name: str
    grade: str
    shortfall_sd: float      # 劣於全國平均幾個標準差;越大越該優先處理
    downstream: List[str]    # 改善這項會帶動的其他落後指標(依年報因果敘述)
    improvement: str
    unit: str


def shortfall_sd(value: float, metric: dict) -> float:
    """劣於全國平均幾個標準差。正值代表比平均差,負值代表比平均好。

    指標方向由百分位推導,與 grading 用同一套邏輯,不另外維護方向表。
    """
    mean = metric["mean"]
    sd = metric["sd"]
    if not sd:
        return 0.0
    if is_higher_better(metric["percentiles"]):
        return (mean - value) / sd
    return (value - mean) / sd


def _is_weak(key: str, result: GradeResult) -> bool:
    """弱項需同時滿足兩個條件:低於中位數,且低於平均。

    兩個條件在偏態分布下可能不一致 —— 有指標的級距落在中位數以下,
    數值卻高於平均。只用級距篩選時,「改善清單」會出現
    「優於全國平均」的項目,自相矛盾,牧場主會質疑整份報告。
    """
    if GRADE_RANK[result.grade] < WEAKNESS_THRESHOLD:
        return False
    try:
        metric = get_metric(key)
    except KeyError:
        return False
    return shortfall_sd(result.value, metric) > 0


def rank_weaknesses(graded: Dict[str, GradeResult]) -> List[Weakness]:
    """把評級結果排成改善優先順序。

    只納入 C 級以下(含)的指標 —— A、B 級不是弱項。
    依標準差距離由遠到近排序;同距離時以級距較差者優先,再以 key 排序確保可重現。
    """
    weak_keys = {
        key for key, result in graded.items()
        if _is_weak(key, result)
    }

    items: List[Weakness] = []
    for key in weak_keys:
        result = graded[key]
        try:
            metric = get_metric(key)
        except KeyError:
            continue

        items.append(Weakness(
            key=key,
            name=metric["name"],
            grade=result.grade,
            shortfall_sd=round(shortfall_sd(result.value, metric), 3),
            downstream=sorted(
                other for other in weak_keys
                if key in upstream_of(other)
            ),
            improvement=metric.get("improvement", ""),
            unit=metric.get("unit", ""),
        ))

    items.sort(key=lambda w: (-w.shortfall_sd, -GRADE_RANK[w.grade], w.key))
    return items
