"""우리집 물건관리 | Python 3.11+ | streamlit run app.py
GitHub에 app.py와 requirements.txt 업로드 후 Streamlit Community Cloud 배포.
GitHub Pages는 지원하지 않습니다.
선택 Secrets: DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require"
외부 PostgreSQL 미설정 시 SQLite: Cloud 재시작/재배포 시 유실 가능.
로그인·가족 계정 기능 없음: 민감한 정보를 입력하지 말고 배포 접근 권한을 제한하세요.
목적별 세 메뉴: 공동구매 / 소진 예측 / 중복 구매 확인.
기존 버전과 별도 테이블을 사용하므로 이전 데이터가 자동 표시되지는 않습니다.
"""
from __future__ import annotations
import io
import re
import unicodedata
from urllib.parse import urlencode, urlsplit, urlunsplit
import os
import sqlite3
import statistics
import uuid
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from PIL import Image, ImageOps

st.set_page_config(page_title="우리집 물건관리", page_icon="🏠", layout="centered")
TZ = ZoneInfo("Asia/Seoul")
CATEGORIES = ["생필품", "식품", "생활물품", "가전·필터", "기타"]


def today():
    return datetime.now(TZ).date()


def now():
    return datetime.now(TZ).isoformat(timespec="microseconds")


def config(key):
    try:
        return str(st.secrets.get(key, os.environ.get(key, "")))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return os.environ.get(key, "")


PG = bool(config("DATABASE_URL"))


@contextmanager
def db():
    if PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(config("DATABASE_URL"), row_factory=dict_row, connect_timeout=12)
    else:
        conn = sqlite3.connect("home_inventory.sqlite3", timeout=20)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sql(conn, query, args=()):
    return conn.execute(query.replace("?", "%s") if PG else query, args)


def read(query, args=()):
    with db() as conn:
        return [dict(r) for r in sql(conn, query, args).fetchall()]


def initialize():
    blob = "BYTEA" if PG else "BLOB"
    with db() as conn:
        sql(conn, f"""CREATE TABLE IF NOT EXISTS simple_items(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL,
            category TEXT NOT NULL, location TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty>=0), unit TEXT NOT NULL,
            expiry TEXT, interval_days INTEGER NOT NULL,
            last_maintenance TEXT, memo TEXT NOT NULL, photo {blob})""")
        sql(conn, """CREATE TABLE IF NOT EXISTS purpose_orders(
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty>0), status TEXT NOT NULL,
            buyer TEXT NOT NULL, created TEXT NOT NULL)""")
        sql(conn, """CREATE TABLE IF NOT EXISTS simple_cycles(
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
            started TEXT NOT NULL, finished TEXT)""")


def lock(conn, item_id):
    if not PG:
        sql(conn, "BEGIN IMMEDIATE")
    row = sql(conn, "SELECT * FROM simple_items WHERE id=?" + (" FOR UPDATE" if PG else ""), (item_id,)).fetchone()
    if not row:
        raise ValueError("물건을 찾을 수 없습니다. 다시 선택하세요.")
    return dict(row)


