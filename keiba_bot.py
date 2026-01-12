import time
import json
import re
import requests
import streamlit as st

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================================================
# 【設定】Secrets読み込み
# ==================================================
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")

# self-host の場合はここを自分のDifyドメインに
DIFY_BASE_URL = st.secrets.get("DIFY_BASE_URL", "https://api.dify.ai")

# ==================================================
# 内部ユーティリティ：UI出力のON/OFFを切り替える
# ==================================================
def _ui_info(ui: bool, msg: str):
    if ui:
        st.info(msg)

def _ui_success(ui: bool, msg: str):
    if ui:
        st.success(msg)

def _ui_warning(ui: bool, msg: str):
    if ui:
        st.warning(msg)

def _ui_error(ui: bool, msg: str):
    if ui:
        st.error(msg)

def _ui_caption(ui: bool, msg: str):
    if ui:
        st.caption(msg)

def _ui_markdown(ui: bool, msg: str):
    if ui:
        st.markdown(msg)

def _ui_divider(ui: bool):
    if ui:
        st.divider()

# ==================================================
# requests session + retry
# ==================================================
def _build_requests_session(total: int = 3, backoff: float = 0.6) -> requests.Session:
    sess = requests.Session()
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess

@st.cache_resource
def get_http_session() -> requests.Session:
    return _build_requests_session(total=3, backoff=0.6)


# ==================================================
# Selenium Driver（競馬ブック用）
# ==================================================
def build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,2200")
    return webdriver.Chrome(options=options)

def login_keibabook(driver: webdriver.Chrome, wait: WebDriverWait):
    driver.get("https://s.keibabook.co.jp/login/login")
    wait.until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(KEIBA_ID)
    driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
    driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
    time.sleep(1)

# ==================================================
# スクレイピング：日程→レースID一覧（競馬ブック）
# ==================================================
def fetch_race_ids_from_schedule(driver, year, month, day, target_place_code, ui: bool = False):
    date_str = f"{year}{month}{day}"
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}10"

    _ui_info(ui, f"📅 日程ページからレースIDを取得中... ({url})")
    driver.get(url)

    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "a")))
    except:
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    race_ids = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"(\d{16})", href)
        if not m:
            continue
        rid = m.group(1)
        if rid[6:8] == target_place_code:
            if rid not in seen:
                race_ids.append(rid)
                seen.add(rid)

    race_ids.sort()
    if not race_ids:
        _ui_warning(ui, f"⚠️ 指定した競馬場コード({target_place_code})のレースIDが見つかりませんでした。")
    else:
        _ui_success(ui, f"✅ {len(race_ids)} 件のレースIDを取得しました。")
    return race_ids

# ==================================================
# 競馬ブック：レース情報/談話/調教
# ==================================================
def parse_race_info(html: str):
    soup = BeautifulSoup(html, "html.parser")
    racetitle = soup.find("div", class_="racetitle")
    if not racetitle:
        return {}

    racemei = racetitle.find("div", class_="racemei")
    p_tags = racemei.find_all("p") if racemei else []
    race_name = ""
    if len(p_tags) >= 2:
        race_name = p_tags[1].get_text(strip=True)
    elif len(p_tags) == 1:
        race_name = p_tags[0].get_text(strip=True)

    sub = racetitle.find("div", class_="racetitle_sub")
    sub_p = sub.find_all("p") if sub else []
    cond = sub_p[1].get_text(" ", strip=True) if len(sub_p) >= 2 else ""

    return {"race_name": race_name, "cond": cond}

def parse_danwa_comments(html: str):
    soup = BeautifulSoup(html, "html.parser")
    danwa_dict = {}
    table = soup.find("table", class_="danwa")

    if table and table.tbody:
        current_uma = None
        for row in table.tbody.find_all("tr"):
            uma_td = row.find("td", class_="umaban")
            if uma_td:
                current_uma = uma_td.get_text(strip=True)
                continue

            txt_td = row.find("td", class_="danwa")
            if txt_td and current_uma:
                danwa_dict[current_uma] = txt_td.get_text(strip=True)
                current_uma = None

    return danwa_dict

