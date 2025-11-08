# -*- coding: utf-8 -*-
"""
UR vacancy monitor (GitHub Actions friendly)
- Try internal API first; if it fails/empty, scrape the public property page.
- Public page fallback is iframe-aware.
- Notifies via ChatWork (if env set); otherwise prints to logs.
"""

import os, json, re, time
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# -------- Config --------
PROP_ID    = os.getenv("PROP_ID", "7080")
STATE_PATH = os.getenv("STATE_FILE", ".state.json")

def to_danchi_code(prop_id: str) -> str:
    # 7080 だけ "7080e" になる（他はそのまま）
    return {"7080": "7080e"}.get(prop_id, prop_id)

DANCHI = to_danchi_code(PROP_ID)

# 人間向けURL（通知に載せる）
URL = f"https://www.ur-net.go.jp/chintai/kanto/tokyo/20_{PROP_ID}.html"

# 監視時間（JST 09:30〜19:00）
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)
WINDOW_END   = (18, 59)

# -------- HTTP --------
API_ENDPOINT = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
API_HEADERS = {
    "Origin": "https://www.ur-net.go.jp",
    "Referer": URL,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "Mozilla/5.0 ur-monitor (+github-actions)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 ur-monitor (+github-actions)",
    "Referer": "https://www.ur-net.go.jp/chintai/",
    "Accept-Language": "ja,en;q=0.8",
}

def make_payload(page: int) -> dict:
    """APIが受け付ける最小のペイロード。indexNo は 1 始まり。"""
    return {"danchiCd": DANCHI, "indexNo": str(page), "pageSize": "20"}

# -------- Common helpers --------
def in_window(now):
    s = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    e = now.replace(hour=WINDOW_END[0], minute=WINDOW_END[1],   second=59, microsecond=0)
    return s <= now <= e

def decode_area(s: str) -> str:
    if not s: return ""
    return (s.replace("㎡", "m²")
             .replace("&sup2;", "²")
             .replace("m&sup2;", "m²")
             .replace("\u33a1", "m²"))

# -------- Parsers --------
def parse_api_text(text: str):
    """APIの応答(JSON想定)→ list[dict]。失敗時は None を返す。"""
    try:
        data = json.loads(text)
    except Exception:
        return None

    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("result", "resultList", "data", "rows"):
            if key in data and isinstance(data[key], list):
                items = data[key]; break
    if items is None:
        return None

    rooms = []
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

