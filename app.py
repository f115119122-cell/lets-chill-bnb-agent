"""浪我們放空民宿（Let's Chill B&B）的 AI 客服 Agent（Gradio app）。

客人的每個問題都先交給 Agent，由 Agent 決定要用哪個工具：
民宿資訊、房型與加購服務來自 SQLite 資料庫，住宿費由程式計算，
工具回答不了的問題轉給真人。畫面右側顯示呼叫了哪些工具、拿回什麼結果。

執行方式：  python app.py
"""

import csv
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).parent        # 不管從哪個資料夾執行，都找得到 CSV 和 .env

# ---------------------------------------------------------------------------
# 1. API 設定：金鑰等設定放在 .env（先把 .env.example 複製成 .env）
# ---------------------------------------------------------------------------
load_dotenv(BASE_DIR / ".env")
CHAT_MODEL = os.getenv("MODEL_NAME")
API_KEY = os.getenv("API_KEY")
if not API_KEY:     # 沒填金鑰也能啟動（工具可以單獨測試），但提醒使用者
    print("Warning: API_KEY is empty. Copy .env.example to .env and fill in API_KEY.")
client = OpenAI(api_key=API_KEY or "not-set", base_url=os.getenv("LLM_API_URL"), timeout=60.0)


# ---------------------------------------------------------------------------
# 2. 民宿資料庫：bnb_info 與 rooms_services 兩張表，每次啟動都從 CSV 重建
# ---------------------------------------------------------------------------
# check_same_thread=False：Gradio 會在不同的執行緒處理請求
connection = sqlite3.connect(BASE_DIR / "bnb_app.db", check_same_thread=False)
connection.row_factory = sqlite3.Row
cursor = connection.cursor()

cursor.execute("DROP TABLE IF EXISTS bnb_info")
cursor.execute("CREATE TABLE bnb_info (topic TEXT PRIMARY KEY, content TEXT NOT NULL)")
cursor.execute("DROP TABLE IF EXISTS rooms_services")
cursor.execute("""CREATE TABLE rooms_services (item_id INTEGER PRIMARY KEY, name TEXT, name_zh TEXT,
                  category TEXT, unit TEXT, weekday_price INTEGER, weekend_price INTEGER,
                  capacity INTEGER, available INTEGER, note TEXT)""")
with open(BASE_DIR / "bnb_info.csv", newline="", encoding="utf-8") as f:
    cursor.executemany("INSERT INTO bnb_info VALUES (?, ?)",
                       [(r["topic"], r["content"]) for r in csv.DictReader(f)])
with open(BASE_DIR / "rooms_services.csv", newline="", encoding="utf-8") as f:
    cursor.executemany("INSERT INTO rooms_services VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(r["item_id"], r["name"], r["name_zh"], r["category"], r["unit"],
                         r["weekday_price"], r["weekend_price"], r["capacity"], r["available"], r["note"])
                        for r in csv.DictReader(f)])
connection.commit()
INFO_TOPICS = [r["topic"] for r in cursor.execute("SELECT topic FROM bnb_info")]
ITEM_COUNT = cursor.execute("SELECT COUNT(*) FROM rooms_services").fetchone()[0]
print(f"Ready: {len(INFO_TOPICS)} info topics, {ITEM_COUNT} rooms and services.")


# ---------------------------------------------------------------------------
# 3. 工具：一般的 Python 函式，以及模型看到的工具說明（TOOL_SPECS）
# ---------------------------------------------------------------------------
# 工具四原則：回傳資料不回傳句子、查不到要明說、計算在工具裡做、模型只填參數不寫 SQL

# available 只代表「目前有開放」，資料庫沒有逐日訂房紀錄；寫進工具結果，避免模型說成「那天有空房」
DATES_NOTE = ("'available' only means the room type or service is offered now. We cannot see bookings, "
              "so we do not know if a room is free on specific dates. The host confirms that on LINE.")