def parse_cyokyo(html: str):
    soup = BeautifulSoup(html, "html.parser")
    cyokyo_dict = {}
    tables = soup.find_all("table", class_="cyokyo")
    for tbl in tables:
        tbody = tbl.find("tbody")
        if not tbody:
            continue
        rows = tbody.find_all("tr", recursive=False)
        if not rows:
            continue

        h_row = rows[0]
        uma_td = h_row.find("td", class_="umaban")
        name_td = h_row.find("td", class_="kbamei")
        if not uma_td or not name_td:
            continue

        umaban = uma_td.get_text(strip=True)
        bamei = name_td.get_text(" ", strip=True)

        tanpyo_elem = h_row.find("td", class_="tanpyo")
        tanpyo = tanpyo_elem.get_text(strip=True) if tanpyo_elem else ""
        detail = rows[1].get_text(" ", strip=True) if len(rows) > 1 else ""

        cyokyo_dict[umaban] = f"【馬名】{bamei} 【短評】{tanpyo} 【詳細】{detail}"

    return cyokyo_dict

# ==================================================
# 地方競馬公式（keiba.go.jp）：DOMで出馬表を堅牢にパース
# ==================================================
_KEIBAGO_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

def _norm_name(s: str) -> str:
    s = (s or "").strip().replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s)
    s = s.replace("▲", "").replace("△", "").replace("☆", "").replace("◇", "")
    return s.strip()

_WEIGHT_RE = re.compile(r"^[☆▲△◇]?\s*\d{1,2}\.\d$")
_PREV_JOCKEY_RE = re.compile(r"\d+人\s+([☆▲△◇]?\s*\S+)\s+\d{1,2}\.\d")

def _extract_jockey_from_cell(td) -> str:
    lines = [x.strip() for x in td.get_text("\n", strip=True).split("\n") if x.strip()]
    lines2 = [ln for ln in lines if not _WEIGHT_RE.match(ln)]
    if lines2:
        return lines2[0].replace(" ", "")
    return "不明"

def fetch_keibago_debatable_small(year: str, month: str, day: str, race_no: int, baba_code: str):
    """
    戻り値: (header, horses, url, race_level_from_nar)
    """
    date_str = f"{year}/{str(month).zfill(2)}/{str(day).zfill(2)}"
    url = (
        "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/DebaTableSmall"
        f"?k_raceDate={requests.utils.quote(date_str)}&k_raceNo={race_no}&k_babaCode={baba_code}"
    )

    sess = _build_requests_session(total=3, backoff=0.6)
    r = sess.get(url, headers=_KEIBAGO_UA, timeout=25)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    header = ""
    top_bs = soup.select_one("table.bs")
    if top_bs:
        header = top_bs.get_text(" ", strip=True)

    # ----------------------------------------------------
    # ★追加: レース名（レベル含む）を取得
    # <span class="midium"><b><font...>芋洗坂賞Ｂ３二選抜特別</font></b></span>
    # ----------------------------------------------------
    nar_race_level = ""
    title_span = soup.select_one("span.midium")
    if title_span:
        nar_race_level = title_span.get_text(strip=True)
    # ----------------------------------------------------

    main_table = soup.select_one("td.dbtbl table.bs[border='1']")
    if not main_table:
        main_table = soup.select_one("table.bs[border='1']")

    horses = {}
    last_waku = ""

    if not main_table:
        return header, horses, url, nar_race_level

    for tr in main_table.find_all("tr"):
        if not tr.select_one("font.bamei"):
            continue

        tds = tr.find_all("td", recursive=False)
        if len(tds) < 8:
            continue

        first_txt = tds[0].get_text(strip=True)
        waku_present = first_txt.isdigit() and len(tds) >= 9
        if waku_present:
            second_txt = tds[1].get_text(strip=True)
            if not second_txt.isdigit():
                waku_present = False

        if waku_present:
            waku = tds[0].get_text(strip=True)
            umaban = tds[1].get_text(strip=True)
            horse_td = tds[2]
            trainer_td = tds[3]
            jockey_td = tds[4]
            zenso_td = tds[8] if len(tds) > 8 else None
            last_waku = waku
        else:
            waku = last_waku or ""
            umaban = tds[0].get_text(strip=True)
            horse_td = tds[1]
            trainer_td = tds[2]
            jockey_td = tds[3]
            zenso_td = tds[7] if len(tds) > 7 else None

        if not umaban.isdigit():
            continue

        bamei_tag = horse_td.select_one("font.bamei b")
        horse = bamei_tag.get_text(strip=True) if bamei_tag else horse_td.get_text(" ", strip=True)

        trainer_raw = trainer_td.get_text(" ", strip=True)
        trainer = trainer_raw.split("（")[0].strip() if trainer_raw else "不明"

        jockey = _extract_jockey_from_cell(jockey_td)

        prev_jockey = ""
        if zenso_td:
            zenso_txt = zenso_td.get_text(" ", strip=True)
            m = _PREV_JOCKEY_RE.search(zenso_txt)
            if m:
                prev_jockey = m.group(1).strip().replace(" ", "")

        cj = _norm_name(jockey)
        pj = _norm_name(prev_jockey)
        is_change = bool(pj and cj and pj != cj)

        horses[str(umaban)] = {
            "waku": str(waku),
            "umaban": str(umaban),
            "horse": horse,
            "trainer": trainer if trainer else "不明",
            "jockey": jockey if jockey else "不明",
            "prev_jockey": prev_jockey,
            "is_change": is_change,
        }

    return header, horses, url, nar_race_level

