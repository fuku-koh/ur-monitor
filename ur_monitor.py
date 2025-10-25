# -*- coding: utf-8 -*-
"""
UR vacancy monitor (GitHub Actions friendly)
- Polls the UR internal endpoint via POST (no JS) and detects diffs.
- Notifies via LINE Notify if LINE_NOTIFY_TOKEN is set; otherwise prints to logs.
"""

# ---- config (上部の定数定義付近に置き換え) ----
# ==== config (物件IDや状態ファイルは可変) ====
import os, json, re, time, sys
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

PROP_ID    = os.getenv("PROP_ID", "7080")                 # 例: 7080/5390/6940
STATE_PATH = os.getenv("STATE_FILE", f".state-{PROP_ID}.json")

def to_danchi_code(prop_id: str) -> str:
    # 7080 -> 708 / 5390 -> 539 / その他は prop_id のまま
    return prop_id[:-1] if (len(prop_id) == 4 and prop_id.endswith("0")) else prop_id

DANCHI = to_danchi_code(PROP_ID)

# 人間向けURL（通知に添える）
URL = f"https://www.ur-net.go.jp/chintai/kanto/tokyo/20_{PROP_ID}.html"
PROPERTY_LINK = URL

# JST 窓
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)   # 09:30 JST
WINDOW_END   = (18, 59)  # 18:59 JST (inclusive)

# UR API エンドポイント
ENDPOINT = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
HEADERS = {
    "Origin":   "https://www.ur-net.go.jp",
    "Referer":  "https://www.ur-net.go.jp/",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "ur-monitor/1.0 (+github-actions)",
}

# API の共通 payload（danchi は必ず可変）
FORM_BASE = (
    "rent_lower=&rent_upper=&floorspace_low=&floorspace_high="
    f"&sshisyo=2&danchi={DANCHI}&shikibetu=0&mebukkenBango="
    "&orderByField=0&orderBy=0&searchIndex=&v=1"
)


print(f"[conf] PROP_ID={PROP_ID} DANCHI={DANCHI} STATE_PATH={STATE_PATH}")
print(f"[api] fetched rooms: {len(rooms)}  link={PROPERTY_LINK}")


# --- Config ---
# Asia/Tokyo window
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)   # 09:30 JST
WINDOW_END   = (18, 59)  # 18:59 JST（inclusive）

# UR endpoint + payload (as shared)
ENDPOINT = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
HEADERS = {
    "Origin": "https://www.ur-net.go.jp",
    "Referer": "https://www.ur-net.go.jp/",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "ur-monitor/1.0 (+github-actions)"
}

# Note: 'danchi' came from DevTools payload. Keep it as provided ("708").
# If the site changes to require "7080", change here.
FORM_BASE = (
    "rent_low=&rent_high=&floorspace_low=&floorspace_high="
    "&shisya=20&danchi=708&shikibetu=0&newBukkenRoom="
    "&orderByField=0&orderBySort=0&pageIndex={idx}&sp="
)
PAGE_INDEXES = [0, 1, 2]  # extend if pages increase

PROPERTY_LINK = "https://www.ur-net.go.jp/chintai/kanto/tokyo/20_7080.html"
STATE_PATH = ".state.json"

# ---------------------- Helpers ----------------------

JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)   # 09:30 JST
WINDOW_END   = (18, 59)  # 18:59 JST（inclusive）

def in_window(now: datetime) -> bool:
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end   = now.replace(hour=18, minute=59, second=59, microsecond=0)
    return start <= now <= end

def decode_area(s: str) -> str:
    if not s:
        return s
    # Replace HTML square meters &#13217; with ㎡
    return s.replace("&#13217;", "㎡").replace("&amp;#13217;", "㎡")

def parse_entries(text: str):
    """Parse either JSON or HTML fragment; return list of dict entries."""
    # Try JSON first
    try:
        data = json.loads(text)
        items = None
        if isinstance(data, dict):
            # Try common keys
            if "result" in data and isinstance(data["result"], list):
                items = data["result"]
            elif "data" in data and isinstance(data["data"], list):
                items = data["data"]
        elif isinstance(data, list):
            items = data
        if items is not None:
            rooms = []
            for it in items:
                rooms.append({
                    "id": str(it.get("id") or it.get("roomId") or ""),
                    "name": str(it.get("name") or it.get("roomNo") or ""),
                    "type": str(it.get("type") or it.get("layout") or ""),
                    "floorspace": decode_area(str(it.get("floorspace") or it.get("area") or "")),
                    "floor": str(it.get("floor") or ""),
                    "rent": str(it.get("rent") or ""),
                    "commonfee": str(it.get("commonfee") or it.get("maintenanceFee") or ""),
                })
            return rooms
    except Exception:
        pass

    # Fallback: HTML fragment
    soup = BeautifulSoup(text, "html.parser")
    rooms = []
    # Try typical card/table containers; be permissive.
    candidates = soup.select(".room, .roomCard, .list, .table, tr, li")
    for c in candidates:
        txt = c.get_text(" ", strip=True)
        if not txt:
            continue
        # Extract key fields from text
        name_m   = re.search(r"(\d{2,4})号室", txt)
        layout_m = re.search(r"((?:[1-4]LDK)|(?:[1-4]DK)|(?:[1-4]K)|(?:ワンルーム))", txt)
        area_m   = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|&#13217;)", txt)
        floor_m  = re.search(r"(\d+)\s*階", txt)
        rent_m   = re.search(r"賃料[:：]?\s*([\d,]+)\s*円|([\d,]+)\s*円", txt)
        comm_m   = re.search(r"共益?費[:：]?\s*([\d,]+)\s*円", txt)

        # Use at least name or rent to decide it's a room line
        if name_m or rent_m:
            rent_val = ""
            if rent_m:
                rent_val = rent_m.group(1) or rent_m.group(2) or ""
                rent_val = rent_val + "円" if rent_val else ""
            rooms.append({
                "id": "",
                "name": (name_m.group(1) + "号室") if name_m else "",
                "type": layout_m.group(1) if layout_m else "",
                "floorspace": (area_m.group(1) + "㎡") if area_m else "",
                "floor": (floor_m.group(1) + "階") if floor_m else "",
                "rent": rent_val,
                "commonfee": (comm_m.group(1) + "円") if comm_m else "",
            })
    return rooms

