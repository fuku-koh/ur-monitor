# -*- coding: utf-8 -*-
"""
UR vacancy monitor (GitHub Actions friendly)
- Polls UR's legacy internal POST API (no JS) and detects diffs.
- Safe-by-default: if fetch/json fails, keeps previous state.
- Notifies via ChatWork; otherwise prints to logs.
"""

import os, json, re, time
from datetime import datetime, timezone, timedelta
import requests

# ========= Config =========
PROP_ID     = os.getenv("PROP_ID", "5390")  # 例: 5390, 7080, 5010...
STATE_PATH  = os.getenv("STATE_FILE", f".state_{PROP_ID}.json")
CHAT_TOKEN  = os.getenv("CHATWORK_TOKEN", "")
CHAT_ROOM   = os.getenv("CHATWORK_ROOM_ID", "")

# 物件ごとの地域/支社コード（URLとshisyaに使用）
PROPERTY_META = {
    # 東京（関東=20）
    "5390": ("kanto/tokyo", "20"),
    "6940": ("kanto/tokyo", "20"),
    "7080": ("kanto/tokyo", "20"),
    "7140": ("kanto/tokyo", "20"),
    "7100": ("kanto/tokyo", "20"),
    # 大阪（関西=80）— 追加分
    "5010": ("kansai/osaka", "80"),
    "4900": ("kansai/osaka", "80"),
    "5020": ("kansai/osaka", "80"),
}
def _meta_for(prop_id: str):
    return PROPERTY_META.get(str(prop_id), ("kanto/tokyo", "20"))

AREA_PATH, SHISYA = _meta_for(PROP_ID)

# 人間向けURL（通知に載せる）
URL = f"https://www.ur-net.go.jp/chintai/{AREA_PATH}/{SHISYA}_{PROP_ID}.html"

# 監視時間（JST 09:30〜19:00）
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)
WINDOW_END   = (18, 59)

# 内部APIエンドポイント（実績のある旧式フォーム）
ENDPOINT = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
HEADERS  = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://www.ur-net.go.jp",
    "Referer": "https://www.ur-net.go.jp/",
    "User-Agent": "ur-monitor/1.0 (+github-actions)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# 7080だけ特殊コードが存在（過去実績）
DANCHI_CD_MAP = {
    "7080": "7080e",
}

def in_window(now: datetime) -> bool:
    s = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    e = now.replace(hour=WINDOW_END[0],   minute=WINDOW_END[1],   second=59, microsecond=0)
    return s <= now <= e

# --- fetch: “以前動いていた”フォームをベースに堅牢化 ---
def _payload_v1(page_index: int, prop_id: str, shisya: str) -> str:
    # 末尾0を落とした案（7080->708 / 5390->539 / 5010->501）
    danchi_trim = prop_id[:-1] if prop_id.endswith("0") else prop_id
    return (
        "rent_low=&rent_high=&"
        "floorspace_low=&floorspace_high=&"
        f"shisya={shisya}&danchi={danchi_trim}&"
        "shikibetu=0&newBukkenRoom=&"
        f"orderByField=0&orderBySort=0&pageIndex={page_index}&sp="
    )

def _payload_v1_alt(page_index: int, prop_id: str, shisya: str) -> str:
    # danchi=PROP_ID のまま
    return (
        "rent_low=&rent_high=&"
        "floorspace_low=&floorspace_high=&"
        f"shisya={shisya}&danchi={prop_id}&"
        "shikibetu=0&newBukkenRoom=&"
        f"orderByField=0&orderBySort=0&pageIndex={page_index}&sp="
    )

def _payload_v2(page_index: int, prop_id: str) -> str:
    # 別形式（7080e等に対応）。indexNoは1始まり固定。
    danchi_cd = DANCHI_CD_MAP.get(prop_id, prop_id)
    index_no  = page_index + 1
    return f"danchiCd={danchi_cd}&indexNo={index_no}&pageSize=20"

