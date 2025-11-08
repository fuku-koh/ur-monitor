# -*- coding: utf-8 -*-
"""
UR vacancy monitor (GitHub Actions friendly)
- Polls the UR internal endpoint via POST (no JS) and detects diffs.
- Notifies via ChatWork (optional); otherwise prints to logs.
"""

import os, json, re, time
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

# -------- Config --------
PROP_ID    = os.getenv("PROP_ID", "7080")
STATE_PATH = os.getenv("STATE_FILE", ".state.json")

def to_danchi_code(prop_id: str) -> str:
    # 7080 だけ "7080e"（他はそのまま）
    return {"7080": "7080e"}.get(prop_id, prop_id)

DANCHI = to_danchi_code(PROP_ID)
DANCHI_SHORT = PROP_ID[:3]  # 旧フォームAPIは3桁団地番号（7080 -> "708"）

# 人間向けURL（通知に載せる）
URL = f"https://www.ur-net.go.jp/chintai/kanto/tokyo/20_{PROP_ID}.html"

# 監視時間（JST 09:30〜19:00）
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)
WINDOW_END   = (18, 59)

# UR の内部API（POST）
ENDPOINT = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
HEADERS = {
    "Origin": "https://www.ur-net.go.jp",
    "Referer": URL,  # f"...20_{PROP_ID}.html"
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "ur-monitor/1.0 (+github-actions)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    # "Accept-Language": "ja-JP,ja;q=0.9",  # 必要なら有効化
}

# 旧フォームAPIのベース（pageIndex 0始まり）
FORM_BASE = (
    "rent_low=&rent_high=&floorspace_low=&floorspace_high="
    f"&shisya=20&danchi={DANCHI_SHORT}&shikibetu=0&newBukkenRoom="
    "&orderByField=0&orderBySort=0&pageIndex={{idx}}&sp="
)

def make_payload(page: int) -> dict:
    """（参考）方式①用の最小ペイロード。indexNo は 1 始まり。"""
    return {"danchiCd": DANCHI, "indexNo": str(page), "pageSize": "20"}

# -------- Helpers --------
def in_window(now: datetime) -> bool:
    start = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0,  microsecond=0)
    end   = now.replace(hour=WINDOW_END[0],   minute=WINDOW_END[1],   second=59, microsecond=0)
    return start <= now <= end

def decode_area(s: str) -> str:
    if not s:
        return ""
    return (s.replace("㎡", "m²")
             .replace("&sup2;", "²")
             .replace("m&sup2;", "m²")
             .replace("\u33a1", "m²"))

