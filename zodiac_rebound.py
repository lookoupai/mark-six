"""Deterministic six/four-zodiac screening; no DB, network or learned state."""

from collections import Counter
from itertools import combinations
import re


STRATEGY_KEY = "zodiac_rebound"
WINDOW = 20
ZODIACS = tuple("鼠牛虎兔龙蛇马羊猴鸡狗猪")
_TRADITIONAL = str.maketrans("龍馬雞豬", "龙马鸡猪")


def _zodiac(value):
    value = str(value or "").strip().translate(_TRADITIONAL)
    return value if value in ZODIACS else ""


def _number(value):
    text = str(value).strip()
    if not text.isdigit() or not 1 <= int(text) <= 49:
        raise ValueError("开奖数据包含无效号码")
    return int(text)


def _values(value):
    return value.split(",") if isinstance(value, str) else list(value or [])


def _draw_key(draw):
    return (str(draw.get("date") or ""),
            tuple(int(n) for n in re.findall(r"\d+", str(draw.get("id") or ""))))


def build_prediction(draws, number_to_zodiac, mapping_for_draw=None, *, window=WINDOW, parity_streak_threshold=5):
    """Use only supplied completed draws, sorted newest first.

    Counts include all seven balls, including repeated zodiacs. Omission uses
    all supplied history; an unseen zodiac has a lower bound, not an exact gap.
    Historical source zodiacs take precedence over date-specific fallback maps.
    """
    if type(window) is not int or not 10 <= window <= 240:
        raise ValueError("统计窗口须为 10 到 240 期")
    if type(parity_streak_threshold) is not int or not 2 <= parity_streak_threshold <= 20:
        raise ValueError("单双连开阈值须为 2 到 20 期")
    mapping = {int(n): _zodiac(z) for n, z in number_to_zodiac.items()}
    if set(mapping) != set(range(1, 50)) or not all(mapping.values()):
        raise ValueError("生肖号码映射不完整")
    groups = {z: [n for n in range(1, 50) if mapping[n] == z] for z in ZODIACS}
    if any(len(numbers) < 2 for numbers in groups.values()):
        raise ValueError("生肖号码映射必须包含十二生肖，每肖至少两个号码")

    history, seen = [], {}
    for draw in sorted(draws or [], key=_draw_key, reverse=True):
        normal = [_number(n) for n in _values(draw.get("no"))]
        special = _number(draw.get("sno"))
        if len(normal) != 6 or len(set(normal + [special])) != 7:
            raise ValueError("每期需要六个正码和一个特码，且号码不能重复")
        raw = [_zodiac(z) for z in _values(draw.get("raw_zodiac"))]
        historical_map = mapping_for_draw(draw) if mapping_for_draw else mapping
        historical_map = {int(n): _zodiac(z) for n, z in historical_map.items()}
        normal_zodiacs = [
            (raw[i] if i < len(raw) else "") or historical_map.get(n, "")
            for i, n in enumerate(normal)
        ]
        special_zodiac = (_zodiac(draw.get("sno_zodiac"))
                          or (raw[6] if len(raw) > 6 else "")
                          or historical_map.get(special, ""))
        if not all(normal_zodiacs + [special_zodiac]):
            raise ValueError("开奖记录缺少可用的生肖映射")
        period = str(draw.get("id") or "")
        identity = period or str(draw.get("date") or "")
        fingerprint = (tuple(normal), special, tuple(normal_zodiacs), special_zodiac)
        if identity and identity in seen:
            if seen[identity] != fingerprint:
                raise ValueError("同一期存在相互冲突的开奖记录")
            continue
        if identity:
            seen[identity] = fingerprint
        history.append({"period": period, "normal": normal, "special": special,
                        "normal_zodiacs": normal_zodiacs, "special_zodiac": special_zodiac})

    if len(history) < window:
        raise ValueError(f"生肖遗漏回补需要至少 {window} 期完整开奖记录，当前仅 {len(history)} 期")
    recent = history[:window]
    total_counts = Counter(z for d in recent for z in d["normal_zodiacs"] + [d["special_zodiac"]])
    special_counts = Counter(d["special_zodiac"] for d in recent)
    latest_normal = Counter(history[0]["normal_zodiacs"])
    latest_total = latest_normal + Counter([history[0]["special_zodiac"]])
    omissions = {z: next((i for i, d in enumerate(history) if d["special_zodiac"] == z),
                         len(history)) for z in ZODIACS}
    parities = {z: ("odd" if all(n % 2 for n in ns) else
                    "even" if all(n % 2 == 0 for n in ns) else "mixed")
                for z, ns in groups.items()}
    last_parity = history[0]["special"] % 2
    streak = 0
    for draw in history:
        if draw["special"] % 2 != last_parity:
            break
        streak += 1
    preferred = ("even" if last_parity else "odd") if streak >= parity_streak_threshold else None

    # Fixed zodiac order is the last tie-breaker, so identical input is stable.
    overdue = sorted(ZODIACS, key=lambda z: (-omissions[z], -total_counts[z], ZODIACS.index(z)))[:4]
    defensive = sorted((z for z in ZODIACS if z not in overdue), key=lambda z: (
        -total_counts[z], -(parities[z] == preferred), -latest_total[z], ZODIACS.index(z)))[:2]
    six = overdue + defensive

    def core_score(combo):
        odd = sum(parities[z] == "odd" for z in combo)
        even = sum(parities[z] == "even" for z in combo)
        return (
            -abs(odd - 2) - abs(even - 2),
            sum(z in overdue[:2] for z in combo),
            sum(latest_normal[z] >= 2 and omissions[z] >= window // 2 for z in combo),
            -sum(latest_total[z] == 0 for z in combo if z not in overdue[:2]),
            sum(total_counts[z] for z in combo),
            sum(omissions[z] for z in combo),
        )

    four = list(max(combinations(six, 4), key=core_score))
    rows = []
    for z in ZODIACS:
        lower_bound = omissions[z] == len(history)
        gap = ("至少 " if lower_bound else "") + str(omissions[z])
        reasons = []
        if z in overdue:
            reasons.append(f"特码遗漏 {gap} 期，列遗漏前四")
        if z in defensive:
            reasons.append(f"近 {window} 期总频次 {total_counts[z]} 次，列其余生肖前二")
        if latest_normal[z] >= 2:
            reasons.append(f"上期正码出现 {latest_normal[z]} 次")
        if latest_total[z] == 0:
            reasons.append("上期七个号码均未出现")
        if preferred and parities[z] == preferred:
            reasons.append("号码单双与当前特码连开方向相反，仅作同频次排序参考")
        rows.append({"zodiac": z, "numbers": groups[z], "parity": parities[z],
                     "total_count": total_counts[z], "special_count": special_counts[z],
                     "omission": omissions[z], "omission_lower_bound": lower_bound,
                     "latest_normal_count": latest_normal[z], "latest_total_count": latest_total[z],
                     "in_six": z in six, "in_four": z in four, "reasons": reasons})

    six_numbers = sorted(n for z in six for n in groups[z])
    four_numbers = sorted(n for z in four for n in groups[z])
    # Compatibility numbers are representatives, not the six/four-zodiac pools.
    frequency = Counter(n for d in recent for n in d["normal"] + [d["special"]])
    number_rank = lambda n: (-frequency[n], n)
    candidates = [n for z in four for n in sorted(groups[z], key=number_rank)]
    special = candidates[0]
    normal = [next(n for n in sorted(groups[z], key=number_rank) if n != special) for z in six]
    analysis = {
        "version": 1, "window": window, "history_count": len(history),
        "source_period": history[0]["period"], "six_zodiacs": six, "four_zodiacs": four,
        "overdue_zodiacs": overdue, "defensive_zodiacs": defensive,
        "six_numbers": six_numbers, "four_numbers": four_numbers,
        "six_random_baseline": len(six_numbers) / 49,
        "four_random_baseline": len(four_numbers) / 49,
        "parity_streak": {"parity": "odd" if last_parity else "even", "length": streak,
                          "lower_bound": streak == len(history), "preferred": preferred},
        "balanced_four": sum(parities[z] == "odd" for z in four) == 2
                         and sum(parities[z] == "even" for z in four) == 2,
        "statistics": rows,
    }
    return {"normal": normal, "special": {"number": str(special), "sno_zodiac": mapping[special]},
            "recommendation_text": describe_analysis(analysis),
            "model_meta": {"zodiac_rebound": analysis, "special_candidates": candidates}}


def describe_analysis(analysis):
    six, four = analysis["six_zodiacs"], analysis["four_zodiacs"]
    lines = [f"**综合六肖：{'、'.join(six)}**", f"**精选四肖：{'、'.join(four)}**", "",
             f"统计截至 {analysis['source_period']} 期；频次统计近 {analysis['window']} 期全部七个号码，"
             f"遗漏回看 {analysis['history_count']} 期。同一期重复生肖按号码个数计数。", ""]
    for z in six:
        row = next(r for r in analysis["statistics"] if r["zodiac"] == z)
        lines.append(f"- {z}：{'；'.join(row['reasons'])}。")
    excluded = [z for z in six if z not in four]
    lines += ["", "四肖在六肖内优先满足两单两双，再依次保留遗漏前二、长遗漏且上期正码重复的生肖，"
              "减少其余轮空生肖，最后比较总频次与遗漏。",
              f"本次剔除：{'、'.join(excluded)}。"
              + ("四肖已满足两单两双。" if analysis["balanced_four"] else
                 "候选生肖无法满足两单两双，已选取最接近平衡的四肖。"),
              "单双按本次生肖号码表判断，每年会变化。", "",
              f"六肖覆盖 {len(analysis['six_numbers'])}/49 个号码，随机基线 "
              f"{analysis['six_random_baseline']:.2%}；四肖覆盖 {len(analysis['four_numbers'])}/49 个号码，"
              f"随机基线 {analysis['four_random_baseline']:.2%}。", "",
              "号码球仅为兼容原有记录展示的代表号码；六肖、四肖命中应按完整覆盖号码分别统计。"
              "遗漏、正码重复和单双连开是历史筛选条件，不代表下一期概率提高。"]
    return "\n".join(lines)


def evaluate_groups(result, draw, target_mapping=None):
    """Evaluate coverage separately from the app's single-zodiac/number hits."""
    analysis = (result.get("model_meta") or {}).get("zodiac_rebound")
    if not analysis:
        return {}
    actual = _number(draw.get("sno"))
    raw_zodiacs = _values(draw.get("raw_zodiac"))
    actual_zodiac = (_zodiac(draw.get("sno_zodiac"))
                     or (_zodiac(raw_zodiacs[6]) if len(raw_zodiacs) > 6 else ""))
    mapping = {int(n): _zodiac(z) for n, z in (target_mapping or {}).items()}
    actual_zodiac = actual_zodiac or mapping.get(actual)
    metrics = {}
    for size in ("six", "four"):
        selected = analysis[f"{size}_zodiacs"]
        metrics[f"{size}_zodiac_hit"] = (actual_zodiac in selected if actual_zodiac
                                          else actual in analysis[f"{size}_numbers"])
        metrics[f"{size}_zodiac_baseline"] = (
            sum(z in selected for z in mapping.values()) / 49 if len(mapping) == 49
            else analysis[f"{size}_random_baseline"])
    return metrics


def summarize_groups(entries):
    metrics = {}
    for size in ("six", "four"):
        samples = [e for e in entries if f"{size}_zodiac_hit" in e]
        if not samples:
            continue
        hits = sum(e[f"{size}_zodiac_hit"] for e in samples)
        rate = hits / len(samples)
        baseline = sum(e[f"{size}_zodiac_baseline"] for e in samples) / len(samples)
        metrics[f"{size}_zodiac"] = {"hits": hits, "total": len(samples),
                                    "hit_rate": round(100 * rate, 2),
                                    "random_baseline": round(100 * baseline, 2),
                                    "lift_vs_random": round(100 * (rate - baseline), 2)}
    return {"zodiac_groups": metrics} if metrics else {}
