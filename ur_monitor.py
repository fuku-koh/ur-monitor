# -*- coding: utf-8 -*-
"""
UR vacancy monitor (GitHub Actions friendly)
- Polls the UR internal endpoint via POST (no JS) and detects diffs.
- Notifies via LINE Notify if LINE_NOTIFY_TOKEN is set; otherwise prints to logs.
"""

import os, json, re, time, sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ---------------------- Config ----------------------
# Asia/Tokyo window: 09:30–18:30 (inclusive), every 30 minutes via cron (UTC)
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)   # 09:30 JST
WINDOW_END   = (18, 30)  # 18:30 JST

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

def in_window(now: datetime) -> bool:
    """Return True if now is between [WINDOW_START, WINDOW_END] JST inclusive."""
    s_h, s_m = WINDOW_START
    e_h, e_m = WINDOW_END
    after_start = (now.hour > s_h) or (now.hour == s_h and now.minute >= s_m)
    before_end  = (now.hour < e_h) or (now.hour == e_h and now.minute <= e_m)
    return after_start and before_end

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
    out = []
    for idx in PAGE_INDEXES:
        # attempt with one retry
        for attempt in (1, 2):
            try:
                rooms = fetch_page(idx)
                if idx == 0 and not rooms:
                    return []
                if not rooms:
                    return out
                out.extend(rooms)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2)
    return out

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
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(tuple(x) for x in data)
        except Exception:
            return set()
    return set()

def save_state(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(s)), f, ensure_ascii=False, indent=2)

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
        return

    try:
        rooms = fetch_all()
    except Exception as e:
        notify(f"【UR監視エラー】{now:%Y-%m-%d %H:%M} 失敗: {e}")
        print("FAIL", e)
        sys.exit(1)

    current = set(canonicalize(rooms))
    prev = load_state()

    if not prev:
        save_state(current)
        notify(f"【UR監視 初期化】{now:%Y-%m-%d %H:%M} 件数: {len(current)}\n{PROPERTY_LINK}")
        return

    added = current - prev
    removed = prev - current

    if added or removed:
        lines = [f"【UR新着/更新】{now:%Y-%m-%d %H:%M}"]
        if added:
            lines.append("＋ 追加:")
            for x in sorted(added):
                lines.append("  - " + " / ".join(filter(None, x)))
        if removed:
            lines.append("－ 消滅:")
            for x in sorted(removed):
                lines.append("  - " + " / ".join(filter(None, x)))
        lines.append(PROPERTY_LINK)
        notify("\n".join(lines))
        save_state(current)
    else:
        print("変更なし")

if __name__ == "__main__":
    main()
