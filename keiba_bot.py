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
# 1. 設定エリア
# ==================================================

def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    if st.session_state["password_correct"]: return True
    
    st.title("🔒 ログイン")
    ADMIN_PASS = st.secrets.get("ADMIN_PASSWORD", "admin123")
    val = st.text_input("パスワード", type="password")
    if st.button("Login"):
        if val == ADMIN_PASS:
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

PLACE_NAMES = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}

# ==================================================
# 2. ヘルパー関数
# ==================================================

@st.cache_resource
def get_supabase_client() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY: return None
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def save_history(year, place_code, place_name, month, day, race_num_str, race_id, ai_answer):
    supabase = get_supabase_client()
    if not supabase: return
    data = {
        "year": str(year), "place_code": str(place_code), "place_name": place_name,
        "day": str(day), "month": str(month), "race_num": race_num_str,
        "race_id": race_id, "output_text": ai_answer
    }
    try:
        supabase.table("history").insert(data).execute()
    except Exception as e:
        st.error(f"Supabase error: {e}")

def get_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    return webdriver.Chrome(options=options)

def login_keibabook(driver):
    if not KEIBA_ID or not KEIBA_PASS:
        st.warning("⚠️ ID/PASS未設定")
        return False
    try:
        driver.get("https://s.keibabook.co.jp/login/login")
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        time.sleep(1)
        return True
    except: return False

def fetch_race_ids(driver, year, month, day, p_code):
    url = f"https://s.keibabook.co.jp/chihou/nittei/{year}{month}{day}10"
    st.info(f"📅 日程取得: {url}")
    driver.get(url)
    time.sleep(1)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    ids = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r'(\d{16})', a['href'])
        if m:
            rid = m.group(1)
            if rid[6:8] == p_code and rid not in seen:
                ids.append(rid)
                seen.add(rid)
    return sorted(ids)

def get_race_meta(html):
    soup = BeautifulSoup(html, "html.parser")
    rt = soup.find("div", class_="racetitle")
    if not rt: return {}
    rm = rt.find("div", class_="racemei")
    rname = rm.find_all("p")[1].get_text(strip=True) if rm and len(rm.find_all("p")) > 1 else ""
    rs = rt.find("div", class_="racetitle_sub")
    cond = rs.find_all("p")[1].get_text(" ", strip=True) if rs and len(rs.find_all("p")) > 1 else ""
    return {"name": rname, "cond": cond}

# ==================================================
# 3. データ取得ロジック (HTML解析)
# ==================================================

def parse_syutuba_page(html):
    """
    【出馬表ページ】解析
    提供されたHTMLに基づき、<table class="syutuba_sp"> を解析する
    """
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    # 提供されたHTMLにあるテーブルクラス
    table = soup.find("table", class_="syutuba_sp")
    
    if not table:
        return {}

    # tbody内の行を取得
    rows = table.find("tbody").find_all("tr")
    
    for row in rows:
        # 1. 馬番の取得 (最初のtd)
        tds = row.find_all("td")
        if not tds: continue
        
        # class="waku1" 等がついていることが多いが、位置で取得が確実
        umaban = tds[0].get_text(strip=True)
        if not umaban.isdigit(): continue # ヘッダー行などの除外

        # 2. 馬名と騎手の取得 (class="left" のtd内にある)
        info_td = row.find("td", class_="left")
        if not info_td: continue

        # 馬名 <p class="kbamei">
        kbamei_p = info_td.find("p", class_="kbamei")
        horse_name = kbamei_p.get_text(strip=True) if kbamei_p else "不明"

        # 騎手 <p class="kisyu">
        kisyu_p = info_td.find("p", class_="kisyu")
        jockey = "不明"
        is_change = False
        
        if kisyu_p:
            # <a>タグの中に騎手名がある
            a_tag = kisyu_p.find("a")
            if a_tag:
                jockey = a_tag.get_text(strip=True)
                # 乗り替わり判定: <a>の中に <strong> または <b> があるか
                if a_tag.find(["strong", "b"]):
                    is_change = True
            else:
                # リンクがない場合のバックアップ
                # "牡2 桑村真 55" のようになっているため、単純取得は危険だがとりあえず取得
                jockey = kisyu_p.get_text(strip=True)
            
            # 親タグレベルでの強調チェック
            if kisyu_p.find(["strong", "b"]):
                is_change = True

        data[umaban] = {
            "horse": horse_name,
            "jockey": jockey,
            "is_change": is_change
        }
            
    return data

def parse_danwa_page(html):
    """
    【談話ページ】解析
    調教師名はこのページのコメント本文から抽出する
    """
    soup = BeautifulSoup(html, "html.parser")
    data = {}
    table = soup.find("table", class_="danwa")
    if table and table.tbody:
        current_uma = None
        for row in table.tbody.find_all("tr"):
            u_td = row.find("td", class_="umaban")
            if u_td:
                current_uma = u_td.get_text(strip=True)
                continue
            txt_td = row.find("td", class_="danwa")
            if txt_td and current_uma:
                text = txt_td.get_text(strip=True)
                # コメント内の「〇〇師」を正規表現で探す
                trainer = "不明"
                m = re.search(r'(\S+師)', text)
                if m: trainer = m.group(1)
                
                data[current_uma] = {"comment": text, "trainer": trainer}
                current_uma = None
    return data