def _try_fetch_page(page_index: int, prop_id: str):
    """
    1ページだけ取得して標準化した list[tuple] を返す。
    JSONにならなければ None（上位で安全停止）。
    """
    payloads = [
        _payload_v1(page_index, prop_id, SHISYA),
        _payload_v1_alt(page_index, prop_id, SHISYA),
        _payload_v2(page_index, prop_id),
    ]
    for pi, data in enumerate(payloads, 1):
        try:
            r = requests.post(ENDPOINT, headers=HEADERS, data=data, timeout=15)
            r.raise_for_status()
            try:
                j = r.json()
            except Exception as e:
                print(f"[fetch] non-JSON pi={pi} page={page_index}: {e} len={len(r.text)}")
                continue
        except Exception as e:
            print(f"[fetch] http_error pi={pi} page={page_index}: {e}")
            continue

        # 標準化
        if isinstance(j, list):
            rows = j
        elif isinstance(j, dict):
            rows = j.get("resultList") or j.get("rows") or j.get("data") or []
        else:
            rows = []

        out = []
        for r0 in rows:
            out.append((
                str(r0.get("id") or r0.get("roomId") or ""),
                str(r0.get("name") or r0.get("roomNo") or ""),
                str(r0.get("type") or r0.get("layout") or ""),
                str(r0.get("floorspace") or r0.get("area") or ""),
                str(r0.get("floor") or ""),
                str(r0.get("rent") or ""),
                str(r0.get("commonfee") or r0.get("maintenanceFee") or ""),
            ))
        return out
    return None

def fetch_all() -> set[tuple]:
    items: list[tuple] = []
    for i in (0, 1, 2):
        page = _try_fetch_page(i, PROP_ID)
        if page is None:
            print(f"[fetch] page{i}: decode_failed -> keep_state")
            return None
        if not page:
            break
        items.extend(page)
    return set(items)

# ========= State & Diff =========
def load_state():
    if not os.path.exists(STATE_PATH):
        return set(), True
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        rooms = data["rooms"] if isinstance(data, dict) and "rooms" in data else data
        if not isinstance(rooms, list):
            return set(), True
        return set(tuple(x) for x in rooms), False
    except Exception:
        return set(), True

def save_state(s: set):
    payload = {"rooms": sorted(list(s))}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def canonicalize(rows: set[tuple]) -> set[tuple]:
    def norm(s: str) -> str:
        if s is None:
            return ""
        s = str(s)
        s = s.replace("㎡", "m²").replace("\u33a1", "m²").replace("&sup2;", "²").replace("m&sup2;", "m²")
        return re.sub(r"[,\s]", "", s)
    out = set()
    for (_rid, name, typ, area, floor, rent, fee) in rows:
        out.add((norm(name), norm(typ), norm(area), norm(floor), norm(rent), norm(fee)))
    return out

# ========= Notify =========
def notify(msg: str):
    if not CHAT_TOKEN or not CHAT_ROOM:
        print(msg)
        return
    body = msg if len(msg) <= 9000 else (msg[:9000] + "\n…(truncated)")
    try:
        r = requests.post(
            f"https://api.chatwork.com/v2/rooms/{CHAT_ROOM}/messages",
            headers={"X-ChatWorkToken": CHAT_TOKEN},
            data={"body": f"[info][title]UR監視 {PROP_ID}[/title]{body}[/info]"},
            timeout=15,
        )
        print(f"chatwork_status={r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"notify_failed: {e}")
        print(msg)

# ========= Main =========
HB_FILE = f".hb_{PROP_ID}.txt"

def _hb_sent_today(now) -> bool:
    try:
        with open(HB_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() == now.strftime("%Y%m%d")
    except Exception:
        return False

def _hb_mark(now) -> None:
    with open(HB_FILE, "w", encoding="utf-8") as f:
        f.write(now.strftime("%Y%m%d"))

def main():
    now = datetime.now(JST)

    # 朝一ハートビート（任意）
    if now.hour == 9 and 30 <= now.minute < 40 and not _hb_sent_today(now):
        try:
            notify(f"[起動HB] JST {now:%H:%M} / {URL}")
            _hb_mark(now)
        except Exception:
            pass

    if not in_window(now):
        print("skip_out_of_window")
        return

    prev, is_init = load_state()
    rows = fetch_all()
    if rows is None:
        print("fetch_failed_keep_state")
        return

    current = canonicalize(rows)
    print(f"[rooms] {len(current)} entries after canon")

    if is_init:
        notify(f"[初期化 {PROP_ID}] 件数: {len(current)}\n{URL}")
    elif current != prev:
        added   = current - prev
        removed = prev - current
        lines = []
        if added:
            lines.append("+ " + " / ".join([a[0] for a in sorted(added)][:5]))
        if removed:
            lines.append("− " + " / ".join([a[0] for a in sorted(removed)][:5]))
        notify("【変化あり】\n" + ("\n".join(lines) if lines else "差分あり") + f"\n{URL}")
    else:
        print("no_change")

    save_state(current)

if __name__ == "__main__":
    main()
