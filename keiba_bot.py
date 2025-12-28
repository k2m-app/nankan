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
from supabase import create_client, Client

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================================================
# 【設定】Secrets読み込み
# ==================================================
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")

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
# Supabase
# ==================================================
@st.cache_resource
def get_supabase_client() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    try:
        return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    except Exception as e:
        print("Supabase client error:", e)
        return None

def save_history(year, place_code, place_name, month, day, race_num_str, race_id, ai_answer):
    supabase = get_supabase_client()
    if not supabase:
        return
    data = {
        "year": str(year),
        "place_code": str(place_code),
        "place_name": place_name,
        "month": str(month).zfill(2),
        "day": str(day).zfill(2),
        "race_num": str(race_num_str),
        "race_id": str(race_id),
        "output_text": ai_answer,
    }
    try:
        supabase.table("history").insert(data).execute()
    except Exception as e:
        print("Supabase insert error:", e)

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
    """
    日程ページから「指定競馬場コード」のレースID(16桁)を拾う（競馬ブック）
    """
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

        # rid[6:8] が競馬場コード（競馬ブック側）
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
# 競馬ブック：レース情報/談話/調教（従来通り）
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
# 地方競馬公式（keiba.go.jp）：DOMで出馬表を「確実に」パース（差し替え版）
# ==================================================
_KEIBAGO_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

def _norm_name(s: str) -> str:
    s = (s or "").strip().replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s)
    # 減量記号を除去（比較用）
    s = s.replace("▲", "").replace("△", "").replace("☆", "").replace("◇", "")
    return s.strip()

def _build_requests_session() -> requests.Session:
    """
    一時的な 5xx/429 や瞬断に強くするため Retry を入れる
    """
    sess = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess

_WEIGHT_RE = re.compile(r"^[☆▲△◇]?\s*\d{1,2}\.\d$")       # 55.0 / ☆ 53.0 等
_PREV_JOCKEY_RE = re.compile(r"\d+人\s+([☆▲△◇]?\s*\S+)\s+\d{1,2}\.\d")  # "5人 ▲高橋優 52.0" 等

def _extract_jockey_from_cell(td) -> str:
    """
    負担重量・所属・成績が混ざるセルから騎手名だけ安定抽出
    """
    lines = [x.strip() for x in td.get_text("\n", strip=True).split("\n") if x.strip()]
    # 先頭の負担重量っぽい行を除去
    lines2 = [ln for ln in lines if not _WEIGHT_RE.match(ln)]
    if lines2:
        return lines2[0].replace(" ", "")
    return "不明"

def fetch_keibago_debatable_small(year: str, month: str, day: str, race_no: int, baba_code: str):
    """
    keiba.go.jp DebaTableSmall を堅牢に読む版（rowspan/列ズレ耐性あり）

    ポイント：
    - 「font.bamei」がある行＝馬行 として確実に拾う（ヘッダ行などを除外）
    - rowspan で枠セルが欠ける行（同枠の2頭目など）は、直前枠 last_waku を継承
    - 列位置を固定せず、「枠セルあり/なし」でオフセットを切り替える
    - requests Retry で瞬間的な 5xx/429 に耐える
    """
    date_str = f"{year}/{str(month).zfill(2)}/{str(day).zfill(2)}"
    url = (
        "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/DebaTableSmall"
        f"?k_raceDate={requests.utils.quote(date_str)}&k_raceNo={race_no}&k_babaCode={baba_code}"
    )

    sess = _build_requests_session()
    r = sess.get(url, headers=_KEIBAGO_UA, timeout=25)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    # ヘッダ（任意）
    header = ""
    top_bs = soup.select_one("table.bs")
    if top_bs:
        header = top_bs.get_text(" ", strip=True)

    # 出馬表テーブル本体
    main_table = soup.select_one("td.dbtbl table.bs[border='1']")
    if not main_table:
        main_table = soup.select_one("table.bs[border='1']")

    horses = {}
    last_waku = ""  # rowspan 欠け行の枠継承用

    if not main_table:
        return header, horses, url

    for tr in main_table.find_all("tr"):
        # 馬行判定：馬名が入る font.bamei がある行だけ
        if not tr.select_one("font.bamei"):
            continue

        tds = tr.find_all("td", recursive=False)
        if len(tds) < 8:
            continue

        # 枠セルあり/なし判定（rowspan の2頭目は枠セルが無い）
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

        # 馬名
        bamei_tag = horse_td.select_one("font.bamei b")
        horse = bamei_tag.get_text(strip=True) if bamei_tag else horse_td.get_text(" ", strip=True)

        # 調教師（所属括弧は落とす）
        trainer_raw = trainer_td.get_text(" ", strip=True)
        trainer = trainer_raw.split("（")[0].strip() if trainer_raw else "不明"

        # 騎手
        jockey = _extract_jockey_from_cell(jockey_td)

        # 前走騎手（前走欄から抽出）
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

    return header, horses, url

# ==================================================
# Dify（streaming）
# ==================================================
def stream_dify_workflow(full_text: str):
    if not DIFY_API_KEY:
        yield "⚠️ DIFY_API_KEY未設定"
        return

    payload = {
        "inputs": {"text": full_text},
        "response_mode": "streaming",
        "user": "keiba-bot",
    }
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        res = requests.post(
            "https://api.dify.ai/v1/workflows/run",
            headers=headers,
            json=payload,
            stream=True,
            timeout=300,
        )

        for line in res.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8", errors="ignore")
            if not decoded.startswith("data:"):
                continue

            raw = decoded.replace("data: ", "").strip()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except:
                continue

            if "answer" in data and isinstance(data["answer"], str):
                yield data["answer"]

            if data.get("event") == "workflow_finished":
                out = data.get("data", {}).get("outputs", {})
                texts = [v for v in out.values() if isinstance(v, str)]
                if texts:
                    yield "".join(texts)

    except Exception as e:
        yield f"⚠️ API Error: {str(e)}"

