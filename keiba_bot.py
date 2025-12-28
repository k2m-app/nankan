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

# ==================================================
# 【設定】Secrets読み込み
# ==================================================
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")

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
def fetch_race_ids_from_schedule(driver, year, month, day, target_place_code):
    """
    日程ページから「指定競馬場コード」のレースID(16桁)を拾う（競馬ブック）
    """
    date_str = f"{year}{month}{day}"
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}10"

    st.info(f"📅 日程ページからレースIDを取得中... ({url})")
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
        st.warning(f"⚠️ 指定した競馬場コード({target_place_code})のレースIDが見つかりませんでした。")
    else:
        st.success(f"✅ {len(race_ids)} 件のレースIDを取得しました。")
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
# 地方競馬公式（keiba.go.jp）：DOMで出馬表を正確にパース（ここが大改修）
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

def fetch_keibago_debatable_small(year: str, month: str, day: str, race_no: int, baba_code: str):
    """
    keiba.go.jp DebaTableSmall を「表の列」で堅牢に読む版

    返り値:
      header: str
      horses: { "馬番": {horse, jockey, trainer, prev_jockey, is_change, waku, umaban} }
      url: str

    前走騎手:
      「前走」列のテキスト内にある  "◯人　(騎手名) 55.0" から抽出
    """
    date_str = f"{year}/{str(month).zfill(2)}/{str(day).zfill(2)}"
    url = (
        "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/DebaTableSmall"
        f"?k_raceDate={requests.utils.quote(date_str)}&k_raceNo={race_no}&k_babaCode={baba_code}"
    )

    r = requests.get(url, headers=_KEIBAGO_UA, timeout=25)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    # --- ヘッダー（上の開催情報） ---
    header = ""
    # 先頭の「2025年12月26日（金） 大井 第1競走 ...」が入る table.bs の1行目を拾う
    top_bs = soup.select_one("table.bs")
    if top_bs:
        header = top_bs.get_text(" ", strip=True)

    # --- 出馬表の「枠/馬番/馬名/調教師/騎手/前走...」が載る本体表 ---
    # class="dbtbl" の中に table(border=1) があり、その中の tr が馬ごとの行
    main_table = soup.select_one("td.dbtbl table.bs")
    if not main_table:
        # HTMLが少し違う場合の保険
        main_table = soup.select_one("table.bs[border='1']")

    horses = {}

    # 前走欄から「前走騎手」を拾う
    # 例: "2/6　5人　桑村真 55.0" → 桑村真
    prev_jockey_re = re.compile(r"\d+人\s+([☆▲△◇]?\S+)\s+\d{1,2}\.\d")

    if main_table:
        for tr in main_table.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 6:
                continue

            # 先頭2列が「枠」「馬番」になっている “馬の行” だけ処理する
            waku = tds[0].get_text(strip=True)
            umaban = tds[1].get_text(strip=True)

            if not (waku.isdigit() and umaban.isdigit()):
                continue

            # 馬名（font.bamei > b の中）
            horse = ""
            bamei_tag = tds[2].select_one("font.bamei b")
            if bamei_tag:
                horse = bamei_tag.get_text(strip=True)
            else:
                # 保険：td内テキストから推定（馬名が<b>になってないケース）
                horse = tds[2].get_text(" ", strip=True)

            # 調教師：td[3] 例: "月岡健（大井）" → 月岡健
            trainer_raw = tds[3].get_text(" ", strip=True)
            trainer = trainer_raw.split("（")[0].strip()

            # 騎手：td[4] は "55.0<br>桑村真<br>（大井）..." のように改行区切り
            jockey_lines = [x.strip() for x in tds[4].get_text("\n", strip=True).split("\n") if x.strip()]
            jockey = "不明"
            # だいたい [斤量, 騎手名, (所属), 成績] の順なので2番目が騎手名になりやすい
            if len(jockey_lines) >= 2:
                jockey = jockey_lines[1]

            # 前走騎手：前走列（通常は td[8]）から抽出
            prev_jockey = ""
            # 列構成が固定（枠/馬番/馬名/調教師/騎手/馬体重/変更/着別/前走/前々走/3走前/4走前）
            if len(tds) >= 9:
                zenso_txt = tds[8].get_text(" ", strip=True)
                m = prev_jockey_re.search(zenso_txt)
                if m:
                    prev_jockey = m.group(1).strip()

            # 乗り替わり判定（前走騎手が取れていて、現在騎手と違う）
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
# メイン：全レース実行
# ==================================================
def run_all_races(year: str, month: str, day: str, place_code: str, target_races: set[int] | None):
    """
    place_code：競馬ブック側（10大井/11川崎/12船橋/13浦和）
    騎手・調教師は keiba.go.jp の babaCode を使う
    """
    place_names = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
    place_name = place_names.get(place_code, "地方")

    # keiba.go.jp 側の babaCode
    baba_map = {"10": "20", "11": "21", "12": "19", "13": "18"}
    baba_code = baba_map.get(place_code)
    if not baba_code:
        st.error("babaCode mapping が未定義です。place_code を確認してください。")
        return

    driver = build_driver()
    wait = WebDriverWait(driver, 12)

    try:
        st.info("🔑 ログイン中...（競馬ブック）")
        login_keibabook(driver, wait)

        race_ids = fetch_race_ids_from_schedule(driver, year, month, day, place_code)
        if not race_ids:
            return

        for i, race_id in enumerate(race_ids):
            race_num = i + 1
            if target_races is not None and race_num not in target_races:
                continue

            race_num_str = f"{race_num:02}"

            st.markdown(f"## {place_name} {race_num}R")
            st.caption(f"race_id(keibabook): {race_id}")

            status_area = st.empty()
            result_area = st.empty()

            try:
                status_area.info("📡 データ収集中...")

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
                st.caption(f"keiba.go.jp: {keibago_url}")
                if header:
                    st.caption(f"keiba.go.jp header: {header}")

                if not keibago_dict:
                    st.warning("⚠️ keiba.go.jp から出馬表が取れませんでした（続行しますが騎手/調教師が不明になります）")

                # --------------------------
                # 1) 談話（競馬ブック）
                # --------------------------
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
                    status_area.warning("データなしのためスキップ")
                    st.divider()
                    continue

                prompt = (
                    f"レース名: {race_meta.get('race_name','')}\n"
                    f"条件: {race_meta.get('cond','')}\n\n"
                    "以下の各馬のデータ（馬名、騎手、乗り替わり、調教師、談話、調教）です。\n"
                    + "\n".join(merged_text)
                )

                status_area.info("🤖 AI分析中...")
                full_ans = ""
                for chunk in stream_dify_workflow(prompt):
                    full_ans += chunk
                    result_area.markdown(full_ans + "▌")

                result_area.markdown(full_ans)
                status_area.success("✅ 完了")

                save_history(year, place_code, place_name, month, day, race_num_str, race_id, full_ans)

            except Exception as e:
                status_area.error(f"Error: {e}")

            st.divider()

    finally:
        driver.quit()