def get_bnb_info(topic):
    """查一項民宿資訊，例如入住時間、停車、取消政策。"""
    row = cursor.execute("SELECT content FROM bnb_info WHERE topic = ?", (topic,)).fetchone()
    if row is None:
        return {"found": False, "topics_available": INFO_TOPICS}
    return {"found": True, "topic": topic, "content": row["content"]}


def item_to_dict(row):
    """把資料庫的一列轉成回傳給模型的資料。"""
    return {"name": row["name"], "name_zh": row["name_zh"], "category": row["category"],
            "unit": row["unit"], "weekday_price": row["weekday_price"], "weekend_price": row["weekend_price"],
            "capacity": row["capacity"], "available": bool(row["available"]), "note": row["note"]}


def find_room_or_service(keyword="", category="", guests=0):
    """用名稱關鍵字（中英文皆可）、類別（room / addon）或入住人數找房型與加購服務。"""
    sql = "SELECT * FROM rooms_services WHERE 1 = 1"
    params = []
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        sql += " AND (name LIKE ? OR name_zh LIKE ?)"
        params += [like, like]
    if category in ("room", "addon"):
        sql += " AND category = ?"
        params.append(category)
    if guests and guests > 0:                       # 依人數找房：只回傳住得下的房型
        sql += " AND category = 'room' AND capacity >= ?"
        params.append(guests)
    rows = cursor.execute(sql, params).fetchall()
    if not rows:
        return {"found": False, "note": f"Nothing matches keyword='{keyword}', category='{category}', "
                                        f"guests={guests}. We have no such room or service."}
    return {"found": True, "items": [item_to_dict(r) for r in rows], "about_available": DATES_NOTE}


def find_one(name, category):
    """計算工具用：名稱必須對應到唯一一筆，否則回傳 (None, 說明)。"""
    found = find_room_or_service(keyword=name, category=category)
    if not found["found"]:
        return None, f"'{name}' is not one of our {'rooms' if category == 'room' else 'add-ons'}."
    if len(found["items"]) > 1:
        names = [i["name"] for i in found["items"]]
        return None, f"'{name}' matches several items {names}. Ask the customer which one."
    return found["items"][0], None


