import streamlit as st
from duckduckgo_search import DDGS
from openai import OpenAI
import trafilatura
import requests
import concurrent.futures
import sys
import time

# --- 1. 文字コード設定 ---
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except:
        pass

# --- 2. ページ設定 ---
st.set_page_config(page_title="My Perplexity V2", page_icon="🤖", layout="wide")
st.title("🤖 My Perplexity V2 (Chat & Switch)")

# --- 3. セッション状態の初期化 (履歴保持用) ---
if "messages" not in st.session_state:
    st.session_state.messages = []

# --- 4. サイドバー設定 ---
with st.sidebar:
    st.header("⚙️ 設定")
    
    # APIキー管理 (Secrets対応 - エラー回避版)
    api_key = ""
    try:
        # st.secrets アクセス時にファイルがないとエラーになるため try-except で囲む
        if "OPENAI_API_KEY" in st.secrets:
            api_key = st.secrets["OPENAI_API_KEY"]
    except FileNotFoundError:
        pass # ローカルでファイルがない場合は無視
    except Exception:
        pass # その他のエラーも無視

    # Secretsから取れなかった場合のみ入力欄を表示
    if not api_key:
        api_key = st.text_input("OpenAI API Key", type="password")

    st.markdown("---")
    
    # ★ モード切替スイッチ ★
    mode = st.radio(
        "動作モード",
        ["🚀 爆速 (単発)", "💬 会話 (文脈)"],
        index=0,
        help="【爆速】履歴を無視して最速で検索します。\n【会話】「それは高い？」など文脈を踏まえて検索します。"
    )
    
    model_name = "gpt-5-nano-2025-08-07" # または gpt-4o-mini
    target_count = st.slider("検索件数", 5, 20, 8)
    
    # 履歴クリアボタン
    if st.button("🗑️ 会話をクリア"):
        st.session_state.messages = []
        st.rerun()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

# --- 5. ロジック関数群 ---

def generate_search_keywords(query, client, mode, history):
    """
    検索キーワードを生成する。
    【会話モード】の場合は、履歴(history)を加味して検索ワードを考える。
    """
    if mode == "🚀 爆速 (単発)":
        # Pythonのみで爆速生成
        keywords = [query]
        keywords.append(f"{query} とは")
        keywords.append(f"{query} news")
        return list(dict.fromkeys(keywords))[:3]
    
    else:
        # 会話モード: 文脈を理解して検索ワードを作る (ここが少し重くなる要因)
        # 直近3ラリー分くらいの履歴を渡す
        recent_history = history[-6:] 
        
        prompt = f"""
        これまでの会話履歴を踏まえて、ユーザーの最新の質問「{query}」を調査するための検索キーワードを3つ生成してください。
        
        【会話履歴】
        {recent_history}
        
        出力はキーワードのみを改行区切りで。
        例: ユーザーが「それはいくら？」と聞いたら -> "iPhone 16 pro 価格" のように具体化すること。
        """
        
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content.strip().split("\n")
        except:
            return [query]

def fetch_worker(url, title, snippet):
    """ハイブリッド取得 (2秒タイムアウト)"""
    data = {'title': title, 'url': url, 'content': "", 'type': "waiting"}
    try:
        response = requests.get(url, headers=HEADERS, timeout=2.0)
        if response.status_code == 200:
            if response.encoding is None or response.encoding == 'ISO-8859-1':
                response.encoding = response.apparent_encoding
            content = trafilatura.extract(response.text, include_comments=False)
            if content and len(content) > 200:
                data['content'] = content[:1000]
                data['type'] = "full"
                return data
    except:
        pass
    
    # 失敗時はスニペット
    if snippet and len(snippet) > 30:
        data['content'] = snippet
        data['type'] = "snippet"
        return data
    return None

# --- 6. メインチャット処理 ---

# 1. 過去の会話を表示
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        # 参照ソースがあれば表示
        if "sources" in msg:
            with st.expander("📚 参照ソース"):
                for src in msg["sources"]:
                    st.markdown(f"- [{src['title']}]({src['url']})")