# ==================================================
# メイン：全レース実行（★ここが重要：文字列を return）
# ==================================================
def run_all_races(
    year: str,
    month: str,
    day: str,
    place_code: str,
    target_races: set[int] | None,
    ui: bool = False,
) -> str:
    """
    place_code：競馬ブック側（10大井/11川崎/12船橋/13浦和）
    騎手・調教師は keiba.go.jp の babaCode を使う

    ui=False（デフォルト）: 画面描画せず、結果文字列だけ返す（app.py向け）
    ui=True              : 従来通り st.* で進捗を出す（単体実行向け）
    """
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
                # --------------------------
                # 0) keiba.go.jp 出馬表（騎手・調教師・前走騎手）
                # --------------------------
                header, keibago_dict, keibago_url = fetch_keibago_debatable_small(
                    year=str(year),
                    month=str(month),
                    day=str(day),
                    race_no=race_num,
                    baba_code=str(baba_code),
                )
                _ui_caption(ui, f"keiba.go.jp: {keibago_url}")
                if header:
                    _ui_caption(ui, f"keiba.go.jp header: {header}")

                if not keibago_dict:
                    _ui_warning(ui, "⚠️ keiba.go.jp から出馬表が取れませんでした（続行しますが騎手/調教師が不明になります）")

                # --------------------------
                # 1) 談話（競馬ブック）
                # --------------------------
                _ui_info(ui, "📡 データ収集中...（談話）")
                driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
                try:
                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "danwa")))
                except:
                    pass

                html_danwa = driver.page_source
                race_meta = parse_race_info(html_danwa)
                danwa_dict = parse_danwa_comments(html_danwa)

                # --------------------------
                # 2) 調教（競馬ブック）
                # --------------------------
                _ui_info(ui, "📡 データ収集中...（調教）")
                driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{race_id}")
                try:
                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "cyokyo")))
                except:
                    pass

                cyokyo_dict = parse_cyokyo(driver.page_source)

                # --------------------------
                # 統合（馬番で揃える）
                # --------------------------
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
                    continue

                prompt = (
                    f"レース名: {race_meta.get('race_name','')}\n"
                    f"条件: {race_meta.get('cond','')}\n\n"
                    "以下の各馬のデータ（馬名、騎手、乗り替わり、調教師、談話、調教）です。\n"
                    + "\n".join(merged_text)
                )

                # --------------------------
                # 3) Dify で分析
                # --------------------------
                _ui_info(ui, "🤖 AI分析中...")
                full_ans = ""

                if ui:
                    result_area = st.empty()
                    for chunk in stream_dify_workflow(prompt):
                        full_ans += chunk
                        result_area.markdown(full_ans + "▌")
                    result_area.markdown(full_ans)
                else:
                    for chunk in stream_dify_workflow(prompt):
                        full_ans += chunk

                full_ans = (full_ans or "").strip()
                if full_ans == "":
                    full_ans = "⚠️ AIの出力が空でした（Dify応答なし/エラーの可能性）"

                _ui_success(ui, "✅ 完了")

                save_history(year, place_code, place_name, month, day, race_num_str, race_id, full_ans)

                block = f"【{place_name} {race_num}R】\n{full_ans}"
                result_blocks.append(block)

            except Exception as e:
                msg = f"【{place_name} {race_num}R】\n⚠️ Error: {e}"
                result_blocks.append(msg)
                _ui_error(ui, f"Error: {e}")

            _ui_divider(ui)

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
    """
    1レース処理が完了するたびに (race_num:int, block_text:str) を yield する。
    app.py 側で逐次表示する用途。
    """
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
                # 0) keiba.go.jp 出馬表
                header, keibago_dict, keibago_url = fetch_keibago_debatable_small(
                    year=str(year),
                    month=str(month),
                    day=str(day),
                    race_no=race_num,
                    baba_code=str(baba_code),
                )
                _ui_caption(ui, f"keiba.go.jp: {keibago_url}")
                if header:
                    _ui_caption(ui, f"keiba.go.jp header: {header}")

                if not keibago_dict:
                    _ui_warning(ui, "⚠️ keiba.go.jp から出馬表が取れませんでした（続行しますが騎手/調教師が不明になります）")

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

                # 統合（馬番で揃える）
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
                    continue

                prompt = (
                    f"レース名: {race_meta.get('race_name','')}\n"
                    f"条件: {race_meta.get('cond','')}\n\n"
                    "以下の各馬のデータ（馬名、騎手、乗り替わり、調教師、談話、調教）です。\n"
                    + "\n".join(merged_text)
                )

                # 3) Dify
                _ui_info(ui, "🤖 AI分析中...")
                full_ans = ""
                for chunk in stream_dify_workflow(prompt):
                    full_ans += chunk

                full_ans = (full_ans or "").strip()
                if full_ans == "":
                    full_ans = "⚠️ AIの出力が空でした（Dify応答なし/エラーの可能性）"

                _ui_success(ui, "✅ 完了")

                save_history(year, place_code, place_name, month, day, race_num_str, race_id, full_ans)

                block = f"【{place_name} {race_num}R】\n{full_ans}"
                yield (race_num, block)

            except Exception as e:
                block = f"【{place_name} {race_num}R】\n⚠️ Error: {e}"
                yield (race_num, block)
                _ui_error(ui, f"Error: {e}")

            _ui_divider(ui)

    finally:
        try:
            driver.quit()
        except:
            pass