def calculate_stay_cost(room_name, check_in_date, nights, guests, addons=None):
    """算住宿總價：逐晚判斷平日／假日（週五、週六晚為假日價），再加上加購服務。

    addons = [{"name": "Breakfast"}, {"name": "Scooter", "quantity": 2, "days": 3}, ...]
    """
    # --- 檢查輸入：不合理就回傳 total=None 和原因，讓模型去問客人 ---
    try:
        check_in = datetime.strptime(check_in_date, "%Y-%m-%d").date()
    except ValueError:
        return {"total": None, "note": f"'{check_in_date}' is not a date. Use YYYY-MM-DD."}
    if check_in < date.today():
        return {"total": None, "note": f"{check_in_date} is in the past. Ask the customer for the date."}
    if not 1 <= nights <= 14:
        return {"total": None, "note": "Nights must be 1 to 14. For longer stays, transfer to a human."}
    if guests < 1:
        return {"total": None, "note": "Ask the customer how many guests."}

    room, problem = find_one(room_name, "room")
    if room is None:
        return {"total": None, "note": problem}
    if not room["available"]:
        return {"total": None, "note": f"{room['name']} cannot be booked: {room['note']}"}
    if guests > room["capacity"]:
        return {"total": None, "note": f"{room['name']} holds {room['capacity']} guests, not {guests}. "
                                       "Suggest a bigger room or two rooms."}

    # --- 房價：一晚一晚算 ---
    night_lines, room_subtotal = [], 0
    for i in range(nights):
        night = check_in + timedelta(days=i)
        is_weekend = night.weekday() in (4, 5)          # 4 = 週五、5 = 週六
        price = room["weekend_price"] if is_weekend else room["weekday_price"]
        night_lines.append({"date": night.isoformat(), "day": night.strftime("%a"),
                            "price_type": "weekend" if is_weekend else "weekday", "price": price})
        room_subtotal += price

    # --- 加購：依計價單位（unit）決定怎麼乘 ---
    addon_lines, addons_subtotal = [], 0
    for entry in addons or []:
        item, problem = find_one(entry.get("name", ""), "addon")
        if item is None:
            return {"total": None, "note": problem}
        if not item["available"]:
            return {"total": None, "note": f"{item['name']} is not offered now: {item['note']}"}
        price, unit = item["weekday_price"], item["unit"]
        if unit == "person_night":                      # 早餐：人數 × 晚數
            count, how = guests * nights, f"{price} x {guests} guests x {nights} days"
        elif unit == "person":                          # 行程：參加人數（預設全部房客）
            people = entry.get("quantity") or guests
            count, how = people, f"{price} x {people} people"
        elif unit == "item_day":                        # 租借：數量 × 天數
            qty, days = entry.get("quantity") or 1, entry.get("days") or 1
            count, how = qty * days, f"{price} x {qty} x {days} days"
        else:                                           # trip / set：按次或按組
            qty = entry.get("quantity") or 1
            count, how = qty, f"{price} x {qty}"
        addon_lines.append({"name": item["name"], "name_zh": item["name_zh"],
                            "calculation": how, "subtotal": price * count})
        addons_subtotal += price * count

    total = room_subtotal + addons_subtotal
    return {"room": room["name"], "room_zh": room["name_zh"], "guests": guests,
            "check_in": check_in.isoformat(), "check_out": (check_in + timedelta(days=nights)).isoformat(),
            "nights": night_lines, "room_subtotal": room_subtotal,
            "addons": addon_lines, "addons_subtotal": addons_subtotal,
            "total": total, "deposit_30_percent": round(total * 0.3), "about_dates": DATES_NOTE}


def transfer_to_human(reason=""):
    """轉給真人。reason 只記錄在工具軌跡裡，方便店家知道為什麼轉接。"""
    return {"message": "Please contact our host Amy: LINE ID @letschillbnb, or call 08-888-1234 (09:00 to 21:00).",
            "reason_logged": reason,
            "note": ("The hand-over is done. First tell the customer in one sentence why, "
                     "then give this message. Do not say that you will connect them: you cannot. "
                     "Do not call this tool again.")}