def change_stock(item_id, delta):
    with db() as conn:
        item = lock(conn, item_id)
        if sql(conn, "SELECT id FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone():
            raise ValueError("사용 중인 주기가 있어요. 소진 예측에서 완료하거나 취소한 뒤 수량을 변경하세요.")
        if item["qty"] + delta < 0:
            raise ValueError("현재 수량보다 많이 차감할 수 없습니다.")
        sql(conn, "UPDATE simple_items SET qty=qty+? WHERE id=?", (delta, item_id))


def start_cycle(item_id):
    with db() as conn:
        item = lock(conn, item_id)
        if item["qty"] < 1:
            raise ValueError("재고가 있어야 사용을 시작할 수 있어요.")
        if sql(conn, "SELECT id FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone():
            raise ValueError("이미 사용 중입니다.")
        sql(conn, "INSERT INTO simple_cycles VALUES(?,?,?,?)", (uuid.uuid4().hex, item_id, now(), None))


def end_cycle(item_id, cancel=False):
    with db() as conn:
        item = lock(conn, item_id)
        cycle = sql(conn, "SELECT * FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone()
        if not cycle:
            raise ValueError("사용 중인 주기가 없어요.")
        if cancel:
            sql(conn, "DELETE FROM simple_cycles WHERE id=?", (cycle["id"],))
        else:
            if item["qty"] < 1:
                raise ValueError("차감할 재고가 없어요.")
            sql(conn, "UPDATE simple_cycles SET finished=? WHERE id=?", (now(), cycle["id"]))
            sql(conn, "UPDATE simple_items SET qty=qty-1 WHERE id=?", (item_id,))


def predict(cycles):
    completed = sorted([c for c in cycles if c["finished"]], key=lambda c: c["finished"])
    durations = [(datetime.fromisoformat(c["finished"]).date() - datetime.fromisoformat(c["started"]).date()).days for c in completed]
    durations = [d for d in durations if d > 0]
    current = next((c for c in cycles if not c["finished"]), None)
    median = statistics.median(durations[-6:]) if len(durations) >= 2 else None
    due = datetime.fromisoformat(current["started"]).date() + timedelta(days=round(median)) if current and median is not None else None
    return len(durations), median, current, due


def navigate(page):
    st.session_state["page"] = page


def perform(fn, message):
    try:
        fn()
    except ValueError as exc:
        st.error(str(exc))
    except Exception:
        st.error("저장하지 못했습니다. DB 연결이나 사진 형식을 확인하고 다시 시도하세요.")
    else:
        st.session_state["action_done"] = message
        st.rerun()


def choose(items, key):
    if not items:
        st.info("등록된 물건이 없어요. 홈의 ‘물건 등록’에서 먼저 등록하세요.")
        return None
    lookup = {i["id"]: i for i in items}
    options = list(lookup)
    if st.session_state.get(key) not in options:
        st.session_state.pop(key, None)
    iid = st.selectbox("물건 선택", options, index=None, placeholder=("필요한 물건을 입력해주세요" if key == "purchase_item" else "물건을 선택하세요"), format_func=lambda x: f'{lookup[x]["name"]} · {lookup[x]["location"]}', key=key)
    return lookup.get(iid)


def as_date(value):
    return date.fromisoformat(value) if value else None


@st.dialog("등록 완료", dismissible=False)
def registration_confirmation():
    st.success("✅ 물건이 등록됐어요!")
    st.write(f"**{st.session_state.get('registered_name', '')}** 등록을 완료했습니다.")
    more, home = st.columns(2)
    if more.button("더 등록하기", use_container_width=True):
        st.session_state.pop("registered_name", None)
        st.rerun()
    if home.button("홈화면으로 돌아가기", type="primary", use_container_width=True):
        st.session_state.pop("registered_name", None)
        navigate("홈")
        st.rerun()


def show_registration(items):
    if "registered_name" in st.session_state:
        registration_confirmation()
        return
    with st.form("register", clear_on_submit=True):
        name = st.text_input("물건 이름 *", max_chars=80)
        location = st.text_input("보관 장소 *", placeholder="예: 다용도실 선반 두 번째 칸", max_chars=120)
        qty_text = st.text_input("현재 수량", value="1", placeholder="숫자를 입력하세요. 예: 3", max_chars=4)
        photo = st.file_uploader("사진 (선택, 2MB 이하)", type=["jpg", "jpeg", "png"])
        memo = st.text_area("메모", max_chars=2000)
        submit = st.form_submit_button("등록", use_container_width=True)
    st.caption("보관 장소에는 보관함 이름을 입력하세요. 예: 주방 2번 서랍. 찾기 쉬운 장소 이름으로 등록하세요.")
    if submit:
        def create():
            if not name.strip() or not location.strip():
                raise ValueError("이름과 보관 장소를 입력하세요.")
            if not re.fullmatch(r"[0-9]{1,4}", qty_text.strip()):
                raise ValueError("현재 수량은 0~9999 사이의 정수로 입력하세요.")
            qty = int(qty_text.strip())
            data = None
            if photo:
                if photo.size > 2 * 1024 * 1024:
                    raise ValueError("사진은 2MB 이하로 올려 주세요.")
                image = ImageOps.exif_transpose(Image.open(photo))
                image.thumbnail((1000, 1000))
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=80)
                data = buffer.getvalue()
            with db() as conn:
                if sql(conn, "SELECT id FROM simple_items WHERE LOWER(name)=LOWER(?) AND LOWER(location)=LOWER(?)", (name.strip(), location.strip())).fetchone():
                    raise ValueError("같은 장소에 같은 이름이 있어요. 기존 물건을 수정하거나 이름을 구분하세요.")
                sql(conn, "INSERT INTO simple_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, name.strip(), "", "기타", location.strip(), qty, "개", None, 0, None, memo, data))
        try:
            create()
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            st.error("저장하지 못했습니다. DB 연결이나 사진 형식을 확인해 주세요.")
        else:
            st.session_state["registered_name"] = name.strip()
            st.rerun()


def show_prediction(items):
    item = choose(items, "predict_item")
    if not item:
        return
    cycles = read("SELECT * FROM simple_cycles WHERE item_id=?", (item["id"],))
    count, median, current, due = predict(cycles)
    if not current and item["qty"] < 1:
        st.warning("이 물건은 현재 재고가 0개라 사용을 시작할 수 없어요.")
        st.write("실제로 가지고 있다면 아래에 추가할 수량을 입력하세요. 구매 중인 물건이라면 공동구매에서 도착 확인을 해주세요.")
        with st.form("prediction_restock"):
            amount = st.text_input("추가할 수량", placeholder="예: 1")
            submitted = st.form_submit_button("수량 반영하기", use_container_width=True)
        if submitted:
            if not amount.strip().isascii() or not amount.strip().isdigit() or int(amount.strip()) < 1:
                st.error("추가할 수량을 1 이상의 숫자로 입력해주세요.")
            else:
                perform(lambda: change_stock(item["id"], int(amount.strip())), "수량을 반영했어요. 이제 사용을 시작할 수 있어요.")
        return
    if due:
        st.success(f"다 쓸 것으로 예상되는 날: {due}")
    elif median is not None:
        st.info("사용 기록이 준비됐어요. 아래 버튼을 누르면 예상일을 알려드려요.")
    elif current:
        st.info("사용 기록을 모으고 있어요. 다 쓰면 아래 ‘다 썼어요’를 눌러주세요.")
    else:
        st.info("아직 소진을 예측할 데이터가 부족해요. 아래 버튼으로 사용 기록을 시작하세요.")
    if current:
        st.write(f'사용 시작일: {current["started"][:10]}')
        if st.button("다 썼어요", use_container_width=True):
            perform(lambda: end_cycle(item["id"]), "다 쓴 날을 기록했어요.")
        if st.button("사용 시작 취소", use_container_width=True):
            perform(lambda: end_cycle(item["id"], cancel=True), "사용 기록을 취소했어요.")
    elif st.button("예측 시작하기", use_container_width=True):
        perform(lambda: start_cycle(item["id"]), "사용 기록을 시작했어요. 다 쓰면 ‘다 썼어요’를 눌러주세요.")
    if median is None:
        st.caption("서로 다른 날짜에 사용을 시작하고 다 쓴 기록이 2번 쌓이면 예상일이 나와요. 같은 날 시작·완료한 기록은 계산에서 제외돼요.")


def normalize(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def matches(item, query):
    target = normalize(query)
    return bool(target) and any(target in normalize(v) for v in [item["name"]] + item["aliases"].replace("，", ",").split(","))


def add_order(item_id, quantity):
    with db() as conn:
        lock(conn, item_id)
        if sql(conn, "SELECT id FROM purpose_orders WHERE item_id=? AND status<>?", (item_id, "도착")).fetchone():
            raise ValueError("이미 구매 예정이거나 주문한 물건입니다. 기존 목록을 확인하세요.")
        sql(conn, "INSERT INTO purpose_orders VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, item_id, quantity, "구매 예정", "", now()))


def advance_order(order_id, buyer=""):
    with db() as conn:
        initial = sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone()
        if not initial:
            raise ValueError("구매 항목이 없습니다.")
        lock(conn, initial["item_id"])
        order = dict(sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone())
        if order["status"] == "구매 예정":
            if not buyer.strip():
                raise ValueError("구매 담당자의 별명을 입력하세요.")
            sql(conn, "UPDATE purpose_orders SET status=?,buyer=? WHERE id=?", ("주문 완료", buyer.strip(), order_id))
        elif order["status"] == "주문 완료":
            # This action is explicit; stale clicks never change a different step.
            raise ValueError("이미 주문됐어요. 최신 목록에서 도착 확인을 누르세요.")
        else:
            raise ValueError("이미 도착 처리된 항목입니다.")


def receive_order(order_id):
    with db() as conn:
        initial = sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone()
        if not initial:
            raise ValueError("구매 항목이 없습니다.")
        lock(conn, initial["item_id"])
        order = dict(sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone())
        if order["status"] != "주문 완료":
            raise ValueError("이미 처리됐거나 아직 주문 전입니다. 새로고침하세요.")
        sql(conn, "UPDATE purpose_orders SET status=? WHERE id=?", ("도착", order_id))
        sql(conn, "UPDATE simple_items SET qty=qty+? WHERE id=?", (order["qty"], order["item_id"]))


def show_purchases(items):
    st.write("살 것을 함께 정하고, 누가 주문했는지 확인하세요.")
    with st.form("purchase_add", clear_on_submit=True):
        name = st.text_input("구매할 물건 이름", placeholder="필요한 물건을 입력해주세요", max_chars=100)
        amount_text = st.text_input("구매할 수량", value="1")
        submitted = st.form_submit_button("공동구매 목록에 추가", use_container_width=True)
    if submitted:
        if not name.strip() or not amount_text.strip().isdigit() or not 1 <= int(amount_text.strip()) <= 999:
            st.error("물건 이름과 1~999 사이의 구매 수량을 입력해주세요.")
        else:
            def create_purchase():
                found = [i for i in items if normalize(i["name"]) == normalize(name)]
                if len(found) > 1:
                    raise ValueError("같은 이름의 물건이 여러 장소에 있어요. 물건 이름을 구분해서 등록해주세요.")
                if found:
                    item_id = found[0]["id"]
                else:
                    item_id = uuid.uuid4().hex
                    with db() as conn:
                        sql(conn, "INSERT INTO simple_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, name.strip(), "", "기타", "미지정", 0, "개", None, 0, None, "", None))
                add_order(item_id, int(amount_text.strip()))
            perform(create_purchase, "구매 목록에 추가했습니다.")
    orders = read("SELECT * FROM purpose_orders WHERE status<>? ORDER BY created DESC", ("도착",))
    item_map = {i["id"]: i for i in items}
    if not orders:
        st.info("진행 중인 구매가 없습니다.")
    for order in orders:
        item = item_map.get(order["item_id"])
        if not item:
            continue
        with st.container(border=True):
            st.subheader(item["name"])
            st.write(f'{order["qty"]}{item["unit"]} 구매 · {order["status"]}')

            if order["status"] == "구매 예정":
                buyer = st.text_input("주문 담당자 별명", key="buyer_" + order["id"], max_chars=30)
                if st.button("내가 주문했어요", key="order_" + order["id"], use_container_width=True):
                    perform(lambda oid=order["id"], b=buyer: advance_order(oid, b), "주문 상태를 공유했습니다.")
                if st.button("구매 예정 취소", key="cancel_" + order["id"]):
                    def cancel(order=order):
                        with db() as conn:
                            lock(conn, order["item_id"])
                            cur = sql(conn, "DELETE FROM purpose_orders WHERE id=? AND status=?", (order["id"], "구매 예정"))
                            if cur.rowcount != 1:
                                raise ValueError("상태가 변경됐어요. 새로고침하세요.")
                    perform(cancel, "구매 예정을 취소했습니다.")
            else:
                st.write(f'주문 담당: {order["buyer"]}')
                if st.button("도착 확인 · 재고 반영", key="receive_" + order["id"], use_container_width=True):
                    perform(lambda oid=order["id"]: receive_order(oid), "도착 수량을 재고에 반영했습니다.")
    st.caption("결제·배송 연동은 없으며 가족이 상태를 직접 변경하는 공유 구매 목록입니다.")


def show_duplicates(items):
    st.write("구매 전에 물건을 검색해 재고와 진행 중인 주문을 확인하세요.")
    with st.form("duplicate_search", clear_on_submit=True):
        query = st.text_input("구매하려는 물건", placeholder="물건 이름 입력")
        submitted = st.form_submit_button("확인", use_container_width=True)
    if submitted:
        if not query.strip():
            st.error("물건 이름을 입력하세요.")
            return
        found = [i for i in items if matches(i, query)]
        results = []
        if not found:
            results.append("등록되지 않은 물건입니다. 집에 없다는 뜻은 아니므로 먼저 확인해 주세요.")
        for item in found:
            results.append(f'물건: {item["name"]} · 보관 장소: {item["location"]}')
            results.append(f'현재 재고: {item["qty"]}개')
            if item["qty"] == 0:
                results.append("재고가 없어요.")
            elif item["qty"] == 1:
                results.append("⚠️ 재고 부족: 1개만 남았어요.")
            orders = read("SELECT * FROM purpose_orders WHERE item_id=? AND status<>?", (item["id"], "도착"))
            results.append("진행 중인 구매가 있어요." if orders else "진행 중인 구매가 없어요.")
            for order in orders:
                results.append(f'{order["status"]}: {order["qty"]}개 · 담당 {order["buyer"] or "미정"}')
        st.session_state["duplicate_result"] = results
        st.session_state["action_done"] = "중복 구매 확인을 완료했습니다."
        st.rerun()


@st.dialog("확인 완료", dismissible=False)
def action_confirmation():
    st.success(st.session_state.get("action_done", "완료했습니다."))
    if "duplicate_result" in st.session_state:
        for entry in st.session_state["duplicate_result"]:
            if entry.startswith("⚠️"):
                st.warning(entry)
            elif entry.startswith("현재 재고:"):
                st.metric("지금 집에 있는 수량", entry.split(":", 1)[1].strip())
            elif entry == "진행 중인 구매가 있어요.":
                st.warning("🛒 진행 중인 구매가 있어요")
            elif entry == "진행 중인 구매가 없어요.":
                st.info("🛒 진행 중인 구매가 없어요")
            elif entry.startswith("물건:"):
                st.subheader(entry)
            else:
                st.write(entry)
    left, right = st.columns(2)
    if left.button("더 하기", use_container_width=True):
        st.session_state.pop("action_done", None)
        st.session_state.pop("duplicate_result", None)
        st.rerun()
    if right.button("홈화면으로 돌아가기", type="primary", use_container_width=True):
        st.session_state.pop("action_done", None)
        st.session_state.pop("duplicate_result", None)
        navigate("홈")
        st.rerun()


def main():
    st.markdown('''<style>
    .block-container{max-width:960px;padding-top:2rem;}
    .stApp{background:#f6f7fb;}
    h1,h2,h3{color:#192b42;}
    div.st-key-tile_purchase button,div.st-key-tile_predict button,
    div.st-key-tile_duplicate button,div.st-key-tile_qr button{
        min-height:170px!important;width:100%;border:0!important;border-radius:24px!important;
        padding:24px!important;box-shadow:0 5px 16px #172b4010;
        transition:transform .15s ease;}
    div.st-key-tile_purchase button p,div.st-key-tile_predict button p,
    div.st-key-tile_duplicate button p,div.st-key-tile_qr button p{
        font-size:25px!important;font-weight:750!important;white-space:pre-line;}
    div.st-key-tile_purchase button{background:#dceaff!important;color:#153f77!important;}
    div.st-key-tile_predict button{background:#e5defa!important;color:#4b2c7c!important;}
    div.st-key-tile_duplicate button{background:#d9f2e5!important;color:#145437!important;}
    div.st-key-tile_qr button{background:#ffead3!important;color:#75400e!important;}
    div.st-key-tile_purchase button:hover,div.st-key-tile_predict button:hover,
    div.st-key-tile_duplicate button:hover,div.st-key-tile_qr button:hover{transform:translateY(-3px);}
    div.st-key-register_banner button{width:100%;min-height:110px!important;background:#b91c1c!important;color:#fff!important;border:0!important;border-radius:22px!important;padding:22px!important;box-shadow:0 6px 18px #b91c1c25;}
    div.st-key-register_banner button p{font-size:28px!important;font-weight:800!important;color:#fff!important;}
    div.st-key-register_banner button:hover{background:#991b1b!important;}
    button:focus-visible{outline:3px solid #193c67!important;outline-offset:3px;}
    /* Phone layout: large tap targets, legible inputs and viewport-safe dialogs. */
    *,*::before,*::after{box-sizing:border-box;}
    .block-container{width:100%;padding-bottom:calc(2rem + env(safe-area-inset-bottom));}
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input,
    [data-testid="stTextArea"] textarea{font-size:16px!important;min-height:48px;}
    [data-baseweb="select"]>div{min-height:48px;font-size:16px;}
    [data-testid="stButton"] button,
    [data-testid="stFormSubmitButton"] button{min-height:48px;touch-action:manipulation;}
    [data-testid="stButton"] button p{white-space:normal;overflow-wrap:anywhere;}
    [role="dialog"]{width:min(500px,calc(100vw - 24px))!important;
        max-width:calc(100vw - 24px)!important;max-height:85dvh;overflow-y:auto;}
    @media(max-width:640px){
      .block-container{max-width:100%;padding-left:16px;padding-right:16px;padding-top:1.2rem;}
      h1{font-size:1.65rem!important;line-height:1.35!important;overflow-wrap:anywhere;}
      h2{font-size:1.35rem!important;}
      h3{font-size:1.15rem!important;}
      [data-testid="stHorizontalBlock"]{flex-direction:column!important;gap:.75rem!important;}
      [data-testid="stColumn"]{width:100%!important;min-width:0!important;flex:1 1 100%!important;}
      div.st-key-register_banner button{min-height:88px!important;border-radius:18px!important;padding:18px!important;}
      div.st-key-register_banner button p{font-size:23px!important;}
      div.st-key-tile_purchase button,div.st-key-tile_predict button,
      div.st-key-tile_duplicate button{min-height:104px!important;padding:18px!important;border-radius:18px!important;}
      div.st-key-tile_purchase button p,div.st-key-tile_predict button p,
      div.st-key-tile_duplicate button p{font-size:22px!important;}
      [data-testid="stFileUploader"] section{padding:12px!important;}
      [data-testid="stCaptionContainer"]{overflow-wrap:anywhere;}
    }
    </style>''', unsafe_allow_html=True)
    try:
        initialize()
        items = read("SELECT * FROM simple_items ORDER BY name,location")
    except Exception:
        st.error("DB에 연결할 수 없습니다. DATABASE_URL과 접속 설정을 확인하세요.")
        return
    page = st.session_state.get("page", "홈")
    if page == "홈":
        st.title("🏠 우리집 생활 도우미")
        st.write("지금 무엇을 하고 싶으세요?")
        with st.container(key="register_banner"):
            st.button("＋ 물건 등록하기", key="home_register", on_click=navigate, args=("물건 등록",), use_container_width=True)
        st.caption("새 물건은 먼저 등록하세요 · 이름 / 사진 / 보관 장소 / 수량")
        tiles = [("purchase","🛒 공동구매","공동구매"),("predict","📉 소진 예측","소진 예측"),("duplicate","🔎 중복 구매 확인","중복 구매 확인")]
        for key, text, target in tiles:
            with st.container(key="tile_" + key):
                st.button(text, key="go_" + key, on_click=navigate, args=(target,), use_container_width=True)
        st.caption("원하는 박스를 누르면 해당 기능만 열립니다.")

        st.caption("외부 DB 연결 중" if PG else "데모 저장 모드 · Cloud 재시작 시 유실 가능. 영구 보관은 DATABASE_URL을 설정하세요.")
        st.caption("로그인 없는 앱입니다. 배포 접근 권한을 제한하고 민감한 개인정보는 등록하지 마세요.")
        return
    st.button("← 목적 선택으로", on_click=navigate, args=("홈",))
    st.title(page)
    if st.button("최신 데이터 새로고침"):
        st.rerun()
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))
    handlers = {"공동구매":show_purchases,"소진 예측":show_prediction,"중복 구매 확인":show_duplicates,"물건 등록":show_registration}
    if page in handlers:
        handlers[page](items)
        if "action_done" in st.session_state:
            action_confirmation()
    else:
        navigate("홈")
        st.rerun()


if __name__ == "__main__":
    main()
