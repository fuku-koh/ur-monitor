# -*- coding: utf-8 -*-
"""
UR vacancy monitor (GitHub Actions friendly)
- Try internal API (v1) with cookies; if fail/empty, try legacy form API (v2),
  then scrape public page (iframe-aware).
- Notifies via ChatWork if env set; otherwise prints logs.
"""
import os, json, re, time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# -------- Config --------
PROP_ID    = os.getenv("PROP_ID", "7080")
STATE_PATH = os.getenv("STATE_FILE", ".state.json")

def to_danchi_code(prop_id: str) -> str:
    # 7080 だけ "7080e"（他はそのまま）
    return {"7080": "7080e"}.get(prop_id, prop_id)

DANCHI  = to_danchi_code(PROP_ID)  # 例: "7080e"
DANCHI3 = PROP_ID[:3]              # 例: "708"（旧APIで必要）

URL = f"https://www.ur-net.go.jp/chintai/kanto/tokyo/20_{PROP_ID}.html"

# 監視時間（JST 09:30〜19:00）
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)
WINDOW_END   = (18, 59)

# -------- HTTP headers / endpoints --------
API_V1 = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
API_V1_HEADERS = {
    "Origin": "https://www.ur-net.go.jp",
    "Referer": URL,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "Mozilla/5.0 ur-monitor (+github-actions)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
# 旧フォーム（ページングは pageIndex=0,1,2…）
API_V2 = API_V1  # 同じエンドポイントで FORM が違うケースがあるため
API_V2_HEADERS = {
    "Origin": "https://www.ur-net.go.jp",
    "Referer": URL,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "Mozilla/5.0 ur-monitor (+github-actions)",
}

PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 ur-monitor (+github-actions)",
    "Referer": "https://www.ur-net.go.jp/chintai/",
    "Accept-Language": "ja,en;q=0.8",
}

# -------- helpers --------
def in_window(now):
    s = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    e = now.replace(hour=WINDOW_END[0],   minute=WINDOW_END[1],   second=59, microsecond=0)
    return s <= now <= e

def decode_area(s: str) -> str:
    if not s: return ""
    return (s.replace("㎡", "m²").replace("&sup2;", "²").replace("m&sup2;", "m²").replace("\u33a1", "m²"))

def parse_api_text(text: str):
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
    if items is None: return None
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