TOOL_FUNCTIONS = {
    "get_bnb_info": get_bnb_info,
    "find_room_or_service": find_room_or_service,
    "calculate_stay_cost": calculate_stay_cost,
    "transfer_to_human": transfer_to_human,
}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_bnb_info",
        "description": ("Get one piece of B&B information: how to book (and who confirms free rooms for dates), "
                        "check-in/check-out time, address, transport, parking, "
                        "payment and deposit, cancellation, price rules (weekday/weekend, children), breakfast, "
                        "pets, Wi-Fi, house rules (quiet hours, smoking), beach items or luggage storage."),
        "parameters": {"type": "object",
                       "properties": {"topic": {"type": "string", "enum": INFO_TOPICS}},
                       "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "find_room_or_service",
        "description": ("Find rooms or add-on services (breakfast, pick-up, scooter or bicycle rental, snorkeling, "
                        "night tour, BBQ, late check-out, SUP): their prices, capacity and whether they are "
                        "available. Use a short keyword such as 'ocean', 'quad', '雙人房' or 'scooter'; "
                        "use category='room' to list all rooms; use guests=N to find rooms for N people."),
        "parameters": {"type": "object",
                       "properties": {"keyword": {"type": "string"},
                                      "category": {"type": "string", "enum": ["room", "addon"]},
                                      "guests": {"type": "integer"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "calculate_stay_cost",
        "description": ("Calculate the price of a stay: one room for some nights, plus optional add-ons. "
                        "The code checks each night for the weekday or weekend price. "
                        "Always use this for any price of more than one night or with add-ons. "
                        "Never add or multiply prices yourself."),
        "parameters": {"type": "object",
                       "properties": {
                           "room_name": {"type": "string", "description": "Room name, e.g. 'Ocean View Double'"},
                           "check_in_date": {"type": "string", "description": "YYYY-MM-DD"},
                           "nights": {"type": "integer"},
                           "guests": {"type": "integer"},
                           "addons": {"type": "array", "items": {
                               "type": "object",
                               "properties": {"name": {"type": "string"},
                                              "quantity": {"type": "integer",
                                                           "description": "people, scooters, trips or sets"},
                                              "days": {"type": "integer", "description": "rental days only"}},
                               "required": ["name"]}}},
                       "required": ["room_name", "check_in_date", "nights", "guests"]}}},
    {"type": "function", "function": {
        "name": "transfer_to_human",
        "description": ("Use this for complaints, refunds, special requests (such as a birthday surprise), "
                        "or B&B questions that no other tool can answer."),
        "parameters": {"type": "object",
                       "properties": {"reason": {"type": "string", "description": "Why, in a few words"}},
                       "required": []}}},
]

SYSTEM_PROMPT = """
You are the online customer service agent of Let's Chill B&B (浪我們放空民宿), a small B&B by Nanwan Beach in Kenting.
All prices are in New Taiwan dollars, written like NT$2,800.

GROUNDING
1. Answer only from tool results. Never answer from memory, and never invent anything.
2. If the tools do not have the answer, say so plainly, then use transfer_to_human. Do not guess.
3. You cannot see bookings. Never say a room is free or booked on certain dates. To book or to check
   dates, the customer contacts the host on LINE (get_bnb_info topic "booking").

SCOPE
4. Only talk about this B&B and staying here. Politely refuse other topics (weather forecasts,
   homework, other hotels, general travel planning) and bring the talk back to the B&B.

WHICH TOOL
5. B&B rules and facts (booking, check-in time, parking, pets, cancellation, payment, transport) -> get_bnb_info.
6. Rooms and add-on services (price per night, capacity, offered or not) -> find_room_or_service.
7. Any total for a stay (several nights, or with add-ons) -> calculate_stay_cost. Never add or multiply prices yourself.
   If the room, check-in date, nights or number of guests is missing, ask the customer first.
8. Complaints, refunds, special requests, or B&B questions no tool can answer -> transfer_to_human.

DATES
9. Turn dates like "this Friday" into YYYY-MM-DD using today's date given below.
   If a date could mean two different days (for example "next Friday" said on a weekend,
   or "10/29, 10/30" which may be the nights or the check-in and check-out days), ask which one first.
10. In every price answer, write the check-in and check-out dates you used, so the customer can check them.

LANGUAGE AND FORMAT
11. Answer politely, in four sentences or less. Use a short list for a price breakdown.
12. In a Chinese answer, use the Chinese names (name_zh).
"""

MAX_STEPS = 6          # 模型卡住時，最多呼叫 6 次就停止，避免無限迴圈


def message_text(content):
    """取出訊息的文字；Gradio 6 有時把內容包成 [{"type": "text", "text": ...}]。"""
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def run_tool(name, raw_arguments):
    """只執行我們自己寫的工具；未知工具或參數錯誤都回傳 error，而不是讓程式當掉。"""
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return {}, {"error": f"Arguments are not valid JSON: {raw_arguments}"}
    if name not in TOOL_FUNCTIONS:
        return arguments, {"error": f"Unknown tool '{name}'. Use only: {list(TOOL_FUNCTIONS)}"}
    try:
        return arguments, TOOL_FUNCTIONS[name](**arguments)
    except TypeError as error:                          # 參數名稱或型別不對
        return arguments, {"error": f"Bad arguments for {name}: {error}"}


def run_agent(chat_history, language="Traditional Chinese"):
    """回答 chat_history 最後一個問題。language 由畫面上的切換按鈕決定。回傳 (answer, trace)。"""
    history = [{"role": m["role"], "content": message_text(m["content"])} for m in chat_history]
    today = date.today()
    context = f"Today is {today.isoformat()} ({today.strftime('%A')}). Reply in {language}."
    messages = ([{"role": "system", "content": SYSTEM_PROMPT}] + history[:-1]
                + [{"role": "system", "content": context}]      # 日期與語言由程式決定
                + history[-1:])
    trace = []

    for step in range(MAX_STEPS):
        message = client.chat.completions.create(
            model=CHAT_MODEL, messages=messages, tools=TOOL_SPECS, temperature=0.2,
        ).choices[0].message

        if not message.tool_calls:                      # 純文字回答：這一輪結束
            return (message.content or "").strip(), trace

        messages.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name,
                                                      "arguments": c.function.arguments}}
                                        for c in message.tool_calls]})
        for call in message.tool_calls:
            arguments, result = run_tool(call.function.name, call.function.arguments)
            trace.append({"step": step + 1, "tool": call.function.name, "arguments": arguments, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False)})

    return ("抱歉，這個問題我沒能處理完，請透過 LINE（@letschillbnb）聯絡房東。\n"
            "Sorry, I could not finish that. Please contact our host on LINE: @letschillbnb."), trace