# ==================================================
# Dify：堅牢かつ余計なテキストを拾わない修正版
# ==================================================
def _dify_url(path: str) -> str:
    base = (DIFY_BASE_URL or "").strip().rstrip("/")
    return f"{base}{path}"

def _format_http_error(res: requests.Response) -> str:
    try:
        j = res.json()
        return f"⚠️ Dify HTTP {res.status_code}: {j}"
    except:
        txt = (res.text or "")[:800]
        return f"⚠️ Dify HTTP {res.status_code}: {txt}"

def _pick_output(outputs: dict) -> str:
    if not isinstance(outputs, dict):
        return ""
    candidates = ["result", "answer", "output", "text"]
    for k in candidates:
        v = outputs.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    
    best = ""
    best_len = 0
    for v in outputs.values():
        if isinstance(v, str):
            s = v.strip()
            if len(s) > best_len:
                best = s
                best_len = len(s)
    return best.strip()

def get_workflow_run_detail(workflow_run_id: str) -> dict:
    url = _dify_url(f"/v1/workflows/run/{workflow_run_id}")
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}"}
    sess = get_http_session()
    r = sess.get(url, headers=headers, timeout=(10, 30))
    if r.status_code != 200:
        raise RuntimeError(_format_http_error(r))
    return r.json() if r.headers.get("Content-Type","").startswith("application/json") else {"raw": r.text}

def poll_workflow_until_done(workflow_run_id: str, max_wait_sec: int = 120, interval_sec: float = 1.5) -> str:
    start = time.time()
    last_status = ""
    while time.time() - start < max_wait_sec:
        try:
            j = get_workflow_run_detail(workflow_run_id) or {}
        except:
            time.sleep(interval_sec)
            continue
            
        status = j.get("status") or j.get("data", {}).get("status") or ""
        last_status = status

        outputs = j.get("outputs") or j.get("data", {}).get("outputs") or {}
        err = j.get("error") or j.get("data", {}).get("error")

        if status in ("succeeded", "failed", "stopped", "partial-succeeded"):
            if err:
                return f"⚠️ workflow {status}: {err}"
            picked = _pick_output(outputs)
            return picked or f"⚠️ workflow {status} だが outputs が空でした"

        time.sleep(interval_sec)

    return f"⚠️ workflow polling timeout（last_status={last_status}）"

