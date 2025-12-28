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

# デフォルト変数
YEAR = "2025"
PLACE_CODE = "11"  # 10:大井, 11:川崎, 12:船橋, 13:浦和
MONTH = "12"
DAY = "29" # 提供されたHTMLに合わせて変更可能

def set_race_params(year, place_code, month, day):
    global YEAR, PLACE_CODE, MONTH, DAY
    YEAR = str(year)
    PLACE_CODE = str(place_code).zfill(2)
    MONTH = str(month).zfill(2)
    DAY = str(day).zfill(2)

# ==================================================
# Supabase & Helper
# ==================================================
@st.cache_resource
def get_supabase_client() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def save_history(year, place_code, place_name, month, day, race_num_str, race_id, ai_answer):
    supabase = get_supabase_client()
    if not supabase: return
    data = {
        "year": str(year),
        "place_code": str(place_code),
        "place_name": place_name,
        "day": str(day),
        "month": str(month),
        "race_num": race_num_str,
        "race_id": race_id,
        "output_text": ai_answer,
    }
    try:
        supabase.table("history").insert(data).execute()
    except Exception as e:
        print("Supabase insert error:", e)

# ==================================================
# スクレイピング関数群
# ==================================================

# 1. レースID一覧を取得
def fetch_race_ids_from_schedule(driver, year, month, day, target_place_code):
    date_str = f"{year}{month}{day}"
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}10"
    
    st.info(f"📅 日程ページからレースIDを取得中... ({url})")
    driver.get(url)
    time.sleep(1)
    
    soup = BeautifulSoup(driver.page_source, "html.parser")
    race_ids = []
    seen = set()
    
    for a in soup.find_all("a", href=True):
        href = a['href']
        match = re.search(r'(\d{16})', href)
        if match:
            rid = match.group(1)
            # IDの5-6桁目が競馬場コード
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

# 2. 騎手情報の取得（提供HTML構造に完全対応）
def parse_syutuba_jockey(html: str):
    soup = BeautifulSoup(html, "html.parser")
    jockey_info = {}
    
    # ターゲット: <table class="syutuba_sp">
    table = soup.find("table", class_="syutuba_sp")
    if not table:
        return {}

    # 行を取得 (tbodyがある場合とない場合に対応するため、tableから直接trを探す)
    rows = table.find_all("tr")
    
    for row in rows:
        tds = row.find_all("td")
        if not tds:
            continue
            
        # 1列目が馬番（数字）であるか確認
        umaban_text = tds[0].get_text(strip=True)
        if not umaban_text.isdigit():
            continue
        
        umaban = umaban_text

        # 騎手欄は <p class="kisyu">
        kisyu_p = row.find("p", class_="kisyu")
        
        if kisyu_p:
            name = ""
            is_change = False
            
            # 優先1: <a>タグを探す (HTMLではここに入っている)
            anchor = kisyu_p.find("a")
            
            if anchor:
                # <a>タグの中のテキストが騎手名
                name = anchor.get_text(strip=True)
                
                # 乗り替わり判定: <a>の中に<strong>があるか
                # 例: <a ...><strong>松崎正</strong></a>
                if anchor.find("strong"):
                    is_change = True
            
            # 優先2: <a>がない場合 (リンク切れ騎手など)
            else:
                # テキスト全体を取得: "牡3 ▲小野俊 53" のようになる
                full_text = kisyu_p.get_text(" ", strip=True)
                
                # <strong>があれば乗り替わり
                if kisyu_p.find("strong"):
                    is_change = True
                    name = kisyu_p.find("strong").get_text(strip=True)
                else:
                    # スペースで分割して、真ん中あたりを取得する簡易ロジック
                    # 例: ["牡3", "騎手名", "53"]
                    parts = full_text.split()
                    if len(parts) >= 2:
                        # 数字を含まないパーツを名前候補とする（簡易的）
                        for part in parts:
                            if not any(char.isdigit() for char in part) and part not in ["牡", "牝", "セン"]:
                                name = part
                                break
                        if not name:
                            name = parts[1] if len(parts) > 1 else full_text
                    else:
                        name = full_text

            if name:
                jockey_info[umaban] = {"name": name, "is_change": is_change}
            
    return jockey_info

