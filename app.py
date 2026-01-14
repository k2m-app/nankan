import streamlit as st
import datetime
import keiba_bot

st.set_page_config(page_title="南関競馬AI予想くん", layout="wide")

st.title("🐎 南関競馬 AI予想生成 & 対戦表")

# --- 日本時間 (JST) の定義 ---
JST = datetime.timezone(datetime.timedelta(hours=9), 'JST')

# --- サイドバー設定 ---
with st.sidebar:
    st.header("開催設定")
    
    # 日本時間の今日の日付を取得
    today = datetime.datetime.now(JST).date()
    target_date = st.date_input("開催日", today)
    
    place_options = {"大井": "10", "川崎": "11", "船橋": "12", "浦和": "13"}
    selected_place = st.selectbox("競馬場", list(place_options.keys()))
    place_code = place_options[selected_place]
    
    st.divider()
    st.subheader("対象レース選択")
    
    # チェックボックスの状態管理用ロジック
    if "chk_1" not in st.session_state:
        default_races = [10, 11, 12] # デフォルトでチェックしたいレース
        for r in range(1, 13):
            st.session_state[f"chk_{r}"] = (r in default_races)

    # コールバック関数: 全選択・全解除用
    def update_all_races(is_selected):
        for r in range(1, 13):
            st.session_state[f"chk_{r}"] = is_selected

    # 結果保存用のステート初期化
    if "results_cache" not in st.session_state:
        st.session_state.results_cache = {}

    # 全選択/解除ボタン (on_clickコールバックを使用)
    col_a, col_c = st.columns(2)
    col_a.button("全選択", on_click=update_all_races, args=(True,))
    col_c.button("全解除", on_click=update_all_races, args=(False,))

    # チェックボックスグリッド
    selected_races_final = []
    cols = st.columns(3)
    for r in range(1, 13):
        with cols[(r-1)%3]:
            # keyを指定することでsession_stateと直接連動させる
            is_checked = st.checkbox(f"{r}R", key=f"chk_{r}")
            if is_checked:
                selected_races_final.append(r)
    
    st.caption("※Dify生成待機: 最大10分/レース")
    
    # ボタンにユニークキーを設定してリセット防止
    start_btn = st.button("予想開始", type="primary", key="btn_start")
    
    # キャッシュクリアボタン
    if st.button("結果クリア"):
        st.session_state.results_cache = {}
        st.rerun()

# --- メイン処理 ---
result_container = st.container()

# 1. 既に計算済みの結果があれば、個別の結果を表示
if st.session_state.results_cache:
    with result_container:
        st.success("📝 前回の生成結果を表示しています")
        for r_num, text in sorted(st.session_state.results_cache.items()):
            st.subheader(f"{selected_place} {r_num}R")
            st.text_area(
                label=f"{r_num}R 結果",
                value=text,
                height=500,
                key=f"res_cache_{r_num}"
            )
            st.divider()

# 2. ボタンが押されたら新規実行
if start_btn:
    if not selected_races_final:
        st.warning("レースを選択してください。")
        st.stop()

    # キャッシュをクリアして再実行
    st.session_state.results_cache = {}
    
    year = target_date.year
    month = f"{target_date.month:02}"
    day = f"{target_date.day:02}"
    
    st.info(f"🚀 {year}/{month}/{day} {selected_place}競馬 ({len(selected_races_final)}レース) の予想を開始します...")

    # ジェネレータ実行
    for race_num, output_text in keiba_bot.run_races_iter(year, month, day, place_code, set(selected_races_final), ui=True):
        
        if race_num == 0:
            st.error(output_text)
        else:
            # 結果をキャッシュに保存
            st.session_state.results_cache[race_num] = output_text
            
            with result_container:
                st.subheader(f"{selected_place} {race_num}R")
                st.text_area(
                    label=f"{race_num}R 結果",
                    value=output_text,
                    height=500,
                    key=f"res_new_{race_num}"
                )
                st.divider()

    st.success("✅ 全ての処理が完了しました！")

# 3. ★全レースの結果まとめ表示（コピー機能付き）
# キャッシュが存在する場合、常に最下部に「まとめ」を表示します
if st.session_state.results_cache:
    st.markdown("### 📋 全レース予想まとめ (一括コピー用)")
    st.caption("以下のボックスの右上にあるコピーアイコンをクリックすると、すべての結果を一括でコピーできます。")
    
    # 全レースのテキストを結合
    all_results_text = ""
    for r_num in sorted(st.session_state.results_cache.keys()):
        text = st.session_state.results_cache[r_num]
        all_results_text += f"{'='*30}\n【{selected_place} {r_num}R】\n{'='*30}\n{text}\n\n"
    
    # st.code を使うと、右上にコピーボタンが自動で付きます
    st.code(all_results_text, language="markdown")
