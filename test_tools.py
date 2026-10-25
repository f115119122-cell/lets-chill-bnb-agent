"""不呼叫 LLM，直接測試工具函式的結果對不對（單元測試）。

執行方式：  python test_tools.py
預期答案都是手算的；日期用「從今天往後找下一個星期幾」，所以哪天執行都能測。
"""

from datetime import date, timedelta

from app import calculate_stay_cost, find_room_or_service, get_bnb_info, run_tool


def next_weekday(weekday):
    """下一個星期幾（0 = 週一 … 4 = 週五），至少是明天。"""
    days_ahead = (weekday - date.today().weekday() - 1) % 7 + 1
    return (date.today() + timedelta(days=days_ahead)).isoformat()


passed = failed = 0


def check(name, actual, expected):
    global passed, failed
    ok = actual == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {actual!r}" + ("" if ok else f", expected {expected!r}"))


# --- 查資訊 ---
check("check-in info found", get_bnb_info("check_in_out")["found"], True)
check("unknown topic -> not found", get_bnb_info("swimming_pool")["found"], False)

# --- 查紀錄 ---
check("'四人房' matches 2 rooms", len(find_room_or_service(keyword="四人房")["items"]), 2)
check("rooms for 4 guests", [i["name"] for i in find_room_or_service(guests=4)["items"]],
      ["Family Quad Room", "Ocean View Quad Room"])
check("all rooms", len(find_room_or_service(category="room")["items"]), 6)
check("pizza -> not found", find_room_or_service(keyword="pizza")["found"], False)

# --- 計算：平日／假日 ---
friday, thursday, monday = next_weekday(4), next_weekday(3), next_weekday(0)
check("Ocean View Double, Fri + Sat", calculate_stay_cost("Ocean View Double", friday, 2, 2)["total"], 3600 + 3600)
check("Ocean View Quad, Thu + Fri + Sat", calculate_stay_cost("Ocean View Quad", thursday, 3, 4)["total"],
      4800 + 6000 + 6000)
result = calculate_stay_cost("Rooftop Suite", monday, 3, 2,
                             [{"name": "Breakfast"}, {"name": "Scooter", "quantity": 2, "days": 2}])
check("Suite Mon-Wed + breakfast + 2 scooters x 2 days", result["total"], 5500 * 3 + 250 * 2 * 3 + 400 * 2 * 2)
check("deposit is 30%", result["deposit_30_percent"], 5880)
check("Chinese room name works", calculate_stay_cost("海景雙人房", monday, 1, 2)["total"], 2800)

# --- 計算：不合理的輸入要回傳 total=None，不能硬算 ---
check("room under renovation", calculate_stay_cost("Family Quad", monday, 1, 4)["total"], None)
check("3 guests in a double room", calculate_stay_cost("Garden Double", monday, 1, 3)["total"], None)
check("date in the past", calculate_stay_cost("Garden Double", "2020-01-01", 1, 2)["total"], None)
check("'Ocean View' is ambiguous", calculate_stay_cost("Ocean View", monday, 1, 2)["total"], None)
check("SUP not offered in winter",
      calculate_stay_cost("Garden Double", monday, 1, 2, [{"name": "SUP"}])["total"], None)

# --- Agent 迴圈的安全保護 ---
check("unknown tool is refused", "error" in run_tool("delete_database", "{}")[1], True)
check("bad JSON is refused", "error" in run_tool("get_bnb_info", "{topic:")[1], True)
check("wrong argument name is refused", "error" in run_tool("get_bnb_info", '{"subject": "wifi"}')[1], True)

print(f"\n{passed} passed, {failed} failed")
