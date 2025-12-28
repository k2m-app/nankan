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
        "race_id": str(race_id),  # 競馬ブックのrace_idを保存
        "output_text": ai_answer,
    }
    try:
        supabase.table("history").insert(data).execute()
    except Exception as e:
        print("Supabase insert error:", e)


# ==================================================
# 競馬ブック：日程→レースID一覧
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
# 競馬ブック：レース情報 / 談話 / 調教
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
# netkeiba（NAR）：騎手＋調教師＋「替」
# ==================================================
def build_netkeiba_nar_race_id(year: str, month: str, day: str, keibabook_place_code: str, race_num: int) -> str:
    """
    netkeiba NAR race_id = YYYY + 場コード(42/43/44/45) + MMDD + RR(2桁)

    keibabook_place_code:
      10:大井 11:川崎 12:船橋 13:浦和
    netkeiba NAR:
      44:大井 45:川崎 43:船橋 42:浦和
    """
    place_map = {"10": "44", "11": "45", "12": "43", "13": "42"}
    nar_place = place_map.get(str(keibabook_place_code), "")
    if not nar_place:
        raise ValueError(f"Unsupported place_code for netkeiba mapping: {keibabook_place_code}")

    mmdd = f"{int(month):02}{int(day):02}"
    rr = f"{int(race_num):02}"
    return f"{year}{nar_place}{mmdd}{rr}"


def fetch_netkeiba_jockey_trainer(nar_race_id: str) -> dict:
    """
    nar.netkeiba.com の競馬新聞から
    {馬番: {"jockey": str, "trainer": str, "is_change": bool}} を返す
    """
    url = f"https://nar.netkeiba.com/race/newspaper.html?race_id={nar_race_id}"

    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    # テーブル候補（netkeibaは変更があり得るので複数で保険）
    table = (
        soup.select_one("table.RaceTable01") or
        soup.select_one("table#race_table_01") or
        soup.select_one("table.race_table_01") or
        soup.select_one("table")  # 最終保険
    )
    if not table:
        return {}

    out = {}
    rows = table.select("tbody tr")
    for row in rows:
        # 馬番：行内で 1-18 の数字だけを探す（最初に見つかったもの）
        umaban = ""
        for td in row.find_all("td"):
            t = td.get_text(strip=True)
            if t.isdigit() and 1 <= int(t) <= 18:
                umaban = t
                break
        if not umaban:
            continue

        # 騎手 / 調教師：classがある場合は優先
        jockey_td = row.select_one("td.Jockey") or row.select_one("td.jockey") or row.select_one("td:nth-of-type(7)")
        trainer_td = row.select_one("td.Trainer") or row.select_one("td.trainer") or row.select_one("td:nth-of-type(8)")

        jockey_raw = jockey_td.get_text(" ", strip=True) if jockey_td else ""
        trainer_raw = trainer_td.get_text(" ", strip=True) if trainer_td else ""

        # 「替」判定
        is_change = "替" in jockey_raw
        jockey = jockey_raw.replace("替", "").strip()

        # 余計な記号除去（念のため）
        jockey = re.sub(r"[☆▲△◇\s]", "", jockey).strip()
        trainer = re.sub(r"\s+", " ", trainer_raw).strip()

        if jockey or trainer:
            out[umaban] = {"jockey": jockey or "不明", "trainer": trainer or "不明", "is_change": is_change}

    return out


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
            st.caption(f"keibabook race_id: {race_id}")

            status_area = st.empty()
            result_area = st.empty()

            try:
                status_area.info("📡 データ収集中...")

                # --------------------------
                # 0) netkeiba（騎手・調教師）
                # --------------------------
                nar_race_id = build_netkeiba_nar_race_id(year, month, day, place_code, race_num)
                st.caption(f"netkeiba nar_race_id: {nar_race_id}")

                netkeiba_dict = {}
                try:
                    netkeiba_dict = fetch_netkeiba_jockey_trainer(nar_race_id)
                    if not netkeiba_dict:
                        st.warning("⚠️ netkeibaから騎手/調教師が取得できませんでした（続行）。")
                except Exception as e:
                    st.warning(f"⚠️ netkeiba取得エラー: {e}（続行）")

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
                    set(danwa_dict.keys()) | set(cyokyo_dict.keys()) | set(netkeiba_dict.keys()),
                    key=lambda x: int(x) if str(x).isdigit() else 999,
                )

                merged_text = []
                for uma in all_uma:
                    n = netkeiba_dict.get(uma, {"jockey": "不明", "trainer": "不明", "is_change": False})
                    jockey = n.get("jockey", "不明")
                    trainer = n.get("trainer", "不明")
                    is_change = bool(n.get("is_change", False))
                    alert = "【⚠️乗り替わり】" if is_change else ""

                    d = danwa_dict.get(uma, "（なし）")
                    c = cyokyo_dict.get(uma, "（なし）")

                    merged_text.append(
                        f"▼[馬番{uma}] 騎手:{jockey} {alert} / 調教師:{trainer}\n"
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
                    "以下の各馬のデータ（騎手、調教師、談話、調教）です。\n"
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