def parse_entries(text: str):
    """JSON/HTML どちらでも部屋リストに正規化する。"""
    # 1) JSON っぽいならJSON優先で解釈
    data = None
    try:
        data = json.loads(text)
    except Exception:
        pass

    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("result", "resultList", "data", "rows"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break

    rooms = []
    if items is not None:
        for it in items:
            rooms.append({
                "id":        str(it.get("id") or it.get("roomId") or ""),
                "name":      str(it.get("name") or it.get("roomNo") or ""),
                "type":      str(it.get("type") or it.get("layout") or ""),
                "floorspace": decode_area(str(it.get("floorspace") or it.get("area") or "")),
                "floor":     str(it.get("floor") or ""),
                "rent":      str(it.get("rent") or ""),
                "commonfee": str(it.get("commonfee") or it.get("maintenanceFee") or ""),
            })
        return rooms

    # 2) HTML断片パース（最終手段にも使う）
    soup = BeautifulSoup(text, "html.parser")
    for c in soup.select(".room, .roomCard, .list, .table, tr, li"):
        txt = c.get_text(" ", strip=True)
        if not txt:
            continue
        name_m   = re.search(r"(\d{2,4})号室", txt)
        layout_m = re.search(r"((?:[1-4]LDK)|(?:[1-4]DK)|(?:[1-4]K)|(?:ワンルーム))", txt)
        area_m   = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|&#13217;)", txt)
        floor_m  = re.search(r"(\d+)\s*階", txt)
        rent_m   = re.search(r"賃料[:：]?\s*([\d,]+)\s*円|([\d,]+)\s*円", txt)
        comm_m   = re.search(r"共益?費[:：]?\s*([\d,]+)\s*円", txt)

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

# ---- HTTP ヘルパー & 3系統フェッチ ----
def _post(url: str, data: dict | str, timeout: int = 10) -> str:
    for attempt in range(3):
        try:
            r = requests.post(url, headers=HEADERS, data=data, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1 + attempt * 2)

# 方式①: danchiCd / indexNo=1..（今までのやり方）
def fetch_api_danchiCd() -> list[dict]:
    out, page = [], 1
    while page <= 10:
        text = _post(ENDPOINT, {"danchiCd": DANCHI, "indexNo": str(page), "pageSize": "20"})
        items = parse_entries(text or "")
        if not items:
            break
        out.extend(items)
        if len(items) < 20:
            break
        page += 1
    return out

# 方式②: 旧フォーム / pageIndex=0..（ここにだけ出ることがある）
def fetch_api_form() -> list[dict]:
    out, page = [], 0
    while page <= 10:
        text = _post(ENDPOINT, FORM_BASE.format(idx=page))
        items = parse_entries(text or "")
        if not items:
            break
        out.extend(items)
        if len(items) < 20:
            break
        page += 1
    return out

# バックアップ: 人向けHTMLを直接パース
def fetch_from_html() -> list[dict]:
    try:
        r = requests.get(
            URL,
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": "https://www.ur-net.go.jp/",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=10,
        )
        r.raise_for_status()
        return parse_entries(r.text)
    except Exception:
        return []

def fetch_all() -> list[dict]:
    # ① まず danchiCd 系
    a = fetch_api_danchiCd()
    if a:
        print(f"[fetch] danchiCd={DANCHI} -> {len(a)} rooms")
        return a
    # ② ダメなら 旧フォーム
    b = fetch_api_form()
    if b:
        print(f"[fetch] form danchi={DANCHI_SHORT} -> {len(b)} rooms")
        return b
    # ③ 最後の砦: HTML
    c = fetch_from_html()
    print(f"[fetch] html fallback -> {len(c)} rooms")
    return c

# -------- Diff helpers / state / notify --------
def canonicalize(rooms):
    """差分比較用に正規化してタプル集合へ。"""
    canon = []
    for r in rooms:
        def clean(s):
            s = decode_area(s or "")
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

def notify(msg: str):
    token   = os.getenv("CHATWORK_TOKEN")
    room_id = os.getenv("CHATWORK_ROOM_ID")
    if not token or not room_id:
        print(msg)
        return
    body = msg if len(msg) <= 9000 else (msg[:9000] + "\n…(truncated)")
    try:
        r = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/messages",
            headers={"X-ChatWorkToken": token},
            data={"body": f"[info][title]UR監視[/title]{body}[/info]"},
            timeout=15,
        )
        print(f"chatwork_status={r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"notify_failed: {e}")
        print(msg)

# -------- Heartbeat state --------
HB_FILE = ".hb-date.txt"

def _hb_sent_today(now) -> bool:
    try:
        with open(HB_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() == now.strftime("%Y%m%d")
    except Exception:
        return False

def _hb_mark(now) -> None:
    with open(HB_FILE, "w", encoding="utf-8") as f:
        f.write(now.strftime("%Y%m%d"))

# -------- Main --------
def main():
    now = datetime.now(JST)

    # 9:30〜9:39 の間、かつ今日まだ送っていなければ一度だけ送る
    if now.hour == 9 and 30 <= now.minute < 40 and not _hb_sent_today(now):
        try:
            notify(f"[UR監視 起動ハートビート] JST {now:%H:%M} / {URL}")
            _hb_mark(now)
        except Exception:
            pass

    if not in_window(now):
        print("skip_out_of_window")
        return

    prev, is_init = load_state()
    rooms = fetch_all()
    current = set(canonicalize(rooms))

    if is_init:
        notify(f"[UR監視 初期化] 件数: {len(current)}\n{URL}")
    elif current != prev:
        added   = current - prev
        removed = prev - current
        lines = []
        if added:
            lines.append("+ " + " / ".join([a[0] for a in sorted(added)][:5]))
        if removed:
            lines.append("− " + " / ".join([a[0] for a in sorted(removed)][:5]))
        notify("【UR監視 変化あり】\n" + ("\n".join(lines) if lines else "差分あり") + f"\n{URL}")
    else:
        print("no_change")

    save_state(current)

if __name__ == "__main__":
    main()