# 2. チャット入力時の処理
if query := st.chat_input("何について調べますか？"):
    
    # APIキーが空ならストップ
    if not api_key:
        st.error("サイドバーでAPIキーを設定してください。")
        st.stop()
        
    client = OpenAI(api_key=api_key)

    # ユーザーの入力を表示＆保存
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    # AIの回答処理
    with st.chat_message("assistant"):
        
        # --- A. 検索戦略フェーズ ---
        status_container = st.status("🚀 リサーチを開始...", expanded=True)
        
        with status_container:
            st.write("検索キーワードを生成中...")
            
            # モードに応じたキーワード生成
            keywords = generate_search_keywords(
                query, 
                client, 
                mode, 
                st.session_state.messages[:-1] # 今回の質問を除く履歴
            )
            st.caption(f"Keywords: {keywords}")
            
            st.write("Webを検索中...")
            candidates = []
            seen_urls = set()
            
            with DDGS() as ddgs:
                for q in keywords:
                    try:
                        region = 'wt-wt' if 'news' in q else 'jp-jp'
                        results = list(ddgs.text(q, region=region, max_results=5))
                        for res in results:
                            if res['href'] not in seen_urls and not res['href'].endswith('.pdf'):
                                seen_urls.add(res['href'])
                                candidates.append(res)
                    except:
                        pass
            
            # 候補を絞る
            candidates = candidates[:target_count * 2]
            st.write(f"🔍 {len(candidates)}件のソースへアクセス中...")
            
            # --- B. 並列取得フェーズ ---
            valid_results = []
            progress_bar = st.progress(0)
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(fetch_worker, res['href'], res['title'], res['body']) for res in candidates]
                
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result:
                        valid_results.append(result)
                        progress_bar.progress(min(len(valid_results) / target_count, 1.0))
                        
                        if len(valid_results) >= target_count:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
            
            status_container.update(label=f"完了！ {len(valid_results)}件の情報を確保。", state="complete", expanded=False)

        # --- C. 回答生成フェーズ ---
        if valid_results:
            # 1. ソースの先行表示 (Level 25のUX)
            st.markdown("### 📚 参照ソース")
            cols = st.columns(4)
            for i, res in enumerate(valid_results):
                with cols[i % 4]:
                    icon = "⚡" if res['type'] == "full" else "📝"
                    short_title = res['title'][:15] + "..."
                    st.info(f"**[{i+1}] {short_title}**\n\n[{icon} Link]({res['url']})")
            
            st.divider()

            # 2. コンテキストの作成
            context_text = ""
            for i, res in enumerate(valid_results):
                context_text += f"[{i+1}] {res['title']}\n{res['content']}\n\n"

            # 3. プロンプト作成 (履歴を入れるかどうかの分岐)
            if mode == "🚀 爆速 (単発)":
                system_prompt = "あなたは高速検索AIです。検索結果のみに基づいて回答してください。"
                user_content = f"質問: {query}\n\n【検索結果】\n{context_text}\n\n結論ファーストで詳しく答えて。"
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ]
            else:
                # 会話モード: 過去のやり取りも含めて投げる
                system_prompt = "あなたは優秀なリサーチアシスタントです。過去の会話と最新の検索結果を統合して回答してください。"
                messages = [{"role": "system", "content": system_prompt}]
                
                # 過去ログを少し追加 (トークン節約のため直近3つくらい)
                for m in st.session_state.messages[-4:-1]:
                    messages.append({"role": m["role"], "content": m["content"]})
                
                user_content = f"最新の質問: {query}\n\n【最新の検索結果】\n{context_text}\n\n文脈を踏まえて詳しく答えて。"
                messages.append({"role": "user", "content": user_content})

            # 4. ストリーミング回答
            response_container = st.empty()
            full_response = ""
            
            try:
                stream = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    stream=True
                )
                
                for chunk in stream:
                    content = chunk.choices[0].delta.content
                    if content:
                        full_response += content
                        response_container.markdown(full_response + "▌")
                
                response_container.markdown(full_response)
                
                # 5. 履歴に保存 (ソース情報も一緒に保存しておくと後で見返せる)
                st.session_state.messages.append({
                    "role": "assistant", 
                    "content": full_response,
                    "sources": valid_results
                })
                
            except Exception as e:
                st.error(f"エラー: {e}")
                
        else:
            st.error("情報が見つかりませんでした。")