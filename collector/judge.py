"""매물 판정: 호가가 기준가 대비 얼마나 싼지, 왜 싼지(동·층·타입 보정), 등급."""
from __future__ import annotations

import math

from .model import FLOOR_LABEL, complex_fair

GRADES = [("SS", -0.08), ("S", -0.05), ("A", -0.02), ("B", 0.02)]  # diff ≤ 기준 → 등급, 그 외 C


def grade_of(diff: float) -> str:
    for g, th in GRADES:
        if diff <= th:
            return g
    return "C"


def confidence(n: int, n12: int, sigma: float, dong_known: bool, has_dong: bool) -> str:
    """high / mid / low. 표본 수·최근 거래·잔차 분산·동 정보로."""
    score = 0
    score += 2 if n >= 40 else (1 if n >= 15 else 0)
    score += 1 if n12 >= 3 else 0
    score += 1 if sigma <= 0.07 else 0
    if has_dong and not dong_known:
        score -= 1
    return "high" if score >= 3 else ("mid" if score >= 2 else "low")


def pct(x: float) -> str:
    s = f"{x * 100:+.1f}%"
    return s.replace("+", "+").replace("-", "−")


def reasons_of(fair: dict, dong_raw: str, floor: int | None, royal: dict | None) -> list[str]:
    """'1층이라 층 보정 −9.5%' 같은 사유 문장 목록 (큰 것부터)."""
    out = []
    p = fair["parts"]
    fl = p["floor"]
    if abs(fl["b"]) >= 0.01 and fl["cat"] != "mid":
        out.append((abs(fl["b"]), f"{fl['label']}이라 층 보정 {pct(fl['b'])}"))
    dg = p["dong"]
    if dg["known"] and abs(dg["b"]) >= 0.01:
        tag = ""
        if royal and dong_raw and royal.get("royal") == _dong_key(dong_raw):
            tag = " (로얄동)"
        elif royal and dong_raw and royal.get("worst") == _dong_key(dong_raw):
            tag = " (비선호동)"
        out.append((abs(dg["b"]), f"{dong_raw or '해당 동'} 동 보정 {pct(dg['b'])}{tag}"))
    elif not dg["known"] and dong_raw:
        out.append((0.0, f"{dong_raw}은 거래 이력이 적어 동 보정 없음"))
    ty = p["type"]
    if ty["key"] and abs(ty["b"]) >= 0.01:
        out.append((abs(ty["b"]), f"{float(ty['key']):.0f}㎡ 타입 보정 {pct(ty['b'])}"))
    out.sort(key=lambda x: -x[0])
    return [s for _, s in out]


def _dong_key(dong_raw: str) -> str:
    from .deals import norm_dong
    return norm_dong(dong_raw)


def royal_dongs(cx_model: dict) -> dict:
    """동 프리미엄이 가장 높은/낮은 동 (표본 3건 이상)."""
    ds = {d: v for d, v in (cx_model.get("dong") or {}).items() if v["n"] >= 3}
    if len(ds) < 2:
        return {}
    best = max(ds, key=lambda d: ds[d]["b"])
    worst = min(ds, key=lambda d: ds[d]["b"])
    if ds[best]["b"] - ds[worst]["b"] < 0.02:
        return {}
    return {"royal": best, "worst": worst, "spread": round(ds[best]["b"] - ds[worst]["b"], 4)}


def judge_listing(model: dict, cid: str, listing: dict) -> dict | None:
    """listing: dong, dongRaw, floor, top, floorHint, area, grp, price. → 판정 dict 또는 None."""
    if listing.get("price") in (None, 0) or listing.get("grp") is None:
        return None
    fair = complex_fair(model, cid, listing["grp"], listing.get("area"), listing.get("dong") or "",
                        listing.get("floor"), listing.get("top"), listing.get("floorHint") or "")
    if not fair:
        return None
    price = float(listing["price"])
    diff = price / fair["fair"] - 1.0
    cx = model["complexes"][cid]
    royal = royal_dongs(cx)
    conf = confidence(fair["n"], fair["n12"], fair["sigma"], fair["parts"]["dong"]["known"], bool(listing.get("dong")))
    z = math.log(price / fair["fair"]) / max(fair["sigma"], 0.02)
    return {
        "fair": fair["fair"], "fairBase": fair["fairBase"], "diffPct": round(diff, 4),
        "diffAmt": int(round(price - fair["fair"])), "grade": grade_of(diff), "conf": conf, "z": round(z, 2),
        "reasons": reasons_of(fair, listing.get("dongRaw") or "", listing.get("floor"), royal),
        "floorCat": fair["parts"]["floor"]["cat"], "floorLabel": FLOOR_LABEL[fair["parts"]["floor"]["cat"]],
        "dongB": fair["parts"]["dong"]["b"], "floorB": fair["parts"]["floor"]["b"], "typeB": fair["parts"]["type"]["b"],
        "n": fair["n"], "n12": fair["n12"], "sigma": fair["sigma"], "royal": royal,
    }
