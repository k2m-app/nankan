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
# 地方競馬公式（keiba.go.jp）：DebaTableSmall を堅牢パース
# ==================================================
_KEIBAGO_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

def _norm_name(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s)
    # 減量記号を除去（比較用）
    s = s.replace("▲", "").replace("△", "").replace("☆", "").replace("◇", "")
    return s.strip()

def fetch_keibago_debatable_small(year: str, month: str, day: str, race_no: int, baba_code: str):
    """
    DebaTableSmall から
      - レース見出し（ページ先頭）
      - {馬番: {horse, jockey, trainer, prev_jockey, is_change}}
    を返す

    乗り替わり判定：
      - ブロック内で最初に出現する「前走」の騎手名（例: '12/14 8人 ▲小野俊 53.0' の小野俊）
      - 現在騎手と不一致なら is_change=True
    """
    # YYYY/MM/DD を URLに入れる
    date_str = f"{year}/{str(month).zfill(2)}/{str(day).zfill(2)}"
    url = (
        "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/DebaTableSmall"
        f"?k_raceDate={requests.utils.quote(date_str)}&k_raceNo={race_no}&k_babaCode={baba_code}"
    )

    r = requests.get(url, headers=_KEIBAGO_UA, timeout=25)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # 見出し（先頭数行を連結）
    header = " / ".join(lines[:5])

    # 1行目の馬ブロック開始（例: "1 1 オルフェーヴル   牝 3"）
    start_re = re.compile(r"^(\d+)\s+(\d+)\s+(.+)$")

    # 前走行の騎手抽出（例: "12/14 8人 ▲小野俊 53.0"）
    prev_jockey_re = re.compile(r"^\d{1,2}/\d{1,2}\s+\d+人\s+([☆▲△◇]?\S+)\s+\d{1,2}\.\d$")

    horses = {}
    i = 0
    cur = None

    def _finalize(cur_obj):
        if not cur_obj:
            return
        umaban = cur_obj.get("umaban")
        if not umaban:
            return
        # 乗り替わり判定
        cj = _norm_name(cur_obj.get("jockey", ""))
        pj = _norm_name(cur_obj.get("prev_jockey", ""))
        is_change = bool(pj and cj and pj != cj)
        cur_obj["is_change"] = is_change
        horses[str(umaban)] = cur_obj

    while i < len(lines):
        ln = lines[i]

        m = start_re.match(ln)
        if m:
            # 新しい馬ブロック開始
            _finalize(cur)
            waku = m.group(1)
            umaban = m.group(2)

            cur = {
                "waku": waku,
                "umaban": umaban,
                "horse": "",
                "jockey": "",
                "trainer": "",
                "prev_jockey": "",
                "is_change": False,
            }

            # 次行：馬名
            if i + 1 < len(lines):
                cur["horse"] = lines[i + 1].strip()

            # 調教師は「馬主 生産牧場 調教師」が同一行に出ることが多い
            # 例: "池谷誠一   上水牧場 福田真"
            # → 行末の2〜4文字程度の日本語姓名を調教師として拾う（所属行は別にある）
            # ただし確実性のため、「（大井）」等の所属行の直前あたりも探す
            trainer = ""
            jockey = ""

            # ざっくりこのブロックの前半（次の start まで or 80行）を走査して
            #  - 「(所属) 斤量」の直後の行を騎手とみなす
            #  - 「(所属) 斤量」の直前の行を含む文脈で調教師を拾う
            scan_end = min(len(lines), i + 120)
            k = i
            while k < scan_end:
                ln2 = lines[k]

                # 騎手：斤量行（例: "(大井) 54.0" / "(大井)▲ 53.0"）の次行
                if re.search(r"^\（.*\）\s*[☆▲△◇]?\s*\d{1,2}\.\d$", ln2):
                    if k + 1 < len(lines):
                        jockey = lines[k + 1].strip()
                        cur["jockey"] = jockey

                # 調教師：調教師が含まれる行（馬主/生産者/調教師が並ぶ行）を拾う
                # 例: "... 上水牧場 福田真"
                # → 末尾トークンを調教師として採用
                if "牧場" in ln2 or "ファーム" in ln2 or "株式会社" in ln2 or "（有）" in ln2 or "（株）" in ln2:
                    parts = re.split(r"\s+", ln2)
                    if len(parts) >= 2:
                        cand = parts[-1].strip()
                        # 所属括弧が混じるケースは除外
                        if "（" not in cand and "）" not in cand and len(cand) <= 6:
                            trainer = cand
                            cur["trainer"] = trainer

                # 前走騎手：最初に見つかったものだけ採用
                if not cur["prev_jockey"]:
                    pm = prev_jockey_re.match(ln2)
                    if pm:
                        cur["prev_jockey"] = pm.group(1).strip()

                # 次の馬ブロック始まりで打ち切り
                if k > i and start_re.match(ln2):
                    break

                k += 1

            # 調教師がまだ空なら、所属行の「一つ前」を調教師候補として拾う保険
            if not cur["trainer"]:
                # 所属行 "(大井) 54.0" を見つけたら、その2つ前くらいに調教師がいることが多い
                for kk in range(i, min(len(lines), i + 80)):
                    if re.search(r"^\（.*\）\s*[☆▲△◇]?\s*\d{1,2}\.\d$", lines[kk]):
                        # 2つ前の行末
                        if kk - 2 >= 0:
                            ln_tr = lines[kk - 2]
                            parts = re.split(r"\s+", ln_tr)
                            if parts:
                                cand = parts[-1].strip()
                                if "（" not in cand and "）" not in cand and len(cand) <= 6:
                                    cur["trainer"] = cand
                        break

            i += 1
            continue

        # ブロック中の前走騎手（最初の1回だけ）
        if cur and not cur["prev_jockey"]:
            pm = prev_jockey_re.match(ln)
            if pm:
                cur["prev_jockey"] = pm.group(1).strip()

        i += 1

    _finalize(cur)
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

                    if jockey == "不明":
                        print(f"Warning: keiba.go.jp jockey not found for umaban={uma} race_num={race_num}")

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