def stream_dify_workflow(full_text: str):
    """
    text_chunk のみを yield し、入力プロンプトを拾わない
    """
    if not DIFY_API_KEY:
        yield "⚠️ DIFY_API_KEY未設定"
        return

    url = _dify_url("/v1/workflows/run")
    payload = {
        "inputs": {"text": full_text},
        "response_mode": "streaming",
        "user": "keiba-bot",
    }
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
    }

    sess = get_http_session()

    workflow_run_id = ""
    got_any_text = False
    got_any_event = False

    try:
        res = sess.post(url, headers=headers, json=payload, stream=True, timeout=(10, 310))
        if res.status_code != 200:
            yield _format_http_error(res)
            return

        for line in res.iter_lines(decode_unicode=True, chunk_size=1):
            if not line:
                continue
            if not line.startswith("data:"):
                continue

            raw = line[5:].lstrip()
            if not raw:
                continue

            try:
                evt = json.loads(raw)
            except:
                continue

            got_any_event = True
            workflow_run_id = workflow_run_id or evt.get("workflow_run_id") or (evt.get("data", {}) or {}).get("workflow_run_id") or ""
            
            event_type = evt.get("event")
            data = evt.get("data") or {}

            # 1. AI生成テキスト (Streaming)
            if event_type == "text_chunk":
                text = data.get("text", "")
                if text:
                    got_any_text = True
                    yield text
                continue

            # 2. ワークフロー完了 (Final Output)
            if event_type == "workflow_finished":
                if not got_any_text:
                    outputs = data.get("outputs", {}) or {}
                    final = _pick_output(outputs)
                    if final:
                        yield final
                return

            continue

        if not got_any_event:
            yield "⚠️ DifyがSSEを返しませんでした"
            return

        if workflow_run_id:
            yield poll_workflow_until_done(workflow_run_id, max_wait_sec=140)

    except Exception as e:
        if workflow_run_id:
            yield poll_workflow_until_done(workflow_run_id, max_wait_sec=140)
        else:
            yield f"⚠️ Dify API Error: {str(e)}"

def run_dify_workflow_blocking(full_text: str) -> str:
    if not DIFY_API_KEY:
        return "⚠️ DIFY_API_KEY未設定"

    url = _dify_url("/v1/workflows/run")
    payload = {
        "inputs": {"text": full_text},
        "response_mode": "blocking",
        "user": "keiba-bot",
    }
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    sess = get_http_session()

    try:
        res = sess.post(url, headers=headers, json=payload, timeout=(10, 95))
        if res.status_code != 200:
            return _format_http_error(res)

        j = res.json() or {}
        data = j.get("data", {}) or {}
        outputs = data.get("outputs", {}) or {}

        picked = _pick_output(outputs)
        if picked:
            return picked

        err = data.get("error")
        if err:
            return f"⚠️ blocking error: {err}"

        return "⚠️ blockingで outputs が空でした"

    except Exception as e:
        return f"⚠️ blocking API Error: {str(e)}"

