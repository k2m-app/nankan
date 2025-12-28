import time
import json
import re
import requests
import streamlit as st
from datetime import datetime
import pytz
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ==================================================
# 1. 設定・定数・Secrets読み込み
# ==================================================

# パスワード認証
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    if st.session_state["password_correct"]: return True
    
    st.title("🔒 ログイン")
    ADMIN_PASS = st.secrets.get("ADMIN_PASSWORD", "admin123")
    user_input = st.text_input("パスワードを入力してください", type="password")
    if st.button("ログイン"):
        if user_input == ADMIN_PASS:
            st.session_state["password_correct"] = True
            st.rerun()
        else: st.error("パスワードが違います")
    return False

if not check_password(): st.stop()

# Secrets
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")

# 場所名マップ
PLACE_NAMES = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}

# ==================================================
# 2. ヘルパー関数 (Supabase, Driver)
# ==================================================

@st.cache_resource
def get_supabase_client() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY: return None
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
        st.error(f"Supabase save error: {e}")

def get_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    return webdriver.Chrome(options=options)

# ==================================================
# 3. 競馬ブック スクレイピング関数
# ==================================================

def login_keibabook(driver):
    if not KEIBA_ID or not KEIBA_PASS:
        st.warning("⚠️ 競馬ブックのID/PASSが設定されていません。")
        return False
    try:
        driver.get("https://s.keibabook.co.jp/login/login")
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        time.sleep(1)
        return True
    except Exception as e:
        st.error(f"競馬ブック ログインエラー: {e}")
        return False

def fetch_race_ids_from_schedule(driver, year, month, day, target_place_code):
    date_str = f"{year}{month}{day}"
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}10" 
    
    st.info(f"📅 日程取得中: {url}")
    driver.get(url)
    time.sleep(1)
    
    soup = BeautifulSoup(driver.page_source, "html.parser")
    race_ids = []
    seen = set()
    
    for a in soup.find_all("a", href=True):
        href = a['href']
        # 例: /chihou/syutuba/2025191003011226
        match = re.search(r'(\d{16})', href)
        if match:
            rid = match.group(1)
            # IDの6-7文字目(場所コード)確認
            if rid[6:8] == target_place_code:
                if rid not in seen:
                    race_ids.append(rid)
                    seen.add(rid)
    race_ids.sort()
    return race_ids

def parse_race_info(html: str):
    """レース名・条件などを取得"""
    soup = BeautifulSoup(html, "html.parser")
    racetitle = soup.find("div", class_="racetitle")
    if not racetitle: return {}
    
    racemei = racetitle.find("div", class_="racemei")
    race_name = racemei.find_all("p")[1].get_text(strip=True) if racemei and len(racemei.find_all("p")) >= 2 else ""
    
    sub = racetitle.find("div", class_="racetitle_sub")
    cond = sub.find_all("p")[1].get_text(" ", strip=True) if sub and len(sub.find_all("p")) >= 2 else ""
    return {"race_name": race_name, "cond": cond}

def parse_danwa_comments(html: str):
    """
    談話（厩舎コメント）を取得。
    ※ここに「〇〇師」などの調教師名が含まれることが多い
    """
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

def parse_syutuba_jockey(html: str):
    """
    出馬表から「騎手」と「乗り替わり」を取得
    """
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    
    # スマートフォン版の各馬ブロック
    sections = soup.find_all("div", class_="section")
    
    for sec in sections:
        umaban_div = sec.find("div", class_="umaban")
        if not umaban_div: continue
        umaban = umaban_div.get_text(strip=True)
        
        kisyu_p = sec.find("p", class_="kisyu")
        jockey_name = "不明"
        is_change = False
        
        if kisyu_p:
            jockey_a = kisyu_p.find("a")
            if jockey_a:
                jockey_name = jockey_a.get_text(strip=True)
                # 乗り替わり判定(strong/b)
                if jockey_a.find("strong") or jockey_a.find("b"):
                    is_change = True
            else:
                jockey_name = kisyu_p.get_text(strip=True)
            
            # 親要素レベルでの強調チェック
            if kisyu_p.find("strong") or kisyu_p.find("b") or "red" in kisyu_p.get("class", []):
                is_change = True

        info[umaban] = {
            "jockey": jockey_name,
            "is_change": is_change
        }
            
    return info

def parse_cyokyo(html: str):
    """調教データを取得"""
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
# 4. Dify API連携
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
                        if "answer" in data:
                            yield data.get("answer", "")
                    except: pass
    except Exception as e:
        yield f"⚠️ API Error: {str(e)}"

# ==================================================
# 5. メイン画面・実行ロジック
# ==================================================

