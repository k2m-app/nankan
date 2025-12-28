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
# スクレイピング：日程→レースID一覧
# ==================================================
def fetch_race_ids_from_schedule(driver, year, month, day, target_place_code):
    """
    日程ページから「指定競馬場コード」のレースID(16桁)を拾う
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

        # IDの7-8桁目（0-index 6:8）が「競馬場コード」になっている前提
        # 例: 2025191003011226 → rid[6:8] == "10"（大井）みたいな形
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
# 出馬表：騎手名 + 乗り替わり（強化版）
# ==================================================
def parse_syutuba_jockey(html: str) -> dict:
    """
    出馬表（table.syutuba_sp）から
    { "馬番": {"name": "騎手名", "is_change": bool} } を返す

    乗り替わり判定: p.kisyu 内に strong があれば True
    騎手名: 基本は p.kisyu a のテキスト。無い場合は p.kisyu のテキストから推定。
    """
    soup = BeautifulSoup(html, "html.parser")
    jockey_info = {}

    table = soup.select_one("table.syutuba_sp")
    if not table:
        return {}

    rows = table.select("tbody tr") or table.select("tr")

    for row in rows:
        # 馬番は waku1〜waku8 の td を優先して取る（列ズレに強い）
        umaban_td = row.find("td", class_=re.compile(r"^waku[1-8]$"))
        if umaban_td:
            umaban = umaban_td.get_text(strip=True)
        else:
            # フォールバック：先頭tdが数字なら馬番扱い
            tds = row.find_all("td")
            if not tds:
                continue
            cand = tds[0].get_text(strip=True)
            if not cand.isdigit():
                continue
            umaban = cand

        if not umaban.isdigit():
            continue

        kisyu_p = row.select_one("p.kisyu")
        if not kisyu_p:
            continue

        is_change = kisyu_p.select_one("strong") is not None

        a = kisyu_p.find("a")
        if a:
            name = a.get_text(strip=True)
        else:
            txt = kisyu_p.get_text(" ", strip=True)
            parts = [p for p in txt.split() if p]
            drop = {"牡", "牝", "セン", "☆", "▲", "△", "◇"}
            cand_parts = []
            for p in parts:
                if p in drop:
                    continue
                if any(ch.isdigit() for ch in p):
                    continue
                cand_parts.append(p)
            name = cand_parts[0] if cand_parts else ""

        if name:
            jockey_info[umaban] = {"name": name, "is_change": is_change}

    return jockey_info

# ==================================================
# その他パース
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

            # streaming中の途中テキスト
            if "answer" in data and isinstance(data["answer"], str):
                yield data["answer"]

            # workflow終了時のoutputs
            if data.get("event") == "workflow_finished":
                out = data.get("data", {}).get("outputs", {})
                texts = [v for v in out.values() if isinstance(v, str)]
                if texts:
                    yield "".join(texts)

    except Exception as e:
        yield f"⚠️ API Error: {str(e)}"

# ==================================================
# Selenium Driver
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
# メイン：全レース実行
# ==================================================
def run_all_races(year: str, month: str, day: str, place_code: str, target_races: set[int] | None):
    place_names = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
    place_name = place_names.get(place_code, "地方")

    driver = build_driver()
    wait = WebDriverWait(driver, 12)

    try:
        st.info("🔑 ログイン中...")
        login_keibabook(driver, wait)

        race_ids = fetch_race_ids_from_schedule(driver, year, month, day, place_code)
        if not race_ids:
            return

        # race_ids は「その競馬場だけ」拾っているので i+1 がレース番号として使える想定
        for i, race_id in enumerate(race_ids):
            race_num = i + 1
            if target_races is not None and race_num not in target_races:
                continue

            race_num_str = f"{race_num:02}"

            st.markdown(f"## {place_name} {race_num}R")
            st.caption(f"race_id: {race_id}")

            status_area = st.empty()
            result_area = st.empty()

            try:
                status_area.info("📡 データ収集中...")

                # --------------------------
                # 1) 談話
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
                # 2) 出馬表（騎手名 & 乗り替わり）
                #    ★ここが最重要：tbody tr まで待つ
                # --------------------------
                driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{race_id}")
                try:
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.syutuba_sp")))
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.syutuba_sp tbody tr")))
                    time.sleep(0.3)
                except:
                    st.warning("出馬表データの読み込みでタイムアウトしました（続行）。")

                html_syutuba = driver.page_source
                jockey_dict = parse_syutuba_jockey(html_syutuba)

                # --------------------------
                # 3) 調教
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
                    set(danwa_dict.keys()) | set(cyokyo_dict.keys()) | set(jockey_dict.keys()),
                    key=lambda x: int(x) if str(x).isdigit() else 999,
                )

                merged_text = []
                for uma in all_uma:
                    j = jockey_dict.get(uma, {"name": "不明", "is_change": False})
                    d = danwa_dict.get(uma, "（なし）")
                    c = cyokyo_dict.get(uma, "（なし）")
                    alert = "【⚠️乗り替わり】" if j["is_change"] else ""

                    if j["name"] == "不明":
                        # デバッグ用（Streamlit logs）
                        print(f"Warning: Jockey not found for umaban={uma} race_id={race_id}")

                    merged_text.append(
                        f"▼[馬番{uma}] 騎手:{j['name']} {alert}\n"
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
                    "以下の各馬のデータ（騎手、談話、調教）です。\n"
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