# ---------------------------------------------------------------------------
# 4. Gradio 畫面（中文優先，可切換英文回覆）
# ---------------------------------------------------------------------------
GREETING = ("您好！我是墾丁浪我們放空民宿的線上客服，房型、價格、入住和活動都可以問我。\n\n"
            "Hello! I am the online agent of Let's Chill B&B in Kenting. "
            "Choose **English** above to get answers in English.")

REPLY_LANGUAGES = {"中文": "Traditional Chinese", "English": "English"}   # 按鈕文字 → 告訴模型的語言

EMPTY_TRACE = "### 工具軌跡 Tools used\n\n（尚未呼叫工具）"

SAMPLE_GROUPS = {
    "B&B information (get_bnb_info)": [
        "What time is check-in?",
        "Can I bring my dog?",
        "How do I get there from the high speed rail?",
    ],
    "Rooms and services (find_room_or_service)": [
        "How much is the ocean view double room?",
        "We are 4 people. Which rooms can we book?",
        "Do you have stand-up paddle boarding?",          # 有這項，但冬天不開放
    ],
    "The bill (calculate_stay_cost)": [
        "Ocean View Double Room for 2 people, check in this Friday for 2 nights. How much?",
        "Rooftop Suite, 2 guests, 3 nights from next Monday, with breakfast and 2 scooters for 2 days.",
    ],
    "Complaint, not in the data, not our job": [
        "The room was dirty and I want a refund.",
        "Is there a swimming pool?",
        "What will the weather be like in Kenting next week?",
    ],
    "A conversation: click these in order, without Clear": [
        "How much is the ocean view quad room?",
        "We are 4 adults. How much for 2 nights from this Saturday?",
        "Can we add breakfast for everyone?",
    ],
}

SAMPLE_GROUPS_ZH = {
    "民宿資訊（get_bnb_info）": [
        "幾點可以入住？",
        "有停車位嗎？",
        "提前一週取消可以退訂金嗎？",
    ],
    "房型與加購（find_room_or_service）": [
        "海景雙人房一晚多少錢？",
        "家庭四人房還能訂嗎？",
        "有浮潛行程嗎？",
    ],
    "算住宿費（calculate_stay_cost）": [
        "海景雙人房兩個人，這週五入住住兩晚，總共多少？",
        "頂樓景觀套房兩人，下週一起住三晚，加早餐和兩台機車租兩天，多少錢？",
    ],
    "客訴、資料沒有、不是客服的事": [
        "房間很髒，我要退費！",
        "可以幫我們布置求婚驚喜嗎？",
        "推薦我墾丁好吃的餐廳。",
    ],
    "多輪對話：依序點選，中間不要按 Clear": [
        "海景四人房多少錢？",
        "我們 4 個大人，這週六入住兩晚多少？",
        "那全部的人都加早餐呢？",
    ],
}