st.title("🏇 競馬ブック専用 AI分析Bot")
jst = pytz.timezone('Asia/Tokyo')
now = datetime.now(jst)

# 設定UI
with st.container():
    c1, c2 = st.columns(2)
    with c1: target_date = st.date_input("分析日", now)
    with c2: 
        PLACE_CODE = st.selectbox("開催場所", ["10", "11", "12", "13"], 
                                  format_func=lambda x: f"{x}: {PLACE_NAMES.get(x)}")
    
    st.write("### 🏁 レース選択")
    all_races = st.checkbox("全レースを一括分析する", value=True)
    target_races = []
    if not all_races:
        cols = st.columns(6)
        for i in range(1, 13):
            with cols[(i-1)//2]:
                if st.checkbox(f"{i}R", key=f"r{i}"): target_races.append(i)
    else:
        target_races = list(range(1, 13))

# 実行ボタン
if st.button("🚀 分析開始", type="primary"):
    date_str = target_date.strftime("%Y%m%d")
    year_str = target_date.strftime("%Y")
    month_str = target_date.strftime("%m")
    day_str = target_date.strftime("%d")
    place_name = PLACE_NAMES.get(PLACE_CODE, "不明")

    driver = get_driver()
    
    try:
        st.info("🔑 競馬ブックへログイン中...")
        login_keibabook(driver)
        
        st.info("📡 レースIDを取得中...")
        race_ids = fetch_race_ids_from_schedule(driver, year_str, month_str, day_str, PLACE_CODE)
        
        if not race_ids:
            st.error("レース情報が見つかりませんでした。休催日か場所コードを確認してください。")
        else:
            for race_id in race_ids:
                race_num = int(race_id[10:12])
                if target_races and race_num not in target_races:
                    continue
                
                st.markdown(f"### {place_name} {race_num}R")
                status_area = st.empty()
                result_area = st.empty()
                
                try:
                    status_area.info("📚 データを収集中...")
                    
                    # 1. 談話・レース情報
                    driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
                    html_danwa = driver.page_source
                    race_meta = parse_race_info(html_danwa)
                    danwa_dict = parse_danwa_comments(html_danwa)
                    
                    # 2. 出馬表 (騎手・乗り替わり)
                    driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{race_id}")
                    syutuba_info = parse_syutuba_jockey(driver.page_source)
                    
                    # 3. 調教
                    driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{race_id}")
                    cyokyo_dict = parse_cyokyo(driver.page_source)
                    
                    # 4. データ結合
                    merged_text = []
                    all_uma = sorted(list(set(list(syutuba_info.keys()) + list(danwa_dict.keys()))), 
                                     key=lambda x: int(x) if x.isdigit() else 999)
                    
                    for uma in all_uma:
                        # 騎手情報
                        s_info = syutuba_info.get(uma, {"jockey": "不明", "is_change": False})
                        jockey_name = s_info["jockey"]
                        is_change = s_info["is_change"]
                        alert = "【⚠️乗り替わり】" if is_change else ""
                        
                        # 談話 (ここに調教師名も含まれる想定)
                        danwa_txt = danwa_dict.get(uma, "（談話なし）")
                        
                        # 調教
                        cyokyo_txt = cyokyo_dict.get(uma, "（調教情報なし）")
                        
                        line = (
                            f"▼[馬番{uma}]\n"
                            f"  騎手: {jockey_name} {alert}\n"
                            f"  厩舎コメント: {danwa_txt}\n"
                            f"  調教: {cyokyo_txt}"
                        )
                        merged_text.append(line)

                    if not merged_text:
                        status_area.warning("データなし。スキップ")
                        continue

                    # 5. プロンプト作成
                    prompt = (
                        f"レース名: {race_meta.get('race_name','')}\n"
                        f"条件: {race_meta.get('cond','')}\n\n"
                        "以下の各馬のデータ（騎手、厩舎コメント、調教）です。\n"
                        + "\n".join(merged_text)
                    )
                    
                    # 6. AI分析
                    status_area.info("🤖 AI分析を実行中...")
                    full_ans = ""
                    for chunk in stream_dify_workflow(prompt):
                        full_ans += chunk
                        result_area.markdown(full_ans + "▌")
                    
                    result_area.markdown(full_ans)
                    status_area.success("分析完了")
                    
                    # 履歴保存
                    save_history(year_str, PLACE_CODE, place_name, month_str, day_str, f"{race_num:02}", race_id, full_ans)
                    
                except Exception as e:
                    status_area.error(f"エラー発生: {e}")
                
                st.divider()

    finally:
        driver.quit()
