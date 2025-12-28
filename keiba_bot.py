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
DAY = "04"

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

# 1. レースID一覧を取得（日程ページから）
def fetch_race_ids_from_schedule(driver, year, month, day, target_place_code):
    """
    指定された日付の日程ページ(nittei)から、対象競馬場(place_code)の全レースIDを取得する
    """
    date_str = f"{year}{month}{day}"
    # URL末尾の10は固定
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}10"
    
    st.info(f"📅 日程ページからレースIDを取得中... ({url})")
    driver.get(url)
    time.sleep(1)
    
    soup = BeautifulSoup(driver.page_source, "html.parser")
    race_ids = []
    
    # ページ内のリンクからレースIDパターン(16桁)を探す
    # IDの5-6桁目が place_code と一致するものだけを抽出
    
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a['href']
        match = re.search(r'(\d{16})', href)
        if match:
            rid = match.group(1)
            # IDの構造: YYYY(4) + PLACE(2) + ...
            # target_place_code (例: "11") が IDの 5,6文字目と一致するか確認
            if rid[6:8] == target_place_code:
                if rid not in seen:
                    race_ids.append(rid)
                    seen.add(rid)
    
    # レース番号順(IDの後ろの方にあるR番号でソート)
    race_ids.sort()
    
    if not race_ids:
        st.warning(f"⚠️ 指定した競馬場コード({target_place_code})のレースIDが見つかりませんでした。開催がない可能性があります。")
    else:
        st.success(f"✅ {len(race_ids)} 件のレースIDを取得しました。")
        
    return race_ids

# 2. 騎手情報の取得（修正済み：リンクなし対応版）
def parse_syutuba_jockey(html: str):
    soup = BeautifulSoup(html, "html.parser")
    jockey_info = {}
    
    # スマートフォン版(SP)の出馬表テーブルを特定
    table = soup.find("table", class_="syutuba_sp")
    if not table:
        return {}

    # tbodyの中身だけを見る
    tbody = table.find("tbody")
    if not tbody:
        return {}
        
    rows = tbody.find_all("tr")
    
    for row in rows:
        # 1. 馬番の取得
        tds = row.find_all("td")
        if not tds:
            continue
            
        # 1列目が馬番（テキストが数字かチェック）
        umaban_text = tds[0].get_text(strip=True)
        if not umaban_text.isdigit():
            continue
            
        umaban = umaban_text

        # 2. 騎手情報の取得
        # <td class="left"> の中にある <p class="kisyu"> を探す
        kisyu_p = row.find("p", class_="kisyu")
        
        if kisyu_p:
            name = ""
            is_change = False
            
            # アンカータグ(a)があるか確認
            anchor = kisyu_p.find("a")
            
            if anchor:
                # パターンA: リンクがある場合
                name = anchor.get_text(strip=True)
                # 乗り替わり判定 (aタグの中、またはpタグ直下にstrongがあるか)
                if anchor.find("strong") or kisyu_p.find("strong"):
                    is_change = True
            else:
                # パターンB: リンクがない場合（ここを修正）
                # pタグのテキストを直接取得
                name = kisyu_p.get_text(strip=True)
                # 乗り替わり判定
                if kisyu_p.find("strong"):
                    is_change = True
            
            # 名前が取得できていれば保存
            if name:
                jockey_info[umaban] = {"name": name, "is_change": is_change}
            
    return jockey_info

# 3. 談話・調教・レース情報のパース
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
    # 談話テーブルの構造に合わせて取得
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
        
        # 短評と詳細
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
        # 1. ログイン
        st.info("🔑 ログイン中...")
        driver.get("https://s.keibabook.co.jp/login/login")
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        time.sleep(1)

        # 2. 日程ページからIDリストを取得
        race_ids = fetch_race_ids_from_schedule(driver, YEAR, MONTH, DAY, PLACE_CODE)
        
        if not race_ids:
            return

        # 3. 各レースをループ処理
        for i, race_id in enumerate(race_ids):
            race_num = i + 1  # リスト順＝レース順と仮定
            
            # 指定されたレース以外はスキップ
            if target_races is not None and race_num not in target_races:
                continue

            race_num_str = f"{race_num:02}"
            
            st.markdown(f"### {place_name} {race_num}R (ID: {race_id})")
            status_area = st.empty()
            result_area = st.empty()
            
            try:
                status_area.info("📡 データ収集中...")
                
                # A. 談話ページ (ここからレース情報も取る)
                driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
                html_danwa = driver.page_source
                race_meta = parse_race_info(html_danwa)
                danwa_dict = parse_danwa_comments(html_danwa)
                
                # B. 出馬表ページ (騎手情報) - /1/無し注意
                driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{race_id}")
                jockey_dict = parse_syutuba_jockey(driver.page_source)
                
                # C. 調教ページ
                driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{race_id}")
                cyokyo_dict = parse_cyokyo(driver.page_source)
                
                # データ結合
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

                # プロンプト作成
                prompt = (
                    f"レース名: {race_meta.get('race_name','')}\n"
                    f"条件: {race_meta.get('cond','')}\n\n"
                    "以下は出走全頭のデータ（騎手、調教師、談話、調教）です。\n"
                    + "\n".join(merged_text)
                )
                
                # AI分析
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
