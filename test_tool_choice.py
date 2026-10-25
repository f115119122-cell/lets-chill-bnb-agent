"""工具選擇測試：Agent 有沒有選對工具（會呼叫 LLM，需要 .env 裡的金鑰）。

執行方式：  python test_tool_choice.py
每題列出「可接受的工具」accept（用到其中一個就算對；"none" 表示不該用任何工具）
和「禁止的工具」forbid（用到就算錯，例如資料不足還硬算錢）。
"""

import re
import sys
import time
from pathlib import Path

from app import run_agent

sys.stdout.reconfigure(encoding="utf-8")       # 模型回答可能有 cp950 沒有的字元（例如 ‑），一律用 UTF-8 輸出
RESULT_FILE = Path(__file__).parent / "test_result.txt"

NONE = "none"      # 不該用工具：反問客人，或婉拒不相關的話題

TEST_SET = [
    # 資料裡有：民宿資訊
    ("What time can we check in?",                      {"get_bnb_info"}, set()),
    ("可以帶狗狗一起住嗎？",                               {"get_bnb_info"}, set()),
    ("如果下週取消訂房，訂金可以退嗎？",                     {"get_bnb_info"}, set()),
    # 資料裡有：房型與加購
    ("How much is the garden double room on a weekday?", {"find_room_or_service"}, set()),
    ("我們 4 個人，有哪些房型可以住？",                      {"find_room_or_service"}, set()),
    ("Do you rent scooters?",                           {"find_room_or_service"}, set()),
    ("早餐多少錢？",                                      {"get_bnb_info", "find_room_or_service"}, set()),
    ("海景雙人房 11/20 那天還有空房嗎？",                    {"get_bnb_info", "find_room_or_service"}, set()),
    # 需要計算
    ("Ocean View Twin Room for 2 guests, check in this Saturday for 2 nights. What is the total?",
                                                        {"calculate_stay_cost"}, set()),
    ("海景四人房 4 個大人，下週五入住兩晚，全部加早餐，總共多少？", {"calculate_stay_cost"}, set()),
    ("庭園雙人房住三晚多少錢？",                            {NONE, "find_room_or_service", "get_bnb_info"},
                                                        {"calculate_stay_cost"}),   # 沒給日期和人數：要反問
    # 資料裡沒有、客訴、特殊需求：轉真人
    ("The air conditioner in my room was broken all night. I want a refund!",
                                                        {"transfer_to_human"}, set()),
    ("你們有游泳池嗎？",                                   {"transfer_to_human"}, set()),
    ("可以幫我們準備生日驚喜布置嗎？",                       {"transfer_to_human"}, set()),
    # 不是客服的事：婉拒，不用工具
    ("Can you recommend a good seafood restaurant in Kenting?", {NONE}, {"transfer_to_human"}),
]


def judge(used, accept, forbid):
    """用到禁止的工具就錯；否則用到任一可接受工具（或該不用工具時真的沒用）就對。"""
    if set(used) & forbid:
        return False
    if not used:
        return NONE in accept
    return bool(set(used) & accept)


lines, misses, correct = [], [], 0


def report(text=""):
    """同時印在螢幕上，並留給結果檔。"""
    print(text, flush=True)
    lines.append(text)


for question, accept, forbid in TEST_SET:
    language = "Traditional Chinese" if re.search(r"[一-鿿]", question) else "English"   # 模擬客人切換按鈕
    answer, trace = run_agent([{"role": "user", "content": question}], language)
    used = [t["tool"] for t in trace]
    ok = judge(used, accept, forbid)
    correct += ok
    if not ok:
        misses.append(f"{question}  used {used or [NONE]}")
    report(f"{'OK  ' if ok else 'MISS'} {question}")
    report(f"     accept {sorted(accept)}" + (f"  forbid {sorted(forbid)}" if forbid else ""))
    report(f"     used   {used or [NONE]}")
    report(f"     answer {answer[:150]!r}\n")
    time.sleep(1)       # 避免呼叫太頻繁（429 錯誤）

report("MISSED QUESTIONS:" if misses else "MISSED QUESTIONS: none")
for miss in misses:
    report(f"  - {miss}")
report(f"tool choice = {correct}/{len(TEST_SET)} = {correct / len(TEST_SET):.0%}")
RESULT_FILE.write_text("\n".join(lines), encoding="utf-8")
print(f"(saved to {RESULT_FILE.name})")