# ---- HTML parsers（公開/iframe 共通）----
_name_re  = re.compile(r"(\d{2,4})号室")
_layout_re= re.compile(r"(?:[1-4]LDK|[1-4]DK|[1-4]K|ワンルーム)")
_area_re  = re.compile(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|&#13217;)")
_floor_re = re.compile(r"(\d+)\s*階")
_rent_re  = re.compile(r"([\d,]+)\s*円")
_comm_re  = re.compile(r"共益?費.*?([\d,]+)\s*円|\(([\d,]+)\s*円\)")

def parse_table_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    rooms = []
    scopes = soup.select("table") or [soup]
    for scope in scopes:
        for tr in scope.select("tr"):
            txt = tr.get_text(" ", strip=True)
            if not txt: continue
            m_name = _name_re.search(txt)
            if not m_name: continue

            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            rent_txt = ""
            for t in tds:
                m = _rent_re.search(t)
                if m: rent_txt = m.group(1) + "円"; break
            if not rent_txt:
                m = _rent_re.search(txt)
                rent_txt = m.group(1) + "円" if m else ""

            comm_txt = ""
            for t in tds:
                m = _comm_re.search(t)
                if m: comm_txt = (m.group(1) or m.group(2)) + "円"; break
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

# -------- fetchers --------
def fetch_from_api_v1(session: requests.Session):
    results = []
    page = 1
    while page <= 10:
        payload = {"danchiCd": DANCHI, "indexNo": str(page), "pageSize": "20"}
        try:
            r = session.post(API_V1, headers=API_V1_HEADERS, data=payload, timeout=12)
        except Exception as e:
            print(f"[fetch] api(v1) error p={page}: {e}"); return None
        if r.status_code != 200:
            print(f"[fetch] api(v1) HTTP {r.status_code} p={page}"); return None
        rooms = parse_api_text(r.text)
        if rooms is None:
            # HTMLが返ってきている（認可/クッキー不足など）
            print(f"[fetch] api(v1) non-JSON p={page}")
            return None
        results.extend(rooms)
        if len(rooms) < 20: break
        page += 1
    print(f"[fetch] api(v1) -> {len(results)} rooms")
    return results

def fetch_from_api_v2(session: requests.Session):
    """旧フォーム：pageIndex と 3桁 danchi を使う"""
    results = []
    page_idx = 0
    while page_idx <= 9:
        form = (
            "rent_low=&rent_high=&floorspace_low=&floorspace_high="
            f"&shisya=20&danchi={DANCHI3}&shikibetu=0&newBukkenRoom="
            f"&orderByField=0&orderBySort=0&pageIndex={page_idx}&sp="
        )
        try:
            r = session.post(API_V2, headers=API_V2_HEADERS, data=form, timeout=12)
        except Exception as e:
            print(f"[fetch] api(v2-old) error i={page_idx}: {e}"); return None
        if r.status_code != 200:
            print(f"[fetch] api(v2-old) HTTP {r.status_code} i={page_idx}"); return None

        # v2 は JSON のときと HTML 断片のときがある
        rooms = parse_api_text(r.text)
        if rooms is None:
            rooms = parse_table_html(r.text)

        if not rooms:
            # 次ページなし
            break
        results.extend(rooms)
        if len(rooms) < 20:
            break
        page_idx += 1
    if results:
        print(f"[fetch] api(v2-old) -> {len(results)} rooms")
    else:
        print("[fetch] api(v2-old) -> 0 rooms")
    return results or None

def fetch_from_public(session: requests.Session):
    # 本体
    try:
        r = session.get(URL, headers=PAGE_HEADERS, timeout=12)
        r.raise_for_status()
    except Exception as e:
        print(f"[fetch] public page error: {e}")
        return []
    rooms = parse_table_html(r.text)
    if rooms:
        print(f"[fetch] public page -> {len(rooms)} rooms")
        return rooms

    soup = BeautifulSoup(r.text, "html.parser")
    candidates = []
    for ifr in soup.select("iframe[src]"):
        src = ifr.get("src", "")
        if any(k in src for k in ("danchi", "bukken", "result", "room", "list")):
            candidates.append(src)
    if not candidates:
        candidates = [ifr.get("src", "") for ifr in soup.select("iframe[src]")]

    for src in candidates:
        if not src: continue
        url2 = urljoin(URL, src)
        try:
            r2 = session.get(url2, headers=PAGE_HEADERS, timeout=12)
            r2.raise_for_status()
        except Exception as e:
            print(f"[fetch] iframe get failed {url2}: {e}")
            continue
        rooms = parse_table_html(r2.text)
        if rooms:
            print(f"[fetch] iframe({url2}) -> {len(rooms)} rooms")
            return rooms

    print("[fetch] html fallback -> 0 rooms")
    return []

def fetch_all():
    # まずセッション確立（クッキー/リファラ）
    session = requests.Session()
    try:
        session.get(URL, headers=PAGE_HEADERS, timeout=10)
    except Exception:
        pass

    # 1) API v1
    rooms = fetch_from_api_v1(session)
    if rooms: return rooms
    # 2) 旧フォーム v2
    rooms = fetch_from_api_v2(session)
    if rooms: return rooms
    # 3) 公開ページ（iframe対応）
    return fetch_from_public(session)

# -------- diff & notify --------
def canonicalize(rooms):
    canon = []
    for r in rooms:
        def clean(s): return re.sub(r"[,\s]", "", decode_area(s or ""))
        canon.append((clean(r.get("name")), clean(r.get("type")), clean(r.get("floorspace")),
                      clean(r.get("floor")), clean(r.get("rent")), clean(r.get("commonfee"))))
    return sorted(set(canon))

def load_state():
    if not os.path.exists(STATE_PATH): return set(), True
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
    token = os.getenv("CHATWORK_TOKEN"); room = os.getenv("CHATWORK_ROOM_ID")
    if not token or not room: print(msg); return
    body = msg if len(msg) <= 9000 else (msg[:9000] + "\n…(truncated)")
    try:
        r = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room}/messages",
            headers={"X-ChatWorkToken": token},
            data={"body": f"[info][title]UR監視[/title]{body}[/info]"},
            timeout=15,
        )
        print(f"chatwork_status={r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"notify_failed: {e}"); print(msg)

# -------- heartbeat --------
HB_FILE = ".hb-date.txt"
def _hb_sent_today(now):
    try:
        with open(HB_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() == now.strftime("%Y%m%d")
    except Exception: return False
def _hb_mark(now):
    with open(HB_FILE, "w", encoding="utf-8") as f:
        f.write(now.strftime("%Y%m%d"))

# -------- Main --------
def main():
    now = datetime.now(JST)
    if now.hour == 9 and 30 <= now.minute < 40 and not _hb_sent_today(now):
        try:
            notify(f"[UR監視 起動ハートビート] JST {now:%H:%M} / {URL}")
            _hb_mark(now)
        except Exception: pass

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