def parse_cyokyo_page(html):
    """
    【調教ページ】解析
    """
    soup = BeautifulSoup(html, "html.parser")
    data = {}
    tables = soup.find_all("table", class_="cyokyo")
    for tbl in tables:
        tbody = tbl.find("tbody")
        if not tbody: continue
        rows = tbody.find_all("tr", recursive=False)
        if len(rows) < 2: continue
        
        r1 = rows[0]
        u_td = r1.find("td", class_="umaban")
        if not u_td: continue
        umaban = u_td.get_text(strip=True)
        
        tanpyo_td = r1.find("td", class_="tanpyo")
        tanpyo = tanpyo_td.get_text(strip=True) if tanpyo_td else ""
        
        r2 = rows[1]
        detail = r2.get_text(" ", strip=True)
        
        data[umaban] = {"tanpyo": tanpyo, "time": detail}
        
    return data

# ==================================================
# 4. Dify API
# ==================================================

def stream_dify(text):
    if not DIFY_API_KEY:
        yield "⚠️ API Key未設定"
        return
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    payload = {"inputs": {"text": text}, "response_mode": "streaming", "user": "keiba-bot"}
    
    try:
        res = requests.post("https://api.dify.ai/v1/workflows/run", headers=headers, json=payload, stream=True, timeout=120)
        for line in res.iter_lines():
            if line:
                d = line.decode('utf-8')
                if d.startswith("data:"):
                    try:
                        j = json.loads(d.replace("data: ", ""))
                        if "answer" in j: yield j["answer"]
                    except: pass
    except Exception as e:
        yield f"Error: {e}"

# ==================================================
# 5. メインアプリ
# ==================================================

st.title("🏇 競馬ブック完全取得Bot")
jst = pytz.timezone('Asia/Tokyo')
now = datetime.now(jst)

with st.container():
    c1, c2 = st.columns(2)
    with c1: target_date = st.date_input("日付", now)
    with c2: PLACE_CODE = st.selectbox("場所", ["10","11","12","13"], format_func=lambda x: f"{x}:{PLACE_NAMES[x]}")
    
    all_races = st.checkbox("全レース", value=True)
    target_races = []
    if not all_races:
        cols = st.columns(6)
        for i in range(1, 13):
            with cols[(i-1)//2]:
                if st.checkbox(f"{i}R", key=f"r{i}"): target_races.append(i)
    else: target_races = list(range(1,13))

if st.button("🚀 分析開始", type="primary"):
    ymd = target_date.strftime("%Y%m%d")
    pname = PLACE_NAMES[PLACE_CODE]
    driver = get_driver()
    
    try:
        st.info("🔑 ログイン中...")
        if not login_keibabook(driver):
            st.stop()
            
        st.info("📡 レースID取得中...")
        rids = fetch_race_ids(driver, target_date.strftime("%Y"), target_date.strftime("%m"), target_date.strftime("%d"), PLACE_CODE)
        
        if not rids:
            st.error("レースが見つかりません")
        else:
            for rid in rids:
                rnum = int(rid[10:12])
                if target_races and rnum not in target_races: continue
                
                st.markdown(f"### {pname} {rnum}R")
                status = st.empty()
                res_area = st.empty()
                
                try:
                    status.info("📚 データ収集中 (出馬表/談話/調教)...")
                    
                    # 1. 出馬表 (騎手・乗り替わり・馬名)
                    driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{rid}")
                    syutuba_data = parse_syutuba_page(driver.page_source)
                    
                    # 2. 談話 (コメント・調教師)
                    driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{rid}")
                    danwa_html = driver.page_source
                    meta = get_race_meta(danwa_html)
                    danwa_data = parse_danwa_page(danwa_html)
                    
                    # 3. 調教 (タイム)
                    driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{rid}")
                    cyokyo_data = parse_cyokyo_page(driver.page_source)
                    
                    # 4. 結合
                    all_keys = set(syutuba_data.keys()) | set(danwa_data.keys()) | set(cyokyo_data.keys())
                    sorted_umas = sorted(list(all_keys), key=lambda x: int(x) if x.isdigit() else 999)
                    
                    lines = []
                    for u in sorted_umas:
                        s = syutuba_data.get(u, {})
                        d = danwa_data.get(u, {})
                        c = cyokyo_data.get(u, {})
                        
                        horse = s.get("horse", "不明")
                        jock = s.get("jockey", "不明")
                        change = "【⚠️乗り替わり】" if s.get("is_change") else ""
                        
                        comment = d.get("comment", "なし")
                        trainer = d.get("trainer", "不明") 
                        
                        cyokyo = f"{c.get('tanpyo','')} {c.get('time','')}"
                        
                        lines.append(
                            f"▼[馬番{u}] {horse}\n"
                            f"  騎手: {jock} {change}\n"
                            f"  調教師: {trainer}\n"
                            f"  厩舎の話: {comment}\n"
                            f"  調教: {cyokyo}"
                        )
                    
                    if not lines:
                        status.warning("データなし")
                        continue
                        
                    prompt = (
                        f"レース: {meta.get('name','')}\n条件: {meta.get('cond','')}\n\n"
                        "レース全出馬表データ(騎手,調教師,厩舎の話,調教)。\n"
                        "調教師名は「厩舎の話」に含まれている。\n"
                        + "\n".join(lines)
                    )
                    
                    status.info("🤖 AI分析中...")
                    ans = ""
                    for chunk in stream_dify(prompt):
                        ans += chunk
                        res_area.markdown(ans + "▌")
                    res_area.markdown(ans)
                    
                    save_history(target_date.year, PLACE_CODE, pname, target_date.month, target_date.day, f"{rnum:02}", rid, ans)
                    status.success("完了")
                    
                except Exception as e:
                    status.error(f"Error: {e}")
                
                st.divider()
                
    finally:
        driver.quit()