# 表解析（公開ページ/iframe 共通）
_room_name_re = re.compile(r"(\d{2,4})号室")
_layout_re    = re.compile(r"(?:[1-4]LDK|[1-4]DK|[1-4]K|ワンルーム)")
_area_re      = re.compile(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|&#13217;)")
_floor_re     = re.compile(r"(\d+)\s*階")
_rent_re      = re.compile(r"([\d,]+)\s*円")
_comm_re      = re.compile(r"共益?費.*?([\d,]+)\s*円|\(([\d,]+)\s*円\)")

def parse_table_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    rooms = []

    # なるべく範囲を絞る：表の候補だけ見る
    tables = soup.select("table")
    if not tables:
        tables = [soup]  # 最悪全体から拾う

    for scope in tables:
        for tr in scope.select("tr"):
            txt = tr.get_text(" ", strip=True)
            if not txt:
                continue
            m_name  = _room_name_re.search(txt)
            if not m_name:
                continue

            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            # 家賃（カラム優先）
            rent_txt = ""
            for t in tds:
                m = _rent_re.search(t)
                if m: rent_txt = m.group(1) + "円"; break
            if not rent_txt:
                m = _rent_re.search(txt)
                rent_txt = m.group(1) + "円" if m else ""

            # 共益費
            comm_txt = ""
            for t in tds:
                m = _comm_re.search(t)
                if m:
                    comm_txt = (m.group(1) or m.group(2)) + "円"; break
            if not comm_txt:
                m = _comm_re.search(txt)
                if m: comm_txt = (m.group(1) or m.group(2)) + "円"

            m_layout = _layout_re.search(txt)
            m_area   = _area_re.search(txt)
            m_floor  = _floor_re.search(txt)

            rooms.append({
                "id": "",
                "name": m_name.group(1) + "号室",
                "type": m_layout.group(0) if m_layout else "",
                "floorspace": (m_area.group(1) + "㎡") if m_area else "",
                "floor": (m_floor.group(1) + "階") if m_floor else "",
                "rent": rent_txt,
                "commonfee": comm_txt,
            })
    return rooms

# -------- Fetchers --------
def fetch_from_api():
    results = []
    page = 1
    while page <= 10:
        payload = make_payload(page)
        try:
            r = requests.post(API_ENDPOINT, headers=API_HEADERS, data=payload, timeout=12)
            status = r.status_code
        except Exception as e:
            print(f"[fetch] api error page={page}: {e}")
            return None

        if status != 200:
            print(f"[fetch] api HTTP {status} page={page}")
            return None

        items = parse_api_text(r.text)
        if items is None:
            print(f"[fetch] api returned non-JSON page={page}")
            return None

        results.extend(items)
        if len(items) < 20:
            break
        page += 1
    return results

def fetch_from_public():
    # 1) 物件ページ本体
    try:
        r = requests.get(URL, headers=PAGE_HEADERS, timeout=12)
        r.raise_for_status()
    except Exception as e:
        print(f"[fetch] public page error: {e}")
        return []

    rooms = parse_table_html(r.text)
    if rooms:
        print(f"[fetch] public page -> {len(rooms)} rooms")
        return rooms

    # 2) 表が iframe 内のケースを辿る
    soup = BeautifulSoup(r.text, "html.parser")
    # 表示用っぽい iframe を探す（danchi / bukken / result などをヒントに）
    candidates = []
    for ifr in soup.select("iframe[src]"):
        src = ifr.get("src", "")
        if any(k in src for k in ("danchi", "bukken", "result", "room", "list")):
            candidates.append(src)
    # 見つからなくても全 iframe を最後に試す
    if not candidates:
        candidates = [ifr.get("src", "") for ifr in soup.select("iframe[src]")]

    base = URL
    for src in candidates:
        if not src: 
            continue
        iframe_url = urljoin(base, src)
        try:
            r2 = requests.get(iframe_url, headers=PAGE_HEADERS, timeout=12)
            r2.raise_for_status()
        except Exception as e:
            print(f"[fetch] iframe get failed {iframe_url}: {e}")
            continue
        rooms = parse_table_html(r2.text)
        if rooms:
            print(f"[fetch] iframe({iframe_url}) -> {len(rooms)} rooms")
            return rooms

    print("[fetch] html fallback -> 0 rooms")
    return []

def fetch_all():
    # 1) まずAPI
    api_rooms = fetch_from_api()
    if api_rooms:
        print(f"[fetch] api -> {len(api_rooms)} rooms")
        return api_rooms
    # 2) ダメなら公開ページ（iframe対応）
    return fetch_from_public()

# -------- Diff helpers --------
def canonicalize(rooms):
    canon = []
    for r in rooms:
        def clean(s): return re.sub(r"[,\s]", "", decode_area(s or ""))
        canon.append((clean(r.get("name")), clean(r.get("type")), clean(r.get("floorspace")),
                      clean(r.get("floor")), clean(r.get("rent")), clean(r.get("commonfee"))))
    return sorted(set(canon))

def load_state():
    if not os.path.exists(STATE_PATH):
        return set(), True
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        rooms = data["rooms"] if isinstance(data, dict) and "rooms" in data else data
        if not isinstance(rooms, list): return set(), True
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
        print(msg); return
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
        print(f"notify_failed: {e}"); print(msg)

# -------- Heartbeat --------
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
        print("skip_out_of_window"); return

    prev, is_init = load_state()
    rooms = fetch_all()
    current = set(canonicalize(rooms))

    if is_init:
        notify(f"[UR監視 初期化] 件数: {len(current)}\n{URL}")
    elif current != prev:
        added   = current - prev
        removed = prev - current
        lines = []
        if added:   lines.append("+ " + " / ".join([a[0] for a in sorted(added)][:5]))
        if removed: lines.append("− " + " / ".join([a[0] for a in sorted(removed)][:5]))
        notify("【UR監視 変化あり】\n" + ("\n".join(lines) if lines else "差分あり") + f"\n{URL}")
    else:
        print("no_change")

    save_state(current)

if __name__ == "__main__":
    main()
