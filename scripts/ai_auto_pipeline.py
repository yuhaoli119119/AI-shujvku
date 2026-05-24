import os
import sys
import time
import requests
from tqdm import tqdm

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

def ai_search(query: str, model: str = "deepseek", max_results: int = 5):
    """Call backend AI search API."""
    print(f"\n🔍 [1/3] 发起 AI 自动检索: '{query}' (模型: {model})")
    url = f"{API_BASE_URL}/api/papers/ai_search"
    payload = {
        "query": query,
        "model": model,
        "max_results": max_results,
        "skip_guard": False
    }
    try:
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        print(f"✅ 生成检索 Prompt: {data.get('prompt_used')}")
        papers = data.get("papers", [])
        print(f"✅ 找到 {len(papers)} 篇候选文献。")
        return papers
    except Exception as e:
        print(f"❌ AI 检索失败: {e}")
        if hasattr(e, "response") and e.response:
            print(e.response.text)
        sys.exit(1)

def download_and_ingest(paper: dict):
    """Call backend discovery/download to fetch PDF and parse it."""
    identifier = paper.get("doi") or paper.get("url") or paper.get("title")
    if not identifier:
        return False, "缺少标识符"
        
    url = f"{API_BASE_URL}/api/papers/discovery/download"
    payload = {
        "identifier": identifier,
        "providers": ["openalex", "crossref", "arxiv", "semantic_scholar", "web_scraping"]
    }
    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        status = data.get("status")
        if status == "already_exists":
            return True, "数据库中已存在"
        elif status == "completed":
            return True, f"成功下载并入库 (ID: {data.get('paper_id')})"
        else:
            return False, f"未知状态: {status}"
    except Exception as e:
        return False, f"下载或解析失败: {e}"

def main():
    if len(sys.argv) < 2:
        print("用法: python ai_auto_pipeline.py \"你的检索词\"")
        print("例如: python ai_auto_pipeline.py \"2023 年电催化高影响因子综述\"")
        sys.exit(1)
        
    query = sys.argv[1]
    
    # 1. 自动检索
    papers = ai_search(query)
    if not papers:
        print("⚠️ 未找到任何文献，退出。")
        sys.exit(0)
        
    # 2. 下载并入库
    print(f"\n📥 [2/3] 开始下载与解析入库（共 {len(papers)} 篇）...")
    success_count = 0
    
    # 使用 tqdm 显示进度
    for paper in tqdm(papers, desc="处理进度", unit="篇"):
        title = paper.get("title", "Unknown Title")
        tqdm.write(f"\n▶ 正在处理: {title[:60]}...")
        
        success, msg = download_and_ingest(paper)
        if success:
            tqdm.write(f"  └─ ✅ {msg}")
            success_count += 1
        else:
            tqdm.write(f"  └─ ❌ {msg}")
            
    # 3. 完成汇总
    print(f"\n🎉 [3/3] 流程结束！成功入库 {success_count}/{len(papers)} 篇文献。")
    print("你现在可以打开客户端或在 Web 前端查看已经解析好的文献信息。")

if __name__ == "__main__":
    main()