# ==================================================
# ★重要：リトライロジック付きラッパー
# ==================================================
def run_dify_with_fallback(full_text: str) -> str:
    """
    streaming → 取れなければ blocking
    ★503エラーやOverloadedなら待機してリトライ
    """
    max_retries = 3
    
    for attempt in range(max_retries):
        chunks = []
        got_error = False
        error_msg = ""

        # 1. Streaming
        for c in stream_dify_workflow(full_text):
            chunks.append(c)
            # エラー文字列チェック
            if isinstance(c, str) and (c.startswith("⚠️ Dify HTTP") or "503" in c or "overloaded" in c or "PluginInvokeError" in c):
                got_error = True
                error_msg = c
                break

        streamed = "".join(chunks).strip()

        # 成功なら即終了
        if streamed and not got_error and "⚠️" not in streamed:
            return streamed
        
        # 2. Blocking（フォールバック）
        blocking_res = (run_dify_workflow_blocking(full_text) or "").strip()
        
        # Blocking側でもエラーかチェック
        is_server_error = False
        if "503" in blocking_res or "overloaded" in blocking_res or "PluginInvokeError" in blocking_res:
            is_server_error = True
        
        if is_server_error:
            if attempt < max_retries - 1:
                wait_time = 10 + (attempt * 5)
                st.warning(f"⚠️ AIが混雑しています（503/Overloaded）。{wait_time}秒待機して再試行します... ({attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                return f"⚠️ {max_retries}回試行しましたがAIが混雑しています: {blocking_res}"
        
        # エラーでなければBlockingの結果を返す
        if blocking_res:
            return blocking_res

        # Streamingのエラーを評価してリトライするか決める
        if error_msg:
             if "503" in error_msg or "overloaded" in error_msg:
                 if attempt < max_retries - 1:
                    wait_time = 10
                    st.warning(f"⚠️ AIが混雑しています。{wait_time}秒待機して再試行します...")
                    time.sleep(wait_time)
                    continue

        return streamed if streamed else "⚠️ Dify出力が空でした"

    return "⚠️ リトライ上限を超えました"

# ==================================================
# メイン：全レース実行（文字列を return）
# ==================================================
def run_all_races(
    year: str,
    month: str,
    day: str,
    place_code: str,
    target_races: set[int] | None,
    ui: bool = False,
) -> str:
    place_names = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
    place_name = place_names.get(place_code, "地方")

    baba_map = {"10": "20", "11": "21", "12": "19", "13": "18"}
    baba_code = baba_map.get(place_code)
    if not baba_code:
        _ui_error(ui, "babaCode mapping が未定義です。place_code を確認してください。")
        return "⚠️ babaCode mapping が未定義です。place_code を確認してください。"

    result_blocks: list[str] = []

    driver = build_driver()
    wait = WebDriverWait(driver, 12)

    try:
        _ui_info(ui, "🔑 ログイン中...（競馬ブック）")
        login_keibabook(driver, wait)

        race_ids = fetch_race_ids_from_schedule(driver, year, month, day, place_code, ui=ui)
        if not race_ids:
            return "⚠️ レースIDが取得できませんでした。日付/競馬場コードを確認してください。"

        for i, race_id in enumerate(race_ids):
            race_num = i + 1
            if target_races is not None and race_num not in target_races:
                continue

            race_num_str = f"{race_num:02}"

            _ui_markdown(ui, f"## {place_name} {race_num}R")
            _ui_caption(ui, f"race_id(keibabook): {race_id}")

            try:
                # 0) keiba.go.jp 出馬表
                # ★修正: nar_race_level を受け取る
                header, keibago_dict, keibago_url, nar_race_level = fetch_keibago_debatable_small(
                    year=str(year),
                    month=str(month),
                    day=str(day),
                    race_no=race_num,
                    baba_code=str(baba_code),
                )
                _ui_caption(ui, f"keiba.go.jp: {keibago_url}")
                if header:
                    _ui_caption(ui, f"keiba.go.jp header: {header}")
                if nar_race_level:
                    _ui_caption(ui, f"Race Level(NAR): {nar_race_level}")

                if not keibago_dict:
                    _ui_warning(ui, "⚠️ keiba.go.jp から出馬表が取れませんでした（続行：騎手/調教師が不明になります）")

                # 1) 談話
                _ui_info(ui, "📡 データ収集中...（談話）")
                driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
                try:
                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "danwa")))
                except:
                    pass

                html_danwa = driver.page_source
                race_meta = parse_race_info(html_danwa)
                danwa_dict = parse_danwa_comments(html_danwa)

                # 2) 調教
                _ui_info(ui, "📡 データ収集中...（調教）")
                driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{race_id}")
                try:
                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "cyokyo")))
                except:
                    pass

                cyokyo_dict = parse_cyokyo(driver.page_source)

                # 統合
                all_uma = sorted(
                    set(danwa_dict.keys()) | set(cyokyo_dict.keys()) | set(keibago_dict.keys()),
                    key=lambda x: int(x) if str(x).isdigit() else 999,
                )

                merged_text = []
                for uma in all_uma:
                    kg = keibago_dict.get(uma, {})
                    horse = kg.get("horse", "")
                    jockey = kg.get("jockey", "不明")
                    trainer = kg.get("trainer", "不明")
                    prev_jockey = kg.get("prev_jockey", "")
                    is_change = kg.get("is_change", False)

                    alert = "【⚠️乗り替わり】" if is_change else ""
                    if prev_jockey:
                        alert += f"（前走:{prev_jockey}）"

                    d = danwa_dict.get(uma, "（なし）")
                    c = cyokyo_dict.get(uma, "（なし）")

                    merged_text.append(
                        f"▼[馬番{uma}] 馬名:{horse} 騎手:{jockey} {alert} 調教師:{trainer}\n"
                        f"談話: {d}\n"
                        f"調教: {c}"
                    )

                if not merged_text:
                    block = f"【{place_name} {race_num}R】\n⚠️ データなしのためスキップ"
                    result_blocks.append(block)
                    _ui_warning(ui, "データなしのためスキップ")
                    _ui_divider(ui)
                    time.sleep(5) # ★負荷軽減
                    continue

                # ★修正: プロンプトにNARから取得したレースレベルを追加
                prompt = (
                    f"{place_name}競馬場のレースのデータです。\n\n"
                    f"レース名: {race_meta.get('race_name','')}\n"
                    f"レースレベル: {nar_race_level}\n"
                    f"条件: {race_meta.get('cond','')}\n\n"
                    "以下の各馬のデータ（馬名、騎手、乗り替わり、調教師、談話、調教）です。\n"
                    + "\n".join(merged_text)
                )

                # 3) Dify
                _ui_info(ui, "🤖 AI分析中...（Dify）")

                if ui:
                    result_area = st.empty()
                    answer_buf = ""
                    got_error = False

                    # まず1回Streamingトライ
                    stream_generator = stream_dify_workflow(prompt)
                    for chunk in stream_generator:
                        if isinstance(chunk, str) and (chunk.startswith("⚠️ Dify HTTP") or "503" in chunk or "overloaded" in chunk):
                            got_error = True
                            break # エラーなら即中断してfallbackへ
                        
                        answer_buf += chunk
                        result_area.markdown(answer_buf + "▌")

                    if got_error or not answer_buf:
                        # エラーだった場合、run_dify_with_fallback（リトライ付き）に任せる
                        result_area.markdown("⚠️ AI混雑のため再試行中...")
                        full_ans = run_dify_with_fallback(prompt)
                        result_area.markdown(full_ans)
                    else:
                        full_ans = answer_buf
                else:
                    # UIなし
                    full_ans = run_dify_with_fallback(prompt)

                full_ans = (full_ans or "").strip()
                if full_ans == "":
                    full_ans = "⚠️ AIの出力が空でした（Dify応答なし/エラーの可能性）"

                _ui_success(ui, "✅ 完了")

                # ここで履歴保存などを行いたい場合は適宜コメントアウト解除など
                # save_history(year, place_code, place_name, month, day, race_num_str, race_id, full_ans)

                block = f"【{place_name} {race_num}R】\n{full_ans}"
                result_blocks.append(block)

            except Exception as e:
                msg = f"【{place_name} {race_num}R】\n⚠️ Error: {e}"
                result_blocks.append(msg)
                _ui_error(ui, f"Error: {e}")

            _ui_divider(ui)
            
            # ★負荷軽減：レースごとに少し待つ
            time.sleep(5)

    finally:
        try:
            driver.quit()
        except:
            pass

    return "\n\n".join(result_blocks).strip()