# 3. その他情報のパース
def parse_race_info(html: str):
    soup = BeautifulSoup(html, "html.parser")
    racetitle = soup.find("div", class_="racetitle")
    if not racetitle: return {}
    
    racemei = racetitle.find("div", class_="racemei")
    race_name = racemei.find_all("p")[1].get_text(strip=True) if racemei and len(racemei.find_all("p")) >= 2 else ""
    
    sub = racetitle.find("div", class_="racetitle_sub")
    cond = sub.find_all("p")[1].get_text(" ", strip=True) if sub and len(sub.find_all("p")) >= 2 else ""
    
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
        if not tbody: continue
        rows = tbody.find_all("tr", recursive=False)
        if not rows: continue
        
        h_row = rows[0]
        uma_td = h_row.find("td", class_="umaban")
        name_td = h_row.find("td", class_="kbamei")
        if not uma_td or not name_td: continue
        
        umaban = uma_td.get_text(strip=True)
        bamei = name_td.get_text(" ", strip=True)
        
        tanpyo = h_row.find("td", class_="tanpyo").get_text(strip=True) if h_row.find("td", class_="tanpyo") else ""
        detail = rows[1].get_text(" ", strip=True) if len(rows) > 1 else ""
        
        cyokyo_dict[umaban] = f"【馬名】{bamei} 【短評】{tanpyo} 【詳細】{detail}"
    return cyokyo_dict

# ==================================================
# Dify 連携
# ==================================================
def stream_dify_workflow(full_text: str):
    if not DIFY_API_KEY:
        yield "⚠️ DIFY_API_KEY未設定"
        return
    
    payload = {"inputs": {"text": full_text}, "response_mode": "streaming", "user": "keiba-bot"}
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    
    try:
        res = requests.post("https://api.dify.ai/v1/workflows/run", headers=headers, json=payload, stream=True, timeout=300)
        for line in res.iter_lines():
            if line:
                decoded = line.decode('utf-8')
                if decoded.startswith("data:"):
                    try:
                        data = json.loads(decoded.replace("data: ", ""))
                        if data.get("event") == "workflow_finished":
                            out = data.get("data", {}).get("outputs", {})
                            yield "".join([v for v in out.values() if isinstance(v, str)])
                        elif "answer" in data:
                            yield data.get("answer", "")
                    except: pass
    except Exception as e:
        yield f"⚠️ API Error: {str(e)}"

# ==================================================
# メイン実行ロジック
# ==================================================
def run_all_races(target_races=None):
    place_names = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
    place_name = place_names.get(PLACE_CODE, "地方")

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)

    try:
        st.info("🔑 ログイン中...")
        driver.get("https://s.keibabook.co.jp/login/login")
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        time.sleep(1)

        race_ids = fetch_race_ids_from_schedule(driver, YEAR, MONTH, DAY, PLACE_CODE)
        
        if not race_ids:
            return

        for i, race_id in enumerate(race_ids):
            race_num = i + 1
            if target_races is not None and race_num not in target_races:
                continue

            race_num_str = f"{race_num:02}"
            st.markdown(f"### {place_name} {race_num}R (ID: {race_id})")
            status_area = st.empty()
            result_area = st.empty()
            
            try:
                status_area.info("📡 データ収集中...")
                
                # 談話ページ
                driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
                html_danwa = driver.page_source
                race_meta = parse_race_info(html_danwa)
                danwa_dict = parse_danwa_comments(html_danwa)
                
                # 出馬表ページ
                driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{race_id}")
                jockey_dict = parse_syutuba_jockey(driver.page_source)
                
                # 調教ページ
                driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{race_id}")
                cyokyo_dict = parse_cyokyo(driver.page_source)
                
                merged_text = []
                all_uma = sorted(list(set(list(danwa_dict.keys()) + list(cyokyo_dict.keys()) + list(jockey_dict.keys()))), 
                                 key=lambda x: int(x) if x.isdigit() else 99)
                
                for uma in all_uma:
                    j = jockey_dict.get(uma, {"name": "不明", "is_change": False})
                    d = danwa_dict.get(uma, "（なし）")
                    c = cyokyo_dict.get(uma, "（なし）")
                    
                    alert = "【⚠️乗り替わり】" if j["is_change"] else ""
                    merged_text.append(f"▼[馬番{uma}] {j['name']} {alert}\n 談話: {d}\n 調教: {c}")

                if not merged_text:
                    status_area.warning("データなしスキップ")
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
                status_area.success("完了")
                save_history(YEAR, PLACE_CODE, place_name, MONTH, DAY, race_num_str, race_id, full_ans)

            except Exception as e:
                status_area.error(f"Error: {e}")
                
            st.write("---")

    finally:
        driver.quit()