def fetch_page(idx: int):
    payload = FORM_BASE.format(idx=idx)
    r = requests.post(ENDPOINT, headers=HEADERS, data=payload, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} on pageIndex={idx}")
    return parse_entries(r.text)

def fetch_all():
    items = []
    for i in (0, 1, 2):
        try:
            r = requests.post(
                "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": "https://www.ur-net.go.jp",
                    "Referer": "https://www.ur-net.go.jp/",
                },
                data=(
                    "rent_low=&rent_high=&floorspace_low=&floorspace_high=&"
                    "shisya=20&danchi=708&shikibetu=0&newBukkenRoom=&"
                    "orderByField=0&orderBySort=0&pageIndex={}&sp="
                ).format(i),
                timeout=15,
            )
            r.raise_for_status()

            # JSONデコード（失敗や想定外の型は安全に中断）
            try:
                j = r.json()
            except Exception as e:
                print(f"json_decode_failed page={i}: {e} body[:200]={r.text[:200]!r}")
                return None

        except Exception as e:
            print(f"fetch_failed page={i}: {e}")
            return None

        # ------ 正規化：dict / list / None すべて吸収 ------
        if isinstance(j, list):
            rows = j
        elif isinstance(j, dict):
            rows = j.get("resultList") or j.get("rows") or j.get("data") or []
        else:
            rows = []
        # -----------------------------------------------

        if not rows:
            # データが空なら次ページ以降は見ない
            break

        for r0 in rows:
            items.append((
                r0.get("id"),
                r0.get("name"),
                r0.get("type"),
                r0.get("floorspace"),
                r0.get("floor"),
                r0.get("rent"),
                r0.get("commonfee"),
            ))

    return set(items)

def canonicalize(rooms):
    canon = []
    for r in rooms:
        def clean(s):
            s = s or ""
            s = decode_area(s)
            return re.sub(r"[,\s]", "", s)
        canon.append((
            clean(r.get("name")),
            clean(r.get("type")),
            clean(r.get("floorspace")),
            clean(r.get("floor")),
            clean(r.get("rent")),
            clean(r.get("commonfee")),
        ))
    return sorted(set(canon))

def load_state():
    # ファイル無い → 初回だけ初期化
    if not os.path.exists(STATE_PATH):
        return set(), True
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 後方互換: 旧 [] 形式 / 新 {"rooms":[...]} 形式の両方を読む
        if isinstance(data, dict) and "rooms" in data:
            rooms = data["rooms"]
        elif isinstance(data, list):
            rooms = data
        else:
            print("state_format_invalid")
            return set(), True
        return set(tuple(x) for x in rooms), False
    except Exception as e:
        print(f"state_load_error: {e}")
        return set(), True

def save_state(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(s)), f, ensure_ascii=False, indent=2)

def save_state(s: set):
    payload = {"rooms": sorted(list(s))}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def notify(msg):
    token = os.getenv("CHATWORK_TOKEN")
    room_id = os.getenv("CHATWORK_ROOM_ID")
    if not token or not room_id:
        print(msg)  # 未設定ならログ出力のみ
        return
    body = msg if len(msg) <= 9000 else (msg[:9000] + "\n…(truncated)")
    try:
        r = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/messages",
            headers={"X-ChatWorkToken": token},
            data={"body": f"[info][title]UR監視[/title]{body}[/info]"},
            timeout=15
        )
        print(f"chatwork_status={r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"notify_failed: {e}")
        print(msg)

def main():
    now = datetime.now(JST)
    if not in_window(now):
        print("skip_out_of_window")
        return  # ← ここは関数内なのでOK

    prev, is_init = load_state()

    current = fetch_all()
    if current is None:
        print("fetch_failed_keep_state")
        return

    if is_init:
        notify(f"[UR監視 初期化] 件数: {len(current)}\n{URL}")
    elif current != prev:
        added  = current - prev
        removed = prev - current
        lines = []
        if added:
            lines.append("+ " + " / ".join([a[1] for a in sorted(added)][:5]))
        if removed:
            lines.append("− " + " / ".join([a[1] for a in sorted(removed)][:5]))
        notify("【UR監視 変化あり】\n" + ("\n".join(lines) if lines else "差分あり") + f"\n{URL}")
    else:
        print("変更なし")

    save_state(current)

if __name__ == "__main__":
    main()