def format_trace(trace):
    """顯示這次回答呼叫了哪些工具，讓工具的選擇看得見。"""
    if not trace:
        return "### 工具軌跡 Tools used\n\n（這個回答沒有用到工具）"
    lines = ["### 工具軌跡 Tools used", ""]
    for entry in trace:
        lines += [f"**{entry['step']}. {entry['tool']}**", "",
                  f"arguments: `{json.dumps(entry['arguments'], ensure_ascii=False)[:200]}`", "",
                  f"result: `{json.dumps(entry['result'], ensure_ascii=False)[:1200]}`", ""]
    return "\n".join(lines)


def start_chat():
    """聊天框的初始狀態：只有開場招呼語。"""
    return [{"role": "assistant", "content": GREETING}]


def add_user_message(user_text, chat_history):
    """先把問題顯示出來，並清空輸入框。"""
    user_text = user_text.strip()
    if not user_text:
        return "", chat_history
    return "", chat_history + [{"role": "user", "content": user_text}]


def answer_question(chat_history, reply_language):
    """對目前的對話執行 Agent，回覆語言照切換按鈕。"""
    if not chat_history or chat_history[-1]["role"] != "user":
        return chat_history, EMPTY_TRACE
    try:
        answer, trace = run_agent(chat_history, REPLY_LANGUAGES[reply_language])
        panel = format_trace(trace)
    except Exception as error:
        answer = f"（系統訊息 system message）API error: {error}"
        panel = "### 工具軌跡 Tools used\n\n（呼叫失敗 the call failed）"
    return chat_history + [{"role": "assistant", "content": answer}], panel


with gr.Blocks(title="浪我們放空民宿 線上客服") as demo:
    gr.Markdown(
        "## 浪我們放空民宿 Let's Chill B&B · 線上客服\n"
        "每個問題都先交給 AI 客服：民宿資訊、房型與加購服務查自 SQLite 資料庫，住宿費由程式計算，"
        "其他問題轉給房東。右側顯示這次回答用了哪些工具。"
    )

    with gr.Row():
        with gr.Column(scale=3):
            reply_language = gr.Radio(list(REPLY_LANGUAGES), value="中文",
                                      label="回覆語言 Reply language")
            chatbot = gr.Chatbot(value=start_chat(), height=430, label="AI 客服")
            with gr.Row():
                user_box = gr.Textbox(placeholder="請輸入問題，例如：海景雙人房一晚多少錢？",
                                      show_label=False, scale=8)
                send_button = gr.Button("送出", variant="primary", scale=1)
                clear_button = gr.Button("清除", scale=1)

            gr.Markdown("**範例問題**（點一題，再按送出）")
            with gr.Tabs():
                with gr.Tab("中文"):
                    for group, questions in SAMPLE_GROUPS_ZH.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)
                with gr.Tab("English"):
                    for group, questions in SAMPLE_GROUPS.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)

        with gr.Column(scale=2):
            trace_panel = gr.Markdown(EMPTY_TRACE)

    # 一輪對話：先顯示問題，再讓 Agent 工作
    send_button.click(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
        answer_question, [chatbot, reply_language], [chatbot, trace_panel])
    user_box.submit(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
        answer_question, [chatbot, reply_language], [chatbot, trace_panel])

    # 按 Clear：回到開場招呼語，清空工具軌跡
    clear_button.click(start_chat, outputs=chatbot).then(
        lambda: ("", EMPTY_TRACE), outputs=[user_box, trace_panel])


if __name__ == "__main__":
    demo.launch()