def run_races_iter(
    year: str,
    month: str,
    day: str,
    place_code: str,
    target_races: set[int] | None,
    ui: bool = False,
):
    place_names = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
    place_name = place_names.get(place_code, "地方")

    baba_map = {"10": "20", "11": "21", "12": "19", "13": "18"}
    baba_code = baba_map.get(place_code)
    if not baba_code:
        yield (0, "⚠️ babaCode mapping が未定義です。place_code を確認してください。")
        return

    driver = build_driver()
    wait = WebDriverWait(driver, 12)

    try:
        _ui_info(ui, "🔑 ログイン中...（競馬ブック）")
        login_keibabook(driver, wait)

        race_ids = fetch_race_ids_from_schedule(driver, year, month, day, place_code, ui=ui)
        if not race_ids:
            yield (0, "⚠️ レースIDが取得できませんでした。日付/競馬場コードを確認してください。")
            return

        for i, race_id in enumerate(race_ids):
            race_num = i + 1
            if target_races is not None and race_num not in target_races:
                continue

            race_num_str = f"{race_num:02}"

            _ui_markdown(ui, f"## {place_name} {race_num}R")
            _ui_caption(ui, f"race_id(keibabook): {race_id}")

            try:
                # ★修正: nar_race_level を受け取る
                header, keibago_dict, keibago_url, nar_race_level = fetch_keibago_debatable_small(
                    year=str(year),
                    month=str(month),
                    day=str(day),
                    race_no=race_num,
                    baba_code=str(baba_code),
                )
                _ui_caption(ui, f"keiba.go.jp: {keibago_url}")
                if header:
                    _ui_caption(ui, f"keiba.go.jp header: {header}")
                if nar_race_level:
                    _ui_caption(ui, f"Race Level(NAR): {nar_race_level}")

                if not keibago_dict:
                    _ui_warning(ui, "⚠️ keiba.go.jp から出馬表が取れませんでした")

                _ui_info(ui, "📡 データ収集中...（談話）")
                driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
                try:
                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "danwa")))
                except:
                    pass

                html_danwa = driver.page_source
                race_meta = parse_race_info(html_danwa)
                danwa_dict = parse_danwa_comments(html_danwa)

                _ui_info(ui, "📡 データ収集中...（調教）")
                driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{race_id}")
                try:
                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "cyokyo")))
                except:
                    pass

                cyokyo_dict = parse_cyokyo(driver.page_source)

                all_uma = sorted(
                    set(danwa_dict.keys()) | set(cyokyo_dict.keys()) | set(keibago_dict.keys()),
                    key=lambda x: int(x) if str(x).isdigit() else 999,
                )

                merged_text = []
                for uma in all_uma:
                    kg = keibago_dict.get(uma, {})
                    horse = kg.get("horse", "")
                    jockey = kg.get("jockey", "不明")
                    trainer = kg.get("trainer", "不明")
                    prev_jockey = kg.get("prev_jockey", "")
                    is_change = kg.get("is_change", False)

                    alert = "【⚠️乗り替わり】" if is_change else ""
                    if prev_jockey:
                        alert += f"（前走:{prev_jockey}）"

                    d = danwa_dict.get(uma, "（なし）")
                    c = cyokyo_dict.get(uma, "（なし）")

                    merged_text.append(
                        f"▼[馬番{uma}] 馬名:{horse} 騎手:{jockey} {alert} 調教師:{trainer}\n"
                        f"談話: {d}\n"
                        f"調教: {c}"
                    )

                if not merged_text:
                    block = f"【{place_name} {race_num}R】\n⚠️ データなしのためスキップ"
                    yield (race_num, block)
                    _ui_warning(ui, "データなしのためスキップ")
                    _ui_divider(ui)
                    time.sleep(5) # ★負荷軽減
                    continue

                # ★修正: プロンプトにNARから取得したレースレベルを追加
                prompt = (
                    f"レース名: {race_meta.get('race_name','')}\n"
                    f"レースレベル: {nar_race_level}\n"
                    f"条件: {race_meta.get('cond','')}\n\n"
                    "以下の各馬のデータ（馬名、騎手、乗り替わり、調教師、談話、調教）です。\n"
                    + "\n".join(merged_text)
                )

                _ui_info(ui, "🤖 AI分析中...（Dify）")
                
                # ここはUIがないイテレータ版なのでリトライロジック付きを呼ぶだけでOK
                full_ans = run_dify_with_fallback(prompt)

                full_ans = (full_ans or "").strip()
                if full_ans == "":
                    full_ans = "⚠️ AIの出力が空でした（Dify応答なし/エラーの可能性）"

                _ui_success(ui, "✅ 完了")

                # save_history(year, place_code, place_name, month, day, race_num_str, race_id, full_ans)

                block = f"【{place_name} {race_num}R】\n{full_ans}"
                yield (race_num, block)

            except Exception as e:
                block = f"【{place_name} {race_num}R】\n⚠️ Error: {e}"
                yield (race_num, block)
                _ui_error(ui, f"Error: {e}")

            _ui_divider(ui)
            
            # ★負荷軽減：レースごとに少し待つ
            time.sleep(5)

    finally:
        try:
            driver.quit()
        except:
            pass
