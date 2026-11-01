"""기준가 모델 — 같은 단지 안의 시점·동·층·타입 차이를 통계적으로 분리합니다.

log(가격) = μ[단지·면적그룹] + S[시군구, 월] + D[단지, 월] + β_동 + β_층 + β_타입 + ε

- S: 시군구 월별 지수 (랜덤워크 평활). 거래가 없는 달은 앞뒤에서 보간.
- D: 단지가 시군구 흐름에서 벗어난 정도 (평활 + 0 으로 수축). 최근 거래가 없는 단지는 시군구 흐름을 따라감.
- β_동·β_층·β_타입: 릿지 회귀. 표본이 적으면 0(동·타입) 또는 시군구 공통 층 효과(층) 쪽으로 수축.
- 층 범주: 1층·2층·3층·저층·중층(기준)·고층·최상층. 최상층은 동별 최고 층(관측값 또는 네이버 highFloor) 기준.
- 해제 거래 제외, 직거래는 기본 제외(가족 간 거래 등 저가 편향).

numpy 만 사용. 결과는 group(묶음코드)별 JSON 으로 저장되어 매물 판정에 재사용됩니다.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

FLOOR_CATS = ["f1", "f2", "f3", "low", "mid", "high", "top"]
FLOOR_REF = "mid"
FLOOR_LABEL = {"f1": "1층", "f2": "2층", "f3": "3층", "low": "저층", "mid": "중층", "high": "고층", "top": "최상층"}
FLOOR_GUESS = {"f1": -0.08, "f2": -0.05, "f3": -0.03, "low": -0.015, "mid": 0.0, "high": 0.015, "top": -0.005}

DEFAULT_CFG = {
    "lam_region": 100.0,   # 시군구 지수 평활
    "lam_complex": 40.0,   # 단지 편차 평활
    "ridge_complex": 1.0,  # 단지 편차 → 0 수축
    "lam_dong": 4.0,
    "lam_floor": 7.0,
    "lam_type": 7.0,
    "recency_tau_m": 60.0,  # 가중치 반감 척도(개월)
    "recency_floor": 0.15,
    "min_trades": 8,       # 이보다 적으면 기준가 없음
    "include_direct": False,
    "iters": 2,
}


def ym_list(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:6])
    ye, me = int(end[:4]), int(end[4:6])
    out = []
    while (y, m) <= (ye, me):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def ym_add(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[4:6]) - 1 + k
    return f"{y + m // 12:04d}{m % 12 + 1:02d}"


def floor_cat(floor: int | None, top: int | None, hint: str = "") -> str:
    if floor is None:
        return {"저": "low", "중": "mid", "고": "high"}.get(hint, FLOOR_REF)
    if floor <= 1:
        return "f1"
    if floor == 2:
        return "f2"
    if floor == 3:
        return "f3"
    if top and top >= 4 and floor >= top:
        return "top"
    if top and top >= 4:
        rel = floor / top
        return "low" if rel < 0.34 else ("mid" if rel < 0.70 else "high")
    return "mid" if floor <= 15 else "high"


def smooth(sum_w: np.ndarray, sum_wr: np.ndarray, lam: float, ridge: float = 0.0) -> np.ndarray:
    """Σ w_m (x_m − r̄_m)² + lam Σ (x_m − x_{m−1})² + ridge Σ x_m² 최소화."""
    M = len(sum_w)
    if M == 0:
        return np.zeros(0)
    A = np.diag(sum_w + ridge)
    for i in range(M):
        if i > 0:
            A[i, i] += lam
            A[i, i - 1] -= lam
        if i < M - 1:
            A[i, i] += lam
            A[i, i + 1] -= lam
    if M == 1:
        return sum_wr / max(sum_w[0] + ridge, 1e-9)
    return np.linalg.solve(A, sum_wr)


def _ridge(X: np.ndarray, r: np.ndarray, w: np.ndarray, lam: np.ndarray, prior: np.ndarray) -> np.ndarray:
    """min Σ w (r − Xβ)² + Σ lam_j (β_j − prior_j)²."""
    if X.shape[1] == 0:
        return np.zeros(0)
    Xw = X * w[:, None]
    A = X.T @ Xw + np.diag(lam)
    b = Xw.T @ r + lam * prior
    return np.linalg.solve(A, b)


class GroupModel:
    """묶음코드(시군구) 하나의 모델 적합."""

    def __init__(self, trades: list[dict], now_ym: str, cfg: dict | None = None,
                 complex_meta: dict[str, dict] | None = None):
        self.cfg = dict(DEFAULT_CFG, **(cfg or {}))
        self.now = now_ym
        self.meta = complex_meta or {}
        rows = [t for t in trades if not t.get("cancelled") and t.get("price") and t.get("grp") is not None
                and t.get("ym") and t["ym"] <= now_ym]
        if not self.cfg["include_direct"]:
            rows = [t for t in rows if not t.get("direct")]
        self.rows = rows

    # ------------------------------------------------------------------
    def fit(self) -> dict:
        rows = self.rows
        if not rows:
            return {"now": self.now, "months": [], "S": [], "complexes": {}, "floorPrior": FLOOR_GUESS, "n": 0}
        months = ym_list(min(r["ym"] for r in rows), self.now)
        midx = {m: i for i, m in enumerate(months)}
        M = len(months)
        n = len(rows)
        y = np.array([math.log(r["price"]) for r in rows])
        mi = np.array([midx[r["ym"]] for r in rows])
        age = (M - 1) - mi
        w = np.maximum(np.exp(-age / self.cfg["recency_tau_m"]), self.cfg["recency_floor"])

        cg_keys = [(r["cid"], r["grp"]) for r in rows]
        cg_ids = {}
        cg = np.array([cg_ids.setdefault(k, len(cg_ids)) for k in cg_keys])
        ncg = len(cg_ids)

        # 1) μ[cg] 와 S[m] 번갈아 추정
        S = np.zeros(M)
        mu = np.zeros(ncg)
        for _ in range(6):
            r = y - S[mi]
            sw = np.bincount(cg, weights=w, minlength=ncg)
            swr = np.bincount(cg, weights=w * r, minlength=ncg)
            mu = swr / np.maximum(sw, 1e-9)
            r2 = y - mu[cg]
            sw_m = np.bincount(mi, weights=w, minlength=M)
            swr_m = np.bincount(mi, weights=w * r2, minlength=M)
            S = smooth(sw_m, swr_m, self.cfg["lam_region"])
            # S 의 기준을 최근 달 = 0 으로 두어 μ 가 '현재 수준'이 되게 함
            shift = S[-1]
            S -= shift
        resid0 = y - mu[cg] - S[mi]

        # 2) 시군구 공통 층 효과 (사전분포 역할)
        tops = self._top_floors(rows)
        fcat = [floor_cat(r["floor"], tops.get((r["cid"], r["dong"]), tops.get((r["cid"], "_")))) for r in rows]
        fcols = [c for c in FLOOR_CATS if c != FLOOR_REF]
        Xf = np.zeros((n, len(fcols)))
        for i, c in enumerate(fcat):
            if c in fcols:
                Xf[i, fcols.index(c)] = 1.0
        prior = np.array([FLOOR_GUESS[c] for c in fcols])
        bf = _ridge(Xf, resid0, w, np.full(len(fcols), 50.0), prior)
        floor_prior = {c: float(b) for c, b in zip(fcols, bf)}
        floor_prior[FLOOR_REF] = 0.0

        # 3) 단지별 동·층·타입·편차
        by_cid: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            by_cid[r["cid"]].append(i)
        complexes = {}
        for cid, idx in by_cid.items():
            complexes[cid] = self._fit_complex(cid, idx, rows, y, mi, w, mu, cg, S, fcat, floor_prior, months, tops)

        return {
            "now": self.now, "months": months, "S": [round(float(v), 5) for v in S],
            "floorPrior": {k: round(v, 5) for k, v in floor_prior.items()},
            "complexes": complexes, "n": n, "cfg": self.cfg,
        }

    # ------------------------------------------------------------------
    def _top_floors(self, rows: list[dict]) -> dict:
        """(cid, dong) → 관측 최고층, (cid, '_') → 단지 최고층. 네이버 highFloor 가 있으면 우선."""
        tops: dict = {}
        cnt: dict = defaultdict(int)
        for r in rows:
            f = r.get("floor")
            if f is None:
                continue
            k = (r["cid"], r["dong"])
            cnt[k] += 1
            tops[k] = max(tops.get(k, 0), f)
            k2 = (r["cid"], "_")
            tops[k2] = max(tops.get(k2, 0), f)
        for (cid, dong), c in list(cnt.items()):
            if dong and c < 4:  # 표본 적은 동은 단지 최고층으로
                tops[(cid, dong)] = tops[(cid, "_")]
        for cid, m in self.meta.items():
            hf = m.get("highFloor")
            try:
                hf = int(float(hf)) if hf not in (None, "") else None
            except ValueError:
                hf = None
            if hf and hf >= 4:
                tops[(cid, "_")] = max(tops.get((cid, "_"), 0), hf)
        return tops

    def _fit_complex(self, cid, idx, rows, y, mi, w, mu, cg, S, fcat, floor_prior, months, tops) -> dict:
        cfgm = self.cfg
        idx = np.array(idx)
        n = len(idx)
        M = len(months)
        r0 = y[idx] - mu[cg[idx]] - S[mi[idx]]
        wi = w[idx]
        sub = [rows[i] for i in idx]

        dongs = sorted({r["dong"] for r in sub if r["dong"]})
        dcols = dongs
        fcols = [c for c in FLOOR_CATS if c != FLOOR_REF]
        # 타입: 면적그룹별 정확 전용면적
        tcols = sorted({(r["grp"], r["area"]) for r in sub})
        tgroups = defaultdict(list)
        for g, a in tcols:
            tgroups[g].append(a)
        # 그룹에 타입이 하나뿐이면 타입 열을 두지 않음
        tcols = [(g, a) for g, a in tcols if len(tgroups[g]) > 1]
        tpos = {k: j for j, k in enumerate(tcols)}

        nd, nf, nt = len(dcols), len(fcols), len(tcols)
        X = np.zeros((n, nd + nf + nt))
        for k, r in enumerate(sub):
            if r["dong"] and r["dong"] in dcols:
                X[k, dcols.index(r["dong"])] = 1.0
            c = fcat[idx[k]]
            if c in fcols:
                X[k, nd + fcols.index(c)] = 1.0
            tk = (r["grp"], r["area"])
            if tk in tpos:
                X[k, nd + nf + tpos[tk]] = 1.0
        lam = np.concatenate([np.full(nd, cfgm["lam_dong"]), np.full(nf, cfgm["lam_floor"]), np.full(nt, cfgm["lam_type"])])
        prior = np.concatenate([np.zeros(nd), np.array([floor_prior[c] for c in fcols]), np.zeros(nt)])

        D = np.zeros(M)
        beta = np.zeros(nd + nf + nt)
        for _ in range(int(cfgm["iters"])):
            beta = _ridge(X, r0 - D[mi[idx]], wi, lam, prior) if X.shape[1] else beta
            r1 = r0 - X @ beta
            sw_m = np.bincount(mi[idx], weights=wi, minlength=M)
            swr_m = np.bincount(mi[idx], weights=wi * r1, minlength=M)
            D = smooth(sw_m, swr_m, cfgm["lam_complex"], cfgm["ridge_complex"])
        resid = r0 - X @ beta - D[mi[idx]]

        # 동별 통계
        dong_out = {}
        for j, d in enumerate(dcols):
            nn = int(sum(1 for r in sub if r["dong"] == d))
            dong_out[d] = {"b": round(float(beta[j]), 4), "n": nn}
        floor_out = {c: round(float(beta[nd + j]), 4) for j, c in enumerate(fcols)}
        floor_out[FLOOR_REF] = 0.0
        floor_n = defaultdict(int)
        for k in range(n):
            floor_n[fcat[idx[k]]] += 1

        # 면적그룹별
        groups = {}
        g_rows = defaultdict(list)
        for k, r in enumerate(sub):
            g_rows[r["grp"]].append(k)
        now_i = M - 1
        for g, ks in g_rows.items():
            ks = np.array(ks)
            cgid = cg[idx[ks[0]]]
            base = float(mu[cgid] + S[now_i] + D[now_i])
            res_g = resid[ks]
            sigma = float(np.sqrt(np.average(res_g ** 2, weights=wi[ks]))) if len(ks) > 1 else 0.12
            n12 = int(sum(1 for k in ks if mi[idx[k]] >= M - 12))
            types = {}
            for a in sorted({sub[k]["area"] for k in ks}):
                tk = (g, a)
                types[f"{a:.2f}"] = round(float(beta[nd + nf + tpos[tk]]), 4) if tk in tpos else 0.0
            # 최근 거래 12건 (표시용)
            recent = sorted(ks, key=lambda k: sub[k]["date"], reverse=True)[:12]
            last = [{"d": sub[k]["date"], "p": sub[k]["price"], "f": sub[k]["floor"], "dg": sub[k]["dong"],
                     "a": sub[k]["area"], "r": round(float(resid[k]), 3)} for k in recent]
            groups[str(g)] = {
                "mu": round(base, 5), "fair": int(round(math.exp(base))), "n": int(len(ks)), "n12": n12,
                "sigma": round(max(sigma, 0.02), 4), "types": types, "last": last,
                "lastYm": max(sub[k]["ym"] for k in ks),
            }

        # 단지 지수(최근 36개월 + 연간) : S + D
        T = S + D
        series36 = [round(float(T[i]), 4) for i in range(max(0, M - 36), M)]
        first_i = int(min(mi[idx]))
        return {
            "n": int(n), "first": months[first_i], "lastYm": months[int(max(mi[idx]))],
            "dong": dong_out, "floor": floor_out, "floorN": dict(floor_n),
            "top": {d: int(tops.get((cid, d), 0)) for d in dcols} | {"_": int(tops.get((cid, "_"), 0))},
            "T36": series36, "Dnow": round(float(D[now_i]), 4),
            "groups": groups,
        }


# ---------------------------------------------------------------- 조회
def complex_fair(model: dict, cid: str, grp: int | str, area: float | None, dong: str, floor: int | None,
                 top: int | None = None, hint: str = "") -> dict | None:
    """저장된 모델로 (단지, 면적그룹, 동, 층, 타입) 의 기준가와 분해 내역을 계산."""
    cx = (model.get("complexes") or {}).get(cid)
    if not cx:
        return None
    g = cx["groups"].get(str(grp))
    if not g:
        return None
    parts = {}
    base = g["mu"]
    d = cx["dong"].get(dong) if dong else None
    bd = d["b"] if d else 0.0
    parts["dong"] = {"b": bd, "n": d["n"] if d else 0, "known": bool(d)}
    top_eff = top or cx["top"].get(dong) or cx["top"].get("_") or None
    fc = floor_cat(floor, top_eff, hint)
    bf = cx["floor"].get(fc, 0.0)
    parts["floor"] = {"cat": fc, "b": bf, "n": cx.get("floorN", {}).get(fc, 0), "label": FLOOR_LABEL[fc]}
    bt = 0.0
    tkey = None
    if area is not None and g["types"]:
        # 가장 가까운 타입
        tkey = min(g["types"], key=lambda k: abs(float(k) - area))
        if abs(float(tkey) - area) <= 1.5:
            bt = g["types"][tkey]
        else:
            tkey = None
    parts["type"] = {"b": bt, "key": tkey}
    logfair = base + bd + bf + bt
    return {
        "fair": int(round(math.exp(logfair))), "fairBase": g["fair"], "logfair": logfair,
        "sigma": g["sigma"], "n": g["n"], "n12": g["n12"], "lastYm": g["lastYm"], "parts": parts,
    }
